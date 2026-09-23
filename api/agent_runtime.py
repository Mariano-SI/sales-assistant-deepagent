"""Dono do grafo compilado e do checkpointer.

Três decisões que valem entender:

1. **O grafo é compilado UMA vez, no startup** — não por request. Compilar custa
   (descobre tools MCP, monta subagents, lê skills do disco). Fazer isso a cada
   request adicionaria centenas de ms e abriria conexão MCP nova toda hora.
   O grafo compilado é stateless e seguro para uso concorrente: todo o estado
   mutável mora no checkpointer, isolado por `thread_id`.

2. **Pool de conexões, não conexão única.** `AsyncPostgresSaver.from_conn_string`
   aparece em todo tutorial, mas abre UMA conexão — com requests concorrentes
   elas se serializam. Em produção se usa `AsyncConnectionPool`.

3. **`autocommit=True` + `row_factory=dict_row`** não são opcionais: o saver do
   LangGraph controla as transações dele e espera linhas como dicionário. Sem
   isso você toma erro em runtime, não no boot.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agent import build_agent
from api.config import settings
from api.elicitation import on_elicitation

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Mantém o grafo e o pool do checkpointer pelo tempo de vida do processo."""

    def __init__(self) -> None:
        self._pool: AsyncConnectionPool | None = None
        self._saver: AsyncPostgresSaver | None = None
        self._graph: CompiledStateGraph | None = None

    async def startup(self) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=settings.checkpointer_url,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                # pgbouncer em modo transaction não suporta prepared statements.
                "prepare_threshold": 0,
            },
        )
        await self._pool.open(wait=True, timeout=10)

        self._saver = AsyncPostgresSaver(self._pool)
        # ESTA é a "migration" do LangGraph: idempotente, versionada na tabela
        # checkpoint_migrations. Você não escreve migration para essas tabelas.
        await self._saver.setup()

        # A ponte de elicitation entra aqui: o agent passa a saber perguntar, e
        # quem responde é o browser via POST /chat/elicit.
        self._graph = await build_agent(
            checkpointer=self._saver, on_elicitation=on_elicitation
        )
        logger.info("Agent compilado e checkpointer pronto.")

    async def shutdown(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._saver = None
        self._graph = None

    @property
    def graph(self) -> CompiledStateGraph:
        if self._graph is None:
            raise RuntimeError("AgentRuntime.startup() ainda não rodou.")
        return self._graph

    @property
    def saver(self) -> BaseCheckpointSaver:
        if self._saver is None:
            raise RuntimeError("AgentRuntime.startup() ainda não rodou.")
        return self._saver


agent_runtime = AgentRuntime()
