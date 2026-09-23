/**
 * A caixa de digitar.
 *
 * Fica desabilitada enquanto há uma pausa na tela: se o agent está esperando
 * uma aprovação ou uma elicitation, mandar outra mensagem não faria sentido —
 * aquela thread está bloqueada até a pergunta ser respondida.
 */

import { useState } from "react";

interface Props {
  disabled: boolean;
  placeholder: string;
  onSend: (text: string) => void;
}

export function Composer({ disabled, placeholder, onSend }: Props) {
  const [text, setText] = useState("");

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  };

  return (
    <div className="composer">
      <textarea
        rows={1}
        value={text}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // Enter envia, Shift+Enter quebra linha.
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send();
          }
        }}
      />
      <button type="button" className="btn btn--primary" onClick={send} disabled={disabled}>
        Enviar
      </button>
    </div>
  );
}
