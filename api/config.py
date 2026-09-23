"""Configuração da aplicação.

Tudo vem de variável de ambiente. O arquivo `.env` é só um *fallback* para
desenvolvimento local — em produção nenhuma `.env` é deployada: a
`DATABASE_URL` chega pelo Secrets Manager / K8s Secret / variável do runtime.
Por isso a precedência do pydantic-settings é: ambiente > .env > default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # DSN "puro" (sem driver no schema). Em produção vira algo como
    # postgresql://user:pass@db.interno:5432/app?sslmode=require
    database_url: str = "postgresql://sales:sales@localhost:5433/sales_assistant"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Servidor MCP de e-mail. Opcional: a API sobe sem ele (só perde as tools de mail).
    mail_server_url: str = "http://127.0.0.1:5002/mcp"
    recursion_limit: int = 50

    # Streaming: por padrão só os tokens do agent principal vão pro cliente.
    stream_subagent_tokens: bool = False

    # Quanto tempo uma elicitation do MCP espera por uma resposta humana antes
    # de desistir (e devolver "cancel" ao servidor). Precisa existir: essa
    # pausa vive na MEMÓRIA do processo, não no checkpoint, então um usuário
    # que fecha a aba deixaria a tool call aberta para sempre.
    elicitation_timeout: float = 300.0

    @property
    def sqlalchemy_url(self) -> str:
        """Mesmo banco, mas com o driver async que o SQLAlchemy precisa."""
        return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    @property
    def checkpointer_url(self) -> str:
        """O saver do LangGraph usa psycopg direto, então quer o DSN puro."""
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
