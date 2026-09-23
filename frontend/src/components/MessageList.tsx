/**
 * As mensagens da conversa, mais o rastro de tools.
 *
 * O rastro existe porque um deep agent passa boa parte do turno sem escrever
 * nada na tela: ele delega para um subagent, que roda SQL, que volta com um
 * resumo. Sem mostrar `task` / `query_chinook` / `mail_list_messages`
 * acontecendo, a UI fica parada e parece travada.
 */

import { useEffect, useRef } from "react";
import type { ChatMessage } from "../types";

interface Props {
  messages: ChatMessage[];
  children?: React.ReactNode;
}

export function MessageList({ messages, children }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  // Cola no fim a cada token novo — o comportamento esperado num chat.
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, children]);

  return (
    <div className="messages">
      {messages.length === 0 && (
        <div className="welcome">
          <h2>Assistente de vendas da Chinook</h2>
          <p>Peça algo que exerça o agent de ponta a ponta:</p>
          <ul>
            <li>“Quantos clientes a Jane atende e em quais países?”</li>
            <li>“Veja o pedido de cotação na caixa de entrada e monte a cotação.”</li>
            <li>
              “Agende um follow-up para a mensagem msg-1.” — esta usa{" "}
              <b>elicitation</b>: o servidor MCP vai te perguntar os detalhes.
            </li>
          </ul>
        </div>
      )}

      {messages.map((message) => (
        <article key={message.key} className={`msg msg--${message.role}`}>
          {message.tools && message.tools.length > 0 && (
            <div className="msg__tools">
              {message.tools.map((tool, i) => (
                <span key={`${tool}-${i}`} className="chip">
                  {tool}
                </span>
              ))}
            </div>
          )}
          <div className="msg__body">
            {message.content}
            {message.streaming && <span className="caret" />}
          </div>
        </article>
      ))}

      {children}
      <div ref={endRef} />
    </div>
  );
}
