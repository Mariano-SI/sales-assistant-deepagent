"""Lógica de um turno de conversa.

O ponto central deste arquivo — e a coisa que mais surpreende quem vem de uma
API de chat "na mão":

    VOCÊ NÃO REENVIA O HISTÓRICO.

Você manda só a mensagem nova. O checkpointer carrega o estado anterior daquela
`thread_id` do Postgres, anexa a mensagem e grava o novo estado no fim do turno.
As tabelas `messages` da sua app são um ESPELHO para a UI ler barato — não são
a fonte de verdade do que o modelo enxerga. A fonte de verdade é o checkpoint.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from api import repository
from api.config import settings
from api.models import Conversation

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def build_config(session_id: uuid.UUID) -> RunnableConfig:
    """`thread_id` é o que isola uma conversa da outra dentro do checkpointer."""
    return {
        "configurable": {"thread_id": str(session_id)},
        "recursion_limit": settings.recursion_limit,
    }


def _message_text(message: Any) -> str:
    """Texto de uma mensagem, seja `content` string ou lista de blocos."""
    text = getattr(message, "text", None)
    if text is not None:
        # Em langchain v1 `.text` é property; versões antigas expõem método.
        value = text() if callable(text) else text
        if value:
            return str(value)

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def extract_reply(state: dict[str, Any]) -> str:
    """Última mensagem do assistente que seja texto de verdade (não tool call)."""
    for message in reversed(state.get("messages", []) or []):
        if getattr(message, "type", None) != "ai":
            continue
        if getattr(message, "tool_calls", None):
            continue
        text = _message_text(message).strip()
        if text:
            return text
    return ""


def extract_interrupts(state: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Interrupts pendentes, em forma serializável.

    O agent para aqui quando uma tool exige aprovação humana
    (`add_customer`, `mail_create_draft`). O cliente responde em /chat/resume.
    """
    raw = state.get("__interrupt__")
    if not raw:
        return None
    return [
        {"id": getattr(item, "id", None), "value": getattr(item, "value", None)}
        for item in raw
    ]


def _is_subagent(metadata: dict[str, Any]) -> bool:
    """Esse token veio de um subagent em vez do agent principal?

    O `task` tool do deepagents injeta `ls_agent_type="subagent"` no config, e o
    LangGraph funde o `configurable` no metadata do stream. O `checkpoint_ns`
    aninhado (com "|") é a checagem reserva, caso essa chave mude de nome.
    """
    if metadata.get("ls_agent_type") == "subagent":
        return True
    return "|" in (metadata.get("checkpoint_ns") or "")


