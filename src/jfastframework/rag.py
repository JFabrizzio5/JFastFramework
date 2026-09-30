"""Retrieval for your modules: ingest documents, search them, cite them.

The ``rag`` plugin builds one :class:`RagService` and provides it as ``rag``::

    rag = get_context(request.app).require("rag")

    await rag.ingest("contract-42", text, tenant_id=tenant, metadata={"kind": "contract"})
    hits = await rag.search("late delivery penalty", tenant_id=tenant, where={"kind": "contract"})
    prompt_context = format_context(hits, max_chars=6000)

What it takes care of, so a module does not have to:

* **Chunking that follows the text.** Paragraphs first, then lines, then
  sentences, then words; a chunk ends at the largest boundary that fits.
  Fixed-width windows cut clauses and tables in half, and a half clause
  retrieves badly.
* **Re-ingesting costs only what changed.** Every chunk carries a hash of its
  text and the embedder that produced its vector; an edited contract
  re-embeds the paragraphs that changed, not the whole document. With a paid
  embedder that is most of the bill.
* **The tenant is part of every call.** ``tenant_id`` is keyword-only and
  required on a tenant-scoped store; there is no way to search "everyone" by
  forgetting an argument.
* **Hybrid search when the store has it**, falling back to vector search with
  one log line when it does not.

No FastAPI in this module: a queue worker ingests exactly as a route does.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from jfastframework import tracing
from jfastframework.vectors.base import Chunk, SearchHit, VectorStore, content_hash

logger = logging.getLogger("jfast.rag")

#: Tried in order; the first that splits a too-long piece wins. Markdown
#: headings come before paragraphs so a section is not glued to the next one.
DEFAULT_SEPARATORS: tuple[str, ...] = (
    "\n# ",
    "\n## ",
    "\n### ",
    "\n\n",
    "\n",
    ". ",
    "; ",
    ", ",
    " ",
    "",
)


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into vectors.

    ``model_id`` is optional but worth setting: it goes into each chunk's hash,
    so changing models re-embeds instead of mixing two vector spaces. An
    ``embed`` that accepts a ``tenant_id`` keyword gets it, which is how a
    budgeted embedder charges the right tenant.
    """

    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def chunk_text(
    text: str,
    *,
    size: int,
    overlap: int,
    strategy: str = "recursive",
    separators: Sequence[str] = DEFAULT_SEPARATORS,
) -> list[str]:
    """Split ``text`` into pieces of at most ``size`` characters.

    ``recursive`` (the default) splits at the largest structural boundary that
    makes pieces fit, then packs neighbouring pieces back together up to
    ``size``, carrying up to ``overlap`` characters of the previous chunk --
    whole pieces only, never a word cut in half. ``fixed`` is the 0.1.0a9
    behaviour: character windows with overlap, for text with no structure.
    """
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be smaller than the chunk size, and not negative")
    if strategy == "fixed":
        return _fixed(text, size=size, overlap=overlap)
    if strategy != "recursive":
        raise ValueError(f"unknown chunking strategy {strategy!r}; use 'recursive' or 'fixed'")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    pieces = _split(text, size, list(separators))
    return _pack(pieces, size, overlap)


def _fixed(text: str, *, size: int, overlap: int) -> list[str]:
    stride = size - overlap
    chunks: list[str] = []
    start = 0
    while start < len(text):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(piece)
        # Stop once the window reaches the end: striding past it would emit a
        # tail already contained in the previous chunk.
        if start + size >= len(text):
            break
        start += stride
    return chunks


def _split(text: str, size: int, separators: list[str]) -> list[str]:
    """Pieces no longer than ``size``, each keeping its separator."""
    if len(text) <= size:
        return [text]
    for index, separator in enumerate(separators):
        if separator == "":
            return [text[i : i + size] for i in range(0, len(text), size)]
        if separator not in text:
            continue
        parts = text.split(separator)
        # Re-attach the separator to the piece it introduces (headings,
        # "\n") or ends (". ", ", "), so packing back reproduces the text.
        leading = separator.startswith("\n")
        rebuilt = [
            (
                separator + part
                if leading and i
                else part + separator
                if not leading and i < len(parts) - 1
                else part
            )
            for i, part in enumerate(parts)
        ]
        out: list[str] = []
        for part in rebuilt:
            if not part:
                continue
            out.extend([part] if len(part) <= size else _split(part, size, separators[index + 1 :]))
        return out
    return [text]


def _pack(pieces: list[str], size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for piece in pieces:
        # A heading opens a chunk and carries nothing over: the section below
        # it is what it describes, and a heading stranded at the end of the
        # previous chunk retrieves as noise there and is missing here.
        if current and piece.lstrip("\n").startswith("#"):
            chunks.append("".join(current).strip())
            current, length = [], 0
        if current and length + len(piece) > size:
            chunks.append("".join(current).strip())
            # Carry whole trailing pieces while they fit in the overlap and
            # leave room for the next piece.
            carried: list[str] = []
            carried_len = 0
            for previous in reversed(current):
                if (
                    carried_len + len(previous) > overlap
                    or carried_len + len(previous) + len(piece) > size
                ):
                    break
                carried.insert(0, previous)
                carried_len += len(previous)
            current, length = carried, carried_len
        current.append(piece)
        length += len(piece)
    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if c]


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    chunks: int
    embedded: int
    reused: int


