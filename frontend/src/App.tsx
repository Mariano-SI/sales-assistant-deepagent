/**
 * A orquestração do chat.
 *
 * Um único lugar consome os eventos do stream (`consume`), usado tanto por um
 * turno novo quanto por uma retomada — porque, do ponto de vista da tela, os
 * dois são a mesma coisa: tokens chegando até um `done`.
 *
 * ---------------------------------------------------------------------------
 * As duas pausas, lado a lado — é isto que vale estudar neste arquivo
 * ---------------------------------------------------------------------------
 *
 *                     INTERRUPT (HITL)              ELICITATION (MCP)
 * quem pausa          o LangGraph, ANTES da tool    o servidor MCP, DENTRO dela
 * onde fica o estado  checkpoint no Postgres        memória do processo da API
 * o stream do turno   FECHOU (`done: interrupted`)  continua ABERTO
 * como responder      POST /chat/resume/stream      POST /chat/elicit
 * o que responder faz abre um stream NOVO           destrava a tool; nada abre
 * sobrevive a restart sim                           não (por isso há timeout)
 *
 * O erro clássico é tratar as duas igual: abrir um stream novo ao responder uma
 * elicitation duplica o turno, porque o original nunca terminou.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import { Composer } from "./components/Composer";
import { ElicitPanel } from "./components/ElicitPanel";
import { InterruptPanel } from "./components/InterruptPanel";
import { MessageList } from "./components/MessageList";
import { Sidebar } from "./components/Sidebar";
import type {
  ChatMessage,
  Conversation,
  DecisionOut,
  Pending,
  StreamEvent,
} from "./types";

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [pending, setPending] = useState<Pending>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // O id da conversa também vive numa ref: dentro de `consume` o valor do
  // estado estaria congelado no render em que o stream começou.
  const sessionRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refreshList = useCallback(async () => {
    try {
      setConversations(await api.listConversations());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  // Cancela o stream em curso se o componente sair de cena.
  useEffect(() => () => abortRef.current?.abort(), []);

  // ------------------------------------------------------------ o consumidor

  const consume = useCallback(
    async (stream: AsyncGenerator<StreamEvent>) => {
      // Cada turno ganha uma bolha de assistente própria, criada já vazia para
      // que o cursor piscando apareça antes do primeiro token.
      const key = `assistant-${Date.now()}`;
      setMessages((prev) => [
        ...prev,
        { key, role: "assistant", content: "", tools: [], streaming: true },
      ]);

      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) => prev.map((m) => (m.key === key ? fn(m) : m)));

      try {
        for await (const frame of stream) {
          switch (frame.event) {
            case "session": {
              sessionRef.current = frame.data.session_id;
              setActiveId(frame.data.session_id);
              break;
            }

            case "token":
              patch((m) => ({ ...m, content: m.content + frame.data.content }));
              break;

            case "tool":
              patch((m) => ({ ...m, tools: [...(m.tools ?? []), frame.data.name] }));
              break;

            case "interrupt":
              // O grafo parou. O `done` que vem logo atrás fecha o stream.
              setPending({ kind: "interrupt", interrupts: frame.data.interrupt });
              break;

            case "elicitation":
              // O grafo NÃO parou — segue bloqueado na tool call, e este mesmo
              // stream continua vivo esperando a resposta sair por ele.
              setPending({ kind: "elicitation", elicitation: frame.data });
              break;

            case "elicitation_closed":
              setPending((p) =>
                p?.kind === "elicitation" &&
                p.elicitation.elicitation_id === frame.data.elicitation_id
                  ? null
                  : p,
              );
              break;

            case "error":
              setError(frame.data.detail);
              break;

            case "done":
              break;
          }
        }
      } catch (e) {
        if ((e as Error).name !== "AbortError") setError(String(e));
      } finally {
        // Bolha vazia (o turno acabou em pausa, sem texto) não deve ficar na tela.
        setMessages((prev) =>
          prev.filter((m) => m.key !== key || m.content.trim() || m.tools?.length),
        );
        patch((m) => ({ ...m, streaming: false }));
        setBusy(false);
        void refreshList();
      }
    },
    [refreshList],
  );

  // ------------------------------------------------------------------ ações

  const send = async (text: string) => {
    setError(null);
    setBusy(true);
    setMessages((prev) => [
      ...prev,
      { key: `user-${Date.now()}`, role: "user", content: text },
    ]);

    abortRef.current = new AbortController();
    try {
      const stream = await api.streamChat(text, sessionRef.current, abortRef.current.signal);
      await consume(stream);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  };

  /** Responde a um interrupt: limpa a pausa e ABRE UM STREAM NOVO. */
  const decide = async (decisions: DecisionOut[]) => {
    if (!sessionRef.current) return;
    setPending(null);
    setBusy(true);
    setError(null);

    abortRef.current = new AbortController();
    try {
      const stream = await api.streamResume(
        sessionRef.current,
        decisions,
        abortRef.current.signal,
      );
      await consume(stream);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  };

  /**
   * Responde a uma elicitation: só um POST. NÃO abrimos stream — o turno
   * original ainda está rodando e é por ele que a resposta vai continuar.
   */
  const answerElicitation = async (
    action: "accept" | "decline" | "cancel",
    content?: Record<string, unknown>,
  ) => {
    if (pending?.kind !== "elicitation") return;
    const id = pending.elicitation.elicitation_id;
    try {
      await api.answerElicitation(id, action, content);
      setPending(null); // o frame `elicitation_closed` também limparia
    } catch (e) {
      // 409 = a pergunta expirou (timeout) ou já foi respondida.
      setError(String(e));
      setPending(null);
    }
  };

  const openConversation = async (id: string) => {
    abortRef.current?.abort();
    setError(null);
    setPending(null);
    sessionRef.current = id;
    setActiveId(id);
    try {
      const detail = await api.getConversation(id);
      setMessages(
        detail.messages.map((m) => ({
          key: `db-${m.id}`,
          role: m.role,
          content: m.content,
        })),
      );
    } catch (e) {
      setError(String(e));
    }
  };

  const newConversation = () => {
    abortRef.current?.abort();
    sessionRef.current = null;
    setActiveId(null);
    setMessages([]);
    setPending(null);
    setError(null);
  };

  const removeConversation = async (id: string) => {
    try {
      await api.deleteConversation(id);
      if (id === sessionRef.current) newConversation();
      await refreshList();
    } catch (e) {
      setError(String(e));
    }
  };

  // ------------------------------------------------------------------ render

  const blocked = busy || pending !== null;

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        disabled={busy}
        onSelect={openConversation}
        onNew={newConversation}
        onDelete={removeConversation}
      />

      <main className="main">
        <MessageList messages={messages}>
          {pending?.kind === "interrupt" && (
            <InterruptPanel
              interrupts={pending.interrupts}
              busy={busy}
              onDecide={decide}
            />
          )}

          {pending?.kind === "elicitation" && (
            <ElicitPanel
              elicitation={pending.elicitation}
              busy={false}
              onAnswer={answerElicitation}
            />
          )}
        </MessageList>

        {error && (
          <div className="error" role="alert">
            <b>Erro:</b> {error}
            <button type="button" onClick={() => setError(null)}>
              dispensar
            </button>
          </div>
        )}

        <Composer
          disabled={blocked}
          placeholder={
            pending
              ? "Responda a pergunta acima para continuar…"
              : busy
                ? "O agent está trabalhando…"
                : "Pergunte alguma coisa (Enter envia, Shift+Enter quebra linha)"
          }
          onSend={send}
        />
      </main>
    </div>
  );
}
