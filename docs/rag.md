# RAG and vector search

Ingest documents, find the passages that answer a question, hand them to a
model with citations. The `rag` plugin does the retrieval half; the
[`llm` plugin](llm.md) does the answering half and pays for the embeddings.

```toml
[plugins]
enabled = ["observability", "database", "cache", "llm", "rag"]

[plugin.rag]
store = "pgvector"        # or "qdrant", or "package.module:Class"
embedder = "llm"          # or "ollama", or "package.module:Class"
dimensions = 1536         # must match the embedding model
text_search_config = "spanish"
```

```python
from jfastframework import get_context
from jfastframework.rag import format_context

rag = get_context(request.app).require("rag")

await rag.ingest("contract-42", text, tenant_id=tenant, metadata={"kind": "contract"})
hits = await rag.search("penalty for late delivery", tenant_id=tenant, where={"kind": "contract"})
context = format_context(hits, max_chars=6000)     # "[1] contract-42 (part 3)\n..."
```

Modules call the **service**, `rag`, with the tenant they already resolved. The
store and the embedder are also provided (`rag.store`, `rag.embedder`) for the
rare module that needs them directly.

---

## The tenant is part of every call

`tenant_id` is a keyword argument of `ingest`, `search` and `delete`, and on a
tenant-scoped store -- the default -- `None` is refused:

```
TenantRequiredError: vector store search without a tenant. Pass tenant_id, or set
[plugin.rag] tenant_scoped = false if this service has a single tenant.
```

It is a `ForbiddenError`, so it reaches HTTP as a 403, not a 500.

Three things make that hold rather than just look like it holds:

