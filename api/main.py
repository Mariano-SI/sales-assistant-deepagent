"""Aplicação FastAPI.

    poetry run uvicorn api.main:app --reload

O `lifespan` é onde mora a diferença entre um script e um serviço: recursos
caros (grafo compilado, pool de conexões) são criados UMA vez no boot e
fechados no shutdown, em vez de nascerem e morrerem a cada request.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.agent_runtime import agent_runtime
from api.config import settings
from api.db import engine
from api.routers import chat, conversations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Note o que NÃO acontece aqui: `Base.metadata.create_all()`. O schema é
    # aplicado por `alembic upgrade head`, um passo explícito do deploy, antes
    # do processo subir. Criar tabela no boot da aplicação é atalho de tutorial:
    # com N réplicas subindo juntas elas correm entre si, e nenhuma delas sabe
    # alterar uma coluna que já existe.
    await agent_runtime.startup()
    logger.info("API pronta em http://%s:%s", settings.api_host, settings.api_port)
    try:
        yield
    finally:
        await agent_runtime.shutdown()
        await engine.dispose()


app = FastAPI(
    title="Chinook Sales Assistant API",
    description="API conversacional sobre o deep agent de vendas.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(conversations.router)
app.include_router(chat.router)


@app.get("/health", tags=["infra"])
async def health() -> dict[str, str]:
    """Liveness probe. O orquestrador (K8s, ECS) bate aqui."""
    return {"status": "ok"}
