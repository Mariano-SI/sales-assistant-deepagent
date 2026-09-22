"""Acesso a dados das tabelas de produto.

Camada fina de propósito — mas existir separada mantém SQL fora dos routers,
que é o que permite testar a lógica de conversa sem subir o FastAPI.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.models import Conversation, Message

TITLE_MAX = 60


def _title_from(text: str) -> str:
    """Primeira mensagem do usuário vira o título da conversa (como no ChatGPT).

    Barato e previsível. A alternativa é pedir um título ao LLM — melhor, mas
    custa uma chamada a mais por conversa.
    """
    clean = " ".join(text.split())
    return clean[:TITLE_MAX] + "…" if len(clean) > TITLE_MAX else clean


async def create_conversation(
    db: AsyncSession, *, title: str | None = None
) -> Conversation:
    conversation = Conversation(id=uuid.uuid4(), title=title or "Nova conversa")
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def get_conversation(
    db: AsyncSession, conversation_id: uuid.UUID, *, with_messages: bool = False
) -> Conversation | None:
    stmt = select(Conversation).where(Conversation.id == conversation_id)
    if with_messages:
        # selectinload evita o N+1 (uma query extra, não uma por mensagem).
        stmt = stmt.options(selectinload(Conversation.messages))
    return await db.scalar(stmt)


async def list_conversations(
    db: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[Conversation]:
    stmt = (
        select(Conversation)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(await db.scalars(stmt))


async def add_message(
    db: AsyncSession, conversation: Conversation, *, role: str, content: str
) -> Message:
    message = Message(conversation_id=conversation.id, role=role, content=content)
    db.add(message)

    # Primeira mensagem do usuário nomeia a conversa.
    if role == "user" and conversation.title == "Nova conversa":
        conversation.title = _title_from(content)

    # Toca o updated_at para a conversa subir na sidebar.
    conversation.updated_at = func.now()

    await db.commit()
    await db.refresh(message)
    return message


async def delete_conversation(db: AsyncSession, conversation: Conversation) -> None:
    # As mensagens vão junto pelo ON DELETE CASCADE da FK.
    await db.delete(conversation)
    await db.commit()
