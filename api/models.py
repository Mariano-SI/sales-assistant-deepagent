"""Tabelas de produto.

Estas tabelas são SUAS — o LangGraph não sabe que elas existem e não as migra.
Elas guardam o que a UI precisa para montar a sidebar de conversas sem ter que
desserializar checkpoint nenhum.

O `id` da conversa é o MESMO valor usado como `thread_id` do LangGraph. Ou seja:
`session_id` (API) == `conversation.id` (sua tabela) == `thread_id` (checkpointer).
Um identificador só atravessando as três camadas — menos mapeamento para errar.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(200), default="Nova conversa")

    # Em produção isto teria também `user_id` + índice composto, vindo do token
    # de autenticação. Sem ele, qualquer um lista as conversas de todo mundo.
    # Fica de propósito fora do escopo deste estudo.

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,  # a sidebar ordena por aqui
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )


class Message(Base):
    __tablename__ = "messages"

    # BigInteger autoincremental em vez de UUID: a ordem de inserção JÁ é a
    # ordem cronológica da conversa. Ordenar por `created_at` seria frágil —
    # duas mensagens gravadas no mesmo instante empatariam.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
