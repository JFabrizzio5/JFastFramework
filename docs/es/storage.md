# Almacenamiento

Los archivos viven en un **disco con nombre**. Tu código escribe en `"public"` o
`"invoices"`; que ese disco sea un directorio, un bucket de S3 o un contenedor
de MinIO es configuración. El mismo handler funciona en desarrollo y en
producción sin un solo cambio.

Esta es la idea de disco de Laravel, y vale la pena copiarla: la alternativa —un
handler que sabe que está escribiendo en `/var/app/uploads`— no se puede
desplegar en ningún otro lado sin reescribirlo.

```bash
jfast new service billing --plugins storage
```

## Los dos discos por defecto

Un servicio nuevo trae dos, y la diferencia entre ellos es todo el punto:

| Disco | Visibilidad | URL | Se usa para |
| --- | --- | --- | --- |
| `public` | `public` | permanente | avatares, logos, cualquier cosa ya pública |
| `private` | `private` | expira | facturas, contratos, exports, uploads |

```toml
[plugin.storage]
default = "public"
serve_local = true                    # development only

[plugin.storage.disks.public]
driver = "local"
root = "storage/public"
visibility = "public"

[plugin.storage.disks.private]
driver = "local"
root = "storage/private"
visibility = "private"
```

Un disco privado **se niega** a producir una URL permanente. No es un chequeo de
comodidad: una URL permanente a un disco privado es exactamente la forma en que
los PDFs de facturas terminan en un índice de búsqueda.

```python
storage = request.app.state.jfast.require("storage")

await storage.disk("public").put("logos/acme.png", data, content_type="image/png")
storage.disk("public").url("logos/acme.png")          # /storage/public/logos/acme.png

await storage.disk("private").put("invoices/1042.pdf", pdf)
storage.disk("private").url("invoices/1042.pdf")      # StorageError
await storage.disk("private").temporary_url("invoices/1042.pdf", expires_in=300)
```

## URLs absolutas

`url()` es relativa a la raíz por defecto —`/storage/public/logos/acme.png`—, lo
cual está bien cuando la página y el archivo salen del mismo origen. Cuando no,
esa URL se resuelve contra el host *del cliente* y da 404, y un `<img>` roto no
muestra nada: sin error en la consola, sin request fallido donde lo buscarías.

`public_base_url` lo arregla, en cualquiera de los dos drivers:

```toml
[plugin.storage.disks.public]
driver = "local"
root = "storage/public"
visibility = "public"
public_base_url = "https://api.example.com/storage/public"

[plugin.storage.disks.assets]
driver = "s3"
bucket = "acme-assets"
visibility = "public"
public_base_url = "https://cdn.example.com"    # CloudFront in front of the bucket
```

Aplica solo a `url()`. `temporary_url()` se queda en la ruta de esta misma app a
propósito: `public_base_url` suele nombrar un caché, y un caché delante de una
URL firmada le entrega el objeto al siguiente que pase, ya vencida la firma.

Una clave de disco que el driver no entiende ahora es un **error de arranque**,
no un no-op silencioso:

```
storage disk 'public' uses driver 'local', which has no setting 'bucket'.
It belongs to the s3 driver.
```

Ese chequeo existe porque `public_base_url` en un disco local se aceptaba y se
descartaba, durante toda una release, sin que nada lo dijera.

## URLs temporales

Para un disco local el link lleva una expiración y una firma HMAC sobre **las
dos cosas**: la clave y esa expiración:

```
/storage/private/invoices/1042.pdf?expires=1793491200&signature=Yk3f...
```

Firmar solo la clave convertiría un link válido en una llave a todo el disco; el
portador podría editar la ruta. Firmar solo la expiración le dejaría editar la
ruta en su lugar. La firma cubre las dos, y la comparación es de tiempo
constante.

Configura la clave o las URLs temporales no funcionan:

```bash
JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)
```

