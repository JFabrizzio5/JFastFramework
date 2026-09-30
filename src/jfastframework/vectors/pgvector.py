"""pgvector-backed vector store.

Default choice: vectors live in the PostgreSQL the service already runs, so
there is no second datastore to operate, back up or monitor. Good up to a few
million chunks per service.

What it does, and why each piece is here:

* **HNSW, not IVFFlat.** IVFFlat builds its centroids from the rows present
  when the index is created. Created on an empty table -- which is what
  ``ensure_schema`` does -- it has nothing to learn from, and with the default
  ``probes = 1`` a search visits one list in a hundred: a few hundred chunks
  return one or two hits where eight were relevant. HNSW needs no training
  data and keeps its recall at every size. An IVFFlat index left by 0.1.0a9 is
  replaced on the next ``ensure_schema``.
* **Tenant-scoped identity.** ``UNIQUE NULLS NOT DISTINCT (tenant_id,
  document_id, chunk_index)`` and every delete filtered by tenant: two tenants
  may use the same document id.
* **Filters that do not starve the result.** With a selective filter, HNSW
  can return fewer rows than ``limit`` because it filters *after* walking the
  graph. On pgvector 0.8+ the store turns on ``hnsw.iterative_scan`` for the
  query, which keeps walking until the limit is met.
* **Hybrid search.** A generated ``tsvector`` column with a GIN index, and
  reciprocal rank fusion of the vector and full-text rankings. Exact terms --
  an article number, an RFC, a product code -- are where embeddings are weak
  and full text is not.
* **Row-level security friendly.** Every transaction sets
  ``jfast.tenant_id``, so ``enable_tenant_rls(op, "<table>")`` works on the
  chunks table like on any other.

Requires: ``pip install jfastframework[db]`` and the ``vector`` extension
(the ``pgvector/pgvector`` image has it).

The table name is interpolated because no database binds an identifier as a
parameter. safe_identifier() validates it at construction, and every value is
bound -- hence the `# nosec B608` waivers.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from jfastframework.sql import safe_identifier
from jfastframework.vectors.base import Chunk, SearchHit, content_hash, require_tenant

# pgvector's HNSW and IVFFlat indexes stop at 2,000 dimensions for `vector`.
MAX_INDEXED_DIMENSIONS = 2000
_TEXT_CONFIG = re.compile(r"^[a-z_]{1,63}$")
_RLS_SETTING = "jfast.tenant_id"


def _name(table: str, suffix: str) -> str:
    """An index or constraint name that PostgreSQL will not silently truncate."""
    candidate = f"{table}_{suffix}"
    if len(candidate) <= 63:
        return candidate
    digest = hashlib.sha1(candidate.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{table[: 63 - len(suffix) - 10]}_{digest}_{suffix}"


def schema_sql(
    table: str,
    *,
    dimensions: int,
    text_search_config: str = "simple",
    hnsw_m: int = 16,
    hnsw_ef_construction: int = 64,
) -> list[str]:
    """The statements ``ensure_schema`` runs, for a migration to run instead.

    With ``[plugin.rag] auto_migrate = false`` in production, put these in an
    Alembic revision::

        from jfastframework.vectors.pgvector import schema_sql

        def upgrade() -> None:
            for statement in schema_sql("rag_chunks", dimensions=1536):
                op.execute(statement)

    Idempotent, and it upgrades a 0.1.0a9 table in place: adds the new
    columns, swaps the global unique key for the tenant-scoped one, and
    replaces the IVFFlat index with HNSW.
    """
    table = safe_identifier(table, kind="vector table")
    if not _TEXT_CONFIG.match(text_search_config):
        raise ValueError(f"text_search_config {text_search_config!r} is not a configuration name")
    if dimensions > MAX_INDEXED_DIMENSIONS:
        raise ValueError(
            f"{dimensions} dimensions cannot be indexed by pgvector (maximum "
            f"{MAX_INDEXED_DIMENSIONS}). Ask the embedding model for fewer -- "
            "text-embedding-3-large accepts dimensions=1536 with little loss."
        )
    identity = _name(table, "identity")
    legacy_unique = _name(table, "document_id_chunk_index_key")
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id           BIGSERIAL PRIMARY KEY,
            tenant_id    TEXT,
            document_id  TEXT NOT NULL,
            chunk_index  INTEGER NOT NULL,
            content      TEXT NOT NULL,
            content_hash TEXT,
            metadata     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            embedding    VECTOR({dimensions}) NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        # 0.1.0a9 tables lack these; ADD COLUMN IF NOT EXISTS is the upgrade.
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS content_hash TEXT",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS updated_at "
        f"TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS search_text TSVECTOR "
        f"GENERATED ALWAYS AS (to_tsvector('{text_search_config}', content)) STORED",
        # The a9 key was global: a second tenant's document with the same id
        # collided with the first. Dropped here, replaced by the scoped one.
        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {legacy_unique}",
        # `identity` is derived from the table name safe_identifier() checked.
        (
            "DO $$ BEGIN IF NOT EXISTS "  # nosec B608
            f"(SELECT 1 FROM pg_constraint WHERE conname = '{identity}') THEN "
            f"ALTER TABLE {table} ADD CONSTRAINT {identity} "
            "UNIQUE NULLS NOT DISTINCT (tenant_id, document_id, chunk_index); "
            "END IF; END $$"
        ),
        # IVFFlat from 0.1.0a9: trained on an empty table, poor recall forever.
        f"DROP INDEX IF EXISTS {_name(table, 'embedding_idx')}",
        f"CREATE INDEX IF NOT EXISTS {_name(table, 'embedding_hnsw')} ON {table} "
        f"USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {int(hnsw_m)}, ef_construction = {int(hnsw_ef_construction)})",
        f"CREATE INDEX IF NOT EXISTS {_name(table, 'search_text_gin')} ON {table} "
        f"USING gin (search_text)",
        f"CREATE INDEX IF NOT EXISTS {_name(table, 'metadata_gin')} ON {table} "
        f"USING gin (metadata jsonb_path_ops)",
        f"DROP INDEX IF EXISTS {_name(table, 'tenant_idx')}",
        f"CREATE INDEX IF NOT EXISTS {_name(table, 'tenant_document')} ON {table} "
        f"(tenant_id, document_id)",
    ]


def _tenant_match(tenant_id: str | None) -> str:
    """``tenant_id = :tenant``, or ``IS NULL`` for an unscoped store.

    Not ``IS NOT DISTINCT FROM :tenant``: no btree index serves it, so every
    write scanned the table -- 19 ms for one document's DELETE at 300k chunks,
    against 0.03 ms through the index, and growing with the table.
    """
    return "tenant_id IS NULL" if tenant_id is None else "tenant_id = :tenant"


class PgVectorStore:
    supports_hybrid = True

    def __init__(
        self,
        engine: Any,
        *,
        table: str,
        dimensions: int,
        tenant_scoped: bool = True,
        text_search_config: str = "simple",
        hnsw_m: int = 16,
        hnsw_ef_construction: int = 64,
        hnsw_ef_search: int = 100,
        hybrid_candidates: int = 4,
        rrf_k: int = 60,
    ) -> None:
        self._engine = engine
        self._table = safe_identifier(table, kind="vector table")
        self._dimensions = dimensions
        self.tenant_scoped = tenant_scoped
        self._schema = schema_sql(
            table,
            dimensions=dimensions,
            text_search_config=text_search_config,
            hnsw_m=hnsw_m,
            hnsw_ef_construction=hnsw_ef_construction,
        )
        self._text_config = text_search_config
        self._ef_search = int(hnsw_ef_search)
        self._hybrid_candidates = max(1, int(hybrid_candidates))
        self._rrf_k = int(rrf_k)
        # Learned from the server at ensure_schema/health; None until then.
        self._iterative_scan: bool | None = None

    # -- schema ----------------------------------------------------------

    async def ensure_schema(self) -> None:
        from sqlalchemy import text

        async with self._engine.begin() as conn:
            for statement in self._schema:
                await conn.execute(text(statement))
        await self._detect_version()

    async def _detect_version(self) -> None:
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            version = (
                await conn.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar()
        parts = [int(p) for p in re.findall(r"\d+", str(version or "0"))[:2]] + [0, 0]
        self._iterative_scan = (parts[0], parts[1]) >= (0, 8)

    async def _begin(self, conn: Any, tenant_id: str | None) -> None:
        """Per-transaction settings: the RLS tenant and the search knobs."""
        from sqlalchemy import text

        await conn.execute(
            text("SELECT set_config(:k, :v, true)"), {"k": _RLS_SETTING, "v": tenant_id or ""}
        )

    # -- writes ----------------------------------------------------------

    async def existing_hashes(self, document_id: str, *, tenant_id: str | None) -> dict[int, str]:
        from sqlalchemy import text

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="read")
        async with self._engine.begin() as conn:
            await self._begin(conn, tenant_id)
            rows = await conn.execute(
                text(
                    f"SELECT chunk_index, content_hash FROM {self._table} "  # nosec B608
                    f"WHERE {_tenant_match(tenant_id)} AND document_id = :doc"
                ),
                {"tenant": tenant_id, "doc": document_id},
            )
            return {int(r[0]): r[1] for r in rows if r[1]}

    async def sync_document(
        self,
        document_id: str,
        *,
        tenant_id: str | None,
        chunks: list[Chunk],
        embeddings: dict[int, list[float]],
    ) -> int:
        from sqlalchemy import text

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="write")
        changed = [c for c in chunks if c.chunk_index in embeddings]
        kept = [c for c in chunks if c.chunk_index not in embeddings]

        async with self._engine.begin() as conn:
            await self._begin(conn, tenant_id)
            await conn.execute(
                text(
                    f"DELETE FROM {self._table} WHERE {_tenant_match(tenant_id)} "  # nosec B608
                    f"AND document_id = :doc AND chunk_index >= :n"
                ),
                {"tenant": tenant_id, "doc": document_id, "n": len(chunks)},
            )
            if changed:
                await conn.execute(
                    text(
                        f"INSERT INTO {self._table} "  # nosec B608
                        f"(tenant_id, document_id, chunk_index, content, content_hash, metadata, "
                        f"embedding) "
                        f"VALUES (:tenant, :doc, :idx, :content, :hash, CAST(:meta AS jsonb), "
                        f"CAST(:emb AS vector)) "
                        f"ON CONFLICT ON CONSTRAINT {_name(self._table, 'identity')} DO UPDATE SET "
                        f"content = EXCLUDED.content, content_hash = EXCLUDED.content_hash, "
                        f"metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding, "
                        f"updated_at = NOW()"
                    ),
                    [
                        {
                            "tenant": tenant_id,
                            "doc": document_id,
                            "idx": c.chunk_index,
                            "content": c.content,
                            "hash": c.content_hash,
                            "meta": json.dumps(c.metadata),
                            "emb": str(embeddings[c.chunk_index]),
                        }
                        for c in changed
                    ],
                )
            if kept:
                # Same text, same vector: only what describes it may change.
                await conn.execute(
                    text(
                        f"UPDATE {self._table} SET metadata = CAST(:meta AS jsonb), "  # nosec B608
                        f"content_hash = :hash, updated_at = NOW() "
                        f"WHERE {_tenant_match(tenant_id)} AND document_id = :doc "
                        f"AND chunk_index = :idx"
                    ),
                    [
                        {
                            "tenant": tenant_id,
                            "doc": document_id,
                            "idx": c.chunk_index,
                            "hash": c.content_hash,
                            "meta": json.dumps(c.metadata),
                        }
                        for c in kept
                    ],
                )
        return len(chunks)

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        if not chunks:
            return 0
        grouped: dict[tuple[str | None, str], list[tuple[Chunk, list[float]]]] = {}
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            grouped.setdefault((chunk.tenant_id, chunk.document_id), []).append((chunk, embedding))
        total = 0
        for (tenant_id, document_id), pairs in grouped.items():
            ordered = sorted(pairs, key=lambda p: p[0].chunk_index)
            renumbered = [
                Chunk(
                    document_id,
                    i,
                    c.content,
                    c.metadata,
                    tenant_id,
                    c.content_hash or content_hash(c.content),
                )
                for i, (c, _) in enumerate(ordered)
            ]
            total += await self.sync_document(
                document_id,
                tenant_id=tenant_id,
                chunks=renumbered,
                embeddings={i: e for i, (_, e) in enumerate(ordered)},
            )
        return total

    async def delete_document(self, document_id: str, *, tenant_id: str | None = None) -> None:
        from sqlalchemy import text

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="delete")
        async with self._engine.begin() as conn:
            await self._begin(conn, tenant_id)
            await conn.execute(
                text(
                    f"DELETE FROM {self._table} WHERE {_tenant_match(tenant_id)} "  # nosec B608
                    f"AND document_id = :doc"
                ),
                {"tenant": tenant_id, "doc": document_id},
            )

    # -- reads -----------------------------------------------------------

    def _filters(
        self,
        tenant_id: str | None,
        document_ids: list[str] | None,
        where: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if tenant_id is not None or self.tenant_scoped:
            clauses.append("tenant_id = :tenant")
            params["tenant"] = tenant_id
        if document_ids:
            clauses.append("document_id = ANY(:docs)")
            params["docs"] = list(document_ids)
        if where:
            clauses.append("metadata @> CAST(:where AS jsonb)")
            params["where"] = json.dumps(where)
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    async def search(
        self,
        embedding: list[float],
        *,
        tenant_id: str | None = None,
        limit: int = 5,
        document_ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        min_score: float | None = None,
        text: str | None = None,
    ) -> list[SearchHit]:
        from sqlalchemy import text as sql

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="search")
        if self._iterative_scan is None:
            await self._detect_version()
        clause, params = self._filters(tenant_id, document_ids, where)
        params |= {"emb": str(embedding), "limit": int(limit)}

        async with self._engine.begin() as conn:
            await self._begin(conn, tenant_id)
            await conn.execute(
                sql("SELECT set_config('hnsw.ef_search', :ef, true)"),
                # pgvector caps ef_search at 1000; below `limit` it cannot return `limit` rows.
                {"ef": str(min(1000, max(self._ef_search, int(limit))))},
            )
            if self._iterative_scan:
                await conn.execute(
                    sql("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
                )

            if text and text.strip():
                rows = await conn.execute(
                    sql(self._hybrid_sql(clause)),
                    params
                    | {
                        "q": text,
                        "cfg": self._text_config,
                        "k": int(limit) * self._hybrid_candidates,
                        "rrf": self._rrf_k,
                    },
                )
            else:
                rows = await conn.execute(
                    sql(
                        f"SELECT document_id, chunk_index, content, metadata, "  # nosec B608
                        f"1 - (embedding <=> CAST(:emb AS vector)) AS score, NULL AS fused "
                        f"FROM {self._table} {clause} "
                        f"ORDER BY embedding <=> CAST(:emb AS vector) LIMIT :limit"
                    ),
                    params,
                )
            hits = [
                SearchHit(
                    document_id=row["document_id"],
                    chunk_index=row["chunk_index"],
                    content=row["content"],
                    score=max(0.0, min(1.0, float(row["score"]))),
                    metadata=row["metadata"] or {},
                    fused_score=None if row["fused"] is None else float(row["fused"]),
                )
                for row in rows.mappings()
            ]
        if min_score is not None:
            hits = [h for h in hits if h.score >= min_score]
        return hits

    def _hybrid_sql(self, clause: str) -> str:
        """Reciprocal rank fusion: 1/(k + rank) summed over the two rankings.

        Ranks, not scores, are fused because a cosine similarity and a
        ts_rank are on scales that have nothing to do with each other.
        """
        text_clause = f"{clause} AND" if clause else "WHERE"
        t = self._table
        return (
            f"WITH q AS (SELECT websearch_to_tsquery(CAST(:cfg AS regconfig), :q) AS query), "  # nosec B608
            f"v AS (SELECT id, "
            f"      row_number() OVER (ORDER BY embedding <=> CAST(:emb AS vector)) AS r "
            f"      FROM {t} {clause} ORDER BY embedding <=> CAST(:emb AS vector) LIMIT :k), "
            f"f AS (SELECT {t}.id, "
            f"      row_number() OVER (ORDER BY ts_rank_cd(search_text, q.query) DESC) AS r "
            f"      FROM {t}, q {text_clause} search_text @@ q.query "
            f"      ORDER BY ts_rank_cd(search_text, q.query) DESC LIMIT :k), "
            f"ids AS (SELECT id FROM v UNION SELECT id FROM f) "
            f"SELECT c.document_id, c.chunk_index, c.content, c.metadata, "
            f"1 - (c.embedding <=> CAST(:emb AS vector)) AS score, "
            f"coalesce(1.0 / (:rrf + v.r), 0) + coalesce(1.0 / (:rrf + f.r), 0) AS fused "
            f"FROM ids JOIN {t} c ON c.id = ids.id "
            f"LEFT JOIN v ON v.id = ids.id LEFT JOIN f ON f.id = ids.id "
            f"ORDER BY fused DESC, score DESC LIMIT :limit"
        )

    async def list_chunks(self, document_id: str, *, tenant_id: str | None) -> list[dict[str, Any]]:
        """What a document was split into, in order. For showing sources."""
        from sqlalchemy import text

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="read")
        async with self._engine.begin() as conn:
            await self._begin(conn, tenant_id)
            rows = await conn.execute(
                text(
                    f"SELECT chunk_index, content, metadata FROM {self._table} "  # nosec B608
                    f"WHERE {_tenant_match(tenant_id)} AND document_id = :doc "
                    f"ORDER BY chunk_index"
                ),
                {"tenant": tenant_id, "doc": document_id},
            )
            return [dict(r) for r in rows.mappings()]

    async def health(self) -> tuple[bool, str]:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text(f"SELECT 1 FROM {self._table} LIMIT 1"))  # nosec B608
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"pgvector table unreachable: {exc}"
        return True, f"pgvector table {self._table} reachable"
