"""Tests for StorageEngine — sync writes, atexit, concurrency, and MCP manager."""

from __future__ import annotations

import asyncio
import threading
import tempfile
from pathlib import Path

import pytest

from kalash.storage.engine import StorageEngine


# ---------------------------------------------------------------------------
# StorageEngine unit tests
# ---------------------------------------------------------------------------


class TestStorageEngine:
    """Tests for the core storage engine."""

    @pytest.fixture
    def engine(self, tmp_path):
        eng = StorageEngine(db_path=tmp_path / "test.db")
        yield eng
        eng.close()

    def test_connection_created_on_first_access(self, engine):
        conn = engine._get_connection()
        assert conn is not None

    def test_wal_mode_enabled(self, engine):
        conn = engine._get_connection()
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"

    def test_foreign_keys_enabled(self, engine):
        conn = engine._get_connection()
        result = conn.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1

    def test_read_context_manager(self, engine):
        with engine.read() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test_read (id INTEGER PRIMARY KEY)")
            conn.execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_write_context_manager(self, engine):
        async with engine.write() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test_write (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO test_write VALUES (1)")

        with engine.read() as conn:
            row = conn.execute("SELECT id FROM test_write").fetchone()
            assert row[0] == 1

    @pytest.mark.asyncio
    async def test_write_rollback_on_error(self, engine):
        # Create table
        async with engine.write() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test_rollback (id INTEGER PRIMARY KEY)")

        # Fail mid-transaction
        with pytest.raises(ValueError):
            async with engine.write() as conn:
                conn.execute("INSERT INTO test_rollback VALUES (42)")
                raise ValueError("intentional")

        # Row should NOT exist due to rollback
        with engine.read() as conn:
            row = conn.execute("SELECT id FROM test_rollback WHERE id = 42").fetchone()
            assert row is None

    def test_execute_write_sync(self, engine):
        """Test the new synchronous write path (A-1 fix)."""
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_sync (id INTEGER, val TEXT)")

        engine.execute_write_sync(
            "INSERT INTO test_sync (id, val) VALUES (?, ?)", (1, "hello")
        )

        rows = engine.execute_read("SELECT val FROM test_sync WHERE id = 1")
        assert len(rows) == 1
        assert rows[0]["val"] == "hello"

    def test_execute_write_sync_rollback(self, engine):
        """Sync write should rollback on error."""
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_sync_rb (id INTEGER PRIMARY KEY)")

        engine.execute_write_sync("INSERT INTO test_sync_rb VALUES (?)", (1,))

        with pytest.raises(Exception):
            # Duplicate primary key should fail
            engine.execute_write_sync("INSERT INTO test_sync_rb VALUES (?)", (1,))

        # Original row still present
        rows = engine.execute_read("SELECT id FROM test_sync_rb")
        assert len(rows) == 1

    def test_execute_read(self, engine):
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_er (id INTEGER)")
        conn.execute("INSERT INTO test_er VALUES (99)")
        rows = engine.execute_read("SELECT id FROM test_er")
        assert rows[0]["id"] == 99

    @pytest.mark.asyncio
    async def test_execute_read_async(self, engine):
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_era (id INTEGER)")
        conn.execute("INSERT INTO test_era VALUES (77)")
        rows = await engine.execute_read_async("SELECT id FROM test_era")
        assert rows[0]["id"] == 77

    @pytest.mark.asyncio
    async def test_execute_write(self, engine):
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_ew (id INTEGER)")
        await engine.execute_write("INSERT INTO test_ew VALUES (?)", (42,))
        rows = engine.execute_read("SELECT id FROM test_ew")
        assert rows[0]["id"] == 42

    @pytest.mark.asyncio
    async def test_execute_many(self, engine):
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_em (id INTEGER)")
        await engine.execute_many(
            "INSERT INTO test_em VALUES (?)", [(1,), (2,), (3,)]
        )
        rows = engine.execute_read("SELECT id FROM test_em ORDER BY id")
        assert [r["id"] for r in rows] == [1, 2, 3]

    def test_close_and_reopen(self, engine):
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_close (id INTEGER)")
        conn.execute("INSERT INTO test_close VALUES (1)")

        engine.close()
        assert engine._conn is None

        # Reopen
        rows = engine.execute_read("SELECT id FROM test_close")
        assert rows[0]["id"] == 1

    def test_concurrent_sync_writes(self, engine):
        """Multiple threads writing via execute_write_sync should not corrupt."""
        conn = engine._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS test_conc (id INTEGER)")

        errors = []

        def writer(start):
            try:
                for i in range(start, start + 20):
                    engine.execute_write_sync(
                        "INSERT INTO test_conc VALUES (?)", (i,)
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i * 100,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Write errors: {errors}"
        rows = engine.execute_read("SELECT COUNT(*) as cnt FROM test_conc")
        assert rows[0]["cnt"] == 80


# ---------------------------------------------------------------------------
# MCP Manager tests
# ---------------------------------------------------------------------------


class TestMCPManagerBasic:
    """Basic MCP manager structure tests (no real servers)."""

    def test_import(self):
        from kalash.mcp.manager import MCPManager, ServerState
        assert MCPManager is not None
        assert ServerState.CONFIGURED == "CONFIGURED"

    def test_initial_state_empty(self):
        from kalash.mcp.manager import MCPManager
        mgr = MCPManager()
        assert mgr.servers == {}
        assert mgr.all_tools == {}

    def test_get_status_nonexistent(self):
        from kalash.mcp.manager import MCPManager
        mgr = MCPManager()
        assert mgr.get_status("nonexistent") is None

    def test_resolve_server_unknown_tool(self):
        from kalash.mcp.manager import MCPManager
        mgr = MCPManager()
        assert mgr.resolve_server("mcp__x__y") is None

    @pytest.mark.asyncio
    async def test_call_tool_unknown_raises(self):
        from kalash.mcp.manager import MCPManager
        from kalash.core.errors import KalashError
        mgr = MCPManager()
        with pytest.raises(KalashError, match="not found"):
            await mgr.call_tool("mcp__fake__tool", {})

    @pytest.mark.asyncio
    async def test_disconnect_all_when_empty(self):
        from kalash.mcp.manager import MCPManager
        mgr = MCPManager()
        await mgr.disconnect_all()  # should not raise


# ---------------------------------------------------------------------------
# Auth store tests
# ---------------------------------------------------------------------------


class TestAuthStoreCache:
    """Tests for credential caching (P-1 fix)."""

    def test_load_empty_returns_empty(self, tmp_path, monkeypatch):
        from kalash.tui import auth_store
        monkeypatch.setattr(auth_store, "_auth_path", lambda: tmp_path / "auth.json")
        # Reset cache
        auth_store._auth_cache = None
        auth_store._auth_cache_mtime = 0.0

        result = auth_store.load_auth()
        assert result == {}

    def test_cache_invalidation_on_save(self, tmp_path, monkeypatch):
        from kalash.tui import auth_store
        monkeypatch.setattr(auth_store, "_auth_path", lambda: tmp_path / "auth.json")
        auth_store._auth_cache = {"old": "data"}
        auth_store._auth_cache_mtime = 12345.0

        auth_store._invalidate_auth_cache()
        assert auth_store._auth_cache is None
        assert auth_store._auth_cache_mtime == 0.0
