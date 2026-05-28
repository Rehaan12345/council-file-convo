import "./Sidebar.css";

export interface Session {
  session_id: string;
  title: string;
  legislation_ids: string[];
  created_at: number;
}

interface SidebarProps {
  sessions: Session[];
  activeSessionId: string;
  loading?: boolean;
  isOpen?: boolean;
  onClose?: () => void;
  onNewChat: () => void;
  onSelectSession: (session: Session) => void;
  onDeleteSession: (sessionId: string) => void;
}

function groupSessions(sessions: Session[]) {
  const startOfToday = new Date(); startOfToday.setHours(0, 0, 0, 0);
  const startOfYesterday = new Date(startOfToday); startOfYesterday.setDate(startOfToday.getDate() - 1);
  const startOfWeek = new Date(startOfToday); startOfWeek.setDate(startOfToday.getDate() - 7);

  const groups: { label: string; sessions: Session[] }[] = [
    { label: "Today", sessions: [] },
    { label: "Yesterday", sessions: [] },
    { label: "Previous 7 days", sessions: [] },
    { label: "Older", sessions: [] },
  ];

  for (const s of sessions) {
    const d = new Date(s.created_at * 1000);
    if (d >= startOfToday) groups[0].sessions.push(s);
    else if (d >= startOfYesterday) groups[1].sessions.push(s);
    else if (d >= startOfWeek) groups[2].sessions.push(s);
    else groups[3].sessions.push(s);
  }

  return groups.filter(g => g.sessions.length > 0);
}

export default function Sidebar({
  sessions,
  activeSessionId,
  loading = false,
  isOpen = false,
  onClose,
  onNewChat,
  onSelectSession,
  onDeleteSession,
}: SidebarProps) {
  const groups = groupSessions(sessions);

  return (
    <>
      {/* Backdrop — mobile only, closes sidebar on tap */}
      {isOpen && (
        <div className="sidebar-backdrop" onClick={onClose} aria-hidden="true" />
      )}

      <aside className={`sidebar${isOpen ? " sidebar--open" : ""}`}>
        <div className="sidebar-header">
          <span className="sidebar-brand">council-file-convo</span>
          <div className="sidebar-header-actions">
            <button className="sidebar-new-btn" onClick={onNewChat} title="New chat" disabled={loading}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
            <button className="sidebar-close-btn" onClick={onClose} title="Close" aria-label="Close history">
              ✕
            </button>
          </div>
        </div>

        <div className="sidebar-sessions">
          {groups.length === 0 && (
            <p className="sidebar-empty">No conversations yet.<br />Ask a question to get started.</p>
          )}
          {groups.map(group => (
            <div key={group.label} className="sidebar-group">
              <p className="sidebar-group-label">{group.label}</p>
              {group.sessions.map(s => (
                <div
                  key={s.session_id}
                  className={`sidebar-item${s.session_id === activeSessionId ? " sidebar-item--active" : ""}${loading ? " sidebar-item--disabled" : ""}`}
                  onClick={() => !loading && onSelectSession(s)}
                >
                  <span className="sidebar-item-title">{s.title}</span>
                  {s.legislation_ids.length > 0 && (
                    <span className="sidebar-item-legs">{s.legislation_ids.join(" · ")}</span>
                  )}
                  <button
                    className="sidebar-item-delete"
                    onClick={e => { e.stopPropagation(); onDeleteSession(s.session_id); }}
                    title="Delete"
                    aria-label="Delete conversation"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
