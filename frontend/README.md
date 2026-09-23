# Frontend — como construir uma interface que conversa com um agent

React + Vite + TypeScript sobre a API conversacional (`api/`). Sem biblioteca de
estado, sem framework de UI, sem cliente HTTP — só `fetch`, `useState` e CSS,
para que nada esconda o que interessa aprender aqui.

---

## 1. O que muda quando o backend é um agent

Uma UI de chat comum manda uma mensagem e espera uma resposta. Com um agent, três
coisas são diferentes — e cada uma força uma decisão de implementação:

| O que acontece | Por quê | O que a UI precisa |
|---|---|---|
| A resposta demora **dezenas de segundos** | o agent delega, consulta o banco, revisa, redige | **streaming** de tokens |
| Ele trabalha **em silêncio** boa parte do tempo | o trabalho acontece dentro de subagents | **rastro de tools** na tela |
| Ele pode **parar e te perguntar** no meio | aprovação humana, ou falta de um dado | **painéis de pausa** |

O terceiro é o que realmente distingue esta UI de um chat qualquer — e são
**duas** formas de parar, não uma. É o assunto da seção 6.

---

## 2. Estrutura do projeto

```
src/
  types.ts        Os contratos da API em TypeScript. Nada é inventado aqui:
                  vem de api/schemas.py e do HumanInTheLoopMiddleware.

  api.ts          Única camada que fala HTTP. Wrappers REST + o parser de SSE.
                  Nenhum componente chama fetch diretamente.

  App.tsx         Orquestração: todo o estado da conversa e UM consumidor de
                  stream usado por todos os turnos.

  components/
    Sidebar.tsx         lista/cria/apaga conversas
    MessageList.tsx     bolhas + chips de tools; auto-scroll
    Composer.tsx        caixa de digitar (desabilita durante pausa)
    InterruptPanel.tsx  pausa 1 — aprovação (approve/edit/reject/respond)
    ElicitPanel.tsx     pausa 2 — formulário gerado de um JSON Schema

  styles.css      CSS puro, um arquivo.
```

**A regra de organização:** `api.ts` é a única fronteira com a rede, `App.tsx` é
o único dono do estado, e os componentes são burros — recebem props e disparam
callbacks. Um painel de aprovação não sabe o que é HTTP.

---

## 3. Os endpoints que consumimos, e onde

| Endpoint | Wrapper em `api.ts` | Chamado em | Quando |
|---|---|---|---|
| `GET /conversations` | `listConversations` | `App.refreshList` | ao abrir e ao fim de cada turno |
| `GET /conversations/{id}` | `getConversation` | `App.openConversation` | ao clicar numa conversa |
| `DELETE /conversations/{id}` | `deleteConversation` | `App.removeConversation` | no × da sidebar |
| `POST /chat/stream` | `streamChat` | `App.send` | mandar mensagem |
| `POST /chat/resume/stream` | `streamResume` | `App.decide` | responder a um **interrupt** |
| `POST /chat/elicit` | `answerElicitation` | `App.answerElicitation` | responder a uma **elicitation** |

Dois endpoints da API que **não** usamos, de propósito:

- **`POST /chat`** (sem streaming) — devolve só a resposta pronta. É o certo para
  integração máquina-a-máquina (webhook, job, outro serviço). Numa UI seria um
  spinner de 40 segundos.
- **`POST /conversations`** — cria uma conversa vazia. Não precisamos: mandar uma
  mensagem sem `session_id` já cria a conversa e devolve o id no primeiro frame.
  "Nova conversa" aqui é só limpar a tela.

---

## 4. O caminho de uma mensagem, ponta a ponta

Este é o fluxo que vale entender; o resto do arquivo detalha as partes.

1. **`Composer`** chama `onSend(texto)` (Enter envia, Shift+Enter quebra linha).
2. **`App.send`** adiciona a bolha do usuário na hora — otimista, sem esperar o
   servidor — e marca `busy`.
