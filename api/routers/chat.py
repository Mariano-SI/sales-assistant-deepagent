"""Endpoints conversacionais.

A regra do `session_id`, que é o coração do pedido:

  - **sem** `session_id` -> conversa nova. A API cria a thread e devolve o id.
  - **com** `session_id` -> continuação. O checkpointer recarrega o estado
    daquela thread; você manda só a mensagem nova, nunca o histórico.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from api import repository, service
from api.db import SessionLocal, get_db
from api.deps import get_graph
from api.models import Conversation
from api.schemas import ChatRequest, ChatResponse, ResumeRequest

router = APIRouter(prefix="/chat", tags=["chat"])

# Sem isso, nginx e alguns proxies acumulam a resposta em buffer e entregam
# tudo de uma vez no final — o streaming "funciona" em dev e some em produção.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _resolve_conversation(
    db: AsyncSession, session_id: uuid.UUID | None
) -> Conversation:
    """Continua a thread existente ou abre uma nova."""
    if session_id is None:
        return await repository.create_conversation(db)

    conversation = await repository.get_conversation(db, session_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Conversa {session_id} não encontrada.",
        )
    return conversation


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    graph: CompiledStateGraph = Depends(get_graph),
) -> dict:
    """Manda uma mensagem e espera a resposta completa.

    Mais simples que o streaming e o certo para integrações máquina-a-máquina
    (webhook, job, outro serviço). Para UI de chat, prefira /chat/stream.
    """
    conversation = await _resolve_conversation(db, payload.session_id)
    return await service.run_turn(
        graph, db, conversation, user_text=payload.message
    )


@router.post("/stream")
async def chat_stream(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    graph: CompiledStateGraph = Depends(get_graph),
) -> StreamingResponse:
    """Mesma coisa, em Server-Sent Events.

    Eventos emitidos, nesta ordem:
      session   -> {session_id, title}   sempre o primeiro
      tool      -> {name}                o agent chamou uma tool
      token     -> {content}             pedaço do texto da resposta
      interrupt -> {interrupt}           parou pedindo aprovação humana
      done      -> {status, message_id}  fim do turno
      error     -> {detail}              falhou no meio do stream

    Por que SSE e não WebSocket: o tráfego aqui é de mão única (servidor ->
    cliente) e SSE é HTTP puro — passa por qualquer proxy, reconecta sozinho no
    browser e não precisa de gerenciamento de conexão. É o que ChatGPT e Claude
    usam. WebSocket só se ganha quando o cliente também precisa empurrar dados.
    """
    # A conversa é resolvida AQUI, com a sessão do request, para que um
    # session_id inválido vire um 404 de verdade. Depois que o
    # StreamingResponse começa, os headers já foram enviados e não há mais como
    # mudar o status code — o erro só poderia virar um frame `error`.
    conversation = await _resolve_conversation(db, payload.session_id)
    conversation_id = conversation.id

    async def generate() -> AsyncIterator[str]:
        # Sessão PRÓPRIA, aberta dentro do gerador. A sessão injetada pelo
        # Depends é encerrada quando o handler retorna — e o handler retorna
        # assim que devolve o StreamingResponse, antes de um único token sair.
        # Usar aquela sessão aqui dá erro de sessão fechada no meio do stream.
        async with SessionLocal() as stream_db:
            conv = await repository.get_conversation(stream_db, conversation_id)
            async for frame in service.stream_turn(
                graph, stream_db, conv, user_text=payload.message
            ):
                yield frame

    return StreamingResponse(
        generate(), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/resume", response_model=ChatResponse)
async def resume(
    payload: ResumeRequest,
    db: AsyncSession = Depends(get_db),
    graph: CompiledStateGraph = Depends(get_graph),
) -> dict:
    """Responde a um interrupt e destrava a thread.

    O agent pausa quando uma tool exige aprovação humana (`add_customer`,
    `mail_create_draft`). Sem este endpoint a conversa ficaria travada para
    sempre — e é justamente isso que o checkpointer torna possível: o processo
    pode reiniciar entre a pausa e a aprovação, que o estado continua no
    Postgres esperando.
    """
    conversation = await _resolve_conversation(db, payload.session_id)
    command = service.build_resume_command(
        payload.decision, args=payload.args, message=payload.message
    )
    return await service.run_turn(graph, db, conversation, resume=command)
