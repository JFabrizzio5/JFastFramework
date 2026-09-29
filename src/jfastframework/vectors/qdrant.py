"""Qdrant-backed vector store.

Pick this over pgvector when you need more scale than one PostgreSQL instance
should carry, quantisation, or a vector workload isolated from the
transactional one. The cost is a second datastore to run, back up and monitor
-- do not take it until pgvector actually stops being enough.

Point ids are derived deterministically from ``(tenant_id, document_id,
chunk_index)``, so re-ingesting a document overwrites rather than duplicates,
and two tenants' documents with the same id never share a point. The ids
changed in 0.1.0a10 (the tenant is now part of them): a collection written by
0.1.0a9 must be re-ingested.

No hybrid search: Qdrant does it with sparse vectors, which need a sparse
embedder this store does not have. ``supports_hybrid`` is False and the
``rag`` plugin falls back to vector search, saying so once in the log.

Requires: ``pip install jfastframework[qdrant]``
"""

from __future__ import annotations

import uuid
from typing import Any

from jfastframework.vectors.base import Chunk, SearchHit, content_hash, require_tenant

# Stable namespace for deriving point ids. Changing it orphans every existing
# point, so treat it as frozen.
_POINT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def point_id(document_id: str, chunk_index: int, tenant_id: str | None = None) -> str:
    # The \x1f separator keeps ("a:b", 1) and ("a", "b:1") from colliding.
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{tenant_id or ''}\x1f{document_id}\x1f{chunk_index}"))


class QdrantStore:
    supports_hybrid = False

    def __init__(
        self,
        client: Any,
        *,
        collection: str,
        dimensions: int,
        distance: str = "Cosine",
        tenant_scoped: bool = True,
    ) -> None:
        self._client = client
        self._collection = collection
        self._dimensions = dimensions
        self._distance = distance
        self.tenant_scoped = tenant_scoped

    async def ensure_schema(self) -> None:
        from qdrant_client import models

        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=self._dimensions,
                    distance=models.Distance[self._distance.upper()],
                ),
            )
        # Payload indexes: without them a filtered search degrades to a full
        # scan once the collection grows. `is_tenant` lets Qdrant co-locate a
        # tenant's points, which is what keeps per-tenant search fast.
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="tenant_id",
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            ),
            wait=True,
        )
        await self._client.create_payload_index(
            collection_name=self._collection,
            field_name="document_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )

    # -- filters ---------------------------------------------------------

    def _filter(
        self,
        tenant_id: str | None,
        *,
        document_ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        from_index: int | None = None,
    ) -> Any:
        from qdrant_client import models

        must: list[Any] = []
        if tenant_id is not None or self.tenant_scoped:
            must.append(
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=tenant_id or "")
                )
            )
        if document_ids:
            must.append(
                models.FieldCondition(
                    key="document_id", match=models.MatchAny(any=list(document_ids))
                )
            )
        for key, value in (where or {}).items():
            must.append(
                models.FieldCondition(key=f"metadata.{key}", match=models.MatchValue(value=value))
            )
        if from_index is not None:
            must.append(
                models.FieldCondition(key="chunk_index", range=models.Range(gte=from_index))
            )
        return models.Filter(must=must) if must else None

    # -- writes ----------------------------------------------------------

    async def existing_hashes(self, document_id: str, *, tenant_id: str | None) -> dict[int, str]:
        require_tenant(tenant_id, scoped=self.tenant_scoped, action="read")
        found: dict[int, str] = {}
        offset = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=self._collection,
                scroll_filter=self._filter(tenant_id, document_ids=[document_id]),
                limit=256,
                offset=offset,
                with_payload=["chunk_index", "content_hash"],
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                if payload.get("content_hash"):
                    found[int(payload["chunk_index"])] = str(payload["content_hash"])
            if offset is None:
                return found

    async def sync_document(
        self,
        document_id: str,
        *,
        tenant_id: str | None,
        chunks: list[Chunk],
        embeddings: dict[int, list[float]],
    ) -> int:
        from qdrant_client import models

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="write")
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=self._filter(tenant_id, document_ids=[document_id], from_index=len(chunks))
            ),
            wait=True,
        )
        points = [
            models.PointStruct(
                id=point_id(document_id, c.chunk_index, tenant_id),
                vector=embeddings[c.chunk_index],
                payload=self._payload(c, tenant_id),
            )
            for c in chunks
            if c.chunk_index in embeddings
        ]
        if points:
            await self._client.upsert(collection_name=self._collection, points=points, wait=True)
        for c in chunks:
            if c.chunk_index not in embeddings:
                await self._client.set_payload(
                    collection_name=self._collection,
                    payload=self._payload(c, tenant_id),
                    points=[point_id(document_id, c.chunk_index, tenant_id)],
                    wait=True,
                )
        return len(chunks)

    @staticmethod
    def _payload(chunk: Chunk, tenant_id: str | None) -> dict[str, Any]:
        return {
            "document_id": chunk.document_id,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "content_hash": chunk.content_hash,
            # "" rather than None: a keyword index does not match null.
            "tenant_id": tenant_id or "",
            "metadata": chunk.metadata,
        }

    async def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
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
        from qdrant_client import models

        require_tenant(tenant_id, scoped=self.tenant_scoped, action="delete")
        await self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(
                filter=self._filter(tenant_id, document_ids=[document_id])
            ),
            wait=True,
        )

    # -- reads -----------------------------------------------------------

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
        require_tenant(tenant_id, scoped=self.tenant_scoped, action="search")
        response = await self._client.query_points(
            collection_name=self._collection,
            query=embedding,
            limit=limit,
            query_filter=self._filter(tenant_id, document_ids=document_ids, where=where),
            score_threshold=min_score,
            with_payload=True,
        )
        hits = []
        for point in response.points:
            payload = point.payload or {}
            hits.append(
                SearchHit(
                    document_id=str(payload.get("document_id", "")),
                    chunk_index=int(payload.get("chunk_index", 0)),
                    content=str(payload.get("content", "")),
                    # Qdrant's cosine score is already a similarity.
                    score=max(0.0, min(1.0, float(point.score))),
                    metadata=dict(payload.get("metadata") or {}),
                )
            )
        return hits

    async def health(self) -> tuple[bool, str]:
        try:
            exists = await self._client.collection_exists(self._collection)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return False, f"qdrant unreachable: {exc}"
        if not exists:
            return False, f"qdrant collection {self._collection!r} does not exist"
        return True, f"qdrant collection {self._collection} reachable"
