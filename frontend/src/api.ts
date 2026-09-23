/**
 * Cliente HTTP da API conversacional.
 *
 * Por que não usamos `EventSource`, que é a API nativa de SSE do browser:
 * ela só sabe fazer **GET**, sem corpo e sem headers. Nossos turnos são POST
 * com JSON. Então lemos o `ReadableStream` do `fetch` e destrinchamos o
 * formato SSE à mão — que é pouca coisa, como dá para ver em `readSse`.
 *
 * O que se perde ao abrir mão do EventSource: a reconexão automática e o
 * `Last-Event-ID`. Para um turno de chat isso não faz falta — se a conexão
 * cair no meio, reconectar sozinho geraria uma resposta duplicada, não uma
 * continuação. O estado de verdade está no checkpoint do servidor.
 */

import type {
  Conversation,
  ConversationDetail,
  DecisionOut,
  StreamEvent,
} from "./types";

/**
 * Em dev isto é `/api`, que o Vite redireciona para a API (ver vite.config.ts).
 * Assim o browser fala com uma origem só e CORS deixa de existir no caminho.
 */
const BASE = import.meta.env.VITE_API_URL ?? "/api";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`${response.status} ${response.statusText} — ${detail}`);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

// ------------------------------------------------------------- conversas

export const listConversations = () => json<Conversation[]>("/conversations");

export const getConversation = (id: string) =>
  json<ConversationDetail>(`/conversations/${id}`);

export const deleteConversation = (id: string) =>
  json<void>(`/conversations/${id}`, { method: "DELETE" });

// --------------------------------------------------------- parser de SSE

/**
 * Transforma o corpo da resposta numa sequência de eventos tipados.
 *
 * O formato na rede é:
 *
 *     event: token\n
 *     data: {"content":"Olá"}\n
 *     \n                          <- a linha EM BRANCO fecha o frame
 *
 * O detalhe que quebra implementações ingênuas: um `chunk` do stream não
 * respeita fronteira de frame. Ele pode terminar no meio de um JSON. Por isso
 * guardamos o resto num `buffer` e só processamos o que já tem `\n\n`.
 */
async function* readSse(response: Response): AsyncGenerator<StreamEvent> {
  if (!response.body) throw new Error("Resposta sem corpo — streaming indisponível.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const raw = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);

        let event = "message";
        const dataLines: string[] = [];
        for (const line of raw.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
          // linhas que começam com ":" são comentários/keep-alive — ignoradas
        }
        if (!dataLines.length) continue;

        try {
          yield { event, data: JSON.parse(dataLines.join("\n")) } as StreamEvent;
        } catch {
          // Um frame corrompido não deve derrubar o turno inteiro.
          console.warn("Frame SSE ilegível, ignorado:", raw);
        }
      }
    }
  } finally {
    // Garante que o servidor perceba a desconexão se sairmos no meio (o
    // `finally` do stream_turn cancela a task do grafo quando isso acontece).
    reader.cancel().catch(() => {});
  }
}

// -------------------------------------------------------------- turnos

async function postStream(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<AsyncGenerator<StreamEvent>> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`${response.status} ${response.statusText} — ${detail}`);
  }
  return readSse(response);
}

/** Manda uma mensagem. Sem `sessionId`, a API abre uma conversa nova. */
export const streamChat = (
  message: string,
  sessionId: string | null,
  signal?: AbortSignal,
) =>
  postStream(
    "/chat/stream",
    { message, session_id: sessionId ?? undefined },
    signal,
  );

/**
 * Responde a um interrupt e RETOMA o turno — por isso devolve um stream novo:
 * depois de aprovar, o agent ainda tem trabalho pela frente e a resposta dele
 * precisa aparecer sendo escrita, como num turno normal.
 */
export const streamResume = (
  sessionId: string,
  decisions: DecisionOut[],
  signal?: AbortSignal,
) =>
  postStream(
    "/chat/resume/stream",
    { session_id: sessionId, decisions },
    signal,
  );

/**
 * Responde a uma elicitation do MCP.
 *
 * Repare que NÃO devolve stream nenhum: o turno original continua aberto e é
 * por ele que a resposta vai seguir saindo. Aqui só destravamos a tool call que
 * está suspensa do outro lado. É a diferença prática entre as duas pausas.
 */
export const answerElicitation = (
  elicitationId: string,
  action: "accept" | "decline" | "cancel",
  content?: Record<string, unknown>,
) =>
  json<{ status: string }>("/chat/elicit", {
    method: "POST",
    body: JSON.stringify({
      elicitation_id: elicitationId,
      action,
      content: content ?? null,
    }),
  });