def format_context(
    hits: Sequence[SearchHit], *, max_chars: int = 8000, title_key: str = "title"
) -> str:
    """Hits as numbered excerpts a model can cite as [1], [2]...

    Stops before ``max_chars`` rather than cutting an excerpt in half: a
    truncated excerpt reads to a model like the source said less than it did.
    """
    blocks: list[str] = []
    used = 0
    for number, hit in enumerate(hits, start=1):
        source = hit.metadata.get(title_key) or hit.document_id
        block = f"[{number}] {source} (part {hit.chunk_index + 1})\n{hit.content.strip()}"
        if blocks and used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)


class RagService:
    """Ingest, search and delete, scoped to a tenant on every call."""

    def __init__(
        self,
        store: VectorStore,
        embedder: Embedder,
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
        chunk_strategy: str = "recursive",
        top_k: int = 5,
        hybrid: bool = True,
        min_score: float | None = None,
        embed_batch_size: int = 64,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunk_strategy = chunk_strategy
        self.top_k = top_k
        self.hybrid = hybrid
        self.min_score = min_score
        self.embed_batch_size = embed_batch_size
        self._warned_hybrid = False
        # Decided once, not by catching TypeError per call: a TypeError raised
        # inside the embedder would otherwise be retried without the tenant.
        parameters = inspect.signature(embedder.embed).parameters.values()
        self._passes_tenant = any(
            p.name == "tenant_id" or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters
        )
        self._embedder_id = str(
            getattr(embedder, "model_id", None)
            or f"{type(embedder).__name__}:{embedder.dimensions}"
        )

    async def _embed(self, texts: list[str], tenant_id: str | None) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.embed_batch_size):
            batch = texts[start : start + self.embed_batch_size]
            if self._passes_tenant:
                vectors.extend(await self.embedder.embed(batch, tenant_id=tenant_id))  # type: ignore[call-arg]
            else:
                vectors.extend(await self.embedder.embed(batch))
        if len(vectors) != len(texts):
            raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors

    async def ingest(
        self,
        document_id: str,
        text: str,
        *,
        tenant_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        """Make the index hold exactly this version of the document.

        Idempotent: ingesting the same text twice embeds nothing the second
        time. An empty text removes the document.
        """
        # Counts only: the text, its chunks and the metadata values stay out of
        # the trace, the same as the llm ledger keeps prompts out.
        with tracing.span(
            "rag.ingest",
            **{
                "rag.document_id": document_id,
                "jfast.tenant_id": tenant_id,
                "rag.store": type(self.store).__name__,
            },
        ):
            result = await self._ingest(document_id, text, tenant_id=tenant_id, metadata=metadata)
            tracing.annotate(
                **{
                    "rag.chunks": result.chunks,
                    "rag.embedded": result.embedded,
                    "rag.reused": result.reused,
                }
            )
            return result

    async def _ingest(
        self,
        document_id: str,
        text: str,
        *,
        tenant_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> IngestResult:
        pieces = chunk_text(
            text, size=self.chunk_size, overlap=self.chunk_overlap, strategy=self.chunk_strategy
        )
        if not pieces:
            await self.store.delete_document(document_id, tenant_id=tenant_id)
            return IngestResult(document_id, 0, 0, 0)

        chunks = [
            Chunk(
                document_id=document_id,
                chunk_index=index,
                content=piece,
                metadata=dict(metadata or {}),
                tenant_id=tenant_id,
                content_hash=content_hash(piece, embedder_id=self._embedder_id),
            )
            for index, piece in enumerate(pieces)
        ]
        stored = await self.store.existing_hashes(document_id, tenant_id=tenant_id)
        stale = [c for c in chunks if stored.get(c.chunk_index) != c.content_hash]
        vectors = await self._embed([c.content for c in stale], tenant_id) if stale else []
        await self.store.sync_document(
            document_id,
            tenant_id=tenant_id,
            chunks=chunks,
            embeddings={c.chunk_index: v for c, v in zip(stale, vectors, strict=True)},
        )
        return IngestResult(document_id, len(chunks), len(stale), len(chunks) - len(stale))

    async def search(
        self,
        query: str,
        *,
        tenant_id: str | None,
        limit: int | None = None,
        document_ids: list[str] | None = None,
        where: dict[str, Any] | None = None,
        min_score: float | None = None,
        hybrid: bool | None = None,
    ) -> list[SearchHit]:
        use_hybrid = self.hybrid if hybrid is None else hybrid
        if use_hybrid and not getattr(self.store, "supports_hybrid", False):
            if not self._warned_hybrid:
                self._warned_hybrid = True
                logger.info(
                    "rag: %s has no hybrid search; using vector search", type(self.store).__name__
                )
            use_hybrid = False
        # Never the query: it is a user's question, often with their data in it.
        with tracing.span(
            "rag.search",
            **{
                "jfast.tenant_id": tenant_id,
                "rag.limit": limit or self.top_k,
                "rag.hybrid": use_hybrid,
                "rag.filtered": bool(where or document_ids),
                "rag.store": type(self.store).__name__,
            },
        ):
            vector = (await self._embed([query], tenant_id))[0]
            hits = await self.store.search(
                vector,
                tenant_id=tenant_id,
                limit=limit or self.top_k,
                document_ids=document_ids,
                where=where,
                min_score=self.min_score if min_score is None else min_score,
                text=query if use_hybrid else None,
            )
            tracing.annotate(**{"rag.hits": len(hits)})
            return hits

    async def delete(self, document_id: str, *, tenant_id: str | None) -> None:
        await self.store.delete_document(document_id, tenant_id=tenant_id)


__all__ = [
    "DEFAULT_SEPARATORS",
    "Embedder",
    "IngestResult",
    "RagService",
    "chunk_text",
    "format_context",
]
