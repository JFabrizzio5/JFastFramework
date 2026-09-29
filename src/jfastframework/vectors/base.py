"""The vector store contract.

The ``rag`` plugin talks to this protocol rather than to pgvector, so the
backing store is a configuration choice.

    [plugin.rag]
    store = "qdrant"        # or "pgvector", or "mypkg.stores:MyStore"

Three decisions every store must honour, because they are where retrieval
goes wrong without anyone noticing:

* **A chunk's identity is ``(tenant_id, document_id, chunk_index)``.** Two
  tenants may each have a document called ``contract-1``; before 0.1.0a10 the
  second one's ingest deleted the first one's chunks.
* **The tenant is required** when the store is ``tenant_scoped`` (the default).
  A search without one is refused with :class:`TenantRequiredError` instead of
  silently searching every tenant's documents.
* **Scores are cosine similarity in [0, 1]**, higher is better, whatever the
  backend returned, so ``min_score`` means the same thing on every store.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jfastframework.errors import ForbiddenError


class TenantRequiredError(ForbiddenError):
    """A tenant-scoped store was asked to read or write without a tenant.

    A 403 rather than a 500 when it reaches HTTP: the caller is not scoped to
    a tenant, which is a statement about the request, not a crash.
    """


def content_hash(content: str, *, embedder_id: str = "") -> str:
    """What decides whether a chunk must be embedded again.

    The embedder is part of the hash: the same text embedded by a different
    model is a different vector, and reusing the old one would mix two spaces
    in one index.
    """
    return hashlib.sha256(f"{embedder_id}\x00{content}".encode()).hexdigest()


@dataclass
class Chunk:
    """One indexed piece of a document."""

    document_id: str
    chunk_index: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.content_hash is None:
            self.content_hash = content_hash(self.content)


@dataclass
class SearchHit:
    """One retrieval result.

    ``score`` is cosine similarity in [0, 1] on every store and in every mode,
    so a ``min_score`` threshold means the same thing everywhere. In hybrid
    mode results are *ordered* by ``fused_score`` (reciprocal rank fusion of
    the vector and the full-text rankings), which is not a similarity and is
    not comparable across queries.
    """

    document_id: str
    chunk_index: int
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    fused_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "score": self.score,
            "fused_score": self.fused_score,
            "metadata": self.metadata,
        }


@runtime_checkable
class VectorStore(Protocol):
    """What the ``rag`` plugin needs from a vector database."""

    #: Whether ``search(..., text=...)`` can fuse full-text with vectors.
    supports_hybrid: bool

    async def ensure_schema(self) -> None:
        """Create or upgrade the table/collection and its indexes."""
        ...

    async def existing_hashes(self, document_id: str, *, tenant_id: str | None) -> dict[int, str]:
        """``{chunk_index: content_hash}`` of what is stored for a document.

        What lets a re-ingest embed only the chunks that changed.
        """
        ...

    async def sync_document(
        self,
        document_id: str,
        *,
        tenant_id: str | None,
        chunks: list[Chunk],
        embeddings: dict[int, list[float]],
    ) -> int:
        """Make the stored document exactly ``chunks``, atomically.

        ``embeddings`` holds a vector for every chunk that is new or whose
        hash changed; a chunk without one keeps its stored vector and only has
        its content hash and metadata refreshed. Chunks past ``len(chunks)``
        are deleted, so a shortened document leaves no orphans.
        """
        ...

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        """Replace whole documents. Kept for 0.1.0a9 callers; prefer ``sync_document``."""
        ...

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
        """Nearest neighbours, most similar first.

        ``where`` is an exact match on metadata keys. ``text`` asks for hybrid
        search and is ignored by a store with ``supports_hybrid = False``.
        """
        ...

    async def delete_document(self, document_id: str, *, tenant_id: str | None = None) -> None: ...

    async def health(self) -> tuple[bool, str]:
        """``(healthy, detail)``. Called from the plugin's ``/ready`` check."""
        ...


def require_tenant(tenant_id: str | None, *, scoped: bool, action: str) -> None:
    """The one check every store runs before touching data."""
    if scoped and not tenant_id:
        raise TenantRequiredError(
            f"vector store {action} without a tenant. Pass tenant_id, or set "
            "[plugin.rag] tenant_scoped = false if this service has a single tenant."
        )


__all__ = [
    "Chunk",
    "SearchHit",
    "TenantRequiredError",
    "VectorStore",
    "content_hash",
    "require_tenant",
]
