"""The rag service and its optional router, over an in-memory store.

No database: the store here implements the VectorStore protocol in a list,
which is also the proof that a custom store needs nothing beyond it.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.auth import AuthPlugin
from jfastframework.plugins.builtin.rag import RagPlugin
from jfastframework.plugins.builtin.tenancy import TenancyPlugin
from jfastframework.rag import RagService, format_context
from jfastframework.testing import build_test_app, client_for
from jfastframework.vectors.base import Chunk, SearchHit, require_tenant

AUTH_SECRET = "a-test-secret-long-enough-for-sha256-at-least-32-bytes"
DIMS = 8


class LetterEmbedder:
    """Counts letters a-h: crude, deterministic, and enough to rank."""

    dimensions = DIMS

    def __init__(self) -> None:
        self.tenants: list[str | None] = []

    async def embed(self, texts: list[str], *, tenant_id: str | None = None) -> list[list[float]]:
        self.tenants.append(tenant_id)
        out = []
        for t in texts:
            v = [float(t.lower().count(c)) for c in "abcdefgh"]
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class MemoryStore:
    supports_hybrid = False
    tenant_scoped = True

    def __init__(self, ctx: Any = None) -> None:
        self.rows: dict[tuple[str | None, str, int], tuple[Chunk, list[float]]] = {}

    async def ensure_schema(self) -> None: ...

    async def existing_hashes(self, document_id: str, *, tenant_id: str | None) -> dict[int, str]:
        require_tenant(tenant_id, scoped=True, action="read")
        return {
            i: c.content_hash or ""
            for (t, d, i), (c, _) in self.rows.items()
            if t == tenant_id and d == document_id
        }

    async def sync_document(
        self,
        document_id: str,
        *,
        tenant_id: str | None,
        chunks: list[Chunk],
        embeddings: dict[int, list[float]],
    ) -> int:
        require_tenant(tenant_id, scoped=True, action="write")
        for key in [
            k
            for k in self.rows
            if k[0] == tenant_id and k[1] == document_id and k[2] >= len(chunks)
        ]:
            del self.rows[key]
        for c in chunks:
            vector = (
                embeddings.get(c.chunk_index)
                or self.rows[(tenant_id, document_id, c.chunk_index)][1]
            )
            self.rows[(tenant_id, document_id, c.chunk_index)] = (c, vector)
        return len(chunks)

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        raise NotImplementedError

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
        require_tenant(tenant_id, scoped=True, action="search")
        self.last_text = text
        hits = [
            SearchHit(
                d, i, c.content, sum(a * b for a, b in zip(embedding, v, strict=True)), c.metadata
            )
            for (t, d, i), (c, v) in self.rows.items()
            if t == tenant_id
        ]
        return sorted(hits, key=lambda h: -h.score)[:limit]

    async def delete_document(self, document_id: str, *, tenant_id: str | None = None) -> None:
        for key in [k for k in self.rows if k[0] == tenant_id and k[1] == document_id]:
            del self.rows[key]

    async def health(self) -> tuple[bool, str]:
        return True, "memory"


async def test_a_store_without_hybrid_falls_back_to_vectors() -> None:
    store = MemoryStore()
    rag = RagService(store, LetterEmbedder(), chunk_size=100, chunk_overlap=0, hybrid=True)
    await rag.ingest("d", "abc abc", tenant_id="t")
    await rag.search("abc", tenant_id="t")
    assert store.last_text is None


async def test_the_embedder_is_told_which_tenant_is_paying() -> None:
    embedder = LetterEmbedder()
    rag = RagService(MemoryStore(), embedder, chunk_size=100, chunk_overlap=0)
    await rag.ingest("d", "abc", tenant_id="acme")
    await rag.search("abc", tenant_id="acme")
    assert embedder.tenants == ["acme", "acme"]


async def test_an_embedder_without_a_tenant_parameter_still_works() -> None:
    class Plain:
        dimensions = DIMS

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] + [0.0] * (DIMS - 1) for _ in texts]

    rag = RagService(MemoryStore(), Plain(), chunk_size=100, chunk_overlap=0)
    assert (await rag.ingest("d", "hello", tenant_id="t")).embedded == 1


def test_format_context_numbers_sources_and_never_cuts_an_excerpt() -> None:
    hits = [
        SearchHit("contract", 0, "a" * 50, 0.9, {"title": "Contrato"}),
        SearchHit("x", 2, "b" * 50, 0.8),
    ]
    text = format_context(hits, max_chars=80)
    assert text.startswith("[1] Contrato (part 1)")
    assert "[2]" not in text  # the second would have been cut; it is left out instead


def _app(**rag: object) -> Any:
    return build_test_app(
        plugins=["observability", "auth", "tenancy", "rag"],
        extra_plugins=[AuthPlugin, TenancyPlugin, RagPlugin],
        raw={
            "plugin": {
                "auth": {
                    "mode": "secret",
                    "secret": AUTH_SECRET,
                    "algorithms": ["HS256"],
                    "issuer": "https://id.example.com/",
                    "audience": "svc",
                    "mount_router": False,
                },
                "tenancy": {"sources": ["token", "user"]},
                "rag": {
                    "store": "test_rag_router:MemoryStore",
                    "embedder": "test_rag_router:LetterEmbedder",
                    "dimensions": DIMS,
                    "mount_router": True,
                    "chunk_size": 100,
                    "chunk_overlap": 0,
                    **rag,
                },
            }
        },
    )


def _bearer(subject: str) -> dict[str, str]:
    from jfastframework.auth.tokens import issue

    token, _, _ = issue(
        subject,
        key=AUTH_SECRET,
        algorithm="HS256",
        lifetime=timedelta(minutes=5),
        audience="svc",
        issuer="https://id.example.com/",
    )
    return {"Authorization": f"Bearer {token}"}


async def test_the_router_needs_a_signed_in_caller() -> None:
    async with client_for(_app()) as client:
        response = await client.post("/rag/search", json={"query": "abc"})
        assert response.status_code == 401


async def test_the_router_takes_the_tenant_from_the_token_not_the_body() -> None:
    async with client_for(_app()) as client:
        ingest = await client.post(
            "/rag/documents",
            headers=_bearer("alice"),
            json={"document_id": "d", "content": "abc abc", "tenant_id": "bob"},
        )
        assert ingest.status_code == 200, ingest.text

        # Bob asks for alice's tenant in the body; the body is ignored.
        bob = await client.post(
            "/rag/search", headers=_bearer("bob"), json={"query": "abc", "tenant_id": "alice"}
        )
        assert bob.json()["results"] == []
        alice = await client.post("/rag/search", headers=_bearer("alice"), json={"query": "abc"})
        assert [r["document_id"] for r in alice.json()["results"]] == ["d"]


async def test_router_scopes_are_enforced() -> None:
    async with client_for(_app(write_scopes=["docs:write"])) as client:
        response = await client.post(
            "/rag/documents", headers=_bearer("alice"), json={"document_id": "d", "content": "abc"}
        )
        assert response.status_code == 403


def test_the_router_cannot_be_mounted_without_auth() -> None:
    app = build_test_app()
    plugin = RagPlugin(
        {
            "store": "test_rag_router:MemoryStore",
            "embedder": "test_rag_router:LetterEmbedder",
            "dimensions": DIMS,
            "mount_router": True,
        }
    )
    with pytest.raises(PluginError, match="needs the auth plugin"):
        plugin.register(app.state.jfast)


def test_the_embedder_and_the_table_must_agree_on_dimensions() -> None:
    app = build_test_app()
    plugin = RagPlugin(
        {
            "store": "test_rag_router:MemoryStore",
            "embedder": "test_rag_router:LetterEmbedder",
            "dimensions": 1536,
        }
    )
    with pytest.raises(PluginError, match="dimensions"):
        plugin.register(app.state.jfast)


def test_the_llm_embedder_needs_the_llm_plugin() -> None:
    app = build_test_app()
    with pytest.raises(PluginError, match="llm plugin"):
        RagPlugin({"store": "test_rag_router:MemoryStore", "embedder": "llm"}).register(
            app.state.jfast
        )