3. **`api.streamChat(texto, sessionRef.current)`** faz `POST /chat/stream`. Se
   `session_id` for `null`, a API abre uma conversa nova.
4. **`readSse`** transforma o corpo da resposta num `AsyncGenerator` de eventos
   tipados.
5. **`App.consume`** cria uma bolha vazia de assistente (para o cursor piscar
   antes do primeiro token) e itera os eventos, aplicando cada um ao estado.
6. Chega `session` → guarda o id. Chegam `token` → concatena no texto. Chegam
   `tool` → adiciona chips.
7. Se chegar `interrupt` ou `elicitation` → abre o painel correspondente.
8. Chega `done` → tira o cursor, libera o `busy`, recarrega a sidebar (o título
   da conversa foi definido pela primeira mensagem, no servidor).

---

## 5. Por que streaming na conversa

Três razões, em ordem de peso:

**1. Elicitation só existe no stream.** A mais decisiva e a menos óbvia. Em
`api/elicitation.py` o servidor responde:

```python
channel = _channel.get()
if channel is None:
    return ElicitResult(action="decline")
```

O `POST /chat` não tem canal para entregar uma pergunta feita no meio da
execução — ele só devolve o resultado final. Por ele, a tool que usa elicitation
**sempre** degradaria para "não informado". Usando `/chat/stream`, a pergunta
sobe como um frame.

**2. Latência percebida.** Um turno leva dezenas de segundos. Sem streaming o
usuário não sabe se está funcionando ou travado.

**3. Visibilidade.** `/chat` devolve só o texto final; você perde o rastro de
tools, que é o que mostra o agent trabalhando.

**O que NÃO é motivo: aprovação.** Interrupt funciona perfeitamente sem
streaming — o `POST /chat` retorna `status: "interrupted"` com o payload. Se
você só quisesse o gate de aprovação, viveria sem stream.

### Como lemos o SSE

O browser tem `EventSource`, a API nativa de SSE — e ela **só faz GET**, sem
corpo e sem headers. Nossos turnos são POST com JSON. Então lemos o
`ReadableStream` do `fetch` e destrinchamos o formato, que é simples:

```
event: token\n
data: {"content":"Olá"}\n
\n                          <- a linha EM BRANCO fecha o frame
```

**A armadilha:** um chunk da rede não respeita fronteira de frame — ele pode
terminar no meio de um JSON. Por isso o parser guarda o resto num buffer
(`api.ts`, `readSse`):

```ts
buffer += decoder.decode(value, { stream: true });
let split: number;
while ((split = buffer.indexOf("\n\n")) !== -1) {
  const raw = buffer.slice(0, split);
  buffer = buffer.slice(split + 2);   // o resto espera o próximo chunk
  // ... parseia "event:" e "data:"
}
```

O que se perde ao abrir mão do `EventSource`: reconexão automática e
`Last-Event-ID`. Num chat isso é bom — reconectar sozinho geraria uma resposta
**duplicada**, não uma continuação.

### Os eventos

| Evento | Payload | O que a UI faz |
|---|---|---|
| `session` | `{session_id, title}` | grava o id (sempre o 1º frame) |
| `token` | `{content}` | concatena na bolha do assistente |
| `tool` | `{name}` | adiciona um chip |
| `interrupt` | `{interrupt: [...]}` | abre o `InterruptPanel` |
| `elicitation` | `{elicitation_id, message, schema}` | abre o `ElicitPanel` |
| `elicitation_closed` | `{elicitation_id}` | fecha o `ElicitPanel` |
| `done` | `{status, message_id}` | fim do turno |
| `error` | `{detail}` | faixa de erro |

Você verá chips de `task` e não de `query_chinook`: as tools dos especialistas
rodam **dentro** dos subagents. É proposital — é o mesmo isolamento que torna o
gate de aprovação incontornável.

---

## 6. Interrupt × Elicitation — e por que não dá para tratar igual

As duas param o agent e mostram uma pergunta. Por baixo, não têm nada em comum.