El plugin avisa al arrancar cuando un disco local privado no tiene clave, en vez
de dejar que lo descubra el primer usuario que haga clic en un link de descarga.

Un link expirado y uno falsificado devuelven el mismo 403 con el mismo mensaje.
Respuestas distintas le dirían a un atacante si la clave existe.

Para un disco S3, `temporary_url()` es una URL prefirmada y la clave de firma es
irrelevante: S3 hace la firma con sus propias credenciales.

## S3 y MinIO

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
region = "eu-west-1"
visibility = "private"
```

Nada de credenciales en la config: la cadena por defecto de boto3 encuentra el
instance role, el task role o `~/.aws/credentials`. Un task role es mejor que
cualquier key que pudieras poner aquí, porque no hay ningún secreto de larga
vida que se pueda filtrar.

MinIO es S3 con dos settings extra:

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "uploads"
endpoint_url = "http://localhost:8006"
access_key = "jfast"
secret_key = "${MINIO_PASSWORD}"
force_path_style = true              # MinIO needs it, real S3 does not
```

Para tener el contenedor en tu archivo de compose:

```toml
[plugin.storage]
minio_include_infra = true
```

`jfast deploy compose` emite entonces MinIO en el puerto base de tu servicio
`+6`, como el contenedor de cualquier otro plugin.

## Las claves no son rutas

Cada backend pasa la clave por el mismo validador antes de que llegue a un
filesystem o a un bucket:

- nada de `..`, sin `/` inicial, sin backslashes, sin null bytes;
- `.` y `..` resueltos *antes* del chequeo, así `a/../../b` se atrapa aquí y no
  en el filesystem;
- solo letras, dígitos, `.`, `_`, `-` y `/`;
- 1024 caracteres como máximo.

Los discos locales además vuelven a chequear después de resolver, porque un
symlink dentro de la raíz del disco puede apuntar afuera y solo la resolución lo
revela.

El path traversal es la vulnerabilidad de almacenamiento más común que existe.
Validar en el backend y no en el plugin significa que un backend usado directo
—desde un worker, desde un script— es tan seguro como uno usado a través de una
ruta.

## Validar uploads

`put()` sobre un disco pelado acepta cualquier cantidad de bytes de cualquier
cosa. Las reglas de tamaño y de tipo no son un asunto de cada handler —cada
handler que se olvida de una es el agujero— así que viven en el disco, en un
**pipeline**:

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
pipeline = ["validate"]

[plugin.storage.disks.uploads.validate]
max_bytes = "10MB"
allow = ["image/jpeg", "image/png", "application/pdf"]
```

Las dos opciones son opcionales. Sin `max_bytes` no hay límite de tamaño; sin
`allow` se acepta cualquier tipo que `validate` sepa reconocer.

El pipeline corre dentro del backend y no en el plugin, por la misma razón que
la validación de claves: un disco usado directo desde un worker o un script
obedece las mismas reglas que uno usado a través de una ruta.

### El tipo sale de los bytes

`validate` lee el content type de los primeros bytes del archivo. No de la
extensión, no del `Content-Type` que mandó el navegador: los dos los elige quien
sube el archivo, y un `.png` sobre un shell script es el ataque de upload más
viejo que hay. El tipo que lee después **reemplaza** al que declaró quien
llamó, porque ese valor es el que se guarda y el que se devuelve en la descarga.

`guess_content_type()`, en `jfastframework.storage`, es el que va por extensión.
Solo es correcto para objetos que escribió este mismo servicio; no lo uses para
lo que entra.

El reconocimiento es una tabla chica de magic numbers, sin dependencias. Conoce:

| | |
| --- | --- |
| imágenes | `image/png` `image/jpeg` `image/gif` `image/webp` `image/tiff` `image/avif` `image/heic` |
| documentos | `application/pdf` `application/zip` `application/gzip` |
| media | `video/mp4` `audio/mpeg` `audio/ogg` |
| fuentes | `font/woff` `font/woff2` |

**Todo lo demás se rechaza**, incluido algo perfectamente sano. Dejar pasar lo
que no se reconoce convertiría el chequeo en decoración.

Dos consecuencias que conviene saber antes de configurar un disco:

- **Los formatos que se identifican parseando solo se reconocen donde un disco
  los pide.** XML, JSON, CSV y texto plano no tienen magic number, y un sniffer
  que llama `text/plain` a cualquier cosa imprimible le dice que sí a un shell
  script. Así que nunca se adivinan: el disco que los lista en `allow` los
  obtiene, verificados parseándolos, y ningún otro disco. Ver abajo.
- **Todo archivo OOXML y OpenDocument es un zip.** Permitir `.docx` significa
  permitir `application/zip`, y eso permite cualquier otro zip. Esa decisión se
  toma en la revisión de la config, donde alguien puede verla.

Listar un tipo que `validate` no puede reconocer es un error de arranque, no una
regla que nunca coincide en silencio.

### XML, JSON, CSV y texto

Un disco para facturas CFDI guarda XML, y `validate` puede exigírselo:

```toml
[plugin.storage.disks.cfdi]
driver = "local"
root = "storage/cfdi"
pipeline = ["validate"]

