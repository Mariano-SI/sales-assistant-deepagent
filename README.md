# Chinook Sales Assistant

Assistente de vendas construído com [deepagents](https://github.com/langchain-ai/deepagents)
sobre LangGraph, exposto como uma API HTTP conversacional. Ele trabalha para
**Jane Peacock**, representante de vendas da Chinook (uma distribuidora de música
fictícia): responde pedidos de cotação que chegam por e-mail, consulta e cadastra
clientes, gera relatórios do território e monta a newsletter semanal.

O agent assiste — a Jane decide. Toda ação com efeito externo (salvar um rascunho
de e-mail, cadastrar um cliente) **pausa a conversa** e espera aprovação humana.

---

## Como funciona

```
                 ┌──────────────────────── FastAPI (api/) ────────────────────────┐
  cliente ──────►│  /chat  /chat/stream  /chat/resume  /conversations             │
                 │        │                                                       │
                 │        ▼                                                       │
                 │   grafo compilado (um só, criado no startup)                   │
                 │   agent principal  ──task──►  subagents                        │
                 └────────┬───────────────────────────┬───────────────────────────┘
                          │                           │
              ┌───────────▼──────────┐     ┌──────────▼───────────┐   ┌───────────────┐
              │ Postgres             │     │ data/chinook.db      │   │ mail server   │
              │  checkpoints (agent) │     │ (SQLite, negócio)    │   │ MCP :5002     │
              │  conversations/msgs  │     └──────────────────────┘   └───────────────┘
              └──────────────────────┘
```

### O agent

O agent principal (`agent.py`) **coordena**; o trabalho estreito fica com
especialistas, e dois deles são o único caminho até um sistema externo:

| Subagent | Faz | Modelo |
|---|---|---|
| `chinook-analyst` | Todo acesso ao banco: preços, clientes, histórico de compras, métricas do território, cadastro de cliente | `gpt-4.1-mini` |
| `inbox-manager` | Todo acesso ao e-mail: busca e lê mensagens, salva rascunhos de resposta | `gpt-4.1-mini` |
| `quote-reviewer` | Revisa itens, desconto e total de uma cotação antes de ela sair | `gpt-4.1` |
| `genre-researcher` | Pesquisa um gênero musical na web para a newsletter (só com `TAVILY_API_KEY`) | `gpt-4.1-mini` |

Delegar também é uma decisão de **custo**: cada subagent roda no próprio contexto
e devolve só o resultado. Trezentas linhas de SQL ficam no contexto do analyst,
não no histórico principal — que é reenviado ao modelo a cada turno.

Como agir em cada tipo de pedido está em playbooks que o agent lê sob demanda,
em `skills/`: `rfq-quote` (cotação), `territory-report` (relatório com gráfico) e
`weekly-newsletter` (newsletter em HTML). As regras gerais ficam em `AGENTS.md`,
carregado como memória do agent.

### Persistência em duas camadas

Os dois conjuntos de tabelas vivem no mesmo Postgres, com papéis diferentes:

| Tabelas | Quem cria | Guarda | Quem lê |
|---|---|---|---|
| `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` | LangGraph, via `saver.setup()` no startup | Estado completo do agent por `thread_id`: mensagens, tool calls, interrupts pendentes | O agent |
| `conversations`, `messages` | Alembic (`migrations/`) | Título, timestamps e o texto legível das mensagens | A UI |

O checkpoint é a **memória** do agent; as tabelas de produto são o **feed de
leitura** da UI (listar 50 conversas sem desserializar 50 blobs). Um único
identificador atravessa as três camadas:
`session_id` (API) == `conversations.id` == `thread_id` (LangGraph).

Consequência prática: o cliente manda **só a mensagem nova**, nunca o histórico.
O checkpointer recarrega o resto. Conversas longas são compactadas
automaticamente pelo middleware de summarização do deepagents.

### Aprovação humana (interrupts)

`add_customer` e `mail_create_draft` exigem aprovação. Quando o agent chega numa
delas, o turno termina com `status: "interrupted"` e a thread fica parada no
checkpoint — pode esperar indefinidamente, inclusive atravessando um restart da
API. A conversa só continua quando o cliente chama `POST /chat/resume`.

---

## Como rodar

### Pré-requisitos

- Python 3.12 e [Poetry](https://python-poetry.org/)
- Docker (Docker Desktop no Windows) — para o Postgres
- `make` — no Windows: `winget install ezwinports.make`, e rode tudo pelo **Git Bash**
- Uma chave da OpenAI; opcionalmente uma da Tavily (pesquisa web da newsletter)

### Primeira vez

```bash
poetry install
cp .env.example .env        # preencha OPENAI_API_KEY
make stack                  # sobe TUDO, incluindo o front
```

`make stack` sobe o Postgres, espera ele aceitar conexão, aplica as migrations,
sobe o servidor de e-mail, sobe a API em background e deixa o Vite em
foreground. A ordem importa: o checkpointer precisa do banco no ar, e o mail
server precisa responder **antes** do agent ser compilado, senão as tools de
e-mail não entram no grafo.

- Front: http://localhost:5173
- API: http://127.0.0.1:8000
- Docs interativas (Swagger): http://127.0.0.1:8000/docs

`Ctrl+C` encerra só o Vite; use `make stack-stop` para derrubar o resto.

Se quiser só o backend (sem front), `make up` deixa a API em foreground com
`--reload`.

### Alvos do Makefile

| Alvo | O que faz |
|---|---|
| `make stack` / `make stack-stop` | Sobe **tudo** (com front) / derruba tudo |
| `make up` / `make down` | Só o backend / derruba mail server e Postgres |
| `make front` · `front-build` | Vite em foreground · build de produção |
| `make db-up` · `db-down` · `db-logs` · `psql` | Postgres (porta **5433** no host) |
| `make db-reset` | Para o Postgres e **apaga o volume** — perde conversas e checkpoints |
| `make migrate` | `alembic upgrade head` |
| `make revision m="..."` | Gera migration por autogenerate |
| `make mail` · `mail-stop` · `mail-logs` | Servidor MCP de e-mail (porta 5002) |
| `make api` | Só a API |
| `make smoke` | Bate em `/health` e abre uma conversa de teste |
| `make help` | Lista tudo |

A API sobe **sem** o servidor de e-mail — só perde as tools de mail. Útil para
trabalhar só com o banco.

### Testando o fluxo de aprovação

O servidor de e-mail é um mock offline. Para simular um cliente pedindo cotação:

```bash
poetry run python mcp/send_to_inbox.py          # carrega o RFQ de mcp/seeds/
poetry run python mcp/send_to_inbox.py --reset  # limpa a caixa antes
```

Depois peça ao agent para processar o pedido — ele vai parar pedindo aprovação
para salvar o rascunho de resposta (veja `/chat/resume` abaixo).

### Testando o fluxo de elicitation

Peça: **“agende um follow-up para a mensagem msg-1”**. Aqui a pausa é de outro
tipo: o servidor MCP interrompe *de dentro* da tool para perguntar em quantos
dias, e o front monta o formulário a partir do JSON Schema que veio na pergunta.

As duas pausas são mecanismos diferentes — `interrupt_on` (LangGraph, antes da
tool, durável no Postgres) contra elicitation (MCP, dentro da tool, em memória).
A comparação completa está em [`frontend/README.md`](frontend/README.md).

---

## Endpoints

| Método e rota | Descrição |
|---|---|
| `POST /chat` | Envia uma mensagem e espera a resposta completa. Sem `session_id` = conversa nova |
| `POST /chat/stream` | O mesmo, em Server-Sent Events |
| `POST /chat/resume` | Responde a um interrupt: `approve`, `edit` ou `reject` |
| `POST /conversations` | Cria uma conversa vazia |
| `GET /conversations` | Lista conversas, mais recentes primeiro (`?limit=50&offset=0`) |
| `GET /conversations/{id}` | Conversa + histórico de mensagens |
| `DELETE /conversations/{id}` | Apaga a conversa **e** o checkpoint dela |
| `GET /health` | Liveness probe |

### Conversa nova e continuação

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "Quantos clientes temos no Brasil?"}'
```

```json
{
  "session_id": "3f2b1c9e-...",
  "status": "completed",
  "reply": "Temos 5 clientes no Brasil ...",
  "message_id": 2,
  "interrupt": null
}
```

Para continuar, mande o `session_id` recebido e **só a mensagem nova**:

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "message": "E no Canadá?"}'
```

Um `session_id` que não existe devolve `404`.

### Streaming

```bash
curl -N localhost:8000/chat/stream -H 'content-type: application/json' \
  -d '{"message": "Resuma as vendas por gênero"}'
```

Eventos, nesta ordem:

| Evento | Dados | Quando |
|---|---|---|
| `session` | `{session_id, title}` | Sempre o primeiro |
| `tool` | `{name}` | O agent chamou uma tool — útil para mostrar "consultando o banco..." |
| `token` | `{content}` | Pedaço do texto da resposta |
| `interrupt` | `{interrupt}` | Parou pedindo aprovação humana |
| `done` | `{status, message_id}` | Fim do turno |
| `error` | `{detail}` | Falhou no meio do stream |

Por padrão só os tokens do agent principal são emitidos; `STREAM_SUBAGENT_TOKENS=true`
inclui os dos subagents.

### Respondendo a um interrupt

Quando `status` vem `"interrupted"`, o campo `interrupt` traz a ação que o agent
quer executar. Responda com uma de três decisões:

```bash
# aprovar como está
curl -s localhost:8000/chat/resume -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "decision": "approve"}'

# aprovar com argumentos corrigidos — args traz o nome da tool e os novos argumentos
curl -s localhost:8000/chat/resume -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "decision": "edit",
       "args": {"name": "mail_create_draft",
                "args": {"to": "cliente@exemplo.com", "subject": "Sua cotação", "body": "..."}}}'

# recusar, explicando o motivo ao agent
curl -s localhost:8000/chat/resume -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "decision": "reject",
       "message": "O desconto não pode passar de 10%."}'
```

A resposta tem o mesmo formato de `POST /chat` — e pode vir `interrupted` de novo,
se o agent precisar de outra aprovação na sequência.

---

## Configuração

Variáveis lidas do ambiente, com o `.env` como fallback (modelo em `.env.example`):

| Variável | Padrão | Para quê |
|---|---|---|
| `OPENAI_API_KEY` | — | Obrigatória. Modelos do agent e dos subagents |
| `TAVILY_API_KEY` | — | Opcional. Sem ela o `genre-researcher` fica desligado |
| `DATABASE_URL` | `postgresql://sales:sales@localhost:5433/sales_assistant` | Postgres do checkpointer e das tabelas de produto |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Onde a API escuta |
| `CORS_ORIGINS` | `["http://localhost:3000","http://localhost:5173"]` | Origens liberadas (lista JSON) |
| `MAIL_SERVER_URL` | `http://127.0.0.1:5002/mcp` | Servidor MCP de e-mail |
| `STREAM_SUBAGENT_TOKENS` | `false` | Emitir tokens dos subagents no streaming |
| `RECURSION_LIMIT` | `50` | Máximo de passos do grafo por turno — trava contra o agent entrar em loop |

---

## Estrutura

```
agent.py            Monta o agent principal — build_agent(checkpointer=...)
subagents.py        Os quatro especialistas e quais tools exigem aprovação
models.py           Modelos de LLM (gpt-4.1-mini e gpt-4.1)
tools/              sql.py, chart.py, html.py, search.py
skills/             Playbooks que o agent lê sob demanda
AGENTS.md           Manual de operação, carregado como memória do agent
data/chinook.db     Banco de negócio (SQLite)
mcp/                Servidor de e-mail falso + send_to_inbox.py para simular mensagens
outputs/            Onde o agent grava entregáveis (cotações, relatórios, newsletters)

api/
  main.py           App FastAPI; o lifespan cria e fecha os recursos caros
  agent_runtime.py  Dono do grafo compilado e do pool do checkpointer
  routers/          chat.py e conversations.py
  service.py        Executa um turno (inteiro ou em streaming) e trata interrupts
  schemas.py        Contratos HTTP (Pydantic), separados dos models do banco
  models.py         Tabelas de produto (SQLAlchemy)
  repository.py     Acesso a conversations/messages
  config.py         Settings lidas do ambiente

migrations/         Alembic — só as tabelas de produto; as do LangGraph ficam de fora
```

---

## Problemas comuns

**`Psycopg cannot use the 'ProactorEventLoop'`** — o psycopg 3 async não funciona
com o event loop padrão do Windows. O Alembic já força o loop compatível
(`migrations/env.py`), e o uvicorn usa o loop certo quando roda com `--reload`,
que é o que `make api` faz. Se você rodar o uvicorn na mão no Windows, mantenha o
`--reload` (ou passe `--workers 2`).

**`make: command not found` logo depois de instalar** — o terminal aberto não
recarregou o `PATH`. Feche e abra o Git Bash de novo.

**`make db-up` falha ao conectar no Docker** — o Docker Desktop não está rodando.

**Conversas somem depois de reiniciar** — elas vivem no volume do Docker.
`make down` preserva; só `make db-reset` (`docker compose down -v`) apaga. Apontar
`DATABASE_URL` para outro banco também faz as conversas "sumirem": os `thread_id`
simplesmente não existem lá.
