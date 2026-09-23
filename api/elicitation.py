"""A ponte entre uma elicitation do MCP e o navegador.

O problema, em uma frase: o servidor MCP faz uma pergunta **no meio** de uma
tool call, e quem sabe a resposta está do outro lado de uma conexão HTTP.

Como isso funciona aqui:

    servidor MCP                  API (este arquivo)              browser
    ------------                  ------------------              -------
    await ctx.elicit(...)  ──────► on_elicitation()
                                     │ 1. registra um Future
                                     │ 2. empurra um frame SSE ────► mostra o
                                     │ 3. await future                 formulário
                                     │                                    │
                                     │                           POST /chat/elicit
                                     ▼                                    │
                                   answer() resolve o Future ◄────────────┘
    ◄────── ElicitResult ──────────  return

Três coisas que valem gravar:

1. **Não dá para usar `interrupt()` do LangGraph aqui.** O callback roda dentro
   do receive loop do MCP, e `mcp/shared/session.py` envolve o dispatch num
   `except Exception` que transforma qualquer exceção em erro JSON-RPC. O
   `GraphInterrupt` seria engolido e o grafo nunca pausaria. Por isso a espera
   é um `asyncio.Future`, não um interrupt.

2. **Esta pausa NÃO é durável.** O Future vive na memória deste processo. Se a
   API reiniciar com uma elicitation pendente, ela se perde — diferente do
   interrupt HITL, que está no checkpoint do Postgres e sobrevive ao restart.
   É a diferença entre "pergunta em voo" e "pergunta persistida", e é a razão
   de existir um timeout logo abaixo.

3. **O canal é um contextvar.** Quando o turno é criado, `stream_turn` guarda a
   fila de frames num contextvar; `asyncio.create_task` copia o contexto, e a
   sessão MCP nasce dentro dessa árvore de tasks — então o callback enxerga a
   fila do turno certo mesmo com várias conversas rodando ao mesmo tempo.
   (Verificado: contextvars propagam até o receive loop do MCP.)

--------------------------------------------------------------------------
ESTE ARQUIVO TEM PRAZO DE VALIDADE
--------------------------------------------------------------------------
Tudo acima existe porque o protocolo MCP na versão **2025-11-25** exige uma
sessão stateful: o pedido de elicitation sobe do servidor pela sessão aberta,
no meio da tool call, e por isso alguém precisa esperar por ele em memória.

A spec **2026-07-28** é stateless-first e resolve isso com MRTR (Multi
Round-Trip Requests): a tool call RETORNA com `resultType: "input_required"`,
e o cliente REPETE a chamada com `inputResponses` anexados. Sem receive loop
bloqueado, um `interrupt()` do LangGraph funcionaria normalmente — a pausa
viraria durável no checkpoint e este módulo inteiro seria desnecessário.

Ainda não dá: `mcp` 1.30.0 declara `LATEST_PROTOCOL_VERSION = "2025-11-25"`, e
`langchain-mcp-adapters` 0.3.2 faz `initialize()` + `call_tool` por chamada.
Quando isso mudar, apague este arquivo, o endpoint /chat/elicit e o ElicitPanel,
e trate elicitation como mais um interrupt.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp.types import ElicitResult

from api.config import settings

logger = logging.getLogger(__name__)

# Item da fila de um turno: (nome do evento SSE, payload). `None` encerra.
StreamItem = tuple[str, dict[str, Any]] | None


@dataclass
class TurnChannel:
    """A fila de frames SSE de UM turno de conversa."""

    session_id: str
    queue: asyncio.Queue[StreamItem] = field(default_factory=asyncio.Queue)

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        await self.queue.put((event, data))


# Canal do turno atual. Setado dentro da task que roda o grafo (ver
# service.stream_turn), e herdado por tudo que ela cria — inclusive a sessão MCP.
_channel: contextvars.ContextVar[TurnChannel | None] = contextvars.ContextVar(
    "elicitation_channel", default=None
)

# Elicitations esperando resposta, por id. É global de propósito: quem responde
# é OUTRO request HTTP (POST /chat/elicit), com outro contexto.
_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}


def set_channel(channel: TurnChannel) -> None:
    """Chamado no início do turno, de dentro da task que roda o grafo."""
    _channel.set(channel)


def pending_ids() -> list[str]:
    """Ids pendentes — usado só para diagnóstico/log."""
    return list(_pending)


async def on_elicitation(mcp_context: Any, params: Any, context: Any) -> ElicitResult:
    """Callback registrado no MultiServerMCPClient (ver agent.py).

    O servidor MCP chamou `ctx.elicit(...)`. Aqui decidimos quem responde.
    """
    channel = _channel.get()

    # Sem canal não há ninguém para perguntar: é o caso do POST /chat (sem
    # streaming), de um job, ou de um webhook. Recusar é o certo — muito melhor
    # que travar o turno esperando uma resposta que nunca vem.
    if channel is None:
        logger.info(
            "Elicitation de %s sem canal de UI — recusando.",
            getattr(context, "tool_name", "?"),
        )
        return ElicitResult(action="decline")

    elicitation_id = str(uuid.uuid4())
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    _pending[elicitation_id] = future

    await channel.emit(
        "elicitation",
        {
            "elicitation_id": elicitation_id,
            "session_id": channel.session_id,
            "tool": getattr(context, "tool_name", None),
            "server": getattr(context, "server_name", None),
            "message": getattr(params, "message", ""),
            # JSON Schema plano — o front renderiza um formulário a partir dele.
            "schema": getattr(params, "requestedSchema", None),
        },
    )

    try:
        answer = await asyncio.wait_for(future, timeout=settings.elicitation_timeout)
    except asyncio.TimeoutError:
        logger.warning("Elicitation %s expirou.", elicitation_id)
        await channel.emit("elicitation_closed", {"elicitation_id": elicitation_id})
        return ElicitResult(action="cancel")
    finally:
        _pending.pop(elicitation_id, None)

    await channel.emit("elicitation_closed", {"elicitation_id": elicitation_id})

    action = answer.get("action", "cancel")
    if action != "accept":
        return ElicitResult(action=action)
    return ElicitResult(action="accept", content=answer.get("content") or {})


def answer(
    elicitation_id: str,
    *,
    action: Literal["accept", "decline", "cancel"],
    content: dict[str, Any] | None = None,
) -> bool:
    """Resolve uma elicitation pendente. `False` se ela não existe mais.

    Não existir mais é normal: expirou, ou o usuário mandou a resposta duas
    vezes. Por isso devolvemos um booleano em vez de levantar exceção.
    """
    future = _pending.get(elicitation_id)
    if future is None or future.done():
        return False
    future.set_result({"action": action, "content": content})
    return True
