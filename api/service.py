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

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from api import repository
from api.config import settings
from api.elicitation import TurnChannel, set_channel
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


def build_resume_command(decisions: list[Any]) -> Command:
    """Traduz as decisões humanas para o formato que o middleware HITL espera.

    Uma decisão POR action_request, na mesma ordem em que vieram no interrupt —
    o middleware valida isso e levanta ValueError se o número não bater. Por
    isso a API recebe uma lista, e não uma decisão só: se o modelo pediu duas
    tool calls no mesmo passo, as duas precisam de veredito.
    """
    payloads: list[dict[str, Any]] = []

    for decision in decisions:
        if decision.type == "approve":
            payloads.append({"type": "approve"})

        elif decision.type == "edit":
            # `edited_action` substitui a tool call inteira: nome e args.
            payloads.append(
                {
                    "type": "edit",
                    "edited_action": {
                        "name": decision.name,
                        "args": decision.args or {},
                    },
                }
            )

        elif decision.type == "reject":
            payload: dict[str, Any] = {"type": "reject"}
            if decision.message:
                payload["message"] = decision.message
            payloads.append(payload)

        else:  # respond — o humano responde NO LUGAR da tool
            payloads.append({"type": "respond", "message": decision.message or ""})

    return Command(resume={"decisions": payloads})


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

    Por que o grafo roda numa TASK separada, empurrando frames para uma fila,
    em vez de um `async for` direto aqui:

        Uma elicitation do MCP acontece DENTRO de uma tool call. Enquanto ela
        espera resposta, o `astream` fica parado — e se este gerador estivesse
        bloqueado nele, a pergunta jamais chegaria ao browser. Seria um impasse:
        a UI esperando o frame, e o frame esperando a UI responder.

        Com a fila no meio, quem produz (o grafo) e quem consome (o SSE) ficam
        desacoplados, e o callback de elicitation consegue enfileirar a pergunta
        mesmo com o grafo suspenso. Ver api/elicitation.py.
    """
    config = build_config(conversation.id)
    payload = await _prepare_payload(db, conversation, user_text, resume)

    # Primeiro frame: o cliente precisa do session_id ANTES da resposta, para já
    # conseguir registrar a conversa nova mesmo se o usuário fechar a aba no meio.
    yield sse(
        "session",
        {"session_id": str(conversation.id), "title": conversation.title},
    )

    channel = TurnChannel(session_id=str(conversation.id))
    chunks: list[str] = []
    interrupts: list[dict[str, Any]] | None = None
    failure: str | None = None

    async def pump() -> None:
        """Roda o grafo e despeja tudo na fila. Vive na própria task."""
        nonlocal interrupts, failure

        # Setado AQUI dentro, e não no gerador: `create_task` copia o contexto,
        # então este `set` fica contido nesta task e em tudo que ela criar —
        # inclusive a sessão MCP, que é onde o callback de elicitation roda.
        # Setar no gerador vazaria o canal para o contexto de quem o consome.
        set_channel(channel)
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
                        await channel.emit("token", {"content": piece})

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
                            await channel.emit("interrupt", {"interrupt": interrupts})
                            continue
                        for name in _tool_names(update):
                            await channel.emit("tool", {"name": name})

        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha no turno da conversa %s", conversation.id)
            failure = str(exc)
        finally:
            # Sentinela: sempre, inclusive em erro — sem ela o gerador abaixo
            # ficaria esperando para sempre e a conexão nunca fecharia.
            await channel.queue.put(None)

    task = asyncio.create_task(pump())

    try:
        while True:
            item = await channel.queue.get()
            if item is None:
                break
            event, data = item
            yield sse(event, data)
    finally:
        # Se o cliente desconectar, o gerador é fechado aqui: cancelamos o grafo
        # em vez de deixar a task rodando órfã, queimando tokens sem leitor.
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    if failure is not None:
        # Num stream os headers já foram enviados — não dá mais para devolver
        # 500. O jeito correto é emitir um frame de erro e encerrar limpo.
        yield sse("error", {"detail": failure})
        return

    if interrupts:
        yield sse("done", {"status": "interrupted", "message_id": None})
        return

    reply = "".join(chunks).strip()
    message = await repository.add_message(
        db, conversation, role="assistant", content=reply
    )
    yield sse("done", {"status": "completed", "message_id": message.id})
