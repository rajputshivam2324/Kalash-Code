"""Tests for the storage layer."""

import pytest

from kalash.storage.blobs import blob_exists, compute_digest, read_blob, store_blob
from kalash.storage.engine import StorageEngine
from kalash.storage.repositories.sessions import SessionRepository

# --- Blobs ---


class TestBlobs:
    def test_compute_digest(self):
        data = b"hello world"
        digest = compute_digest(data)
        assert len(digest) == 64  # SHA-256 hex = 64 chars
        # Same input = same digest
        assert compute_digest(data) == digest

    def test_store_and_read(self, tmp_path, monkeypatch):
        # Point blobs to tmp dir
        monkeypatch.setattr("kalash.storage.blobs.blobs_dir", lambda: tmp_path)

        data = b"test blob content"
        digest = store_blob(data)
        assert blob_exists(digest)

        retrieved = read_blob(digest)
        assert retrieved == data

    def test_read_verifies_integrity(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kalash.storage.blobs.blobs_dir", lambda: tmp_path)

        data = b"original"
        digest = store_blob(data)

        # Corrupt the blob
        from kalash.storage.blobs import blob_path

        path = blob_path(digest)
        path.write_bytes(b"corrupted")

        with pytest.raises(ValueError, match="integrity"):
            read_blob(digest)

    def test_deduplication(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kalash.storage.blobs.blobs_dir", lambda: tmp_path)

        data = b"same content"
        d1 = store_blob(data)
        d2 = store_blob(data)
        assert d1 == d2  # Content-addressed = same digest


# --- Storage Engine ---


class TestStorageEngine:
    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, tmp_path):
        db_path = tmp_path / "test.db"
        engine = StorageEngine(db_path)
        await engine.initialize()

        # Check that sessions table exists
        rows = engine.execute_read(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        assert len(rows) == 1

        engine.close()

    @pytest.mark.asyncio
    async def test_write_serialization(self, tmp_path):
        db_path = tmp_path / "test.db"
        engine = StorageEngine(db_path)
        await engine.initialize()

        # Write should work
        await engine.execute_write(
            "INSERT INTO sessions (id, project_dir, agent, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ses_test",
                "/tmp",
                "default",
                "CREATED",
                "2024-01-01T00:00:00Z",
                "2024-01-01T00:00:00Z",
            ),
        )

        # Read it back
        rows = engine.execute_read("SELECT * FROM sessions WHERE id = ?", ("ses_test",))
        assert len(rows) == 1
        assert rows[0]["project_dir"] == "/tmp"

        engine.close()


# --- Session Repository ---


class TestSessionRepository:
    @pytest.mark.asyncio
    async def test_create_session(self, tmp_path):
        db_path = tmp_path / "test.db"
        engine = StorageEngine(db_path)
        await engine.initialize()
        repo = SessionRepository(engine)

        session_id = await repo.create_session(project_dir="/home/user/project")
        assert session_id.startswith("ses_")

        session = await repo.get_session(session_id)
        assert session is not None
        assert session["project_dir"] == "/home/user/project"
        assert session["state"] == "CREATED"

        engine.close()

    @pytest.mark.asyncio
    async def test_create_turn_and_message(self, tmp_path):
        db_path = tmp_path / "test.db"
        engine = StorageEngine(db_path)
        await engine.initialize()
        repo = SessionRepository(engine)

        session_id = await repo.create_session(project_dir="/tmp")
        turn_id = await repo.create_turn(session_id, seq=1, role="user")
        await repo.add_message(turn_id, seq=1, role="user", content="Hello")

        messages = await repo.get_messages(turn_id)
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello"

        engine.close()
