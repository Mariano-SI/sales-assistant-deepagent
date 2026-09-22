"""Contratos de entrada/saída da API (Pydantic).

Separados dos models do SQLAlchemy de propósito: o schema do banco pode mudar
sem quebrar o contrato HTTP, e nenhuma coluna interna vaza para o cliente por
acidente.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    # Ausente => conversa nova. Presente => continua a thread existente.
    session_id: uuid.UUID | None = None


class ResumeRequest(BaseModel):
    """Responde a um interrupt (o agent pediu aprovação humana)."""

    session_id: uuid.UUID
    decision: Literal["approve", "edit", "reject"]
    # Para "edit": os argumentos corrigidos da tool. Para "reject": o motivo.
    args: dict[str, Any] | None = None
    message: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    # "completed" = respondeu. "interrupted" = parou pedindo aprovação humana;
    # o cliente precisa chamar POST /chat/resume para destravar.
    status: Literal["completed", "interrupted"]
    reply: str
    message_id: int | None = None
    interrupt: list[dict[str, Any]] | None = None
