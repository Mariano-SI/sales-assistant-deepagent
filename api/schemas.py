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


class DecisionIn(BaseModel):
    """Uma decisão humana sobre UMA tool call que o agent quis fazer.

    Os quatro tipos vêm do HumanInTheLoopMiddleware do LangChain:

    | type      | o que acontece com a tool          | campo usado        |
    |-----------|------------------------------------|--------------------|
    | `approve` | roda como o modelo pediu           | —                  |
    | `edit`    | roda com os args que o humano deu  | `name` + `args`    |
    | `reject`  | NÃO roda; o modelo recebe o motivo | `message`          |
    | `respond` | NÃO roda; o humano responde por ela| `message`          |

    `respond` é o menos óbvio: em vez de rodar a tool, a resposta do humano
    volta para o modelo como se FOSSE o retorno dela. Serve para tools do tipo
    "pergunte ao usuário", cuja implementação de verdade é a pessoa.
    """

    type: Literal["approve", "edit", "reject", "respond"]
    # Para "edit": nome e argumentos corrigidos da tool.
    name: str | None = None
    args: dict[str, Any] | None = None
    # Para "reject": o motivo. Para "respond": o conteúdo da resposta.
    message: str | None = None


class ResumeRequest(BaseModel):
    """Responde a um interrupt (o agent pediu aprovação humana).

    É uma LISTA porque um interrupt pode trazer várias `action_requests` de uma
    vez — o modelo pode pedir duas tool calls no mesmo passo. O middleware exige
    exatamente uma decisão por action_request, na mesma ordem.
    """

    session_id: uuid.UUID
    decisions: list[DecisionIn] = Field(min_length=1)


class ElicitAnswerRequest(BaseModel):
    """Responde a uma elicitation do MCP (pergunta feita DENTRO da tool call).

    Não confundir com `ResumeRequest`: aqui não há retomada de grafo nenhuma. O
    grafo nunca parou — ele está bloqueado dentro da tool call, esperando este
    valor chegar pela memória do processo.
    """

    elicitation_id: str
    action: Literal["accept", "decline", "cancel"]
    # Só usado com "accept": os campos do formulário, conforme o schema pedido.
    content: dict[str, Any] | None = None


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
