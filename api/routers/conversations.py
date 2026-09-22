"""CRUD das conversas — o que a sidebar do chat consome."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from api import repository
from api.agent_runtime import agent_runtime
from api.db import get_db
from api.deps import get_conversation_or_404
from api.models import Conversation
from api.schemas import ConversationDetail, ConversationOut

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(db: AsyncSession = Depends(get_db)) -> Conversation:
    """Cria uma conversa vazia e devolve o `session_id`.

    Opcional: POST /chat sem `session_id` já cria uma. Este endpoint existe para
    a UI que quer abrir a tela "Nova conversa" antes do usuário digitar.
    """
    return await repository.create_conversation(db)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    """Lista conversas, mais recentes primeiro. Paginado — a sidebar cresce."""
    return await repository.list_conversations(db, limit=limit, offset=offset)


@router.get("/{session_id}", response_model=ConversationDetail)
async def get_conversation(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    """Conversa + histórico, para a UI reidratar a tela ao abrir uma thread.

    Lê da tabela `messages` (o espelho), não do checkpoint: é uma query simples
    em vez de desserializar o estado inteiro do agent.
    """
    conversation = await repository.get_conversation(
        db, session_id, with_messages=True
    )
    if conversation is None:
        # Reaproveita a mensagem de erro da dependency.
        await get_conversation_or_404(session_id, db)
    return conversation


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation: Conversation = Depends(get_conversation_or_404),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Apaga a conversa nas DUAS camadas.

    Só apagar suas tabelas deixaria o checkpoint órfão no banco — invisível na
    UI, mas ocupando espaço e ainda contendo os dados do usuário. Em qualquer
    coisa sujeita a LGPD/GDPR, esquecer esta segunda chamada é um problema real.
    """
    thread_id = str(conversation.id)
    await repository.delete_conversation(db, conversation)
    await agent_runtime.saver.adelete_thread(thread_id)