[plugin.storage.disks.cfdi.validate]
max_bytes = "5MB"
allow = ["application/xml"]
```

| En `allow` | El archivo debe |
| --- | --- |
| `application/xml` | Ser XML bien formado **sin declaración DOCTYPE ni ENTITY** -- ahí viven XXE y las bombas de expansión de entidades, y un archivo de datos no necesita ninguna -- y no ser SVG ni HTML, que un navegador renderizaría y ejecutaría |
| `application/json` | Parsearse a un objeto o un arreglo |
| `text/csv` | Ser texto UTF-8 que el módulo `csv` lea en al menos una fila |
| `text/plain` | Ser UTF-8 sin caracteres de control más allá de tabulador y saltos de línea |

Se prueban solo cuando el archivo no coincidió con ningún magic number, y en ese
orden, así que un XML en un disco que permite XML y texto es XML. Un disco que no
los lista los rechaza exactamente como antes.

## Archivos grandes: `put_stream`

`put()` recibe bytes, así que el archivo entero está en memoria antes de revisar
nada. Para un archivo de varios gigabytes, mándalo en streaming:

```python
disk = storage.disk("archives")
stored = await disk.put_stream("2026/04/cfdi.zip", request.stream())
```

Sirve cualquier iterable asíncrono de bytes: el cuerpo de una request, un archivo
leído por pedazos, una descarga desde otro lado. En el camino:

- **`max_bytes` se aplica mientras llegan los bytes.** Un upload demasiado grande
  se detiene en el límite, en vez de después de llenar el disco.
- **El tipo se decide por la cabeza** -- los primeros 64 KB -- con las mismas
  firmas, y para XML, JSON y texto con lo que la cabeza puede mostrar: el XML
  abre con una etiqueta y no declara DOCTYPE ni ENTITY (un DTD va antes del
  elemento raíz, así que estaría ahí), el JSON abre un objeto o un arreglo, el
  texto se decodifica como UTF-8. Un archivo que falla se rechaza antes de que
  nada llegue a S3.
- **Nada es visible hasta escribir el último byte.** En local el stream va a un
  archivo temporal que se renombra al final; en S3 es un multipart upload que se
  aborta si algo falla, así que el bucket no guarda partes huérfanas.
- **Un disco cuyo pipeline reescribe los uploads rechaza los streams.**
  `optimise-image` necesita el archivo entero; ahí usa `put()`.

### Optimizar imágenes

`optimise-image` reescribe, a la entrada, los formatos de imagen que reconoce.
Es opcional, está apagado salvo que un disco lo nombre, y trae Pillow:

```
pip install "jfastframework[images]"
```

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
pipeline = ["validate", "optimise-image"]

[plugin.storage.disks.uploads.optimise-image]
format = "webp"          # webp | jpeg | png | gif | keep
quality = 82             # 1-100; no significa nada para png ni gif
max_dimension = 2400     # lado mayor en píxeles; 0 deja el tamaño como está
strip_metadata = true    # el default
max_pixels = 50000000    # rechaza lo que decodifique más grande que esto
```

