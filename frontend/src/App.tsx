import { useState, useRef, useEffect, useCallback } from "react";
import Message, { type MessageData } from "./components/Message";
import StarterQuestions from "./components/StarterQuestions";
import Sidebar, { type Session } from "./components/Sidebar";
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

interface HotSheetEntry {
  full_id: string;
  base_file: string;
  branch: string | null;
  title: string;
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
  const [sessionId, setSessionId] = useState<string>(() => crypto.randomUUID());

  // Sidebar
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Add council file form
  const [addInput, setAddInput] = useState("");
  const [addError, setAddError] = useState("");
  const [addLoading, setAddLoading] = useState(false);
  const [ingestJob, setIngestJob] = useState<IngestJob | null>(null);

  // Branch confirm prompt: shown when branches are detected for a submitted council file
  const [branchConfirm, setBranchConfirm] = useState<{ cf: string; baseFile: string } | null>(null);

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

  // ── Hot Sheet state ────────────────────────────────────────────────────────
  const [hotSheetOpen, setHotSheetOpen] = useState(false);
  const [hotSheetUrl, setHotSheetUrl] = useState("");
  const [hotSheetDate, setHotSheetDate] = useState("");
  const [hotSheetEntries, setHotSheetEntries] = useState<HotSheetEntry[]>([]);
  const [hotSheetParsing, setHotSheetParsing] = useState(false);
  const [hotSheetParseError, setHotSheetParseError] = useState("");
  const [hotSheetLoadError, setHotSheetLoadError] = useState("");

