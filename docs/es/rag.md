# RAG y búsqueda vectorial

Ingerir documentos, encontrar los pasajes que responden una pregunta y
dárselos a un modelo con citas. El plugin `rag` hace la mitad de recuperación;
el [plugin `llm`](llm.md) hace la de responder y paga los embeddings.

```toml
[plugins]
enabled = ["observability", "database", "cache", "llm", "rag"]

[plugin.rag]
store = "pgvector"        # o "qdrant", o "paquete.modulo:Clase"
embedder = "llm"          # o "ollama", o "paquete.modulo:Clase"
dimensions = 1536         # tiene que coincidir con el modelo de embedding
text_search_config = "spanish"
```

```python
from jfastframework import get_context
from jfastframework.rag import format_context

rag = get_context(request.app).require("rag")

await rag.ingest("contrato-42", texto, tenant_id=tenant, metadata={"tipo": "contrato"})
hits = await rag.search("pena por entrega tardía", tenant_id=tenant, where={"tipo": "contrato"})
contexto = format_context(hits, max_chars=6000)     # "[1] contrato-42 (part 3)\n..."
```

Los módulos llaman al **servicio**, `rag`, con el tenant que ya resolvieron. El
store y el embedder también se proveen (`rag.store`, `rag.embedder`) para el
raro módulo que los necesite directo.

---

## El tenant es parte de cada llamada

`tenant_id` es argumento con nombre de `ingest`, `search` y `delete`, y en un
store limitado por tenant -- el default -- `None` se rechaza:

```
TenantRequiredError: vector store search without a tenant. Pass tenant_id, or set
[plugin.rag] tenant_scoped = false if this service has a single tenant.
```

Es un `ForbiddenError`, así que llega a HTTP como 403, no como 500.

Tres cosas hacen que eso se sostenga, no solo que lo parezca:

| | |
| --- | --- |
| Identidad | Un fragmento es `(tenant_id, document_id, chunk_index)`. Dos tenants pueden tener cada uno un `contrato-1`; ninguna ingesta toca la del otro. |
| Cada sentencia | Cada lectura, escritura y borrado filtra por tenant. No hay camino en el código que busque "en todos". |
| La base de datos | Cada transacción del store fija `jfast.tenant_id`, así que el [row-level security](multitenancy.md#aislamiento-que-impone-la-base-row-level-security) funciona sobre la tabla de fragmentos como sobre cualquier otra. |

Para la capa de base de datos, agrega la política en una migración y conéctate
con un rol al que las políticas apliquen:

```python
from jfastframework.db.rls import enable_tenant_rls

def upgrade() -> None:
    enable_tenant_rls(op, "rag_chunks")
```

Entonces hasta un `SELECT * FROM rag_chunks` crudo regresa las filas de un solo
tenant. `tests/test_rag_pgvector.py` lo demuestra con un rol que no es
superusuario.

Un servicio con exactamente un tenant pone `tenant_scoped = false` y pasa
`tenant_id=None`. Es un switch deliberado porque "olvidé el tenant" y "solo hay
un tenant" se ven idénticos en el código.

---

## Ingesta: solo cuesta lo que cambió

```python
resultado = await rag.ingest("contrato-42", texto, tenant_id=tenant, metadata={...})
resultado.chunks, resultado.embedded, resultado.reused     # (18, 2, 16)
```

`ingest` deja el índice con **exactamente esta versión** del documento:

- cada fragmento lleva un hash de su texto *y del embedder* que produjo su
  vector;
- los fragmentos con el hash igual conservan su vector -- solo se refresca su
  metadata;
- los fragmentos más allá del nuevo final se borran, así que un documento más
  corto no deja huérfanos;
- todo en una sola transacción.

Un contrato editado vuelve a embeber los dos párrafos que cambiaron, no el
documento completo. Ingerir el mismo texto dos veces no embebe nada la segunda.
Cambiar de modelo de embedding cambia todos los hashes, así que la siguiente
ingesta vuelve a embeber en vez de mezclar dos espacios vectoriales en un
índice.

Un texto vacío borra el documento.

**Ingiere desde un job de la cola, no desde el request**, en cuanto los
documentos pasan de una o dos páginas: `RagService` no depende de FastAPI, así
que el worker lo llama igual que una ruta.

### Fragmentación

La estrategia por defecto, `recursive`, parte en la frontera estructural más
grande que haga caber un pedazo -- encabezados Markdown, luego párrafos,
líneas, oraciones, cláusulas, palabras -- y vuelve a juntar pedazos vecinos
hasta `chunk_size`. Un encabezado siempre abre un fragmento nuevo: describe lo
que sigue, y varado al final del fragmento anterior es ruido ahí y falta aquí.

| Setting | Default | |
| --- | --- | --- |
| `chunk_size` | `1000` | caracteres |
| `chunk_overlap` | `150` | se arrastra en pedazos completos, nunca media palabra |
| `chunk_strategy` | `recursive` | `fixed` es la ventana de caracteres de 0.1.0a9 |

`chunk_text()` se importa de `jfastframework.rag` para ver en qué se convierte
un documento antes de pagar por embeberlo.

---

## Búsqueda

```python
hits = await rag.search(
    "pena por entrega tardía",
    tenant_id=tenant,
    limit=8,
    document_ids=["contrato-42", "anexo-3"],   # solo estos documentos
    where={"tipo": "contrato", "anio": 2026},  # coincidencia exacta en metadata
    min_score=0.35,                            # similitud coseno, 0..1
    hybrid=True,                               # default: [plugin.rag] hybrid
)
for hit in hits:
    hit.document_id, hit.chunk_index, hit.content, hit.score, hit.metadata
```

`score` es similitud coseno en [0, 1] en todos los stores y en todos los
modos, así que un umbral significa lo mismo en pgvector y en Qdrant.

### Búsqueda híbrida

Los embeddings son buenos con el significado y malos con los tokens exactos: un
número de artículo, un RFC, un código de producto, un apellido. La búsqueda de
texto completo es lo contrario. Con `hybrid = true` (el default) pgvector corre
las dos y fusiona los dos rankings con **reciprocal rank fusion** -- rangos, no
scores, porque una similitud coseno y un `ts_rank` están en escalas sin
relación. Los resultados se ordenan por `fused_score`; `score` sigue siendo la
similitud coseno.

`text_search_config` elige el diccionario de PostgreSQL: `simple` funciona para
cualquier idioma; `spanish` o `english` agregan stemming ("entrega" encuentra
"entregará"). Es parte de una columna generada, así que cambiarlo exige
reconstruirla -- decídelo antes de la primera ingesta.

Qdrant aquí no tiene modo híbrido (necesita un embedder disperso); el servicio
cae a búsqueda vectorial y lo dice una vez en el log.

### Filtros que no dejan el resultado vacío

HNSW filtra *después* de recorrer el grafo, así que un filtro selectivo -- un
documento entre miles -- puede regresar dos hits cuando pediste ocho. En
pgvector 0.8 o posterior el store activa `hnsw.iterative_scan` en cada
consulta, que sigue recorriendo hasta llenar el límite. Las versiones
anteriores no lo tienen; el store detecta la versión y lo omite.

---

## pgvector: cómo es la tabla

`ensure_schema` (corre al arrancar con `auto_migrate = true`) crea o actualiza:

| | |
| --- | --- |
| `UNIQUE NULLS NOT DISTINCT (tenant_id, document_id, chunk_index)` | la identidad del fragmento |
| HNSW sobre `embedding vector_cosine_ops` | `m = 16`, `ef_construction = 64` por default |
| `search_text tsvector` generado de `content`, índice GIN | búsqueda híbrida |
| GIN sobre `metadata jsonb_path_ops` | filtros `where={...}` |
| `(tenant_id, document_id)` | ingesta, borrado, `document_ids` |

**Por qué HNSW y no IVFFlat.** IVFFlat aprende sus centroides de las filas que
hay cuando se construye el índice. Construido sobre una tabla vacía -- que es
lo que hace una migración al arrancar -- no aprende nada, y con el default
`probes = 1` una búsqueda visita una lista de cien: unos cientos de fragmentos
regresan uno o dos hits donde había ocho relevantes. HNSW no necesita datos de
entrenamiento y mantiene su recall a cualquier tamaño.

`hnsw_ef_search` (default `100`) cambia velocidad por recall en cada consulta.

Los índices de pgvector llegan hasta **2,000 dimensiones**.
`text-embedding-3-large` es de 3,072 de origen; pide `embedding_dimensions =
1536` en `[plugin.llm]`, que pierde poco. El plugin rechaza unas `dimensions`
mayores al arrancar en vez de fallar en el primer insert.

### En producción: una migración, no `auto_migrate`

```python
from jfastframework.vectors.pgvector import schema_sql

def upgrade() -> None:
    for statement in schema_sql("rag_chunks", dimensions=1536, text_search_config="spanish"):
        op.execute(statement)
```

Luego `auto_migrate = false`. Las sentencias son idempotentes, y son las mismas
que corre `ensure_schema`.

---

## El router HTTP

Apagado por default. Los módulos deberían llamar al servicio con el tenant que
ya resolvieron; un endpoint `/rag` genérico casi nunca es lo que un producto
quiere.

```toml
[plugin.rag]
mount_router = true
read_scopes = ["docs:read"]      # vacío = cualquiera con sesión
write_scopes = ["docs:write"]
```

Encendido, **exige el plugin `auth`** (sin él no arranca), pide una sesión
iniciada y toma el tenant del [plugin tenancy](multitenancy.md) o del claim
`tenant_id` del token -- nunca del body. Un campo `tenant_id` en el body se
ignora.

| Método | Ruta | Hace |
| --- | --- | --- |
| POST | `/rag/documents` | `{document_id, content, metadata}` → `{chunks, embedded, reused}` |
| POST | `/rag/search` | `{query, limit?, document_ids?, where?, min_score?, hybrid?}` |
| DELETE | `/rag/documents/{id}` | |

---

## Responder con citas

Recuperar es la mitad. La otra mitad, con el [plugin `llm`](llm.md):

```python
from jfastframework.rag import format_context

hits = await rag.search(pregunta, tenant_id=tenant, limit=8)
if not hits:
    return "Ningún documento menciona eso."

respuesta = await llm.chat(
    [
        {"role": "system", "content": (
            "Responde solo con los extractos. Cítalos como [1], [2]. "
            "Si no responden la pregunta, dilo."
        )},
        {"role": "user", "content": f"Extractos:\n{format_context(hits, max_chars=8000)}\n\n"
                                    f"Pregunta: {pregunta}"},
    ],
    tenant_id=tenant,
    purpose="rag-respuesta",
)
```

`format_context` numera los extractos en el orden de `hits` y se detiene antes
de `max_chars` en vez de cortar un extracto: un extracto truncado se le lee al
modelo como si la fuente dijera menos de lo que dice.

Los números que importan -- plazos, montos, fechas -- se calculan en código a
partir de lo que encontró la recuperación, no los calcula el modelo. "Quince
días hábiles después del 24" lo va a equivocar más seguido de lo que quisieras.

---

## Un store propio

Implementa el protocolo de `jfastframework.vectors.base.VectorStore` y apunta
la config a la clase; se construye con el `AppContext`:

```python
from jfastframework.vectors.base import Chunk, SearchHit, require_tenant


class WeaviateStore:
    supports_hybrid = False

    def __init__(self, ctx):
        self._client = ctx.require("weaviate.client")
        self.tenant_scoped = True

    async def ensure_schema(self) -> None: ...
    async def existing_hashes(self, document_id, *, tenant_id) -> dict[int, str]: ...
    async def sync_document(self, document_id, *, tenant_id, chunks, embeddings) -> int: ...
    async def upsert(self, chunks, embeddings) -> int: ...
    async def search(self, embedding, *, tenant_id=None, limit=5, document_ids=None,
                     where=None, min_score=None, text=None) -> list[SearchHit]: ...
    async def delete_document(self, document_id, *, tenant_id=None) -> None: ...
    async def health(self) -> tuple[bool, str]: ...
```

Llama `require_tenant(tenant_id, scoped=self.tenant_scoped, action="search")` al
inicio de cada método que toque datos. `tests/test_rag_router.py` tiene un store
completo en memoria en sesenta líneas.

Un embedder propio necesita `dimensions` y un `embed(texts)` async. Dale un
`model_id` para que un cambio de modelo vuelva a embeber, y acepta un argumento
`tenant_id` si cobra por tenant -- el servicio se lo pasa cuando la firma lo
tiene.

---

## Actualizar desde 0.1.0a9

| Antes | Ahora | Qué hacer |
| --- | --- | --- |
| `UNIQUE (document_id, chunk_index)`, IVFFlat | llave por tenant, HNSW | nada: `ensure_schema` actualiza la tabla en su lugar, sin perder filas |
| `search(tenant_id=None)` buscaba en todos los tenants | `TenantRequiredError` | pasa el tenant, o `tenant_scoped = false` |
| Router encendido, sin autenticación, tenant del body | apagado; encendido exige auth | `mount_router = true` y el plugin `auth`, si lo usabas |
| Ids de Qdrant de `(documento, fragmento)` | de `(tenant, documento, fragmento)` | vuelve a ingerir las colecciones de Qdrant |
| `chunk_text` en ventanas fijas | recursivo por default | `chunk_strategy = "fixed"` para conservar los fragmentos de a9 |

## Lo que no está aquí

- **Reranking.** Una pasada de cross-encoder sobre los 30 primeros antes de
  quedarte con 8 mejora la precisión notablemente; es lo siguiente que vale la
  pena agregar.
- **OCR.** Un PDF escaneado no tiene texto que fragmentar. Extráelo antes -- un
  modelo con visión vía el [plugin `llm`](llm.md) sirve para unas cuantas
  páginas.
- **Búsqueda híbrida en Qdrant.**

## Ver también

- [Modelos de lenguaje](llm.md) -- el presupuesto del que gastan los embeddings
- [Multi-tenancy](multitenancy.md) -- de dónde sale el tenant
- [Almacenes de datos](datastores.md) -- pgvector o Qdrant
