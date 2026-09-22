"""Ambiente do Alembic.

Dois ajustes importantes em relação ao template padrão:

1. A URL do banco NÃO fica no alembic.ini — vem de `api.config.settings`, ou
   seja, do ambiente. Assim `alembic upgrade head` roda igual no seu laptop e
   no pipeline de deploy, só trocando a variável.

2. `include_name` restringe o autogenerate às tabelas do seu `Base.metadata`.
   Isto é ESSENCIAL aqui: o LangGraph cria as tabelas dele (`checkpoints`,
   `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) no mesmo
   banco. Sem esse filtro, o autogenerate veria tabelas "desconhecidas" e
   geraria um `op.drop_table("checkpoints")` — apagando o estado de todas as
   conversas no primeiro deploy.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Importar os models registra as tabelas em Base.metadata.
from api import models  # noqa: F401
from api.config import settings
from api.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` é escape do configparser — a senha pode conter um.
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url.replace("%", "%%"))

target_metadata = Base.metadata


def include_name(name, type_, parent_names) -> bool:
    """Ignora tudo que não seja tabela nossa (ver docstring do módulo)."""
    if type_ == "table":
        return name in target_metadata.tables
    return True


def run_migrations_offline() -> None:
    """Gera o SQL sem conectar (`alembic upgrade head --sql`).

    Útil quando o DBA quer revisar/aplicar o SQL à mão em produção.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_name=include_name,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
