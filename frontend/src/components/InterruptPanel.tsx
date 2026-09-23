/**
 * Pausa 1: o interrupt human-in-the-loop do LangGraph.
 *
 * Quando isto aparece, o agent JÁ PAROU. O estado dele está gravado no
 * checkpoint do Postgres, e vai continuar lá se a API reiniciar agora. O turno
 * anterior terminou com `done: interrupted` — o stream fechou. Para destravar,
 * abrimos um stream NOVO em POST /chat/resume/stream.
 *
 * A regra que não dá para errar: **uma decisão por `action_request`, na mesma
 * ordem**. O middleware conta e levanta erro se o número não bater. Por isso
 * este componente guarda um rascunho de decisão por índice e só habilita o
 * envio quando todas estão preenchidas.
 */

import { useState } from "react";
import type { DecisionOut, DecisionType, Interrupt } from "../types";

interface Props {
  interrupts: Interrupt[];
  busy: boolean;
  onDecide: (decisions: DecisionOut[]) => void;
}

const LABELS: Record<DecisionType, string> = {
  approve: "Aprovar",
  edit: "Editar e aprovar",
  reject: "Rejeitar",
  respond: "Responder pela tool",
};

const HINTS: Record<DecisionType, string> = {
  approve: "A tool roda exatamente como o modelo pediu.",
  edit: "A tool roda, mas com os argumentos que você corrigir abaixo.",
  reject: "A tool NÃO roda. O modelo recebe o motivo e segue daí.",
  respond: "A tool NÃO roda. O que você escrever volta como se fosse o retorno dela.",
};

/** Rascunho local antes de virar uma DecisionOut. */
interface Draft {
  type: DecisionType;
  argsText: string;
  message: string;
}

export function InterruptPanel({ interrupts, busy, onDecide }: Props) {
  // Um interrupt pode trazer várias action_requests; achatamos todas.
  const actions = interrupts.flatMap((i) => i.value?.action_requests ?? []);
  const configs = interrupts.flatMap((i) => i.value?.review_configs ?? []);

  const [drafts, setDrafts] = useState<Record<number, Draft>>({});

  const allowedFor = (name: string): DecisionType[] =>
    configs.find((c) => c.action_name === name)?.allowed_decisions ?? ["approve", "reject"];

  const pick = (index: number, type: DecisionType, args: Record<string, unknown>) => {
    setDrafts((prev) => ({
      ...prev,
      [index]: {
        type,
        argsText: prev[index]?.argsText ?? JSON.stringify(args, null, 2),
        message: prev[index]?.message ?? "",
      },
    }));
  };

  const update = (index: number, patch: Partial<Draft>) =>
    setDrafts((prev) => ({ ...prev, [index]: { ...prev[index], ...patch } }));

  const ready = actions.every((_, i) => drafts[i] !== undefined);

  const submit = () => {
    const decisions: DecisionOut[] = actions.map((action, i) => {
      const draft = drafts[i];
      switch (draft.type) {
        case "edit":
          return {
            type: "edit",
            name: action.name,
            // Se o JSON estiver inválido, mandamos os args originais em vez de
            // quebrar o envio — o textarea já avisa o usuário visualmente.
            args: safeParse(draft.argsText) ?? action.args,
          };
        case "reject":
          return { type: "reject", message: draft.message || null };
        case "respond":
          return { type: "respond", message: draft.message };
        default:
          return { type: "approve" };
      }
    });
    onDecide(decisions);
  };

  return (
    <section className="panel panel--interrupt">
      <header className="panel__head">
        <span className="panel__badge">aprovação necessária</span>
        <h3>O agent quer executar {actions.length === 1 ? "uma ação" : `${actions.length} ações`}</h3>
        <p className="panel__sub">
          Pausa durável: está no checkpoint do Postgres e sobrevive a um restart da API.
        </p>
      </header>

      {actions.map((action, index) => {
        const draft = drafts[index];
        const allowed = allowedFor(action.name);

        return (
          <article key={`${action.name}-${index}`} className="action">
            <div className="action__title">
              <code>{action.name}</code>
              {draft && <span className="action__chosen">{LABELS[draft.type]}</span>}
            </div>

            {action.description && (
              <pre className="action__desc">{action.description}</pre>
            )}

            <pre className="action__args">{JSON.stringify(action.args, null, 2)}</pre>

            <div className="action__buttons">
              {allowed.map((type) => (
                <button
                  key={type}
                  type="button"
                  disabled={busy}
                  title={HINTS[type]}
                  className={`btn btn--${type} ${draft?.type === type ? "is-active" : ""}`}
                  onClick={() => pick(index, type, action.args)}
                >
                  {LABELS[type]}
                </button>
              ))}
            </div>

            {draft && <p className="action__hint">{HINTS[draft.type]}</p>}

            {draft?.type === "edit" && (
              <label className="field">
                <span>Argumentos corrigidos (JSON)</span>
                <textarea
                  rows={Math.min(14, draft.argsText.split("\n").length + 1)}
                  value={draft.argsText}
                  spellCheck={false}
                  className={safeParse(draft.argsText) ? "" : "is-invalid"}
                  onChange={(e) => update(index, { argsText: e.target.value })}
                />
                {!safeParse(draft.argsText) && (
                  <small className="field__error">
                    JSON inválido — corrija para que a edição valha.
                  </small>
                )}
              </label>
            )}

            {(draft?.type === "reject" || draft?.type === "respond") && (
              <label className="field">
                <span>
                  {draft.type === "reject"
                    ? "Motivo (vai para o modelo)"
                    : "Resposta (volta como retorno da tool)"}
                </span>
                <textarea
                  rows={3}
                  value={draft.message}
                  placeholder={
                    draft.type === "reject"
                      ? "Ex.: o desconto está acima do permitido"
                      : "Ex.: o cliente prefere receber por telefone"
                  }
                  onChange={(e) => update(index, { message: e.target.value })}
                />
              </label>
            )}
          </article>
        );
      })}

      <footer className="panel__foot">
        <button
          type="button"
          className="btn btn--primary"
          disabled={!ready || busy}
          onClick={submit}
        >
          {busy ? "Enviando…" : `Enviar ${actions.length > 1 ? "decisões" : "decisão"}`}
        </button>
        {!ready && (
          <span className="panel__note">
            Escolha uma decisão para cada ação — o middleware exige uma por ação.
          </span>
        )}
      </footer>
    </section>
  );
}

function safeParse(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}
