/**
 * A lista de conversas.
 *
 * Ela lê de `GET /conversations`, que consulta as tabelas `conversations` /
 * `messages` — o ESPELHO legível. Nunca o checkpoint do LangGraph: ler o
 * checkpoint para montar uma sidebar significaria desserializar o estado
 * inteiro do agent (mensagens, tool calls, arquivos) só para descobrir um
 * título. É a razão de existirem duas camadas de persistência.
 */

import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  disabled: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
}

export function Sidebar({
  conversations,
  activeId,
  disabled,
  onSelect,
  onNew,
  onDelete,
}: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar__head">
        <h1>Chinook</h1>
        <button type="button" className="btn btn--primary" onClick={onNew} disabled={disabled}>
          Nova conversa
        </button>
      </div>

      <nav className="sidebar__list">
        {conversations.length === 0 && (
          <p className="sidebar__empty">Nenhuma conversa ainda.</p>
        )}

        {conversations.map((conversation) => (
          <div
            key={conversation.id}
            className={`thread ${conversation.id === activeId ? "is-active" : ""}`}
          >
            <button
              type="button"
              className="thread__open"
              onClick={() => onSelect(conversation.id)}
              disabled={disabled}
            >
              <span className="thread__title">{conversation.title}</span>
              <span className="thread__date">
                {new Date(conversation.updated_at).toLocaleString("pt-BR", {
                  day: "2-digit",
                  month: "2-digit",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            </button>
            <button
              type="button"
              className="thread__delete"
              title="Apagar conversa (some das suas tabelas E do checkpoint)"
              onClick={() => onDelete(conversation.id)}
              disabled={disabled}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <footer className="sidebar__foot">
        Jane Peacock · Sales Support
      </footer>
    </aside>
  );
}
