import "./Message.css";

interface MessageData {
  role: "user" | "assistant";
  text: string;
  sources?: string[];
  followups?: string[];
}

interface Props {
  message: MessageData;
  onFollowup: (q: string) => void;
}

export default function Message({ message, onFollowup }: Props) {
  const isUser = message.role === "user";

  return (
    <div className={`message ${isUser ? "message--user" : "message--assistant"}`}>
      <div className="message-bubble">
        <p className="message-text">{message.text}</p>

        {!isUser && message.sources && message.sources.length > 0 && (
          <div className="message-sources">
            <span className="sources-label">Sources: </span>
            {message.sources.map((s) => (
              <span key={s} className="source-tag">{s}</span>
            ))}
          </div>
        )}

        {!isUser && message.followups && message.followups.length > 0 && (
          <div className="message-followups">
            <p className="followups-label">You might also ask:</p>
            <div className="followups-list">
              {message.followups.map((q) => (
                <button key={q} className="followup-chip" onClick={() => onFollowup(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export type { MessageData };
