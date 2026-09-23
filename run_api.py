"""Launcher da API — existe por causa de uma armadilha do Windows.

    poetry run python run_api.py              # sem reload (usado pelo `make stack`)
    poetry run python run_api.py --reload     # com reload (usado pelo `make api`)

--------------------------------------------------------------------------
O problema
--------------------------------------------------------------------------
No Windows o asyncio usa o **ProactorEventLoop** por padrão, e o psycopg em
modo async NÃO funciona nele:

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

O uvicorn escolhe o loop assim (uvicorn/loops/asyncio.py):

    if sys.platform == "win32" and not use_subprocess:
        return asyncio.ProactorEventLoop     # <- sem --reload
    return asyncio.SelectorEventLoop         # <- com --reload

`use_subprocess` é True quando há `--reload` (ou vários workers). Ou seja:
rodar `uvicorn api.main:app --reload` funciona no Windows **por acidente**, e
rodar sem `--reload` quebra no boot, ao abrir o pool do checkpointer.

--------------------------------------------------------------------------
Por que não dá para resolver com set_event_loop_policy
--------------------------------------------------------------------------
É o truque usado em migrations/env.py para o Alembic, mas aqui não serve: o
uvicorn passa `loop_factory=` explicitamente para o `asyncio.run`
(uvicorn/server.py:86), e um loop factory explícito ignora a policy. Por isso
este launcher constrói o Server na mão e escolhe o factory ele mesmo.

Em Linux/macOS nada disso se aplica — o caminho é o `server.run()` normal.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from api.config import settings


def main() -> None:
    reload = "--reload" in sys.argv

    # Com --reload o uvicorn precisa orquestrar o processo filho que observa os
    # arquivos; deixamos isso com ele, que nesse caminho já escolhe o
    # SelectorEventLoop sozinho.
    if reload:
        uvicorn.run(
            "api.main:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=True,
        )
        return

    config = uvicorn.Config(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        # O ponto inteiro deste arquivo.
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        server.run()


if __name__ == "__main__":
    main()
