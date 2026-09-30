"""Retrieval-Augmented Generation over a pluggable vector store.

    [plugins]
    enabled = ["observability", "database", "cache", "llm", "rag"]

    [plugin.rag]
    store = "pgvector"          # or "qdrant", or "package.module:Class"
    embedder = "llm"            # or "ollama", or "package.module:Class"
    dimensions = 1536
    collection = "rag_chunks"

Modules use the service, not the store::

    rag = get_context(request.app).require("rag")      # jfastframework.rag.RagService
    await rag.ingest(doc_id, text, tenant_id=tenant)
    hits = await rag.search(question, tenant_id=tenant)

What changed in 0.1.0a10, because each was a real failure:

* **Tenant isolation.** A chunk's identity includes the tenant, and a
  tenant-scoped store (``tenant_scoped = true``, the default) refuses to read
  or write without one. Before, two tenants with the same document id
  overwrote each other, and a search with no tenant searched everyone.
* **HNSW instead of IVFFlat** on pgvector, hybrid (full-text + vector) search,
  structure-aware chunking, and re-ingest that embeds only changed chunks.
* **The HTTP router is off by default and authenticated when on.** It took
  the tenant from the request body -- anyone could read any tenant -- and did
  not check a token. Now it needs the ``auth`` plugin, a signed-in caller, and
  takes the tenant from the tenancy plugin or the token, never from the body.

Requires: ``pip install jfastframework[rag]`` plus the extra for the store.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from jfastframework.errors import PluginError, ServiceUnavailableError
from jfastframework.plugins.base import HealthReport, Plugin, PluginMeta, PluginSettings
from jfastframework.rag import Embedder, RagService, chunk_text
from jfastframework.vectors.base import TenantRequiredError, VectorStore

if TYPE_CHECKING:
    from jfastframework.context import AppContext


class OllamaEmbedder:
    """Local embeddings through an Ollama server."""

    def __init__(self, base_url: str, model: str, dimensions: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.model_id = f"ollama:{model}:{dimensions}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        async with httpx.AsyncClient(base_url=self.base_url, timeout=60.0) as client:
            response = await client.post("/api/embed", json={"model": self.model, "input": texts})
            if response.status_code >= 400:
                raise ServiceUnavailableError(
                    f"Ollama embedding failed ({response.status_code}): {response.text[:200]}"
                )
            return list(response.json()["embeddings"])


def _load_class(path: str, what: str) -> Any:
    module_path, _, attr = path.partition(":")
    if not module_path or not attr:
        raise PluginError(
            f"Invalid {what} {path!r}. Use a built-in name or 'package.module:ClassName'."
        )
    try:
        return getattr(importlib.import_module(module_path), attr)
    except (ImportError, AttributeError) as exc:
        raise PluginError(f"Cannot load {what} {path!r}: {exc}") from exc


class RagSettings(PluginSettings):
    model_config = SettingsConfigDict(env_prefix="JFAST_RAG_", env_file=".env", extra="ignore")

    # "pgvector" | "qdrant" | "package.module:ClassName"
    store: str = "pgvector"
    # "llm" | "ollama" | "package.module:ClassName"
    embedder: str = "ollama"

    # Table name for pgvector, collection name for Qdrant.
    collection: str = "rag_chunks"
    dimensions: int = 768
    # Every read and write needs a tenant. Set false only for a service that
    # has exactly one tenant; it is what makes "forgot the tenant" an error
    # instead of a search across every customer.
    tenant_scoped: bool = True

    chunk_size: int = Field(default=1000, ge=100)
    chunk_overlap: int = Field(default=150, ge=0)
    # "recursive" follows headings, paragraphs and sentences; "fixed" is the
    # 0.1.0a9 character window.
    chunk_strategy: str = "recursive"
    top_k: int = Field(default=5, ge=1, le=100)
    min_score: float | None = None
    # Full-text + vector with reciprocal rank fusion, where the store supports it.
    hybrid: bool = True
    # PostgreSQL text search configuration for hybrid search: "simple" works
    # for any language; "spanish", "english"... add stemming.
    text_search_config: str = "simple"
    embed_batch_size: int = Field(default=64, ge=1)

    # HNSW (pgvector). Defaults are pgvector's; ef_search trades speed for recall.
    hnsw_m: int = 16
    hnsw_ef_construction: int = 64
    hnsw_ef_search: int = 100

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "nomic-embed-text"

    # Off by default: modules should call the `rag` service with the tenant
    # they already resolved. When on, the router needs the auth plugin.
    mount_router: bool = False
    prefix: str = "/rag"
    # Scopes the router demands, empty = any signed-in caller.
    read_scopes: list[str] = Field(default_factory=list)
    write_scopes: list[str] = Field(default_factory=list)
    # Creates or upgrades the table/collection on startup. Convenient in
    # development; in production run jfastframework.vectors.pgvector.schema_sql
    # from a migration and turn this off.
    auto_migrate: bool = True


class IngestRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=300)
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int | None = Field(default=None, ge=1, le=100)
    document_ids: list[str] | None = None
    where: dict[str, Any] | None = None
    min_score: float | None = Field(default=None, ge=0, le=1)
    hybrid: bool | None = None


def request_tenant(request: Request) -> str | None:
    """The tenant this request is for: the tenancy plugin's answer, else the token's.

    Never a header or a body field -- those are whatever the caller typed.
    """
    tenant = getattr(request.state, "tenant_id", None)
    if tenant:
        return str(tenant)
    principal = getattr(request.state, "principal", None)
    return getattr(principal, "tenant_id", None) or None


class RagPlugin(Plugin):
    meta = PluginMeta(
        name="rag",
        version="0.3.0",
        description="Tenant-scoped semantic and hybrid search over pgvector or Qdrant.",
        # Not `requires`: which backend this needs depends on configuration, so
        # the check lives in register() where the config is known. `after`
        # still guarantees those plugins start first when enabled.
        after=("observability", "database", "qdrant", "cache", "llm", "auth", "tenancy"),
        provides=("rag", "rag.store", "rag.embedder"),
        default_enabled=False,
        extra="jfastframework[rag]",
    )
    Settings = RagSettings

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._store: VectorStore | None = None
        self._embedder: Embedder | None = None
        self._service: RagService | None = None
        self._setup_error: str | None = None

    # -- construction --------------------------------------------------

    def _build_embedder(self, ctx: AppContext) -> Embedder:
        settings: RagSettings = self.settings
        if settings.embedder == "ollama":
            return OllamaEmbedder(
                base_url=settings.ollama_url,
                model=settings.ollama_model,
                dimensions=settings.dimensions,
            )
        if settings.embedder == "llm":
            if not ctx.has("llm"):
                raise PluginError(
                    'rag embedder "llm" needs the llm plugin. Add "llm" to [plugins].enabled '
                    'before "rag", or choose another embedder.'
                )
            from jfastframework.llm import LLMClient, LLMEmbedder

            client = ctx.require("llm", LLMClient)
            if client.embedding_dimensions and client.embedding_dimensions != settings.dimensions:
                raise PluginError(
                    f"[plugin.llm] embedding_dimensions = {client.embedding_dimensions} but "
                    f"[plugin.rag] dimensions = {settings.dimensions}; they must match."
                )
            return LLMEmbedder(client, dimensions=settings.dimensions)
        embedder: Embedder = _load_class(settings.embedder, "embedder")()
        return embedder

    def _build_store(self, ctx: AppContext) -> VectorStore:
        settings: RagSettings = self.settings

        if settings.store == "pgvector":
            if not ctx.has("db.engine"):
                raise PluginError(
                    "rag store 'pgvector' needs the 'database' plugin. "
                    'Add "database" to [plugins].enabled, or set '
                    '[plugin.rag] store = "qdrant".'
                )
            from jfastframework.vectors.pgvector import PgVectorStore

            try:
                return PgVectorStore(
                    ctx.require("db.engine"),
                    table=settings.collection,
                    dimensions=settings.dimensions,
                    tenant_scoped=settings.tenant_scoped,
                    text_search_config=settings.text_search_config,
                    hnsw_m=settings.hnsw_m,
                    hnsw_ef_construction=settings.hnsw_ef_construction,
                    hnsw_ef_search=settings.hnsw_ef_search,
                )
            except ValueError as exc:
                raise PluginError(f"[plugin.rag] {exc}") from exc

        if settings.store == "qdrant":
            if not ctx.has("qdrant.client"):
                raise PluginError(
                    "rag store 'qdrant' needs the 'qdrant' plugin. "
                    'Add "qdrant" to [plugins].enabled.'
                )
            from jfastframework.vectors.qdrant import QdrantStore

            return QdrantStore(
                ctx.require("qdrant.client"),
                collection=settings.collection,
                dimensions=settings.dimensions,
                tenant_scoped=settings.tenant_scoped,
            )

        store: VectorStore = _load_class(settings.store, "vector store")(ctx)
        return store

    # -- lifecycle -----------------------------------------------------

    def register(self, ctx: AppContext) -> None:
        settings: RagSettings = self.settings
        if settings.chunk_overlap >= settings.chunk_size:
            raise PluginError("[plugin.rag] chunk_overlap must be smaller than chunk_size.")
        if settings.chunk_strategy not in ("recursive", "fixed"):
            raise PluginError('[plugin.rag] chunk_strategy must be "recursive" or "fixed".')

        self._embedder = self._build_embedder(ctx)
        self._store = self._build_store(ctx)
        if getattr(self._embedder, "dimensions", settings.dimensions) != settings.dimensions:
            raise PluginError(
                f"the embedder produces {self._embedder.dimensions} dimensions but "
                f"[plugin.rag] dimensions = {settings.dimensions}."
            )
        self._service = RagService(
            self._store,
            self._embedder,
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            chunk_strategy=settings.chunk_strategy,
            top_k=settings.top_k,
            hybrid=settings.hybrid,
            min_score=settings.min_score,
            embed_batch_size=settings.embed_batch_size,
        )

        ctx.provide("rag", self._service)
        ctx.provide("rag.store", self._store)
        ctx.provide("rag.embedder", self._embedder)

        if not settings.tenant_scoped:
            ctx.logger.info("rag is not tenant-scoped: every search covers every document")

        if settings.mount_router:
            if not ctx.has("auth"):
                raise PluginError(
                    "[plugin.rag] mount_router = true needs the auth plugin: the router must know "
                    'who is calling. Enable "auth", or leave the router off and call the `rag` '
                    "service from your own routes."
                )
            ctx.app.include_router(self._build_router(), prefix=settings.prefix, tags=["rag"])

    async def startup(self, ctx: AppContext) -> None:
        # Same reasoning as the queue plugin: a store that is not up yet
        # should make the service unready, not make it crash-loop.
        if self.settings.auto_migrate and self._store is not None:
            try:
                await self._store.ensure_schema()
            except Exception as exc:  # noqa: BLE001 - reported through /ready
                self._setup_error = str(exc)
                ctx.logger.error(
                    "rag schema setup failed; the service is serving but not ready",
                    extra={"store": self.settings.store, "error": str(exc)},
                )
            else:
                self._setup_error = None

    async def health(self, ctx: AppContext) -> HealthReport:
        if self._store is None:
            return HealthReport.fail("rag store not initialised")
        if self._setup_error is not None:
            return HealthReport.fail(f"rag schema setup failed: {self._setup_error}")
        healthy, detail = await self._store.health()
        meta = {
            "store": self.settings.store,
            "embedder": self.settings.embedder,
            "tenant_scoped": self.settings.tenant_scoped,
        }
        return HealthReport.ok(detail, **meta) if healthy else HealthReport.fail(detail, **meta)

    # -- http ----------------------------------------------------------

    def _build_router(self) -> APIRouter:
        from jfastframework.plugins.builtin.auth import require_scopes

        router = APIRouter()
        settings: RagSettings = self.settings
        reader = Depends(require_scopes(*settings.read_scopes))
        writer = Depends(require_scopes(*settings.write_scopes))

        def tenant_of(request: Request) -> str | None:
            from jfastframework.plugins.builtin.tenancy import raise_if_denied

            # A tenant the request named and was refused is refused here too,
            # rather than falling back to the token's own tenant.
            raise_if_denied(request)
            tenant = request_tenant(request)
            if settings.tenant_scoped and not tenant:
                raise TenantRequiredError("this request is not scoped to a tenant")
            return tenant

        @router.post("/documents", summary="Ingest or replace a document", dependencies=[writer])
        async def ingest(payload: IngestRequest, request: Request) -> dict[str, Any]:
            result = await self._require_service().ingest(
                payload.document_id,
                payload.content,
                tenant_id=tenant_of(request),
                metadata=payload.metadata,
            )
            return {
                "document_id": result.document_id,
                "chunks": result.chunks,
                "embedded": result.embedded,
                "reused": result.reused,
            }

        @router.post("/search", summary="Semantic or hybrid search", dependencies=[reader])
        async def search(payload: SearchRequest, request: Request) -> dict[str, Any]:
            hits = await self._require_service().search(
                payload.query,
                tenant_id=tenant_of(request),
                limit=payload.limit,
                document_ids=payload.document_ids,
                where=payload.where,
                min_score=payload.min_score,
                hybrid=payload.hybrid,
            )
            return {"query": payload.query, "results": [hit.as_dict() for hit in hits]}

        @router.delete(
            "/documents/{document_id}", summary="Delete a document", dependencies=[writer]
        )
        async def delete(document_id: str, request: Request) -> dict[str, str]:
            await self._require_service().delete(document_id, tenant_id=tenant_of(request))
            return {"status": "deleted", "document_id": document_id}

        return router

    def _require_service(self) -> RagService:
        if self._service is None:
            raise ServiceUnavailableError("rag plugin is not initialised")
        return self._service


__all__ = ["OllamaEmbedder", "RagPlugin", "RagSettings", "chunk_text", "request_tenant"]
