# Stack local do Chinook Sales Assistant.
#
#   make stack       sobe TUDO, incluindo o front (Postgres, mail, API, Vite)
#   make stack-stop  derruba tudo que o `stack` subiu
#   make up          só o backend (API em foreground, sem front)
#   make help        lista os alvos
#
# Windows: rode pelo Git Bash. As receitas são POSIX (o cmd.exe não entende
# `for`, `[ -f ]` nem `&&` desta forma).

SHELL := bash
.DEFAULT_GOAL := help

PY        := poetry run
FRONT_DIR := frontend
MAIL_PORT := 5002
MAIL_URL  := http://127.0.0.1:$(MAIL_PORT)
API_HOST  ?= 127.0.0.1
API_PORT  ?= 8000

.PHONY: help install check-env db-up db-down db-reset db-logs psql \
        migrate revision mail mail-stop mail-logs api up down smoke clean \
        front front-install front-build stack stack-stop

help:  ## Lista os alvos disponíveis
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- ambiente
install:  ## Instala as dependências do Poetry
	poetry install

check-env:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo ">> criei .env a partir de .env.example — preencha OPENAI_API_KEY"; \
	fi

# ---------------------------------------------------------------- Postgres
db-up:  ## Sobe o Postgres e espera ele aceitar conexão
	@docker compose up -d
	@printf 'aguardando Postgres'
	@for i in $$(seq 1 30); do \
		if docker compose exec -T postgres pg_isready -U sales -d sales_assistant >/dev/null 2>&1; then \
			echo ' ok'; exit 0; \
		fi; \
		printf '.'; sleep 1; \
	done; \
	echo; echo '>> Postgres não respondeu em 30s (o Docker Desktop está rodando?)'; exit 1

db-down:  ## Para o Postgres (mantém os dados no volume)
	docker compose down

db-reset:  ## Para o Postgres e APAGA o volume — perde conversas e checkpoints
	docker compose down -v

db-logs:  ## Segue o log do Postgres
	docker compose logs -f postgres

psql:  ## Abre um psql no banco
	docker compose exec postgres psql -U sales -d sales_assistant

# -------------------------------------------------------------- migrations
migrate: check-env  ## Aplica as migrations (alembic upgrade head)
	$(PY) alembic upgrade head

revision: check-env  ## Gera migration por autogenerate: make revision m="mensagem"
	@if [ -z "$(m)" ]; then echo '>> uso: make revision m="descrição da mudança"'; exit 1; fi
	$(PY) alembic revision --autogenerate -m "$(m)"

# ------------------------------------------------------------- mail server
# Processo separado (MCP, streamable-http). A API sobe sem ele — só perde as
# tools de e-mail —, por isso este alvo nunca derruba o `make up`.
mail: check-env  ## Sobe o servidor MCP de e-mail em background
	@if curl -s --max-time 1 $(MAIL_URL)/ping >/dev/null 2>&1; then \
		echo ">> mail server já está no ar em :$(MAIL_PORT)"; exit 0; \
	fi
	@rm -f .mail.pid
	@echo ">> subindo mail server em :$(MAIL_PORT) ..."
	@$(PY) python mcp/mail_server.py > .mail.log 2>&1 & echo $$! > .mail.pid
	@for i in $$(seq 1 15); do \
		if curl -s --max-time 1 $(MAIL_URL)/ping >/dev/null 2>&1; then \
			echo ">> mail server up (pid $$(cat .mail.pid))"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo ">> mail server não respondeu — seguindo sem ele (log em .mail.log)"

mail-stop:  ## Para o servidor MCP de e-mail
	@if [ -f .mail.pid ]; then \
		PID=$$(cat .mail.pid); \
		kill $$PID 2>/dev/null || taskkill //PID $$PID //F >/dev/null 2>&1 || true; \
		rm -f .mail.pid; echo ">> mail server parado"; \
	else \
		echo ">> nenhum .mail.pid — nada para parar"; \
	fi

mail-logs:  ## Segue o log do mail server
	@tail -f .mail.log

# --------------------------------------------------------------------- API
# Via run_api.py, e não `uvicorn` direto: no Windows o uvicorn sem --reload
# escolhe o ProactorEventLoop, incompatível com o psycopg async. Ver run_api.py.
api: check-env  ## Sobe a API com --reload (foreground)
	$(PY) python run_api.py --reload

up: db-up migrate mail api  ## Sobe backend inteiro e deixa a API em foreground

# ---------------------------------------------------------------- frontend
front-install:  ## Instala as dependências do front (só na primeira vez)
	@cd $(FRONT_DIR) && npm install

front: front-install  ## Sobe o Vite em foreground (precisa da API no ar)
	@cd $(FRONT_DIR) && npm run dev

front-build:  ## Build de produção do front (typecheck + bundle em frontend/dist)
	@cd $(FRONT_DIR) && npm run build

# ------------------------------------------------------------ tudo junto
# `up` deixa a API em foreground, então ela não pode ser uma dependência daqui:
# o make nunca chegaria ao alvo do front. Por isso a API sobe em BACKGROUND e o
# Vite fica em foreground — é nele que você quer ver o log enquanto mexe na UI.
stack: db-up migrate mail front-install  ## Sobe tudo: Postgres, mail, API (bg) e front (fg)
	@if curl -s --max-time 1 http://$(API_HOST):$(API_PORT)/health >/dev/null 2>&1; then \
		echo ">> API já está no ar em :$(API_PORT)"; \
	else \
		rm -f .api.pid; \
		echo ">> subindo API em :$(API_PORT) ..."; \
		$(PY) python run_api.py > .api.log 2>&1 & echo $$! > .api.pid; \
		for i in $$(seq 1 40); do \
			if curl -s --max-time 1 http://$(API_HOST):$(API_PORT)/health >/dev/null 2>&1; then \
				echo ">> API up (pid $$(cat .api.pid))"; break; \
			fi; \
			if [ $$i -eq 40 ]; then echo ">> API não respondeu em 40s — veja .api.log"; exit 1; fi; \
			sleep 1; \
		done; \
	fi
	@echo ""
	@echo ">> tudo no ar. front em http://localhost:5173 (Ctrl+C encerra só o Vite)"
	@echo ">> depois, rode 'make stack-stop' para derrubar API, mail e Postgres."
	@echo ""
	@cd $(FRONT_DIR) && npm run dev

stack-stop: down  ## Derruba API, mail server e Postgres
	@if [ -f .api.pid ]; then \
		PID=$$(cat .api.pid); \
		kill $$PID 2>/dev/null || taskkill //PID $$PID //F >/dev/null 2>&1 || true; \
		rm -f .api.pid; echo ">> API parada"; \
	else \
		echo ">> nenhum .api.pid — nada para parar"; \
	fi

down: mail-stop db-down  ## Derruba mail server e Postgres

# ------------------------------------------------------------------ extras
smoke:  ## Testa a API no ar: /health e uma conversa nova
	@curl -s $(API_HOST):$(API_PORT)/health; echo
	@curl -s $(API_HOST):$(API_PORT)/chat -H 'content-type: application/json' \
		-d '{"message":"Quantos clientes temos no Brasil?"}'; echo

clean:  ## Remove bytecode e artefatos locais do mail server
	@rm -f .mail.pid .mail.log
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo ">> limpo"
