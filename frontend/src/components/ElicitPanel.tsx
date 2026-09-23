/**
 * Pausa 2: a elicitation do MCP.
 *
 * Diferença essencial para o InterruptPanel ao lado: aqui o agent **não
 * parou**. Ele está bloqueado DENTRO de uma tool call, e o stream do turno
 * continua aberto esperando. Responder não retoma nada — só destrava a tool
 * que está suspensa no `await ctx.elicit(...)` do servidor MCP.
 *
 * Consequências práticas que a UI precisa respeitar:
 *   - responder é POST /chat/elicit, e NÃO abre stream novo;
 *   - essa pausa não está em checkpoint nenhum: se a API reiniciar, ela morre;
 *   - por isso existe um timeout do lado do servidor (ELICITATION_TIMEOUT).
 *
 * O formulário é gerado a partir do `schema` que veio na própria pergunta. A
 * spec do MCP limita esse schema a campos primitivos justamente para que
 * qualquer cliente consiga desenhar isto sem saber nada sobre a tool.
 */

import { useMemo, useState } from "react";
import type { Elicitation, ElicitField } from "../types";

interface Props {
  elicitation: Elicitation;
  busy: boolean;
  onAnswer: (
    action: "accept" | "decline" | "cancel",
    content?: Record<string, unknown>,
  ) => void;
}

export function ElicitPanel({ elicitation, busy, onAnswer }: Props) {
  const fields = useMemo(
    () => Object.entries(elicitation.schema?.properties ?? {}),
    [elicitation],
  );
  const required = elicitation.schema?.required ?? [];

  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      fields.map(([name, field]) => [
        name,
        field.default !== undefined ? String(field.default) : "",
      ]),
    ),
  );

  const missing = required.filter((name) => values[name]?.trim() === "");

  const accept = () => {
    // Converte cada campo para o tipo que o schema pediu. Mandar "5" onde o
    // servidor espera 5 faz o pydantic do lado de lá rejeitar a resposta.
    const content: Record<string, unknown> = {};
    for (const [name, field] of fields) {
      const raw = values[name];
      if (raw === "" && !required.includes(name)) continue;
      content[name] = coerce(raw, field);
    }
    onAnswer("accept", content);
  };

  return (
    <section className="panel panel--elicit">
      <header className="panel__head">
        <span className="panel__badge panel__badge--elicit">
          o servidor MCP está perguntando
        </span>
        <h3>{elicitation.message}</h3>
        <p className="panel__sub">
          <code>{elicitation.server}</code> · <code>{elicitation.tool}</code> — pausa em
          voo, dentro da tool call. Não sobrevive a um restart da API.
        </p>
      </header>

      {fields.map(([name, field]) => (
        <label key={name} className="field">
          <span>
            {field.title ?? name}
            {required.includes(name) && <b className="field__req"> *</b>}
          </span>
          {field.description && <small className="field__desc">{field.description}</small>}

          {field.enum ? (
            <select
              value={values[name]}
              onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
            >
              <option value="">—</option>
              {field.enum.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          ) : field.type === "boolean" ? (
            <select
              value={values[name]}
              onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
            >
              <option value="">—</option>
              <option value="true">Sim</option>
              <option value="false">Não</option>
            </select>
          ) : (
            <input
              type={field.type === "number" || field.type === "integer" ? "number" : "text"}
              min={field.minimum}
              max={field.maximum}
              value={values[name]}
              onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
            />
          )}
        </label>
      ))}

      <footer className="panel__foot">
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy || missing.length > 0}
          onClick={accept}
        >
          {busy ? "Enviando…" : "Responder"}
        </button>
        <button
          type="button"
          className="btn btn--reject"
          disabled={busy}
          onClick={() => onAnswer("decline")}
          title="A tool continua e recebe 'decline' — ela decide o que fazer com isso."
        >
          Recusar
        </button>
        {missing.length > 0 && (
          <span className="panel__note">Falta preencher: {missing.join(", ")}</span>
        )}
      </footer>
    </section>
  );
}

function coerce(raw: string, field: ElicitField): unknown {
  if (field.type === "integer") return Number.parseInt(raw, 10);
  if (field.type === "number") return Number(raw);
  if (field.type === "boolean") return raw === "true";
  return raw;
}