| | |
| --- | --- |
| Identity | A chunk is `(tenant_id, document_id, chunk_index)`. Two tenants may both have a `contract-1`; neither ingest touches the other's. |
| Every statement | Every read, write and delete filters by tenant. There is no code path that searches "everyone". |
| The database | Every store transaction sets `jfast.tenant_id`, so [row-level security](multitenancy.md#isolation-the-database-enforces-row-level-security) works on the chunks table like on any other table. |

For the database layer, add the policy in a migration and connect as a role
the policies bind:

```python
from jfastframework.db.rls import enable_tenant_rls

def upgrade() -> None:
    enable_tenant_rls(op, "rag_chunks")
```

Then even a raw `SELECT * FROM rag_chunks` returns one tenant's rows.
`tests/test_rag_pgvector.py` proves it with a non-superuser role.

A service with exactly one tenant sets `tenant_scoped = false` and passes
`tenant_id=None`. It is a deliberate switch because "forgot the tenant" and
"there is only one tenant" look identical in code.

---

## Ingest: only what changed costs money

```python
result = await rag.ingest("contract-42", text, tenant_id=tenant, metadata={...})
result.chunks, result.embedded, result.reused     # (18, 2, 16)
```

`ingest` makes the index hold **exactly this version** of the document:

- every chunk carries a hash of its text *and of the embedder* that produced
  its vector;
- chunks whose hash is unchanged keep their vector -- only their metadata is
  refreshed;
- chunks past the new end are deleted, so a shortened document leaves no
  orphans;
- all of it in one transaction.

An edited contract re-embeds the two paragraphs that changed, not the whole
document. Ingesting the same text twice embeds nothing the second time.
Changing the embedding model changes every hash, so the next ingest re-embeds
instead of mixing two vector spaces in one index.

An empty text removes the document.

**Ingest from a queue job, not from the request**, once documents are more than
a page or two: `RagService` has no FastAPI dependency, so the worker calls it
exactly as a route would.

### Chunking

The default strategy, `recursive`, splits at the largest structural boundary
that makes a piece fit -- Markdown headings, then paragraphs, lines, sentences,
clauses, words -- and packs neighbouring pieces back up to `chunk_size`. A
heading always opens a new chunk: it describes what follows, and stranded at
the end of the previous chunk it is noise there and missing here.

| Setting | Default | |
| --- | --- | --- |
| `chunk_size` | `1000` | characters |
| `chunk_overlap` | `150` | carried as whole pieces, never half a word |
| `chunk_strategy` | `recursive` | `fixed` is the 0.1.0a9 character window |

`chunk_text()` is importable from `jfastframework.rag` to see what a document
becomes before paying to embed it.

---

## Search

```python
hits = await rag.search(
    "penalty for late delivery",
    tenant_id=tenant,
    limit=8,
    document_ids=["contract-42", "annex-3"],   # only these documents
    where={"kind": "contract", "year": 2026},  # exact match on metadata
    min_score=0.35,                            # cosine similarity, 0..1
    hybrid=True,                               # default: [plugin.rag] hybrid
)
for hit in hits:
    hit.document_id, hit.chunk_index, hit.content, hit.score, hit.metadata
```

`score` is cosine similarity in [0, 1] on every store and in every mode, so a
threshold means the same thing on pgvector and Qdrant.

### Hybrid search

Embeddings are good at meaning and bad at exact tokens: an article number, an
RFC, a product code, a surname. Full-text search is the opposite. With
`hybrid = true` (the default) pgvector runs both and fuses the two rankings with
**reciprocal rank fusion** -- ranks, not scores, because a cosine similarity and
a `ts_rank` are on unrelated scales. Results are ordered by `fused_score`;
`score` stays the cosine similarity.

`text_search_config` picks the PostgreSQL dictionary: `simple` works for any
language; `spanish` or `english` add stemming ("entregará" finds "entrega").
It is part of a generated column, so changing it needs the column rebuilt --
decide it before the first ingest.

Qdrant has no hybrid mode here (it needs a sparse embedder); the service falls
back to vector search and says so once in the log.

### Filters that do not starve the result

HNSW filters *after* walking the graph, so a selective filter -- one document
out of thousands -- can return two hits where you asked for eight. On pgvector
0.8 or later the store turns on `hnsw.iterative_scan` for every query, which
keeps walking until the limit is met. Earlier versions do not have it; the
store detects the version and skips it.

---

## pgvector: what the table looks like

`ensure_schema` (run on startup with `auto_migrate = true`) creates or upgrades:

| | |
| --- | --- |
| `UNIQUE NULLS NOT DISTINCT (tenant_id, document_id, chunk_index)` | the chunk's identity |
| HNSW on `embedding vector_cosine_ops` | `m = 16`, `ef_construction = 64` by default |
| `search_text tsvector` generated from `content`, GIN index | hybrid search |
| GIN on `metadata jsonb_path_ops` | `where={...}` filters |
| `(tenant_id, document_id)` | ingest, delete, `document_ids` |

**Why HNSW and not IVFFlat.** IVFFlat learns its centroids from the rows present
when the index is built. Built on an empty table -- which is what a startup
migration does -- it learns nothing, and with the default `probes = 1` a search
visits one list in a hundred: a few hundred chunks return one or two hits where
eight were relevant. HNSW needs no training data and keeps its recall at every
size.

`hnsw_ef_search` (default `100`) trades speed for recall per query.

pgvector indexes stop at **2,000 dimensions**. `text-embedding-3-large` is 3,072
natively; ask for `embedding_dimensions = 1536` in `[plugin.llm]`, which loses
little. The plugin refuses a larger `dimensions` at startup instead of failing
at the first insert.

### In production: a migration, not `auto_migrate`

```python
from jfastframework.vectors.pgvector import schema_sql

def upgrade() -> None:
    for statement in schema_sql("rag_chunks", dimensions=1536, text_search_config="spanish"):
        op.execute(statement)
```

Then `auto_migrate = false`. The statements are idempotent, and they are the
same ones `ensure_schema` runs.

---

## The HTTP router

Off by default. Modules should call the service with the tenant they already
resolved; a generic `/rag` endpoint is rarely what a product wants.

```toml
[plugin.rag]
mount_router = true
read_scopes = ["docs:read"]      # empty = any signed-in caller
write_scopes = ["docs:write"]
```

When on, it **requires the `auth` plugin** (startup fails without it), needs a
signed-in caller, and takes the tenant from the [tenancy plugin](multitenancy.md)
or the token's `tenant_id` claim -- never from the body. A `tenant_id` field in
the request body is ignored.

| Method | Path | Does |
| --- | --- | --- |
| POST | `/rag/documents` | `{document_id, content, metadata}` → `{chunks, embedded, reused}` |
| POST | `/rag/search` | `{query, limit?, document_ids?, where?, min_score?, hybrid?}` |
| DELETE | `/rag/documents/{id}` | |

---

## Answering with citations

Retrieval is half. The other half, with the [`llm` plugin](llm.md):

```python
from jfastframework.rag import format_context

hits = await rag.search(question, tenant_id=tenant, limit=8)
if not hits:
    return "No document mentions that."

answer = await llm.chat(
    [
        {"role": "system", "content": (
            "Answer only from the excerpts. Cite them as [1], [2]. "
            "If they do not answer the question, say so."
        )},
        {"role": "user", "content": f"Excerpts:\n{format_context(hits, max_chars=8000)}\n\n"
                                    f"Question: {question}"},
    ],
    tenant_id=tenant,
    purpose="rag-answer",
)
```

`format_context` numbers the excerpts in the order of `hits` and stops before
`max_chars` rather than cutting an excerpt: a truncated excerpt reads to a
model as if the source said less than it did.

Numbers that matter -- deadlines, amounts, dates -- should be computed in code
from what retrieval found, not by the model. It will get "fifteen business days
after the 24th" wrong more often than you would like.

---

## A custom store

Implement the protocol in `jfastframework.vectors.base.VectorStore` and point
the config at the class; it is constructed with the `AppContext`:

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

Call `require_tenant(tenant_id, scoped=self.tenant_scoped, action="search")` at
the top of every method that touches data. `tests/test_rag_router.py` has a
complete in-memory store in sixty lines.

A custom embedder needs `dimensions` and an async `embed(texts)`. Give it a
`model_id` so a model change re-embeds, and accept a `tenant_id` keyword if it
charges per tenant -- the service passes it when the signature has it.

---

## Upgrading from 0.1.0a9

| Before | Now | What to do |
| --- | --- | --- |
| `UNIQUE (document_id, chunk_index)`, IVFFlat | tenant-scoped key, HNSW | nothing: `ensure_schema` upgrades the table in place, rows kept |
| `search(tenant_id=None)` searched every tenant | `TenantRequiredError` | pass the tenant, or `tenant_scoped = false` |
| Router on, unauthenticated, tenant from the body | off; when on, auth required | `mount_router = true` and the `auth` plugin, if you used it |
| Qdrant point ids from `(document, chunk)` | from `(tenant, document, chunk)` | re-ingest Qdrant collections |
| `chunk_text` fixed windows | recursive by default | `chunk_strategy = "fixed"` to keep a9 chunks |

## What is not here

- **Reranking.** A cross-encoder pass over the top 30 before keeping 8 improves
  precision noticeably; it is the next thing worth adding.
- **OCR.** A scanned PDF has no text to chunk. Extract it first -- a vision
  model through the [`llm` plugin](llm.md) works for a few pages.
- **Hybrid search on Qdrant.**

## See also

- [Language models](llm.md) -- the budget the embeddings spend from
- [Multi-tenancy](multitenancy.md) -- where the tenant comes from
- [Datastores](datastores.md) -- pgvector or Qdrant