  // ── Fetch session history for sidebar ─────────────────────────────────────
  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions`);
      if (!res.ok) return;
      setSessions(await res.json());
    } catch { /* silent */ }
  }, []);

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
    fetchSessions();
  }, [fetchLegislations, fetchSessions]);

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
  // Clicking a tab body: switch primary, reset context to just that tab, clear chat + session
  function clickTab(leg: string) {
    if (loading) return;
    if (leg === primaryLeg && contextLegs.length === 1) return;
    setPrimaryLeg(leg);
    setContextLegs([leg]);
    setMessages([]);
    setInput("");
    setSessionId(crypto.randomUUID());
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
          session_id: sessionId,
        }),
      });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: data.answer, sources: data.sources, followups: data.followups },
      ]);
      fetchSessions(); // update sidebar after each exchange
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

  // ── Sidebar session management ─────────────────────────────────────────────

  function handleNewChat() {
    if (loading) return;
    setMessages([]);
    setInput("");
    setSessionId(crypto.randomUUID());
  }

  async function handleSelectSession(session: Session) {
    if (loading) return;
    setSidebarOpen(false);
    try {
      const res = await fetch(`${API_URL}/api/sessions/${session.session_id}/messages`);
      if (!res.ok) return;
      const data = await res.json();
      const loaded: MessageData[] = data.messages.map((m: { role: string; content: string }) => ({
        role: m.role as "user" | "assistant",
        text: m.content,
      }));
      setMessages(loaded);
      setSessionId(session.session_id);
      setInput("");
    } catch { /* silent */ }
  }

  async function handleDeleteSession(sessionId: string) {
    try {
      await fetch(`${API_URL}/api/sessions/${sessionId}`, { method: "DELETE" });
      fetchSessions();
    } catch { /* silent */ }
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

    // Strip any -Sx suffix to get the base file for branch checking
    const baseFile = cf.replace(/-S\d+$/, "");

    // Always probe for branches before ingesting
    setAddLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/check-branches/${encodeURIComponent(baseFile)}`);
      const { has_branches } = await res.json();
      if (has_branches) {
        // Let user choose: just this file, or all branches
        setAddLoading(false);
        setBranchConfirm({ cf, baseFile });
        return;
      }
    } catch {
      // If the probe fails, fall through and attempt a normal ingest
      setAddLoading(false);
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
          setSessionId(crypto.randomUUID());
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
  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current);
  }, []);

  // ── Hot Sheet ──────────────────────────────────────────────────────────────

  /** Parse: just fetch the entry list from the backend — no ingesting yet. */
  async function handleHotSheetParse(e: React.FormEvent) {
    e.preventDefault();
    if (!hotSheetUrl.trim()) return;
    setHotSheetParsing(true);
    setHotSheetParseError("");
    setHotSheetLoadError("");
    setHotSheetEntries([]);
    setHotSheetDate("");
    try {
      const res = await fetch(`${API_URL}/api/hot-sheet/parse`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: hotSheetUrl.trim() }),
      });
      if (!res.ok) {
        const body = await res.json();
        setHotSheetParseError(body.detail || "Failed to parse hot sheet");
        return;
      }
      const data = await res.json();
      setHotSheetDate(data.date || "");
      // Deduplicate by base_file — hot sheet shows one entry per base file
      const seen = new Set<string>();
      const entries: HotSheetEntry[] = [];
      for (const e of data.entries as HotSheetEntry[]) {
        if (!seen.has(e.full_id)) {
          seen.add(e.full_id);
          entries.push(e);
        }
      }
      setHotSheetEntries(entries);
    } catch {
      setHotSheetParseError("Could not reach the server");
    } finally {
      setHotSheetParsing(false);
    }
  }

  /** Load: download + index the entire hot sheet as one collection. */
  async function handleHotSheetLoad() {
    if (hotSheetEntries.length === 0) return;
    setHotSheetLoadError("");
    try {
      const res = await fetch(`${API_URL}/api/hot-sheet/load`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: hotSheetDate, entries: hotSheetEntries }),
      });
      if (!res.ok) {
        const body = await res.json();
        setHotSheetLoadError(body.detail || "Failed to start load");
        return;
      }
      const { job_id, hs_id } = await res.json();
      setIngestJob({ job_id, status: "downloading", message: `Starting hot sheet load…` });
      setHotSheetOpen(false);   // close panel; status bar in header shows progress
      startPolling(job_id, hs_id);
    } catch {
      setHotSheetLoadError("Could not reach the server");
    }
  }

  function closeHotSheet() {
    setHotSheetOpen(false);
    setHotSheetParseError("");
    setHotSheetLoadError("");
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  const primaryLegData = legislations.find((l) => l.id === primaryLeg);
  const meta = {
    title: contextLegs.length > 1
      ? `${contextLegs.length} council files`
      : primaryLeg.startsWith("HS-")
        ? (primaryLegData?.subtitle || `Hot Sheet ${primaryLeg.slice(3)}`)
        : `Council File ${primaryLeg}`,
    subtitle: contextLegs.length > 1
      ? contextLegs.join(" · ")
      : (primaryLegData?.subtitle || "LA City Council legislation"),
  };
  const showWelcome = messages.length === 0 && !loading;
  const showStarters = !loading && !!primaryLeg && !!primaryLegData?.starters;

  const statusEmoji =
    ingestJob?.status === "downloading" ? "⏳" :
    ingestJob?.status === "indexing" ? "⚙️" :
    ingestJob?.status === "done" ? "✓" : "✗";

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        activeSessionId={sessionId}
        loading={loading}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onNewChat={handleNewChat}
        onSelectSession={handleSelectSession}
        onDeleteSession={handleDeleteSession}
      />
      <div className="main-panel">
      <header className="app-header">
        <div className="header-inner">
          {/* ── Top row: title + add form ── */}
          <div className="header-top">
            <button
              className="sidebar-toggle-btn"
              onClick={() => setSidebarOpen((o) => !o)}
              aria-label="Toggle history"
            >
              ☰
            </button>
            <div className="header-text">
              <h1 className="header-title">{meta.title}</h1>
              <p className="header-subtitle">{meta.subtitle}</p>
            </div>
            <div className="header-right">
              <div className="header-actions">
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
                <button
                  className={`hot-sheet-toggle-btn${hotSheetOpen ? " hot-sheet-toggle-btn--active" : ""}`}
                  type="button"
                  onClick={() => (hotSheetOpen ? closeHotSheet() : setHotSheetOpen(true))}
                  title="Load from Hot Sheet URL"
                >
                  📋 Hot Sheet
                </button>
              </div>
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
                        disabled={loading}
                      />

                      <button
                        className={`leg-tab${isPrimary ? " leg-tab--active" : ""}`}
                        onClick={() => clickTab(leg.id)}
                        title={`${leg.chunks.toLocaleString()} chunks indexed`}
                        disabled={loading}
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

        {/* ── Hot Sheet panel ── */}
        {hotSheetOpen && (
          <div className="hot-sheet-panel">
            <form className="hot-sheet-url-row" onSubmit={handleHotSheetParse}>
              <input
                className="hot-sheet-url-input"
                placeholder="Paste hot sheet URL…"
                value={hotSheetUrl}
                onChange={(e) => setHotSheetUrl(e.target.value)}
                disabled={hotSheetParsing}
              />
              <button className="hot-sheet-parse-btn" type="submit" disabled={hotSheetParsing || !hotSheetUrl.trim()}>
                {hotSheetParsing ? "Loading…" : "Parse"}
              </button>
              <button className="hot-sheet-close-btn" type="button" onClick={closeHotSheet}>✕</button>
            </form>

            {hotSheetParseError && <p className="hot-sheet-error">{hotSheetParseError}</p>}
            {hotSheetLoadError && <p className="hot-sheet-error">{hotSheetLoadError}</p>}

            {hotSheetEntries.length > 0 && (
              <div className="hot-sheet-results">
                <div className="hot-sheet-summary">
                  Found {hotSheetEntries.length} council files
                  {hotSheetDate ? ` — ${hotSheetDate}` : ""}
                </div>

                <div className="hot-sheet-entries">
                  {hotSheetEntries.slice(0, 10).map((entry) => (
                    <div key={entry.full_id} className="hot-sheet-entry hot-sheet-entry--preview">
                      <span className="hot-sheet-entry-id">{entry.full_id}</span>
                      {entry.title && (
                        <span className="hot-sheet-entry-title">{entry.title}</span>
                      )}
                    </div>
                  ))}
                  {hotSheetEntries.length > 10 && (
                    <div className="hot-sheet-more">
                      …and {hotSheetEntries.length - 10} more
                    </div>
                  )}
                </div>

                <div className="hot-sheet-footer">
                  <button
                    className="hot-sheet-open-btn"
                    type="button"
                    onClick={handleHotSheetLoad}
                  >
                    Load as "Hot Sheet {hotSheetDate || "today"}"
                  </button>
                  <span className="hot-sheet-footer-hint">
                    All {hotSheetEntries.length} files will be searchable as branches
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        {branchConfirm && !addLoading && (
          <div className="branch-confirm">
            <span className="branch-confirm-text">
              <strong>{branchConfirm.baseFile}</strong> has multiple sub-files (branches). Load how much?
            </span>
            <div className="branch-confirm-btns">
              <button
                className="branch-btn branch-btn--single"
                onClick={() => startIngest(branchConfirm.cf, false)}
              >
                Just {branchConfirm.cf}
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
          {showWelcome && primaryLeg && (
            <div className="welcome">
              <p className="welcome-text">
                {contextLegs.length > 1
                  ? `Asking across ${contextLegs.length} council files. I'll answer in plain language and cite which document each piece comes from.`
                  : "Ask me anything about this legislation. I'll answer in plain language and tell you exactly which document my answer comes from."
                }
              </p>
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

          {showStarters && (
            <StarterQuestions starters={primaryLegData?.starters} onSelect={sendQuestion} />
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
    </div>
  );
}
