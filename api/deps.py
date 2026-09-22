"""Dependências compartilhadas pelos routers."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from api import repository
from api.agent_runtime import agent_runtime
from api.db import get_db
from api.models import Conversation


def get_graph() -> CompiledStateGraph:
    """O grafo compilado no startup. Um só para toda a aplicação."""
    return agent_runtime.graph


async def get_conversation_or_404(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    conversation = await repository.get_conversation(db, session_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversa {session_id} não encontrada.",
        )
    return conversation
