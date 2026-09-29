"""The pgvector store and RagService against a real PostgreSQL with pgvector.

Skipped without a server at JFAST_TEST_PG_URL, or when it has no `vector`
extension (the CI uses the pgvector/pgvector image for exactly this).

The embedder is a deterministic bag of words hashed into 64 dimensions: two
texts sharing words are close, which is all these tests need, and it costs
nothing and never changes between runs.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from jfastframework.rag import RagService
from jfastframework.vectors.base import TenantRequiredError
from jfastframework.vectors.pgvector import PgVectorStore

PG_BASE = os.environ.get("JFAST_TEST_PG_URL", "postgresql+asyncpg://jfast:jfast@localhost:5499")
DSN = f"{PG_BASE}/jfast"
DIMS = 64
TABLE = "rag_test_chunks"


class WordEmbedder:
    dimensions = DIMS
    model_id = "words:64"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], *, tenant_id: str | None = None) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            vec = [0.0] * DIMS
            for word in t.lower().replace(".", " ").replace(",", " ").split():
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIMS] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    @property
    def embedded(self) -> int:
        return sum(len(c) for c in self.calls)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    except Exception as exc:  # noqa: BLE001 - no server, or no pgvector on it
        await engine.dispose()
        pytest.skip(f"no PostgreSQL with pgvector at {PG_BASE}: {type(exc).__name__}")
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
    await engine.dispose()


@pytest.fixture
async def store(engine: AsyncEngine) -> PgVectorStore:
    store = PgVectorStore(engine, table=TABLE, dimensions=DIMS)
    await store.ensure_schema()
    return store


@pytest.fixture
def embedder() -> WordEmbedder:
    return WordEmbedder()


@pytest.fixture
def rag(store: PgVectorStore, embedder: WordEmbedder) -> RagService:
    return RagService(store, embedder, chunk_size=80, chunk_overlap=0)


CONTRACT = (
    "The supplier delivers the goods within thirty days of the order.\n\n"
    "Late delivery carries a penalty of two percent per week.\n\n"
    "Payment is due fifteen days after the invoice, by bank transfer."
)


async def test_schema_is_idempotent_and_uses_hnsw(
    engine: AsyncEngine, store: PgVectorStore
) -> None:
    await store.ensure_schema()
    async with engine.connect() as conn:
        indexes = dict(
            (
                await conn.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
                    {"t": TABLE},
                )
            ).all()
        )
    assert any("hnsw" in d for d in indexes.values())
    assert not any("ivfflat" in d for d in indexes.values())
    assert any("gin" in d and "search_text" in d for d in indexes.values())


async def test_two_tenants_can_use_the_same_document_id(rag: RagService) -> None:
    await rag.ingest("contract-1", CONTRACT, tenant_id="acme")
    await rag.ingest("contract-1", "Globex rents the warehouse on Elm street.", tenant_id="globex")

    acme = await rag.search("penalty for late delivery", tenant_id="acme")
    globex = await rag.search("penalty for late delivery", tenant_id="globex")
    assert acme and all("warehouse" not in h.content for h in acme)
    assert globex and all("warehouse" in h.content for h in globex)

    # Deleting one tenant's document leaves the other's alone.
    await rag.delete("contract-1", tenant_id="acme")
    assert await rag.search("penalty", tenant_id="acme") == []
    assert await rag.search("warehouse", tenant_id="globex")


async def test_a_tenant_scoped_store_refuses_to_work_without_a_tenant(rag: RagService) -> None:
    with pytest.raises(TenantRequiredError):
        await rag.search("anything", tenant_id=None)
    with pytest.raises(TenantRequiredError):
        await rag.ingest("doc", "text", tenant_id=None)
    with pytest.raises(TenantRequiredError):
        await rag.delete("doc", tenant_id=None)


async def test_a_single_tenant_store_works_with_no_tenant(
    engine: AsyncEngine, embedder: WordEmbedder
) -> None:
    store = PgVectorStore(engine, table=TABLE, dimensions=DIMS, tenant_scoped=False)
    await store.ensure_schema()
    rag = RagService(store, embedder, chunk_size=200, chunk_overlap=0)
    await rag.ingest("manual", CONTRACT, tenant_id=None)
    await rag.ingest("manual", CONTRACT, tenant_id=None)  # the NULL tenant still conflicts
    hits = await rag.search("bank transfer payment", tenant_id=None)
    assert hits[0].document_id == "manual"
    assert len({(h.document_id, h.chunk_index) for h in hits}) == len(hits)


async def test_reingest_embeds_only_what_changed(rag: RagService, embedder: WordEmbedder) -> None:
    first = await rag.ingest("c", CONTRACT, tenant_id="acme")
    assert (first.chunks, first.embedded, first.reused) == (3, 3, 0)

    again = await rag.ingest("c", CONTRACT, tenant_id="acme")
    assert (again.embedded, again.reused) == (0, 3)

    edited = CONTRACT.replace("two percent", "five percent")
    changed = await rag.ingest("c", edited, tenant_id="acme")
    assert (changed.embedded, changed.reused) == (1, 2)
    assert any(
        "five percent" in h.content for h in await rag.search("penalty percent", tenant_id="acme")
    )

    shorter = await rag.ingest("c", CONTRACT.split("\n\n")[0], tenant_id="acme")
    assert shorter.chunks == 1
    hits = await rag.search("payment invoice transfer", tenant_id="acme", limit=10)
    assert {h.chunk_index for h in hits} == {0}


async def test_metadata_changes_without_reembedding(
    rag: RagService, embedder: WordEmbedder
) -> None:
    await rag.ingest("c", CONTRACT, tenant_id="acme", metadata={"status": "draft"})
    before = embedder.embedded
    result = await rag.ingest("c", CONTRACT, tenant_id="acme", metadata={"status": "signed"})
    assert result.embedded == 0
    assert embedder.embedded == before + 0  # the search below embeds the query only
    hits = await rag.search("delivery", tenant_id="acme", where={"status": "signed"})
    assert hits and all(h.metadata["status"] == "signed" for h in hits)
    assert await rag.search("delivery", tenant_id="acme", where={"status": "draft"}) == []


async def test_filters_by_document(rag: RagService) -> None:
    await rag.ingest("a", "Invoices are paid by transfer.", tenant_id="acme")
    await rag.ingest("b", "Invoices are paid in cash.", tenant_id="acme")
    hits = await rag.search("invoices paid", tenant_id="acme", document_ids=["b"])
    assert [h.document_id for h in hits] == ["b"]


async def test_hybrid_search_finds_the_exact_code(rag: RagService) -> None:
    # Many similar documents; only one carries the product code. Full text is
    # what finds an exact token, and fusion puts it first.
    for i in range(12):
        await rag.ingest(
            f"note-{i}", f"Shipment note {i} for the warehouse order batch.", tenant_id="acme"
        )
    await rag.ingest("target", "Shipment note for order batch with code ZX-4471.", tenant_id="acme")

    hybrid = await rag.search("ZX-4471", tenant_id="acme", limit=3, hybrid=True)
    assert hybrid[0].document_id == "target"
    assert hybrid[0].fused_score is not None
    assert 0.0 <= hybrid[0].score <= 1.0


async def test_min_score_drops_weak_matches(rag: RagService) -> None:
    await rag.ingest("c", CONTRACT, tenant_id="acme")
    assert (
        await rag.search("zebra xylophone quartz", tenant_id="acme", min_score=0.5, hybrid=False)
        == []
    )


async def test_upgrades_a_0_1_0a9_table_in_place(engine: AsyncEngine) -> None:
    # What 0.1.0a9's ensure_schema created: global unique key, IVFFlat index.
    async with engine.begin() as conn:
        await conn.execute(
            text(f"""
            CREATE TABLE {TABLE} (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT, document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL, content TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb, embedding VECTOR({DIMS}) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (document_id, chunk_index))""")
        )
        await conn.execute(
            text(
                f"CREATE INDEX {TABLE}_embedding_idx ON {TABLE} "
                f"USING ivfflat (embedding vector_cosine_ops) "
                f"WITH (lists = 100)"
            )
        )
        await conn.execute(text(f"CREATE INDEX {TABLE}_tenant_idx ON {TABLE} (tenant_id)"))
        await conn.execute(
            text(
                f"INSERT INTO {TABLE} (tenant_id, document_id, chunk_index, content, embedding) "
                f"VALUES ('acme', 'old', 0, 'legacy row', CAST(:e AS vector))"
            ),
            {"e": str([1.0] + [0.0] * (DIMS - 1))},
        )

    store = PgVectorStore(engine, table=TABLE, dimensions=DIMS)
    await store.ensure_schema()

    async with engine.connect() as conn:
        defs = [
            r[0]
            for r in await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": TABLE}
            )
        ]
        constraints = [
            r[0]
            for r in await conn.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
                {"t": TABLE},
            )
        ]
        rows = (await conn.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar()
    assert rows == 1
    assert not any("ivfflat" in d for d in defs) and any("hnsw" in d for d in defs)
    assert f"{TABLE}_identity" in constraints
    assert f"{TABLE}_document_id_chunk_index_key" not in constraints

    # And the global key is really gone: another tenant can now reuse the id.
    rag = RagService(store, WordEmbedder(), chunk_size=200, chunk_overlap=0)
    await rag.ingest("old", "globex text", tenant_id="globex")
    assert (await rag.search("legacy row", tenant_id="acme", hybrid=False))[
        0
    ].content == "legacy row"


async def test_row_level_security_on_the_chunks_table(
    engine: AsyncEngine, store: PgVectorStore
) -> None:
    """The store sets jfast.tenant_id per transaction, so tenant RLS binds it too.

    Proven with a role that is neither superuser nor BYPASSRLS: the database,
    not the store's WHERE clause, is what hides globex's rows from acme.
    """
    from jfastframework.db.rls import enable_tenant_rls

    class _Op:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

    op = _Op()
    enable_tenant_rls(op, TABLE)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'jfast_app') "
                "THEN CREATE ROLE jfast_app LOGIN PASSWORD 'jfast_app' NOSUPERUSER NOBYPASSRLS; "
                "END IF; END $$"
            )
        )
        await conn.execute(text(f"GRANT ALL ON {TABLE} TO jfast_app"))
        await conn.execute(text(f"GRANT ALL ON SEQUENCE {TABLE}_id_seq TO jfast_app"))
        for statement in op.statements:
            await conn.execute(text(statement))

    app_engine = create_async_engine(DSN.replace("://jfast:jfast@", "://jfast_app:jfast_app@"))
    try:
        app_store = PgVectorStore(app_engine, table=TABLE, dimensions=DIMS)
        rag = RagService(app_store, WordEmbedder(), chunk_size=80, chunk_overlap=0)
        await rag.ingest("doc", "acme secret pricing", tenant_id="acme")
        await rag.ingest("doc", "globex secret pricing", tenant_id="globex")
        assert [h.content for h in await rag.search("secret pricing", tenant_id="acme")] == [
            "acme secret pricing"
        ]

        # Even a query that forgets its own tenant filter sees one tenant only.
        async with app_engine.begin() as conn:
            await conn.execute(text("SELECT set_config('jfast.tenant_id', 'globex', true)"))
            seen = [r[0] for r in await conn.execute(text(f"SELECT content FROM {TABLE}"))]
        assert seen == ["globex secret pricing"]
    finally:
        await app_engine.dispose()
