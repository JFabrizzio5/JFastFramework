"""The vector store is a configuration choice, and a wrong one fails loudly."""

from __future__ import annotations

import pytest

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.rag import RagPlugin, chunk_text
from jfastframework.testing import build_test_app
from jfastframework.vectors.qdrant import point_id


def _context(providers: dict[str, object]):
    app = build_test_app()
    ctx = app.state.jfast
    for key, value in providers.items():
        ctx.provide(key, value)
    return ctx


def test_pgvector_without_the_database_plugin_names_the_fix() -> None:
    plugin = RagPlugin({"store": "pgvector"})
    with pytest.raises(PluginError, match="needs the 'database' plugin"):
        plugin.register(_context({}))


def test_qdrant_without_the_qdrant_plugin_names_the_fix() -> None:
    plugin = RagPlugin({"store": "qdrant"})
    with pytest.raises(PluginError, match="needs the 'qdrant' plugin"):
        plugin.register(_context({}))


def test_pgvector_is_selected_when_the_engine_is_present() -> None:
    plugin = RagPlugin({"store": "pgvector", "mount_router": False})
    plugin.register(_context({"db.engine": object()}))
    from jfastframework.vectors.pgvector import PgVectorStore

    assert isinstance(plugin._store, PgVectorStore)


def test_qdrant_is_selected_when_the_client_is_present() -> None:
    plugin = RagPlugin({"store": "qdrant", "mount_router": False})
    plugin.register(_context({"qdrant.client": object()}))
    from jfastframework.vectors.qdrant import QdrantStore

    assert isinstance(plugin._store, QdrantStore)


def test_an_unknown_store_path_is_rejected() -> None:
    plugin = RagPlugin({"store": "not-a-path"})
    with pytest.raises(PluginError, match="Invalid vector store"):
        plugin.register(_context({}))


def test_qdrant_point_ids_are_deterministic() -> None:
    # Re-ingesting a document must overwrite its points, not duplicate them.
    assert point_id("doc-1", 0, "acme") == point_id("doc-1", 0, "acme")
    # ...and two tenants' documents with the same id are different points.
    assert point_id("doc-1", 0, "acme") != point_id("doc-1", 0, "globex")
    assert point_id("doc-1", 0) != point_id("doc-1", 1)
    assert point_id("doc-1", 0) != point_id("doc-2", 0)


def test_chunking_overlaps_without_emitting_a_redundant_tail() -> None:
    # Striding past the end would append "j", already inside "ghij".
    assert chunk_text("abcdefghij", size=4, overlap=1, strategy="fixed") == ["abcd", "defg", "ghij"]


def test_chunking_covers_text_that_does_not_divide_evenly() -> None:
    chunks = chunk_text("abcdefgh", size=5, overlap=2, strategy="fixed")
    assert chunks == ["abcde", "defgh"]
    assert "".join(dict.fromkeys("".join(chunks))) == "abcdefgh"


def test_text_shorter_than_one_chunk_is_a_single_chunk() -> None:
    assert chunk_text("short", size=100, overlap=10) == ["short"]


def test_blank_text_produces_no_chunks() -> None:
    assert chunk_text("   \n  ", size=4, overlap=1) == []


def test_chunking_rejects_an_overlap_that_would_not_advance() -> None:
    with pytest.raises(ValueError, match="overlap must be smaller"):
        chunk_text("abc", size=2, overlap=2)
