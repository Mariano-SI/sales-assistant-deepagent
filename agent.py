"""Chinook Sales Assistant.

Uses a local FilesystemBackend, a QuickJS code interpreter for arithmetic
and data prep, and a dedicated chart tool for rendering.

Ponto de entrada único: `build_agent(checkpointer=...)`, usado pela API
(api/agent_runtime.py), que passa um AsyncPostgresSaver para o estado viver no
Postgres. Sem checkpointer o agent roda, mas esquece tudo entre invocações.

Start with:
    make up
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_mcp_adapters.callbacks import Callbacks, ElicitationCallback
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_quickjs import CodeInterpreterMiddleware
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from tools.chart import render_pie_chart
from tools.html import markdown_to_html

from models import strong_model
from subagents import build_subagents

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent

SYSTEM_PROMPT = (
    "You are a sales assistant for Jane Peacock, a Sales Support Agent at "
    "Chinook, an online music distributor. Follow your operating manual (loaded "
    "from your memory) and use the matching playbook from /skills/ for each task."
)

MAIL_SERVER_URL = os.environ.get("MAIL_SERVER_URL", "http://127.0.0.1:5002/mcp")
MAIL_SERVER = {"transport": "streamable-http", "url": MAIL_SERVER_URL}

_enable_search = bool(os.environ.get("TAVILY_API_KEY"))
if not _enable_search:
    logger.info("TAVILY_API_KEY not set — newsletter research subagent disabled.")

_backend = FilesystemBackend(root_dir=str(HERE), virtual_mode=True)


async def _load_mail_tools(on_elicitation: ElicitationCallback | None) -> list:
    """Descobre as tools do servidor MCP de e-mail.

    Falha de forma suave: o servidor de mail é um processo à parte, e a API não
    deve deixar de subir só porque ele está fora do ar. Sem ele o agent perde as
    tools de e-mail, mas continua respondendo sobre a base Chinook.

    `on_elicitation` é quem responde quando uma tool do servidor pergunta algo
    no meio da execução (`mail_schedule_followup`). Ele é INJETADO em vez de
    importado: assim este módulo continua sem saber que existe uma API HTTP, e
    quem roda o agent decide como consultar o humano — pelo browser (a ponte em
    api/elicitation.py), por um prompt no terminal, ou por nada (recusa).
    """
    try:
        client = MultiServerMCPClient(
            {"email-server": MAIL_SERVER},
            callbacks=Callbacks(on_elicitation=on_elicitation),
        )
        return await client.get_tools()
    except Exception:
        logger.warning(
            "Mail MCP server unreachable at %s — email tools disabled.",
            MAIL_SERVER_URL,
        )
        return []


async def build_agent(
    checkpointer: BaseCheckpointSaver | None = None,
    on_elicitation: ElicitationCallback | None = None,
) -> CompiledStateGraph:
    """Compila o agent. Passe um checkpointer para o estado sobreviver ao processo."""
    mail_tools = await _load_mail_tools(on_elicitation)

    return create_deep_agent(
        model=strong_model,
        # As tools de e-mail NÃO entram aqui de propósito. Elas vivem só no
        # inbox-manager, que é quem tem o gate de aprovação em
        # `mail_create_draft`. Colocá-las também no agent principal abre um
        # caminho sem gate: o principal chama `mail_create_draft` direto e o
        # rascunho é salvo sem ninguém aprovar — e, de quebra, o subagent
        # general-purpose herda as tools do principal, então o desvio existiria
        # por duas vias. Mesma razão pela qual `add_customer` só existe no
        # chinook-analyst. Ver AGENTS.md: "You have no email tools yourself."
        tools=[render_pie_chart, markdown_to_html],
        system_prompt=SYSTEM_PROMPT,
        skills=["/skills"],
        subagents=build_subagents(
            _backend, enable_search=_enable_search, mail_tools=mail_tools
        ),
        memory=["/AGENTS.md"],
        backend=_backend,
        middleware=[CodeInterpreterMiddleware()],
        checkpointer=checkpointer,
        name="chinook-sales-assistant",
    )

