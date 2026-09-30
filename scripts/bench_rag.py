"""RAG at scale on PostgreSQL + pgvector: ingest, index, search, size.

    python scripts/bench_rag.py --pg postgresql+asyncpg://jfast:jfast@localhost:5487 \\
        --chunks 100000 --tenants 1000 --dims 384

Drives ``PgVectorStore`` -- the store the ``rag`` plugin uses -- so every
number is the framework's path, not a hand-tuned one:

1. **Ingest with the HNSW index in place** (the steady state: documents
   arriving one by one into a live table), for the first ``--live-ingest``
   chunks, ``--writers`` documents at a time.
2. **Bulk ingest without it** for the rest, then **build the index** once,
   the way a first import or a re-embedding should be done. The build runs
   with ``maintenance_work_mem = --build-mem``; below the graph's size
   pgvector builds on disk and takes several times longer.
3. **Search** ``--queries`` times from random tenants: vector only (the
   tenant filter is always on -- the store is tenant scoped), vector plus a
   metadata filter, and hybrid (vector + full text, fused), each as p50/p95/
   p99 of single queries and as throughput with ``--concurrency`` at once.
4. **Recall@10** of the tenant-filtered HNSW search against an exact scan of
   the same tenant, because a fast filtered search that misses results is
   not fast, it is wrong.
5. **Sizes**: table, TOAST, each index.

Embeddings are synthetic: each tenant has a few topic centroids and every
chunk is a centroid plus noise, normalised. Uniform random vectors would be
the worst case for HNSW and no real corpus looks like that; this is closer
to real text without pretending to be it. The text for full-text search is
drawn from a synthetic Zipf vocabulary for the same reason.

Needs numpy (installed with the ``qdrant`` extra) and a PostgreSQL with
pgvector >= 0.8. Never point it at a database you care about: it drops and
recreates ``--table``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from typing import Any

WORDS = 5000
KINDS = ("invoice", "contract", "receipt", "memo", "ruling")


def _vocabulary(rng: random.Random) -> list[str]:
    syllables = ["ka", "lo", "mi", "tu", "re", "sa", "no", "vi", "de", "po", "zu", "fe"]
    words: set[str] = set()
    while len(words) < WORDS:
        words.add("".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))))
    return sorted(words)


class Corpus:
    """Deterministic synthetic chunks, generated a document at a time."""

    def __init__(self, *, chunks: int, tenants: int, dims: int, per_doc: int, seed: int) -> None:
        import numpy as np

        self.np = np
        self.chunks, self.tenants, self.dims, self.per_doc = chunks, tenants, dims, per_doc
        self.rng = random.Random(seed)
        self.nrng = np.random.default_rng(seed)
        self.words = _vocabulary(self.rng)
        # Zipf weights: a few words everywhere, most words rare -- what
        # makes full-text ranking do real work.
        self.weights = [1.0 / (rank + 1) for rank in range(WORDS)]
        # Five topics per tenant, shared across tenants' spaces only by chance.
        self.centroids = self._unit(self.nrng.standard_normal((tenants, 5, dims)))

    def _unit(self, matrix: Any) -> Any:
        return matrix / self.np.linalg.norm(matrix, axis=-1, keepdims=True)

    def tenant(self, index: int) -> str:
        return f"tenant-{index:05d}"

    def documents(self) -> Any:
        """Yields (tenant, document_id, [(text, metadata, vector)])."""
        documents = (self.chunks + self.per_doc - 1) // self.per_doc
        for number in range(documents):
            tenant = number % self.tenants
            count = min(self.per_doc, self.chunks - number * self.per_doc)
            topics = self.nrng.integers(0, 5, size=count)
            noise = self.nrng.standard_normal((count, self.dims)) * 0.35
            vectors = self._unit(self.centroids[tenant, topics] + noise / self.np.sqrt(self.dims))
            rows = []
            for i in range(count):
                text = " ".join(self.rng.choices(self.words, weights=self.weights, k=60))
                meta = {"kind": KINDS[(number + i) % len(KINDS)], "year": 2020 + number % 7}
                rows.append((text, meta, vectors[i].round(5).tolist()))
            yield self.tenant(tenant), f"doc-{number:07d}", rows

    def query(self, tenant: int) -> tuple[list[float], str]:
        topic = int(self.nrng.integers(0, 5))
        noise = self.nrng.standard_normal(self.dims) * 0.35 / self.np.sqrt(self.dims)
        vector = self._unit(self.centroids[tenant, topic] + noise)
        words = self.rng.choices(self.words[50:2000], k=2)
        return vector.round(5).tolist(), " ".join(words)


def _pct(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))]


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(_pct(values, 0.95), 2),
        "p99_ms": round(_pct(values, 0.99), 2),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from jfastframework.vectors.base import Chunk
    from jfastframework.vectors.pgvector import PgVectorStore, _name

    admin = create_async_engine(f"{args.pg}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        if not await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": args.database}
        ):
            await conn.execute(text(f'CREATE DATABASE "{args.database}"'))
    await admin.dispose()

    engine = create_async_engine(
        f"{args.pg}/{args.database}", pool_size=args.writers + args.concurrency + 2
    )
    table = args.table
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    store = PgVectorStore(engine, table=table, dimensions=args.dims, tenant_scoped=True)
    await store.ensure_schema()
    hnsw = _name(table, "embedding_hnsw")

    corpus = Corpus(
        chunks=args.chunks,
        tenants=args.tenants,
        dims=args.dims,
        per_doc=args.per_doc,
        seed=args.seed,
    )
    result: dict[str, Any] = {
        "config": {
            k: getattr(args, k)
            for k in (
                "chunks",
                "tenants",
                "dims",
                "per_doc",
                "writers",
                "live_ingest",
                "build_mem",
                "build_workers",
            )
        }
    }

    async def ingest(documents: Any, limit: int) -> tuple[int, float]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=args.writers * 2)
        written = 0

        async def writer() -> None:
            nonlocal written
            while (item := await queue.get()) is not None:
                tenant, doc, rows = item
                chunks = [
                    Chunk(doc, i, content, meta, tenant)
                    for i, (content, meta, _) in enumerate(rows)
                ]
                # Awaited first: `written += await ...` reads `written` before
                # the await, and eight writers would lose each other's counts.
                count = await store.upsert(chunks, [vector for _, _, vector in rows])
                written += count

        started = time.perf_counter()
        workers = [asyncio.create_task(writer()) for _ in range(args.writers)]
        fed = 0
        for item in documents:
            await queue.put(item)
            fed += len(item[2])
            if fed >= limit:
                break
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
        return written, time.perf_counter() - started

    documents = corpus.documents()
    live = min(args.live_ingest, args.chunks)
    written, seconds = await ingest(documents, live)
    result["ingest_with_hnsw"] = {"chunks": written, "chunks_per_s": round(written / seconds)}
    print(f"ingest with HNSW in place: {written} chunks, {written / seconds:,.0f}/s", flush=True)

    rest = args.chunks - written
    if rest > 0:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP INDEX IF EXISTS {hnsw}"))
        more, seconds = await ingest(documents, rest)
        result["ingest_without_hnsw"] = {"chunks": more, "chunks_per_s": round(more / seconds)}
        print(f"bulk ingest without HNSW: {more} chunks, {more / seconds:,.0f}/s", flush=True)

        started = time.perf_counter()
        async with engine.begin() as conn:
            await conn.execute(text(f"SET maintenance_work_mem = '{args.build_mem}'"))
            # A parallel build keeps the graph in dynamic shared memory, which
            # in a container is /dev/shm: 64 MB unless shm_size raises it (the
            # generated compose sets 1gb). Over it, the build fails with
            # "could not resize shared memory segment".
            await conn.execute(
                text(f"SET max_parallel_maintenance_workers = {int(args.build_workers)}")
            )
            await conn.execute(
                text(
                    f"CREATE INDEX {hnsw} ON {table} USING hnsw (embedding vector_cosine_ops) "
                    "WITH (m = 16, ef_construction = 64)"
                )
            )
        build = time.perf_counter() - started
        result["hnsw_build_s"] = round(build, 1)
        print(f"HNSW build over {args.chunks} chunks: {build:.1f} s", flush=True)

    async with engine.begin() as conn:
        await conn.execute(text(f"ANALYZE {table}"))
        sizes = (
            await conn.execute(
                text("SELECT pg_table_size(:t), pg_indexes_size(:t), pg_total_relation_size(:t)"),
                {"t": table},
            )
        ).one()
        indexes = (
            await conn.execute(
                text(
                    "SELECT indexrelid::regclass::text, pg_relation_size(indexrelid) "
                    "FROM pg_index WHERE indrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        ).all()
    mb = 1024 * 1024
    result["size_mb"] = {
        "table_with_toast": round(sizes[0] / mb, 1),
        "indexes": round(sizes[1] / mb, 1),
        "total": round(sizes[2] / mb, 1),
        "by_index": {name: round(size / mb, 1) for name, size in indexes},
    }
    print(f"sizes: {json.dumps(result['size_mb'])}", flush=True)

    queries = [
        (corpus.tenant(t), *corpus.query(t))
        for t in (random.Random(args.seed + 1).randrange(args.tenants) for _ in range(args.queries))
    ]

    async def timed(mode: str, tenant: str, vector: list[float], words: str) -> float:
        started = time.perf_counter()
        await store.search(
            vector,
            tenant_id=tenant,
            limit=10,
            where={"kind": "invoice"} if mode == "filtered" else None,
            text=words if mode == "hybrid" else None,
        )
        return (time.perf_counter() - started) * 1000

    result["search"] = {}
    for mode in ("vector", "filtered", "hybrid"):
        for query in queries:  # one warm pass: steady state, not first touch
            await timed(mode, *query)
        latencies = [await timed(mode, *query) for query in queries]
        semaphore = asyncio.Semaphore(args.concurrency)

        async def bounded(
            query: Any, mode: str = mode, semaphore: asyncio.Semaphore = semaphore
        ) -> float:
            async with semaphore:
                return await timed(mode, *query)

        started = time.perf_counter()
        await asyncio.gather(*(bounded(q) for q in queries))
        qps = len(queries) / (time.perf_counter() - started)
        result["search"][mode] = {**_summary(latencies), f"qps_at_{args.concurrency}": round(qps)}
        print(f"search {mode}: {json.dumps(result['search'][mode])}", flush=True)

    result["plan"] = await _plan(engine, table, queries[0])
    print(f"plan for a tenant-filtered vector search: {result['plan']}", flush=True)
    result["recall_at_10"] = await _recall(engine, store, table, queries[: args.recall_queries])
    print(f"recall@10 (tenant-filtered HNSW vs exact): {result['recall_at_10']}", flush=True)
    await engine.dispose()
    return result


async def _plan(engine: Any, table: str, query: Any) -> list[str]:
    """Which indexes the planner picks for one tenant's vector search.

    With few chunks per tenant it reads the tenant's rows by the btree and
    sorts them exactly -- recall 1.0, and the HNSW index unused. With many,
    it walks the HNSW graph and filters. Which one happened explains the
    latency and the recall next to it.
    """
    from sqlalchemy import text

    tenant, vector, _ = query
    async with engine.begin() as conn:
        await conn.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
        raw = (
            await conn.execute(
                text(
                    f"EXPLAIN (FORMAT JSON) SELECT id FROM {table} WHERE tenant_id = :t "
                    "ORDER BY embedding <=> CAST(:e AS vector) LIMIT 10"
                ),
                {"t": tenant, "e": str(vector)},
            )
        ).scalar()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    found: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if "Index Name" in node:
            found.append(f"{node['Node Type']} on {node['Index Name']}")
        elif node.get("Node Type") in ("Seq Scan", "Sort"):
            found.append(str(node["Node Type"]))
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    return found


async def _recall(engine: Any, store: Any, table: str, queries: list[Any]) -> float:
    """Share of the exact top 10 of a tenant that the HNSW search returns."""
    from sqlalchemy import text

    found = total = 0
    for tenant, vector, _ in queries:
        approximate = await store.search(vector, tenant_id=tenant, limit=10)
        async with engine.begin() as conn:
            # With index scans off the planner has to compute every distance
            # in the tenant: the exact answer.
            await conn.execute(text("SET LOCAL enable_indexscan = off"))
            await conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            exact = (
                await conn.execute(
                    text(
                        f"SELECT document_id, chunk_index FROM {table} WHERE tenant_id = :t "
                        "ORDER BY embedding <=> CAST(:e AS vector) LIMIT 10"
                    ),
                    {"t": tenant, "e": str(vector)},
                )
            ).all()
        truth = {(d, i) for d, i in exact}
        found += len(truth & {(h.document_id, h.chunk_index) for h in approximate})
        total += len(truth)
    return round(found / total, 4) if total else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pg", default=os.environ.get("JFAST_TEST_PG_URL", ""), required=False)
    parser.add_argument("--database", default="jfast_rag_bench")
    parser.add_argument("--table", default="rag_bench_chunks")
    parser.add_argument("--chunks", type=int, default=100_000)
    parser.add_argument("--tenants", type=int, default=1000)
    parser.add_argument("--dims", type=int, default=384)
    parser.add_argument("--per-doc", type=int, default=20)
    parser.add_argument("--writers", type=int, default=8, help="documents written at once")
    parser.add_argument("--live-ingest", type=int, default=20_000)
    parser.add_argument("--build-mem", default="1GB", help="maintenance_work_mem for the build")
    parser.add_argument(
        "--build-workers",
        type=int,
        default=0,
        help="parallel workers for the build; >0 needs /dev/shm larger than --build-mem",
    )
    parser.add_argument("--queries", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--recall-queries", type=int, default=100)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=str, help="also write the result here")
    args = parser.parse_args(argv)
    if not args.pg:
        parser.error("--pg (or JFAST_TEST_PG_URL) is required")

    started = time.perf_counter()
    result = asyncio.run(run(args))
    result["wall_s"] = round(time.perf_counter() - started, 1)
    print(json.dumps(result, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
