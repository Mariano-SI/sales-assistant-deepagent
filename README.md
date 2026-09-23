# Chinook Sales Assistant

Assistente de vendas construído com [deepagents](https://github.com/langchain-ai/deepagents)
sobre LangGraph, exposto como uma API HTTP conversacional e uma interface de
chat em React. Ele trabalha para **Jane Peacock**, representante de vendas da
Chinook (uma distribuidora de música fictícia): responde pedidos de cotação que
chegam por e-mail, consulta e cadastra clientes, gera relatórios do território e
monta a newsletter semanal.

O agent assiste — a Jane decide. Toda ação com efeito externo (salvar um rascunho
de e-mail, cadastrar um cliente) **pausa a conversa** e espera aprovação humana.
E há uma segunda forma de pausa, vinda do protocolo MCP, em que o próprio
servidor interrompe a execução para perguntar algo — as duas são comparadas
[mais abaixo](#as-duas-formas-de-o-agent-parar-e-perguntar).

---

## Como funciona

```
  ┌─ frontend/ (React+Vite :5173) ─┐
  │  chat, sidebar, painéis de     │   SSE (tokens, tools, pausas)
  │  aprovação e de elicitation    │◄──────────────┐
  └────────────┬───────────────────┘               │
               │ POST /chat/stream                 │
               │      /chat/resume/stream          │
               │      /chat/elicit                 │
               ▼                                   │
  ┌──────────────────────── FastAPI (api/) ────────┴───────────────┐
  │  grafo compilado (um só, criado no startup)                    │
  │  agent principal  ──task──►  subagents                         │
  └────────┬───────────────────────────┬──────────────────┬────────┘
           │                           │                  │
┌──────────▼───────────┐   ┌───────────▼──────────┐   ┌───▼───────────┐
│ Postgres             │   │ data/chinook.db      │   │ mail server   │
│  checkpoints (agent) │   │ (SQLite, negócio)    │   │ MCP :5002     │
│  conversations/msgs  │   └──────────────────────┘   └───────────────┘
└──────────────────────┘
```

O front não fala com nada além da API, e a API é o único processo que fala com
o Postgres, com o SQLite e com o servidor MCP.

### O agent

O agent principal (`agent.py`) **coordena**; o trabalho estreito fica com
especialistas, e dois deles são o único caminho até um sistema externo:

| Subagent | Faz | Modelo |
|---|---|---|
| `chinook-analyst` | Todo acesso ao banco: preços, clientes, histórico de compras, métricas do território, cadastro de cliente | `gpt-4.1-mini` |
| `inbox-manager` | Todo acesso ao e-mail: busca e lê mensagens, salva rascunhos de resposta, agenda follow-ups | `gpt-4.1-mini` |
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

### As duas formas de o agent parar e perguntar

O agent pode suspender à espera de um humano por **dois mecanismos distintos**,
e confundi-los é a principal armadilha deste projeto.

| | **Interrupt (HITL)** | **Elicitation (MCP)** |
|---|---|---|
| De quem é o conceito | LangGraph, no cliente | protocolo MCP, no servidor |
| Quando pausa | **antes** de a tool rodar | **dentro** dela, já rodando |
| Quem decidiu pausar | quem montou o agent (`interrupt_on`) | quem escreveu o servidor MCP |
| Onde fica o estado | checkpoint no Postgres | memória do processo da API |
| O turno | **termina** (`status: "interrupted"`) | continua **aberto** |
| Como responder | `POST /chat/resume[/stream]` | `POST /chat/elicit` |
| Sobrevive a um restart | **sim** | **não** |

**Interrupt** — `add_customer` e `mail_create_draft` exigem aprovação. A thread
fica parada no checkpoint e pode esperar indefinidamente, inclusive atravessando
um restart da API. É aqui que o checkpointer mostra serviço.

**Elicitation** — `mail_schedule_followup` recebe só o id da mensagem e, no meio
da execução, pergunta à Jane em quantos dias fazer o follow-up. A tool fica
suspensa num `await ctx.elicit(...)` dentro do servidor MCP; a API segura a
espera num `asyncio.Future` (`api/elicitation.py`) e emite a pergunta pelo
stream. Por isso ela **só funciona em `/chat/stream`**: no `POST /chat` não há
canal para entregá-la, e a resposta é `decline` — o que é o correto num job ou
webhook, onde não há ninguém para perguntar.

A comparação detalhada, com o que cada uma exige da UI, está em
[`frontend/README.md`](frontend/README.md).

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
| `POST /chat/resume` | Responde a um interrupt, resposta completa |
| `POST /chat/resume/stream` | Responde a um interrupt e retoma **em streaming** — é o que a UI usa |
| `POST /chat/elicit` | Responde a uma elicitation em voo (`202`, ou `409` se já expirou) |
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
| `interrupt` | `{interrupt}` | Parou pedindo aprovação humana — o turno **termina** logo depois |
| `elicitation` | `{elicitation_id, tool, message, schema}` | O servidor MCP está perguntando — o turno **continua** |
| `elicitation_closed` | `{elicitation_id}` | A pergunta foi respondida ou expirou |
| `done` | `{status, message_id}` | Fim do turno |
| `error` | `{detail}` | Falhou no meio do stream |

Por padrão só os tokens do agent principal são emitidos; `STREAM_SUBAGENT_TOKENS=true`
inclui os dos subagents.

### Respondendo a um interrupt

Quando `status` vem `"interrupted"`, o campo `interrupt` traz as ações que o
agent quer executar:

```jsonc
{"interrupt": [{"id": "8f3c…", "value": {
  "action_requests": [{"name": "add_customer", "args": {"first_name": "Ana", …}}],
  "review_configs":  [{"action_name": "add_customer",
                       "allowed_decisions": ["approve", "edit", "reject"]}]}}]}
```

**`decisions` é uma lista, e isso não é estilo — é exigência.**
`action_requests` também é lista: o modelo pode pedir várias tool calls no mesmo
passo, e o middleware exige **uma decisão por ação, na mesma ordem**, levantando
`ValueError` se o número não bater.

```bash
# aprovar como está
curl -s localhost:8000/chat/resume -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "decisions": [{"type": "approve"}]}'

# aprovar com argumentos corrigidos
curl -s localhost:8000/chat/resume -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "decisions": [
        {"type": "edit", "name": "mail_create_draft",
         "args": {"to": "cliente@exemplo.com", "subject": "Sua cotação", "body": "..."}}]}'

# recusar, explicando o motivo ao agent
curl -s localhost:8000/chat/resume -H 'content-type: application/json' \
  -d '{"session_id": "3f2b1c9e-...", "decisions": [
        {"type": "reject", "message": "O desconto não pode passar de 10%."}]}'
```

Há um quarto tipo, `respond`: a tool **não** roda e o texto volta ao modelo como
se fosse o retorno dela — para tools do tipo "pergunte ao usuário". Nenhum gate
do projeto o habilita hoje (`subagents.py`), e habilitá-lo numa tool de escrita
seria arriscado: o modelo acreditaria que o cadastro aconteceu sem que nenhuma
linha fosse gravada.

A resposta tem o mesmo formato de `POST /chat` — e pode vir `interrupted` de novo,
se o agent precisar de outra aprovação na sequência.

### Respondendo a uma elicitation

Chega pelo evento `elicitation`, **sem** encerrar o turno. Responder não retoma
nada: só destrava a tool que está suspensa do outro lado.

```bash
curl -s localhost:8000/chat/elicit -H 'content-type: application/json' \
  -d '{"elicitation_id": "8561ec5f-...", "action": "accept",
       "content": {"days": 7, "note": "cobrar retorno da cotação"}}'
```

`action` aceita `accept`, `decline` e `cancel` — os três voltam para dentro da
tool, que decide o que fazer. Uma elicitation recusada é um **desfecho normal**,
não um erro. O `409` significa que a pergunta já expirou (`ELICITATION_TIMEOUT`)
ou já foi respondida.

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
| `ELICITATION_TIMEOUT` | `300` | Segundos que uma elicitation espera por resposta. Precisa existir: essa pausa vive na memória, não no checkpoint — sem timeout, fechar a aba deixaria a tool call aberta para sempre |

O front não precisa de variável nenhuma: o Vite faz proxy de `/api` para a API
(`frontend/vite.config.ts`). O `CORS_ORIGINS` fica como rede de segurança, para
quem preferir apontar `VITE_API_URL` direto.

---

## Estrutura

```
agent.py            Monta o agent principal — build_agent(checkpointer=..., on_elicitation=...)
subagents.py        Os quatro especialistas e quais tools exigem aprovação
models.py           Modelos de LLM (gpt-4.1-mini e gpt-4.1)
run_api.py          Launcher da API — escolhe o event loop certo no Windows
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
  elicitation.py    Ponte entre o callback de elicitation do MCP e o browser
  schemas.py        Contratos HTTP (Pydantic), separados dos models do banco
  models.py         Tabelas de produto (SQLAlchemy)
  repository.py     Acesso a conversations/messages
  config.py         Settings lidas do ambiente

frontend/           Chat em React + Vite + TS (ver frontend/README.md)
  src/api.ts        Única camada que fala HTTP: REST + parser de SSE
  src/App.tsx       Estado da conversa e o consumidor de stream
  src/components/   Sidebar, mensagens, e os dois painéis de pausa

migrations/         Alembic — só as tabelas de produto; as do LangGraph ficam de fora
```

---

## Problemas comuns

**`Psycopg cannot use the 'ProactorEventLoop'`** — o psycopg 3 async não funciona
com o event loop padrão do Windows. Os alvos do Makefile já resolvem: o Alembic
força o loop compatível (`migrations/env.py`) e a API sobe por `run_api.py`.

Se você chamar `uvicorn` na mão no Windows, vai bater nisso — a API morre no
boot com `PoolTimeout` ao abrir o pool do checkpointer. A causa está em
`uvicorn/loops/asyncio.py`: sem `--reload` ele escolhe `ProactorEventLoop`; com
`--reload` cai no `SelectorEventLoop` e funciona *por acidente*. Use
`python run_api.py` (com ou sem `--reload`) em vez do uvicorn direto.

**O agent diz que não consegue ver e-mail / a tool de follow-up não existe** — o
servidor MCP não estava no ar quando a API subiu. `build_agent()` descobre as
tools MCP no startup; se ele não responde, o agent compila sem elas e a API sobe
assim mesmo, deixando só um `WARNING` no log. Suba o mail server (`make mail`) e
**reinicie a API**.

**O agent pede confirmação em prosa e nada é criado** — o `AGENTS.md` instrui a
chamar a tool diretamente, porque a pausa de aprovação *é* o pedido de
permissão. O modelo às vezes ignora isso e responde "vou enviar para a Jane
aprovar" sem chamar tool nenhuma. Responder "confirmo" costuma destravar.

**`make: command not found` logo depois de instalar** — o terminal aberto não
recarregou o `PATH`. Feche e abra o Git Bash de novo.

**`make db-up` falha ao conectar no Docker** — o Docker Desktop não está rodando.

**Conversas somem depois de reiniciar** — elas vivem no volume do Docker.
`make down` preserva; só `make db-reset` (`docker compose down -v`) apaga. Apontar
`DATABASE_URL` para outro banco também faz as conversas "sumirem": os `thread_id`
simplesmente não existem lá.