| | **Interrupt (HITL)** | **Elicitation (MCP)** |
|---|---|---|
| De quem é o conceito | LangGraph, no cliente | protocolo MCP, no servidor |
| Quando pausa | **antes** da tool rodar | **dentro** da tool, já rodando |
| Quem decidiu pausar | quem montou o agent (`interrupt_on`) | quem escreveu o servidor MCP |
| Onde fica o estado | checkpoint no Postgres | memória do processo da API |
| O stream do turno | **fechou** (`done: interrupted`) | continua **aberto** |
| Como responder | `POST /chat/resume/stream` | `POST /chat/elicit` |
| Responder… | abre um stream **novo** | **não** abre stream nenhum |
| Sobrevive a restart | **sim** | **não** |

**A diferença que a UI é obrigada a respeitar** é a linha do stream.

No interrupt, o turno **acabou**. O agent gravou o estado e desligou; para
continuar é preciso pedir um turno novo — daí `resume/stream` devolver um stream
e `App.decide` chamar `consume()` de novo.

Na elicitation, o turno **não acabou**. O grafo está parado dentro de uma tool
call, e aquele mesmo stream continua aberto esperando a resposta sair por ele.
Por isso `App.answerElicitation` é só um POST:

```ts
await api.answerElicitation(id, action, content);
setPending(null);
// e pronto — NÃO chamamos consume(). O stream original já está rodando.
```

> **O erro clássico:** tratar as duas igual. Abrir um stream novo ao responder
> uma elicitation **duplica o turno**, porque o original nunca terminou — você
> veria a resposta sendo escrita duas vezes.

Na prática, o estado da UI modela isso num tipo só (`types.ts`), porque
visualmente são a mesma coisa, mas quem responde é código diferente:

```ts
export type Pending =
  | { kind: "interrupt"; interrupts: Interrupt[] }
  | { kind: "elicitation"; elicitation: Elicitation }
  | null;
```

### 6.1 Interrupt — o painel de aprovação

Dispara em `mail_create_draft` e `add_customer`. O `value` vem do
`HumanInTheLoopMiddleware`:

```jsonc
{
  "action_requests": [
    { "name": "mail_create_draft", "args": { "to": "...", "body": "..." } }
  ],
  "review_configs": [
    { "action_name": "mail_create_draft",
      "allowed_decisions": ["approve", "edit", "reject"] }
  ]
}
```

Quatro decisões possíveis:

| `type` | O que acontece com a tool | Campo usado |
|---|---|---|
| `approve` | roda como o modelo pediu | — |
| `edit` | roda com os args que você corrigir | `name` + `args` |
| `reject` | **não** roda; o modelo recebe o motivo | `message` |
| `respond` | **não** roda; sua resposta volta como se fosse o retorno dela | `message` |

O painel renderiza só os botões que estão em `allowed_decisions` — quem manda é
o servidor, não a UI. Hoje `respond` não aparece porque nenhum gate o habilita.

**A regra que não dá para errar:** `action_requests` é uma **lista** (o modelo
pode pedir duas tool calls no mesmo passo) e o middleware exige **uma decisão por
ação, na mesma ordem**, falhando se o número não bater. Por isso o componente
guarda um rascunho por índice:

```ts
const [drafts, setDrafts] = useState<Record<number, Draft>>({});
const ready = actions.every((_, i) => drafts[i] !== undefined);
```

…e só libera o envio quando todas estão preenchidas.

### 6.2 Elicitation — formulário gerado do schema

Dispara em `mail_schedule_followup`. O front **não sabe o que é um follow-up**:
ele monta o formulário a partir do schema que veio junto da pergunta.

```ts
const fields = Object.entries(elicitation.schema?.properties ?? {});
```

Isso é possível porque a spec do MCP restringe o `requestedSchema` a objetos
**planos de campos primitivos** (string, number, integer, boolean, enum) —
justamente para que qualquer cliente consiga desenhar a tela sem conhecer a tool.

