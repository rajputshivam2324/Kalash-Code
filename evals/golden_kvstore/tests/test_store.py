"""Tests for KVStore."""
from __future__ import annotations

import time
from pathlib import Path
from kvstore.store import KVStore


def test_set_and_get(tmp_path: Path) -> None:
    store = KVStore(tmp_path / "store.json")
    store.set("greeting", "hello world")
    assert store.get("greeting") == "hello world"
    assert store.get("nonexistent") is None


def test_ttl_expiration(tmp_path: Path) -> None:
    store = KVStore(tmp_path / "store.json")
    store.set("temp", "expires soon", ttl_seconds=0.1)
    assert store.get("temp") == "expires soon"
    time.sleep(0.15)
    assert store.get("temp") is None


def test_delete(tmp_path: Path) -> None:
    store = KVStore(tmp_path / "store.json")
    store.set("item", 42)
    assert store.delete("item") is True
    assert store.delete("item") is False
    assert store.get("item") is None


def test_persistence(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    s1 = KVStore(path)
    s1.set("user", {"name": "Shivam", "role": "admin"})
    s2 = KVStore(path)
    assert s2.get("user") == {"name": "Shivam", "role": "admin"}


def test_list_keys_and_clear(tmp_path: Path) -> None:
    store = KVStore(tmp_path / "store.json")
    store.set("a", 1)
    store.set("b", 2)
    assert store.list_keys() == ["a", "b"]
    store.clear()
    assert store.list_keys() == []
