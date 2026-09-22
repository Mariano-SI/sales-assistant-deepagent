"""Engine e sessão do SQLAlchemy (async).

Note que existem DOIS pools de conexão nesta aplicação:

  1. Este aqui  — SQLAlchemy, para as tabelas de produto (conversations/messages).
  2. api/agent_runtime.py — psycopg_pool cru, para o checkpointer do LangGraph.

Eles apontam para o MESMO banco, mas o LangGraph não usa SQLAlchemy: ele fala
psycopg direto. Tentar compartilhar um pool só entre os dois dá mais dor de
cabeça do que ganho; dois pools pequenos é o arranjo normal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from api.config import settings

engine = create_async_engine(
    settings.sqlalchemy_url,
    # Derruba conexões mortas antes de usar (essencial atrás de pgbouncer
    # ou quando o banco reinicia).
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base declarativa. O Alembic lê `Base.metadata` para gerar as migrations."""


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependency do FastAPI: uma sessão por request, fechada no fim."""
    async with SessionLocal() as session:
        yield session
