"""Tests for the memory service and turn capture pipeline."""

from __future__ import annotations

import pytest

from kalash.memory.capture import capture_turn_memories
from kalash.memory.pipeline.extract import ExtractionContext, run_extraction
from kalash.memory.protocol import RecallQuery, Source
from kalash.memory.service import get_memory_service, reset_memory_service
from kalash.memory.session import get_session_memory, project_scope
from kalash.models.normalize import Message, Role, TextBlock


@pytest.fixture
async def memory_service(tmp_path, monkeypatch):
    from kalash.core.config import KalashConfig, MemoryConfig, load_config

    await reset_memory_service()

    cfg = load_config()
    cfg = KalashConfig(
        project_dir=tmp_path,
        memory=MemoryConfig(
            enabled=True,
            primary="local",
            providers=["local"],
            provider_configs={"local": {"db_path": str(tmp_path / "mem.db")}},
        ),
        model=cfg.model,
        permissions=cfg.permissions,
        budget=cfg.budget,
        raw=cfg.raw,
    )

    monkeypatch.chdir(tmp_path)
    service = await get_memory_service(config=cfg)
    yield service
    await reset_memory_service()


@pytest.mark.asyncio
async def test_remember_and_recall_via_router(memory_service, tmp_path):
    session = get_session_memory("ses_mem", tmp_path)
    mem_id = await session.remember("always use pnpm in this repo", scope="project")
    assert mem_id.startswith("mem_")

    blocks = await session.recall_blocks("pnpm package manager", limit=5)
    assert blocks
    assert "pnpm" in blocks[0].lower()
    assert blocks[0].lstrip().startswith("<memory>")


@pytest.mark.asyncio
async def test_user_preference_extraction_and_capture(memory_service, tmp_path):
    scope = project_scope(tmp_path, session_id="ses_cap")
    writes = await run_extraction(
        "I prefer pnpm over npm for this project.",
        ExtractionContext(source=Source.USER_STATED, session_id="ses_cap"),
        scope,
    )
    assert writes
    assert any("pnpm" in w.content.lower() or "prefer" in w.content.lower() for w in writes)

    count = await capture_turn_memories(
        session_id="ses_cap",
        cwd=tmp_path,
        user_prompt="I prefer pnpm over npm for this project.",
        conversation=[
            Message(role=Role.USER, content=[TextBlock(text="setup deps")]),
        ],
        turn_seq=1,
    )
    assert count >= 1

    hits = await memory_service.router.recall(
        RecallQuery(text="pnpm", scope=scope, limit=5)
    )
    assert hits
