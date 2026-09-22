# Stack local do Chinook Sales Assistant.
#
#   make up        sobe tudo (Postgres -> migrations -> mail server -> API)
#   make down      derruba mail server e Postgres
#   make help      lista os alvos
#
# Windows: rode pelo Git Bash. As receitas são POSIX (o cmd.exe não entende
# `for`, `[ -f ]` nem `&&` desta forma).

SHELL := bash
.DEFAULT_GOAL := help

PY        := poetry run
MAIL_PORT := 5002
MAIL_URL  := http://127.0.0.1:$(MAIL_PORT)
API_HOST  ?= 127.0.0.1
API_PORT  ?= 8000

.PHONY: help install check-env db-up db-down db-reset db-logs psql \
        migrate revision mail mail-stop mail-logs api up down smoke clean

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
api: check-env  ## Sobe a API (uvicorn com --reload)
	$(PY) uvicorn api.main:app --reload --host $(API_HOST) --port $(API_PORT)

up: db-up migrate mail api  ## Sobe a stack inteira e deixa a API em foreground

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