Um detalhe de tipo que morde: valores de `<input>` são **sempre string**. Mandar
`"7"` onde o schema pede `integer` faz o pydantic do outro lado rejeitar. Daí o
`coerce` no fim do arquivo.

Três respostas possíveis — `accept` (com os campos), `decline` (recusa) e
`cancel` (dispensou/expirou). As três voltam **para dentro da tool**, que decide
o que fazer: no nosso caso ela devolve `followup_decline` e o agent conta isso ao
usuário. **Uma elicitation recusada é um desfecho normal, não um erro.**

---

## 7. O estado da UI

Quatro peças em `App.tsx`, e nenhuma biblioteca:

```ts
const [messages, setMessages]   = useState<ChatMessage[]>([]);  // a conversa
const [pending, setPending]     = useState<Pending>(null);      // a pausa atual
const [busy, setBusy]           = useState(false);              // turno rodando
const [conversations, setConvs] = useState<Conversation[]>([]); // a sidebar
```

Duas decisões que valem copiar:

**Um `consume()` só para todos os turnos.** Turno novo e retomada produzem
exatamente os mesmos eventos; só muda quem criou o stream. Duplicar essa função
seria duplicar o tratamento de oito eventos.

**O `session_id` vive numa ref, não só no state:**

```ts
const sessionRef = useRef<string | null>(null);
```

Dentro de `consume` o valor do state estaria **congelado no render em que o
stream começou** (stale closure). Com o state, a segunda mensagem da conversa
criaria uma conversa nova em vez de continuar a existente — um bug clássico e
difícil de enxergar.

---

## 8. Rodando

```bash
make stack        # na raiz: Postgres, MCP, API e este front
```

Ou só o front, com a API já no ar:

```bash
npm install
npm run dev       # http://localhost:5173
npm run build     # typecheck (tsc -b) + bundle
```

O `vite.config.ts` redireciona `/api` → `127.0.0.1:8000`. **Por que proxy e não
chamar `:8000` direto?** Porque origens diferentes acionam CORS e preflight, e a
primeira coisa que quebra nesse arranjo é o streaming. Com o proxy, o browser vê
tudo na mesma origem — que é também como se faz em produção, com nginx servindo
os estáticos e repassando `/api`.

> O Vite escuta em `localhost` (IPv6 `[::1]`). Testando com `curl`, use
> `http://localhost:5173` — `http://127.0.0.1:5173` não conecta.

### Prompts para exercitar cada caminho

A caixa de entrada vem semeada com `msg-1` (um pedido de cotação).

| Caminho | Prompt |
|---|---|
| Sem pausa | *Quantos clientes a Jane atende e em quais países?* |
| **Elicitation** | *Agende um follow-up para a mensagem msg-1 da caixa de entrada.* |
| **Interrupt** (draft) | *Leia a mensagem msg-1 e salve um rascunho agradecendo e avisando que a cotação sai amanhã.* |
| **Interrupt** (cliente) | *Cadastre um novo cliente: Morgan Vale, morgan.vale@northern-lights-cafes.example, Toronto, Canadá.* |
| **Várias ações numa pausa** | *Cadastre dois clientes: Ana Lima (ana.lima@exemplo.com) e Bruno Sá (bruno.sa@exemplo.com).* |

---

## 9. Limites conhecidos

1. **Recarregar a página com uma pausa na tela perde o painel.** O histórico vem
   da tabela `messages`, que não guarda interrupts. O caminho seria ler
   `graph.aget_state(config)` num endpoint novo — e funcionaria só para o
   interrupt; a elicitation não sobrevive mesmo.
2. **Sem autenticação.** A sidebar lista as conversas de todos.
3. **Respostas em texto puro.** O agent escreve markdown; mostramos com
   `white-space: pre-wrap`. Um `react-markdown` resolve.
4. **Não dá para cancelar um turno.** O `AbortController` já existe e o servidor
   cancela o grafo quando o cliente some — falta só o botão.
5. **Erro é uma faixa global**, não um estado por mensagem, e não há retry.
