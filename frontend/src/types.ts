/**
 * Os contratos que a API fala, em TypeScript.
 *
 * Nada aqui é inventado: os nomes vêm de `api/schemas.py` (nosso) e do
 * `HumanInTheLoopMiddleware` do LangChain (o formato do interrupt). Se um dia
 * divergirem, o lugar de olhar é http://127.0.0.1:8000/docs.
 */

// ---------------------------------------------------------------- conversas

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ApiMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ApiMessage[];
}

// --------------------------------------------------- pausa 1: interrupt HITL

/** Uma tool call que o modelo quis fazer e que precisa de aval humano. */
export interface ActionRequest {
  name: string;
  args: Record<string, unknown>;
  description?: string;
}

export type DecisionType = "approve" | "edit" | "reject" | "respond";

/** Quais decisões são permitidas para uma determinada tool. */
export interface ReviewConfig {
  action_name: string;
  allowed_decisions: DecisionType[];
}

/**
 * O `value` de um interrupt. Repare que `action_requests` é uma LISTA: o
 * modelo pode pedir várias tool calls no mesmo passo, e o middleware exige
 * uma decisão para cada uma, na mesma ordem.
 */
export interface HitlRequest {
  action_requests: ActionRequest[];
  review_configs: ReviewConfig[];
}

export interface Interrupt {
  id: string | null;
  value: HitlRequest;
}

/** O que mandamos de volta em POST /chat/resume/stream. */
export interface DecisionOut {
  type: DecisionType;
  name?: string | null;
  args?: Record<string, unknown> | null;
  message?: string | null;
}

// ------------------------------------------------ pausa 2: elicitation (MCP)

/**
 * JSON Schema plano — a spec do MCP só permite campos primitivos aqui, o que é
 * justamente o que torna possível desenhar um formulário genérico a partir dele.
 */
export interface ElicitSchema {
  type: "object";
  properties: Record<string, ElicitField>;
  required?: string[];
  title?: string;
}

export interface ElicitField {
  type?: "string" | "number" | "integer" | "boolean";
  title?: string;
  description?: string;
  default?: unknown;
  enum?: string[];
  minimum?: number;
  maximum?: number;
}

export interface Elicitation {
  elicitation_id: string;
  session_id: string;
  tool: string | null;
  server: string | null;
  message: string;
  schema: ElicitSchema | null;
}

// ------------------------------------------------------------- eventos SSE

export type StreamEvent =
  | { event: "session"; data: { session_id: string; title: string } }
  | { event: "token"; data: { content: string } }
  | { event: "tool"; data: { name: string } }
  | { event: "interrupt"; data: { interrupt: Interrupt[] } }
  | { event: "elicitation"; data: Elicitation }
  | { event: "elicitation_closed"; data: { elicitation_id: string } }
  | {
      event: "done";
      data: { status: "completed" | "interrupted"; message_id: number | null };
    }
  | { event: "error"; data: { detail: string } };

// --------------------------------------------------------------- UI local

export interface ChatMessage {
  /** id local; mensagens vindas do banco reaproveitam o id numérico. */
  key: string;
  role: "user" | "assistant";
  content: string;
  /** tools chamadas durante ESTA resposta, para o rastro na UI. */
  tools?: string[];
  streaming?: boolean;
}

/**
 * As duas pausas possíveis, num tipo só — porque a UI trata as duas como
 * "tem uma pergunta na tela", mas o jeito de responder é diferente.
 * Ver o README para a diferença entre elas.
 */
export type Pending =
  | { kind: "interrupt"; interrupts: Interrupt[] }
  | { kind: "elicitation"; elicitation: Elicitation }
  | null;
