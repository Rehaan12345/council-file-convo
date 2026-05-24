import { useState, useRef, useEffect, useCallback } from "react";
import Message, { type MessageData } from "./components/Message";
import StarterQuestions from "./components/StarterQuestions";
import "./App.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";


const CF_PATTERN = /^\d{2}-\d{4}(-S\d+)?$/;

interface Legislation {
  id: string;
  chunks: number;
  subtitle?: string;
  starters?: string[];
}

interface IngestJob {
  job_id: string;
  status: "downloading" | "indexing" | "done" | "error";
  message: string;
}

export default function App() {
  const [legislations, setLegislations] = useState<Legislation[]>([]);
  // primaryLeg: which tab drives the header title and starter questions
  const [primaryLeg, setPrimaryLeg] = useState("");
  // contextLegs: all legs included in chat queries (always contains primaryLeg)
  const [contextLegs, setContextLegs] = useState<string[]>([]);
  const [messages, setMessages] = useState<MessageData[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);

  // Add council file form
  const [addInput, setAddInput] = useState("");
  const [addError, setAddError] = useState("");
  const [addLoading, setAddLoading] = useState(false);
  const [ingestJob, setIngestJob] = useState<IngestJob | null>(null);

  // Branch confirm prompt: shown when user submits a -Sx file
  const [branchConfirm, setBranchConfirm] = useState<{ cf: string; baseFile: string; branchNum: number } | null>(null);

  // Delete confirm: which legislation tab is pending deletion
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);

  // Branch dropdowns: available branches & current selection per legislation
  const [branchesAvailable, setBranchesAvailable] = useState<Record<string, string[]>>({});
  const [branchSelection, setBranchSelection] = useState<Record<string, string[]>>({});
  const [branchDropdownOpen, setBranchDropdownOpen] = useState<string | null>(null);
  const tabsAreaRef = useRef<HTMLDivElement>(null);   // wraps tabs + dropdown panel
  const dropdownRef = useRef<HTMLDivElement>(null);   // the dropdown panel itself (unused for click-out)

  const bottomRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch available legislations from backend ──────────────────────────────
  const fetchLegislations = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/legislations`);
      if (!res.ok) return;
      const data: Legislation[] = await res.json();
      setLegislations(data);
      // Auto-select first if nothing is selected yet
      setPrimaryLeg((prev) => prev || data[0]?.id || "");
      setContextLegs((prev) => prev.length ? prev : (data[0] ? [data[0].id] : []));
    } catch {
      // backend not running yet — silent fail
    }
  }, []);

  useEffect(() => {
    fetchLegislations();
  }, [fetchLegislations]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  // ── Branch loading ─────────────────────────────────────────────────────────
  const fetchBranchesForLeg = useCallback(async (legId: string) => {
    if (branchesAvailable[legId] !== undefined) return; // already loaded
    try {
      const res = await fetch(`${API_URL}/api/legislations/${encodeURIComponent(legId)}/branches`);
      if (!res.ok) return;
      const { branches } = await res.json();
      setBranchesAvailable((prev) => ({ ...prev, [legId]: branches }));
    } catch { /* silent */ }
  }, [branchesAvailable]);

  // Fetch branches for each new legislation that appears
  useEffect(() => {
    legislations.forEach((leg) => fetchBranchesForLeg(leg.id));
  }, [legislations, fetchBranchesForLeg]);

  // Close branch dropdown on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (tabsAreaRef.current && !tabsAreaRef.current.contains(e.target as Node)) {
        setBranchDropdownOpen(null);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  // ── Legislation selection ──────────────────────────────────────────────────
  // Clicking a tab body: switch primary, reset context to just that tab, clear chat
  function clickTab(leg: string) {
    if (leg === primaryLeg && contextLegs.length === 1) return;
    setPrimaryLeg(leg);
    setContextLegs([leg]);
    setMessages([]);
    setInput("");
    setBranchDropdownOpen(null);
  }

  // Checkbox: toggle a leg in/out of contextLegs without clearing chat
  function toggleContextLeg(leg: string) {
    setContextLegs((prev) => {
      if (prev.includes(leg)) {
        // Can't remove the last one
        if (prev.length === 1) return prev;
        const next = prev.filter((l) => l !== leg);
        // If we removed the primary, shift primary to first remaining
        if (leg === primaryLeg) setPrimaryLeg(next[0]);
        return next;
      }
      return [...prev, leg];
    });
  }

  // ── Chat ───────────────────────────────────────────────────────────────────
  async function sendQuestion(question: string) {
    if (!question.trim() || loading) return;
    setMessages((prev) => [...prev, { role: "user", text: question }]);
    setInput("");
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          legislations: contextLegs,
          branches: Object.fromEntries(
            contextLegs
              .filter((l) => (branchSelection[l] ?? []).length > 0)
              .map((l) => [l, branchSelection[l]])
          ),
        }),
      });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: data.answer, sources: data.sources, followups: data.followups },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: "Sorry, something went wrong connecting to the server. Is the backend running?" },
      ]);
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendQuestion(input);
    }
  }

  // ── Add council file ───────────────────────────────────────────────────────
  function validateAddInput(val: string): string {
    if (!val.trim()) return "Enter a council file number";
    if (!CF_PATTERN.test(val.trim())) return "Format must be like 17-0090 or 17-0090-S4";
    if (legislations.some((l) => l.id === val.trim()))
      return `${val.trim()} is already loaded`;
    return "";
  }

  async function handleAddSubmit(e: React.FormEvent) {
    e.preventDefault();
    const cf = addInput.trim();
    const err = validateAddInput(cf);
    if (err) { setAddError(err); return; }
    setAddError("");

    // If it's a -Sx file, show branch confirm prompt instead of immediately ingesting
    const branchMatch = cf.match(/^(.+)-S(\d+)$/);
    if (branchMatch) {
      setBranchConfirm({ cf, baseFile: branchMatch[1], branchNum: parseInt(branchMatch[2], 10) });
      return;
    }

    await startIngest(cf, false);
  }

  async function startIngest(cf: string, loadAllBranches: boolean) {
    setAddLoading(true);
    setBranchConfirm(null);
    try {
      const res = await fetch(`${API_URL}/api/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ council_file: cf, load_all_branches: loadAllBranches }),
      });
      if (!res.ok) {
        const body = await res.json();
        setAddError(body.detail || "Failed to start ingest");
        setAddLoading(false);
        return;
      }
      const { job_id } = await res.json();
      setIngestJob({ job_id, status: "downloading", message: `Starting download for ${cf}...` });
      setAddInput("");
      // When loading all branches the backend switches council_file to the base file on done
      const targetFile = loadAllBranches ? cf.replace(/-S\d+$/, "") : cf;
      startPolling(job_id, targetFile);
    } catch {
      setAddError("Could not reach the server");
      setAddLoading(false);
    }
  }

  function startPolling(job_id: string, fallback_file: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${API_URL}/api/ingest/status/${job_id}`);
        if (!res.ok) return;
        const job = await res.json();
        setIngestJob(job);

        if (job.status === "done") {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          setAddLoading(false);
          await fetchLegislations();
          // Use council_file from backend response (may differ if all-branches mode changed it)
          const switchTo = job.council_file || fallback_file;
          setPrimaryLeg(switchTo);
          setContextLegs([switchTo]);
          setMessages([]);
          // Hide status bar after 4s
          setTimeout(() => setIngestJob(null), 4000);
        } else if (job.status === "error") {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          setAddLoading(false);
        }
      } catch {
        // polling failure — keep trying
      }
    }, 2000);
  }

  // ── Delete council file ────────────────────────────────────────────────────
  async function confirmDelete(leg: string) {
    setDeleteLoading(true);
    setDeleteConfirm(null);
    try {
      const res = await fetch(`${API_URL}/api/legislations/${encodeURIComponent(leg)}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const body = await res.json();
        console.error("Delete failed:", body.detail);
        return;
      }
      // If we just deleted something in context, clear chat
      if (contextLegs.includes(leg)) {
        setMessages([]);
        setInput("");
      }
      // Remove from context if it was selected; fetchLegislations auto-picks next primary
      setContextLegs((prev) => prev.filter((l) => l !== leg));
      if (primaryLeg === leg) setPrimaryLeg("");
      await fetchLegislations();
    } catch {
      console.error("Delete request failed");
    } finally {
      setDeleteLoading(false);
    }
  }

  // Cleanup polling on unmount
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  // ── Render ─────────────────────────────────────────────────────────────────
  const primaryLegData = legislations.find((l) => l.id === primaryLeg);
  const meta = {
    title: contextLegs.length > 1
      ? `${contextLegs.length} council files`
      : `Council File ${primaryLeg}`,
    subtitle: contextLegs.length > 1
      ? contextLegs.join(" · ")
      : (primaryLegData?.subtitle || "LA City Council legislation"),
  };
  const showStarters = messages.length === 0 && !loading;

  const statusEmoji =
    ingestJob?.status === "downloading" ? "⏳" :
    ingestJob?.status === "indexing" ? "⚙️" :
    ingestJob?.status === "done" ? "✓" : "✗";

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-inner">
          {/* ── Top row: title + add form ── */}
          <div className="header-top">
            <div className="header-text">
              <h1 className="header-title">{meta.title}</h1>
              <p className="header-subtitle">{meta.subtitle}</p>
            </div>
            <div className="header-right">
              <form className="add-file-form" onSubmit={handleAddSubmit}>
                <input
                  className={`add-file-input${addError ? " add-file-input--error" : ""}`}
                  placeholder="Add file… e.g. 22-0100"
                  value={addInput}
                  onChange={(e) => { setAddInput(e.target.value); setAddError(""); }}
                  disabled={addLoading}
                />
                <button className="add-file-btn" type="submit" disabled={addLoading}>
                  {addLoading ? "…" : "+ Add"}
                </button>
              </form>
              {addError && <p className="add-file-error">{addError}</p>}
            </div>
          </div>

          {/* ── Tab bar: one tab per loaded legislation ── */}
          {legislations.length > 0 && (
            <div ref={tabsAreaRef}>
              <div className="leg-tabs">
                {legislations.map((leg) => {
                  const isPrimary = primaryLeg === leg.id;
                  const inContext = contextLegs.includes(leg.id);
                  const isPendingDelete = deleteConfirm === leg.id;
                  const branches = branchesAvailable[leg.id] ?? [];
                  const hasBranches = branches.length > 0;
                  const selected = branchSelection[leg.id] ?? [];
                  const isDropdownOpen = branchDropdownOpen === leg.id;

                  if (isPendingDelete) {
                    return (
                      <div key={leg.id} className="leg-tab leg-tab--delete-confirm">
                        <span className="leg-tab-delete-prompt">Remove {leg.id}?</span>
                        <div className="leg-tab-delete-btns">
                          <button
                            className="leg-tab-delete-yes"
                            onClick={() => confirmDelete(leg.id)}
                            disabled={deleteLoading}
                          >
                            {deleteLoading ? "…" : "Remove"}
                          </button>
                          <button
                            className="leg-tab-delete-no"
                            onClick={() => setDeleteConfirm(null)}
                            disabled={deleteLoading}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    );
                  }

                  return (
                    <div
                      key={leg.id}
                      className={`leg-tab-wrap${isPrimary ? " leg-tab-wrap--active" : ""}${inContext && !isPrimary ? " leg-tab-wrap--checked" : ""}`}
                    >
                      {/* Context-inclusion checkbox */}
                      <input
                        type="checkbox"
                        className="leg-tab-check"
                        checked={inContext}
                        title={inContext ? "Remove from context" : "Add to context"}
                        onChange={() => toggleContextLeg(leg.id)}
                      />

                      <button
                        className={`leg-tab${isPrimary ? " leg-tab--active" : ""}`}
                        onClick={() => clickTab(leg.id)}
                        title={`${leg.chunks.toLocaleString()} chunks indexed`}
                      >
                        <span className="leg-tab-id">{leg.id}</span>
                        {leg.subtitle && (
                          <span className="leg-tab-desc">{leg.subtitle}</span>
                        )}
                        {hasBranches && selected.length > 0 && (
                          <span className="leg-tab-branch-badge">{selected.length}/{branches.length}</span>
                        )}
                      </button>

                      {hasBranches && (
                        <button
                          className={`leg-tab-branch-btn${isDropdownOpen ? " leg-tab-branch-btn--open" : ""}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            setBranchDropdownOpen(isDropdownOpen ? null : leg.id);
                          }}
                          title="Select branches"
                          aria-label="Select branches"
                        >
                          ▾
                        </button>
                      )}

                      <button
                        className="leg-tab-remove"
                        onClick={(e) => { e.stopPropagation(); setDeleteConfirm(leg.id); }}
                        title={`Remove ${leg.id}`}
                        aria-label={`Remove ${leg.id}`}
                      >
                        ×
                      </button>
                    </div>
                  );
                })}
              </div>

              {/* Branch dropdown — outside the scrollable row so overflow: auto doesn't clip it */}
              {(() => {
                const openLeg = branchDropdownOpen ? legislations.find((l) => l.id === branchDropdownOpen) : null;
                if (!openLeg) return null;
                const branches = branchesAvailable[openLeg.id] ?? [];
                if (branches.length === 0) return null;
                const selected = branchSelection[openLeg.id] ?? [];
                const selectionLabel = selected.length === 0
                  ? `All ${branches.length} branches`
                  : `${selected.length} of ${branches.length} selected`;
                return (
                  <div className="branch-dropdown" ref={dropdownRef}>
                    <div className="branch-dropdown-header">
                      <span className="branch-dropdown-title">{openLeg.id} — {selectionLabel}</span>
                      <div className="branch-dropdown-actions">
                        <button
                          className="branch-dropdown-action"
                          onClick={() => setBranchSelection((prev) => ({ ...prev, [openLeg.id]: [] }))}
                        >
                          All
                        </button>
                        <button
                          className="branch-dropdown-action"
                          onClick={() => setBranchSelection((prev) => ({ ...prev, [openLeg.id]: [...branches] }))}
                        >
                          None
                        </button>
                      </div>
                    </div>
                    <div className="branch-dropdown-list">
                      {branches.map((branch) => {
                        const isChecked = selected.length === 0 || selected.includes(branch);
                        return (
                          <label key={branch} className="branch-dropdown-item">
                            <input
                              type="checkbox"
                              checked={isChecked}
                              onChange={() => {
                                setBranchSelection((prev) => {
                                  const cur = prev[openLeg.id] ?? [];
                                  const effective = cur.length === 0 ? [...branches] : [...cur];
                                  const next = effective.includes(branch)
                                    ? effective.filter((b) => b !== branch)
                                    : [...effective, branch];
                                  const isAll = next.slice().sort().join() === [...branches].sort().join();
                                  return { ...prev, [openLeg.id]: isAll ? [] : next };
                                });
                              }}
                            />
                            <span>{branch}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
        </div>

        {branchConfirm && !addLoading && (
          <div className="branch-confirm">
            <span className="branch-confirm-text">
              <strong>{branchConfirm.cf}</strong> is branch S{branchConfirm.branchNum} of{" "}
              <strong>{branchConfirm.baseFile}</strong>. Load how much?
            </span>
            <div className="branch-confirm-btns">
              <button
                className="branch-btn branch-btn--single"
                onClick={() => startIngest(branchConfirm.cf, false)}
              >
                S{branchConfirm.branchNum} only
              </button>
              <button
                className="branch-btn branch-btn--all"
                onClick={() => startIngest(branchConfirm.cf, true)}
              >
                All branches of {branchConfirm.baseFile}
              </button>
              <button
                className="branch-btn branch-btn--cancel"
                onClick={() => setBranchConfirm(null)}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {ingestJob && (
          <div className={`ingest-status ingest-status--${ingestJob.status}`}>
            <span className="ingest-status-icon">{statusEmoji}</span>
            {ingestJob.message}
          </div>
        )}
      </header>

      <main className="chat-area">
        <div className="chat-inner">
          {showStarters && primaryLeg && (
            <div className="welcome">
              <p className="welcome-text">
                {contextLegs.length > 1
                  ? `Asking across ${contextLegs.length} council files. I'll answer in plain language and cite which document each piece comes from.`
                  : "Ask me anything about this legislation. I'll answer in plain language and tell you exactly which document my answer comes from."
                }
              </p>
              <StarterQuestions starters={primaryLegData?.starters} onSelect={sendQuestion} />
            </div>
          )}

          {messages.map((msg, i) => (
            <Message key={i} message={msg} onFollowup={sendQuestion} />
          ))}

          {loading && (
            <div className="message message--assistant">
              <div className="message-bubble loading-bubble">
                <span className="dot" /><span className="dot" /><span className="dot" />
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </main>

      <footer className="input-area">
        <div className="input-inner">
          <textarea
            className="chat-input"
            rows={1}
            placeholder="Ask a question… (Enter to send, Shift+Enter for new line)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading || contextLegs.length === 0}
          />
          <button
            className="send-btn"
            onClick={() => sendQuestion(input)}
            disabled={loading || !input.trim() || contextLegs.length === 0}
            aria-label="Send"
          >
            ↑
          </button>
        </div>
      </footer>
    </div>
  );
}