**Solo toca cuatro tipos**: `image/jpeg`, `image/png`, `image/gif` e
`image/webp`. Todo lo demás pasa byte por byte, incluso cosas que claramente
podría abrir. Ese es justamente el punto: un PDF firmado que pasa por un códec
de imagen queda inválido, y reescribir una foto en raw destruye el negativo.
`image/tiff` queda afuera exactamente por eso —los raw Canon CR2 y Nikon NEF
*son* contenedores TIFF, con los mismos magic bytes y el mismo tipo detectado, y
nada en esta capa puede distinguir uno del otro—. `avif` y `heic` también quedan
afuera, porque Pillow no decodifica ninguno de los dos sin un plugin que quizá
no esté en tu imagen.

El tipo lo lee de los bytes, no de `upload.content_type`, así que se comporta
igual corra o no `validate` delante de él.

**Qué hace con los casos incómodos**, cada uno de los cuales es una pérdida de
datos en silencio si te equivocas:

| caso | qué pasa |
| --- | --- |
| GIF o WebP animado | queda intacto: un cuadro fijo no es la animación |
| canal alfa con `format = "jpeg"` | se codifica como WebP: JPEG no tiene alfa, y componer sobre un color de fondo que nadie eligió es editar la imagen en silencio |
| EXIF, XMP, texto PNG | se descartan; `strip_metadata = true` es el default porque el EXIF lleva coordenadas GPS |
| orientación EXIF | se aplica a los píxeles antes de descartar el tag, siempre: quitar la marca sin rotar deja el retrato acostado |
| perfil ICC | se conserva aunque se quiten los metadatos. Un perfil de color es color, no privacidad |
| salida más grande que la entrada | se conserva el original, salvo que `max_dimension` haya redimensionado |
| más píxeles que `max_pixels` | `UploadRejected`. Una expansión de 40 GB desde un archivo chico es el CVE de decompression bomb, y un archivo hostil se rechaza en vez de guardarse |
| Pillow no puede decodificarlo | pasa de largo. Este step optimiza; decidir qué es admisible es tarea de `validate` |

**La clave cambia junto con los bytes.** Un disco local deduce el content type
de un objeto a partir de su clave, así que `holiday.jpg` con bytes WebP adentro
se serviría como `image/jpeg`. `put()` devuelve la clave que realmente escribió
—quédate con esa, no con la que pasaste—:

```python
stored = await disk.put("holiday.jpg", data)
stored.key  # 'holiday.webp'
```

**Cuánto ahorra**, medido en `tests/test_storage_images.py` con
`format = "webp"`, `quality = 82` y Pillow 12.3:

| entrada | antes | después | ahorro |
| --- | --- | --- | --- |
| fotografía JPEG q90, 1600×1200 | 588.527 B | 311.426 B | 47% |
| fotografía PNG, 1600×1200 | 3.175.929 B | 339.070 B | 89% |
| fotografía PNG con alfa, 800×600 | 480.285 B | 39.654 B | 92% |
| captura de pantalla PNG de color plano, 1600×1200 | 9.384 B | 9.054 B | 4% |

Esa última fila es la honesta. El ahorro es una propiedad de la *entrada*, no
del step: un PNG que guarda una fotografía la está guardando sin pérdida y tiene
todo para ganar, un JPEG ya perdió la mayor parte, y el color plano ya era el
caso en el que PNG es bueno. Nada de esto es gratis: cada una de esas filas
costó decodificar y volver a codificar la imagen entera, y por eso ocurre en un
thread.