def sse(event: str, data: dict[str, Any]) -> str:
    """Monta um frame Server-Sent Events.

    Formato: uma linha "event: <nome>", uma linha "data: <json>", e uma linha
    EM BRANCO. É a linha em branco que sinaliza fim de frame — sem ela o
    cliente segura tudo em buffer e o streaming não aparece.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _tool_names(update: Any) -> list[str]:
    """Nomes das tools que o agent acabou de decidir chamar."""
    if not isinstance(update, dict):
        return []
    names = []
    for message in update.get("messages", []) or []:
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                names.append(name)
    return names


def build_resume_command(
    decision: str, *, args: dict[str, Any] | None, message: str | None
) -> Command:
    """Traduz a decisão humana para o formato que o middleware HITL espera.

    O payload é {"decisions": [...]} — lista porque o agent pode ter pedido
    aprovação de várias tool calls no mesmo interrupt.
    """
    if decision == "approve":
        payload: dict[str, Any] = {"type": "approve"}
    elif decision == "edit":
        edited = args or {}
        payload = {
            "type": "edit",
            "edited_action": {
                "name": edited.get("name"),
                "args": edited.get("args", {}),
            },
        }
    else:  # reject
        payload = {"type": "reject"}
        if message:
            payload["message"] = message

    return Command(resume={"decisions": [payload]})


async def _prepare_payload(
    db: AsyncSession,
    conversation: Conversation,
    user_text: str | None,
    resume: Command | None,
) -> Any:
    """Entrada do grafo: mensagem nova, ou retomada de um interrupt."""
    if resume is not None:
        return resume
    if user_text is None:
        raise ValueError("user_text é obrigatório quando não há resume.")
    await repository.add_message(db, conversation, role="user", content=user_text)
    return {"messages": [{"role": "user", "content": user_text}]}


# --------------------------------------------------------------------------
# turno sem streaming
# --------------------------------------------------------------------------
async def run_turn(
    graph: CompiledStateGraph,
    db: AsyncSession,
    conversation: Conversation,
    *,
    user_text: str | None = None,
    resume: Command | None = None,
) -> dict[str, Any]:
    """Roda um turno até o fim (ou até o agent pedir aprovação humana)."""
    config = build_config(conversation.id)
    payload = await _prepare_payload(db, conversation, user_text, resume)

    state = await graph.ainvoke(payload, config=config)

    interrupts = extract_interrupts(state)
    if interrupts:
        return {
            "session_id": conversation.id,
            "status": "interrupted",
            "reply": "",
            "message_id": None,
            "interrupt": interrupts,
        }

    reply = extract_reply(state)
    message = await repository.add_message(
        db, conversation, role="assistant", content=reply
    )
    return {
        "session_id": conversation.id,
        "status": "completed",
        "reply": reply,
        "message_id": message.id,
        "interrupt": None,
    }


# --------------------------------------------------------------------------
# turno com streaming (SSE)
# --------------------------------------------------------------------------
async def stream_turn(
    graph: CompiledStateGraph,
    db: AsyncSession,
    conversation: Conversation,
    *,
    user_text: str | None = None,
    resume: Command | None = None,
) -> AsyncIterator[str]:
    """Mesma coisa, emitindo frames SSE conforme o agent trabalha.

    Dois `stream_mode` ao mesmo tempo:
      - "messages" -> token a token do LLM (o que a UI digita na tela);
      - "updates"  -> o que cada nó devolveu, usado aqui para mostrar qual tool
        está rodando ("consultando o banco...") e para detectar o interrupt.
    """
    config = build_config(conversation.id)
    payload = await _prepare_payload(db, conversation, user_text, resume)

    # Primeiro frame: o cliente precisa do session_id ANTES da resposta, para já
    # conseguir registrar a conversa nova mesmo se o usuário fechar a aba no meio.
    yield sse(
        "session",
        {"session_id": str(conversation.id), "title": conversation.title},
    )

    chunks: list[str] = []
    interrupts: list[dict[str, Any]] | None = None

    try:
        async for stream_mode, chunk in graph.astream(
            payload, config=config, stream_mode=["messages", "updates"]
        ):
            if stream_mode == "messages":
                message, metadata = chunk
                if _is_subagent(metadata) and not settings.stream_subagent_tokens:
                    continue
                if getattr(message, "type", "") not in ("ai", "AIMessageChunk"):
                    continue
                piece = _message_text(message)
                if piece:
                    chunks.append(piece)
                    yield sse("token", {"content": piece})

            elif stream_mode == "updates":
                for node, update in (chunk or {}).items():
                    if node == "__interrupt__":
                        interrupts = [
                            {
                                "id": getattr(i, "id", None),
                                "value": getattr(i, "value", None),
                            }
                            for i in (update or [])
                        ]
                        yield sse("interrupt", {"interrupt": interrupts})
                        continue
                    for name in _tool_names(update):
                        yield sse("tool", {"name": name})

    except Exception as exc:  # noqa: BLE001
        # Num stream os headers já foram enviados — não dá mais para devolver
        # 500. O jeito correto é emitir um frame de erro e encerrar limpo.
        logger.exception("Falha no turno da conversa %s", conversation.id)
        yield sse("error", {"detail": str(exc)})
        return

    if interrupts:
        yield sse("done", {"status": "interrupted", "message_id": None})
        return

    reply = "".join(chunks).strip()
    message = await repository.add_message(
        db, conversation, role="assistant", content=reply
    )
    yield sse("done", {"status": "completed", "message_id": message.id})
