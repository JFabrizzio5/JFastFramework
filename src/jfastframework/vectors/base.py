"""The vector store contract.

The ``rag`` plugin talks to this protocol rather than to pgvector, so the
backing store is a configuration choice.

    [plugin.rag]
    store = "qdrant"        # or "pgvector", or "mypkg.stores:MyStore"

Adding a store means implementing four methods. It does not mean touching the
plugin, the router, or anything that consumes search results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Chunk:
    """One indexed piece of a document."""

    document_id: str
    chunk_index: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None


@dataclass
class SearchHit:
    """One retrieval result.

    ``score`` is cosine similarity in [0, 1], higher is better -- normalised by
    each store so callers do not have to know whether the backend returned a
    distance or a similarity.
    """

    document_id: str
    chunk_index: int
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "score": self.score,
            "metadata": self.metadata,
        }


@runtime_checkable
class VectorStore(Protocol):
    """What the ``rag`` plugin needs from a vector database."""

    async def ensure_schema(self) -> None:
        """Create the collection/table and indexes if they do not exist."""
        ...

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        """Replace every chunk of the documents represented in ``chunks``.

        Implementations must delete the document's existing chunks first, so
        re-ingesting a shortened document does not leave orphans behind.
        """
        ...

    async def search(
        self,
        embedding: list[float],
        *,
        limit: int = 5,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        """Nearest neighbours, most similar first."""
        ...

    async def delete_document(self, document_id: str) -> None: ...

    async def health(self) -> tuple[bool, str]:
        """``(healthy, detail)``. Called from the plugin's ``/ready`` check."""
        ...
