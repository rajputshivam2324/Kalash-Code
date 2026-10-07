"""Atomic conversation recording. Persistence failure blocks subsequent tool effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kalash.core.privacy import scrub_blocks
from kalash.runtime.serialize import serialize_blocks

if TYPE_CHECKING:
    from kalash.core.budget import Usage
    from kalash.models.normalize import Message
    from kalash.storage.repositories.sessions import SessionRepository


@dataclass
class Transcript:
    repository: SessionRepository | None
    session_id: str

    async def append(
        self,
        message: Message,
        *,
        model: str | None = None,
        usage: Usage | None = None,
        source_id: str | None = None,
        run_id: str = "",
    ) -> None:
        if self.repository is not None:
            await self.repository.append_turn(
                session_id=self.session_id,
                role=message.role.value,
                content=serialize_blocks(scrub_blocks(message.content)),
                model=model,
                token_count=usage.total_tokens if usage else None,
                source_id=source_id,
                run_id=run_id,
            )