**Corre en `asyncio.to_thread`.** Los códecs de imagen son CPU, no I/O, que es
justo por lo que parecen inofensivos: nada en `Image.open(...)` se ve como una
llamada de red, y un JPEG de 24 megapíxeles siguen siendo cientos de
milisegundos de decodificación y segundos de recodificación con todos los demás
requests de ese worker esperando. `jfast contracts check` conoce Pillow
—`Image.open`, `Image.new`, `ImageOps`, `ImageFilter` y los métodos de lo que
devuelven—, así que una recodificación síncrona escrita dentro de un `async def`
es un hallazgo y no un misterio de latencia en producción.

### Escribir un step

`validate` y `optimise-image` son los dos que vienen, no la única forma de uno.
El escaneo de virus se enchufa igual:

```python
from dataclasses import replace
from jfastframework.storage import Upload, register_step

class Thumbnail:
    name = "thumbnail"

    def __init__(self, disk: str, config: dict) -> None:
        self.width = int(config.get("width", 512))

    async def process(self, upload: Upload) -> Upload:
        smaller = await asyncio.to_thread(shrink, upload.data, self.width)
        return replace(upload, data=smaller)

register_step("thumbnail", Thumbnail)
```

Un step recibe un `Upload` y devuelve uno. Devolver en vez de mutar hace que un
step que reescribe los bytes y uno que solo los inspecciona se lean igual, y que
un step que falla a mitad de camino no pueda dejar un upload a medio reescribir
para el siguiente.

Los steps son `async def` porque el pipeline corre dentro de un request, y
cualquier cosa CPU-bound adentro de uno tiene que ir a `asyncio.to_thread`: la
misma disciplina que sigue `LocalStorage.put`, y la que hace cumplir
`jfast contracts check`. Rechazar un upload es levantar `UploadRejected`, que
nombra el límite y el valor real:

```
disk 'uploads': 'holiday.jpg' is 24.3MB (25480051 bytes),
over this disk's max_bytes of 10MB (10485760 bytes)
```

## Servir archivos

`serve_local = true` monta `/storage/{disk}/{key}` para que las descargas
funcionen sin nada más corriendo. Es para desarrollo.

En producción, pon Caddy o un CDN delante del disco público y configura
`serve_local = false`. Un worker de Python que mantiene una conexión abierta
para transmitir un PDF de 40 MB es un worker que no está sirviendo requests. El
plugin loguea un warning si se encuentra sirviendo archivos en producción.

Las descargas se mandan como `Content-Disposition: attachment` con
`X-Content-Type-Options: nosniff`. Un `.html` o `.svg` subido y renderizado
inline corre el script de quien lo subió en tu origen, contra las cookies de tus
usuarios — así que los uploads se guardan, no se renderizan. Pasa `inline=True`
a `sanitised_download_headers()` solo para archivos que produjo tu propio
código.

## Mover un archivo entre discos

`/storage/{disk}/{key}` nombra el disco, así que mover un objeto de `local` a
`s3` da 404 en todas las URLs que alguna vez repartiste. Eso suele ser lo que
traba la migración a S3, y por eso existe `/storage/{key}`: nombra el objeto y
deja que la app averigüe dónde vive.

```toml
[plugin.storage]
resolve_by_key = true
resolve_strategy = "recorded"     # or "probe"
```

Apagado por defecto. `/storage/{disk}/{key}` sigue funcionando igual; un primer
segmento que coincide con el nombre de un disco siempre se lee como nombre de
disco, y ese es el precio de no romper la forma anterior.

### Las dos estrategias, y lo que cuesta cada una

**`recorded`** — la de por defecto. Quien escribió el objeto también anotó qué
disco lo recibió, y resolver es una sola consulta:

```python
storage = request.app.state.jfast.require("storage")
await storage.disk("uploads").put(key, data)
await storage.record(key, "uploads")
```

Exacta y barata. Solo conoce los objetos que se anotaron, así que no hace nada
por los archivos que ya estaban en el disco viejo el día que la prendes, y cada
escritura pasa a tener una segunda cosa que se puede olvidar.

El ledger por defecto está **en memoria**: sirve para tests y para un servidor
de desarrollo de un solo proceso, y está mal en cualquier otro lado, porque dos
réplicas no lo comparten y un reinicio pierde todos los mapeos. Apúntalo a la
fila que ya tienes:

```python
class Files:
    async def record(self, key: str, disk: str) -> None: ...
    async def locate(self, key: str) -> str | None: ...
    async def forget(self, key: str) -> None: ...

storage.use_ledger(Files())
```

**`probe`** — para una ventana de migración. Prueba los discos en orden y se
queda con el primero que tenga el objeto:

```toml
[plugin.storage]
resolve_by_key = true
resolve_strategy = "probe"
read_order = ["s3", "local"]      # newest first
copy_on_read = true
```

No lleva contabilidad y funciona desde el día uno, incluso con archivos escritos
hace años. Cuesta un round trip por cada disco que *no* tiene el objeto: un miss
es un `HEAD` contra cada disco de la lista antes del 404, y contra S3 eso es
latencia real en tu camino de 404. Es una ventana, no un estado permanente.

`copy_on_read` cierra la ventana a medida que pasa el tráfico: cuando el probing
encuentra un objeto en un disco viejo, lo copia al primer disco de `read_order`
y lo anota, así el request siguiente ya es un hit. Convierte un `GET` en una
lectura más una escritura, así que el primer request de cada objeto paga una
copia completa: bien para unos miles de avatares, mal para un bucket de video.
Por eso es opt-in.

### Mover uno, explícitamente

```python
await storage.copy(key, "local", "s3")     # both disks hold it
await storage.move(key, "local", "s3")     # copy, delete, re-point the ledger
```

`move` copia antes de borrar: un crash en el medio deja dos copias, y eso se
recupera. Ninguno de los dos corre el pipeline del disco destino —el objeto ya
está guardado, y revalidarlo dejaría que una regla que endureciste hoy rechace
un archivo que era legal cuando se escribió, en plena migración.

### Links firmados que sobreviven la mudanza

```python
storage.stable_url(key)                              # /storage/invoices/1042.pdf
await storage.stable_temporary_url(key, expires_in=300)
```

Estos se firman con la clave del servicio y no con la del disco, así que el link
sigue verificando después de que el objeto se mudó a un disco que no firma nada
por su cuenta. La regla de visibilidad no cambia: decide el disco al que
resuelve la clave.

## Salud

`/ready` reporta cada disco: un disco local que falta o es de solo lectura, un
bucket de S3 que no se puede alcanzar. Que el almacenamiento falle no vuelve al
servicio unhealthy por sí solo —una API que todavía puede responder consultas
debería seguir en el load balancer— así que se reporta como degradado.

## Lo que esto no hace

- **Sin miniaturas.** `optimise-image` reescribe *un* objeto en un objeto.
  Derivar un juego de tamaños a partir de un upload es un job, no un step del
  pipeline: un step que produce cuatro objetos no tiene dónde dejar tres.
- **Sin descargas en streaming ni range requests.** La ruta de descarga lee el
  objeto entero en memoria antes de responder. Un PDF de 40 MB son 40 MB
  residentes por descarga concurrente, y un video servido por ahí no se puede
  buscar. Pon Caddy o un CDN delante de cualquier cosa grande.
- **Sin escaneo de virus.** Si aceptas uploads del público, lo necesitas; aquí
  no está. Es el otro step para el que se diseñó el pipeline.

## Ver también

- [Multi-tenancy](multitenancy.md) — prefijos por tenant y subdominios
- [Despliegue](deploy.md) — Caddy delante del disco público
