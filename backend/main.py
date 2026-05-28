import asyncio
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

import rag
import downloader
import hot_sheet as hs
import legislation_meta as leg_meta
from ingest import ingest_legislation, collection_name
from hot_sheet import date_to_hs_id
from chromadb.utils import embedding_functions

load_dotenv()

DOCS_PATH = Path(os.getenv("DOCS_PATH", "/data/council-files"))
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "chroma_db")))

# Valid council file format: "17-0090" or "17-0090-S4"
CF_PATTERN = re.compile(r"^\d{2}-\d{4}(-S\d+)?$")


async def _auto_ingest_missing():
    """On startup: ingest any DOCS_PATH folders not yet in ChromaDB."""
    try:
        indexed = {l["id"] for l in rag.get_available_legislations()}
        missing = [
            f for f in DOCS_PATH.iterdir()
            if f.is_dir() and not f.name.startswith(".") and f.name not in indexed
        ]
        if not missing:
            print(f"[startup] All DOCS_PATH folders already indexed: {sorted(indexed)}")
            return
        print(f"[startup] Found {len(missing)} unindexed folder(s): {[f.name for f in missing]}")
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        client = rag.get_chroma_client()
        for folder in sorted(missing):
            print(f"[startup] Auto-ingesting {folder.name}...")
            count = ingest_legislation(folder, client, ef)
            print(f"[startup] ✓ {folder.name} — {count} chunks indexed")
            leg_meta.ensure_meta(folder.name)
    except Exception as e:
        print(f"[startup] WARNING: auto-ingest failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run on startup — background so server is immediately ready for requests
    asyncio.create_task(_auto_ingest_missing())
    yield  # server runs here


app = FastAPI(title="Council File Chat API", lifespan=lifespan)

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# In-memory job tracker for background ingest tasks
ingest_jobs: dict[str, dict] = {}


# ─── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    legislations: list[str]               # one or more legislation IDs
    branches: dict[str, list[str]] = {}   # legislation_id → selected branches (empty = all)
    session_id: str | None = None         # opaque UUID from the frontend; enables memory


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]
    followups: list[str]


class HotSheetRequest(BaseModel):
    url: str


class IngestRequest(BaseModel):
    council_file: str
    load_all_branches: bool = False


class IngestStarted(BaseModel):
    job_id: str
    council_file: str


class IngestStatus(BaseModel):
    job_id: str
    council_file: str
    status: str   # "downloading" | "indexing" | "done" | "error"
    message: str


class HotSheetLoadRequest(BaseModel):
    date: str
    entries: list[dict]   # [{full_id, base_file, branch, title}, ...]


class HotSheetLoadStarted(BaseModel):
    job_id: str
    hs_id: str


# ─── Background task ───────────────────────────────────────────────────────────

async def _run_ingest(job_id: str, council_file: str, load_all_branches: bool = False):
    job = ingest_jobs[job_id]
    try:
        ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        client = rag.get_chroma_client()

        # ── Branched flow: download S1..SN for the base file ──────────────────
        branch_match = re.search(r"^(.+)-S(\d+)$", council_file)
        if load_all_branches:
            # Works whether the user entered "14-1174" or "14-1174-S4"
            if branch_match:
                base_file = branch_match.group(1)
                known_min = int(branch_match.group(2))
            else:
                base_file = council_file
                known_min = 1  # check-branches already confirmed S1 exists

            # Step 1: discover last branch
            job["status"] = "downloading"
            job["message"] = f"Finding all branches of {base_file}..."
            print(f"[ingest-job:{job_id}] Discovering branches of {base_file} from S{known_min}")
            last_branch = await downloader.discover_last_branch(base_file, known_min, job)
            print(f"[ingest-job:{job_id}] Last branch: S{last_branch}")

            # Step 2: download all branches
            job["message"] = f"Downloading {last_branch} branches of {base_file}..."
            dest_folder, pdf_count = await downloader.download_all_branches(
                base_file=base_file,
                last_branch=last_branch,
                docs_path=DOCS_PATH,
                job_status=job,
            )

            # Step 3: index the whole base folder (all branches combined)
            job["status"] = "indexing"
            job["message"] = f"Indexing {pdf_count} PDFs across {last_branch} branches..."
            print(f"[ingest-job:{job_id}] Indexing {pdf_count} PDFs from {dest_folder}")
            chunks_indexed = ingest_legislation(dest_folder, client, ef)

            rag.invalidate_collection_cache(base_file)
            job["council_file"] = base_file  # switch to base file for frontend auto-select
            job["status"] = "done"
            job["message"] = (
                f"Ready — {chunks_indexed} chunks indexed from {pdf_count} PDFs "
                f"across {last_branch} branches of {base_file}"
            )
            print(f"[ingest-job:{job_id}] Done. {chunks_indexed} chunks, {last_branch} branches.")
            leg_meta.generate_and_save_meta(base_file)

        # ── Single-file flow ───────────────────────────────────────────────────
        else:
            # Step 1: download
            job["status"] = "downloading"
            job["message"] = f"Downloading PDFs from city clerk for {council_file}..."
            print(f"[ingest-job:{job_id}] Starting download for {council_file}")

            dest_folder, pdf_count = await downloader.download_and_extract(
                council_file=council_file,
                docs_path=DOCS_PATH,
                cleanup=False,
                job_status=job,
            )

            # Step 2: index
            job["status"] = "indexing"
            job["message"] = f"Indexing {pdf_count} PDFs into the search database..."
            print(f"[ingest-job:{job_id}] Indexing {pdf_count} PDFs from {dest_folder}")
            chunks_indexed = ingest_legislation(dest_folder, client, ef)

            rag.invalidate_collection_cache(council_file)
            job["status"] = "done"
            job["message"] = f"Ready — {chunks_indexed} chunks indexed from {pdf_count} PDFs"
            print(f"[ingest-job:{job_id}] Done. {chunks_indexed} chunks indexed.")
            leg_meta.generate_and_save_meta(council_file)

    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {str(e)}"
        print(f"[ingest-job:{job_id}] ERROR: {e}")


async def _run_hot_sheet_load(job_id: str, hs_id: str, date: str, entries: list[dict]):
    """
    Background task: download every council file in a hot sheet into
    DOCS_PATH/{hs_id}/{full_id}/, then index the whole folder as one collection.
    """
    job = ingest_jobs[job_id]
    try:
        hs_folder = DOCS_PATH / hs_id
        hs_folder.mkdir(parents=True, exist_ok=True)

        # ── Concurrent downloads (up to 6 at a time) ──────────────────────────
        total = len(entries)
        completed = 0
        total_pdfs = 0
        sem = asyncio.Semaphore(6)

        async def download_one(entry: dict):
            nonlocal completed, total_pdfs
            full_id = entry["full_id"]
            dest = hs_folder / full_id

            # Skip already-downloaded folders
            existing = list(dest.rglob("*.pdf")) if dest.exists() else []
            if existing:
                total_pdfs += len(existing)
                completed += 1
                job["message"] = f"Downloading… {completed}/{total} ({full_id} cached)"
                return

            async with sem:
                try:
                    _, count = await downloader.download_and_extract(
                        council_file=full_id,
                        docs_path=hs_folder,
                        job_status=None,
                    )
                    total_pdfs += count
                    print(f"[hot-sheet-load:{job_id}] {full_id}: {count} PDFs")
                except Exception as e:
                    print(f"[hot-sheet-load:{job_id}] WARNING: {full_id} failed — {e}")
                completed += 1
                job["message"] = f"Downloading… {completed}/{total}"

        job["status"] = "downloading"
        job["message"] = f"Downloading {total} council files for {hs_id}…"
        print(f"[hot-sheet-load:{job_id}] Downloading {total} entries into {hs_folder}")
        await asyncio.gather(*[download_one(e) for e in entries])

        # ── Index the whole hs_folder as one collection ────────────────────────
        job["status"] = "indexing"
        job["message"] = f"Indexing {total_pdfs} PDFs from {total} council files…"
        print(f"[hot-sheet-load:{job_id}] Indexing {total_pdfs} PDFs from {hs_folder}")

        ef = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        client = rag.get_chroma_client()
        chunks_indexed = ingest_legislation(hs_folder, client, ef)

        # Metadata is deterministic — no Claude call needed
        leg_meta.save_hot_sheet_meta(hs_id, date, entries)

        rag.invalidate_collection_cache(hs_id)
        job["council_file"] = hs_id
        job["status"] = "done"
        job["message"] = (
            f"Ready — {chunks_indexed} chunks indexed from {total} council files "
            f"({total_pdfs} PDFs)"
        )
        print(f"[hot-sheet-load:{job_id}] Done. {chunks_indexed} chunks, {total} entries.")

    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {str(e)}"
        print(f"[hot-sheet-load:{job_id}] ERROR: {e}")


# ─── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    print("[health] Health check requested")
    legislations = rag.get_available_legislations()
    print(f"[health] {len(legislations)} legislations available")
    return {"status": "ok", "legislations": legislations}


@app.get("/api/legislations")
def list_legislations():
    """Return all available legislations with chunk counts and display metadata."""
    legislations = rag.get_available_legislations()
    meta = leg_meta.load_meta()
    for leg in legislations:
        m = meta.get(leg["id"], {})
        leg["subtitle"] = m.get("subtitle", "")
        leg["starters"] = m.get("starters", [])
    print(f"[legislations] Returning {len(legislations)} legislations")
    return legislations


@app.post("/api/ingest", response_model=IngestStarted)
async def start_ingest(req: IngestRequest, background_tasks: BackgroundTasks):
    council_file = req.council_file.strip()
    print(f"\n[ingest] Request to ingest council file: '{council_file}'")

    if not CF_PATTERN.match(council_file):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid council file format '{council_file}'. "
                   f"Expected format: '17-0090' or '17-0090-S4'",
        )

    # Check if already indexed (for all-branches mode, check the base file)
    existing = [l["id"] for l in rag.get_available_legislations()]
    branch_match = re.search(r"^(.+)-S(\d+)$", council_file)
    check_id = branch_match.group(1) if (req.load_all_branches and branch_match) else council_file
    if check_id in existing:
        raise HTTPException(
            status_code=409,
            detail=f"'{check_id}' is already indexed. "
                   f"Re-ingestion not supported yet.",
        )

    job_id = str(uuid.uuid4())
    ingest_jobs[job_id] = {
        "job_id": job_id,
        "council_file": council_file,
        "status": "downloading",
        "message": f"Starting download for {council_file}...",
    }

    background_tasks.add_task(_run_ingest, job_id, council_file, req.load_all_branches)
    print(f"[ingest] Job {job_id} started for {council_file} (load_all_branches={req.load_all_branches})")
    return IngestStarted(job_id=job_id, council_file=council_file)


@app.get("/api/ingest/status/{job_id}", response_model=IngestStatus)
def ingest_status(job_id: str):
    job = ingest_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return IngestStatus(**job)


@app.get("/api/check-branches/{council_file}")
async def check_branches(council_file: str):
    """
    Probe the scrape-cf API to see if S1 exists for this base council file.
    Strips any -Sx suffix so callers can pass either "14-1174" or "14-1174-S4".
    Returns {"has_branches": bool, "base_file": str}.
    """
    base_file = re.sub(r"-S\d+$", "", council_file.strip())
    has_branches = await downloader.probe_branch_exists(base_file, 1)
    return {"has_branches": has_branches, "base_file": base_file}


@app.get("/api/legislations/{council_file}/branches")
def list_branches(council_file: str):
    """
    Return the sorted list of branch sub-folder names for a legislation.
    For regular council files: ["26-0900-S1", "26-0900-S2", ...]
    For hot sheet collections (HS-YYYY-MM-DD): all direct subfolders (council file IDs).
    Returns an empty list if the legislation has no branches.
    """
    council_file = council_file.strip()
    leg_folder = DOCS_PATH / council_file
    if not leg_folder.is_dir():
        return {"branches": []}

    if council_file.startswith("HS-"):
        # Hot sheet: every direct subdirectory is a council file branch
        branches = sorted(
            d.name for d in leg_folder.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    else:
        branch_pattern = re.compile(rf"^{re.escape(council_file)}-S\d+$")
        branches = sorted(
            d.name for d in leg_folder.iterdir()
            if d.is_dir() and branch_pattern.match(d.name)
        )
    return {"branches": branches}


@app.delete("/api/legislations/{council_file}")
def delete_legislation(council_file: str):
    """Remove a legislation's ChromaDB collection and docs folder."""
    council_file = council_file.strip()
    print(f"\n[delete] Request to delete council file: '{council_file}'")

    available = [l["id"] for l in rag.get_available_legislations()]
    if council_file not in available:
        raise HTTPException(
            status_code=404,
            detail=f"'{council_file}' is not indexed.",
        )

    chunk_count = rag.delete_legislation(council_file, DOCS_PATH)
    print(f"[delete] Deleted '{council_file}' ({chunk_count} chunks removed)")
    return {"deleted": council_file, "chunks_removed": chunk_count}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    question = req.question.strip()
    legislations = [l.strip() for l in req.legislations if l.strip()]

    print(f"\n[chat] Incoming question: '{question}' (legislations: {legislations})")

    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if not legislations:
        raise HTTPException(status_code=400, detail="At least one legislation must be specified.")

    available = [l["id"] for l in rag.get_available_legislations()]
    unknown = [l for l in legislations if l not in available]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Legislation(s) not indexed: {unknown}. Available: {available}",
        )

    # Clean up branch selections — drop empty lists
    branches = {k: v for k, v in req.branches.items() if v}
    if branches:
        print(f"[chat] Branch filter: {branches}")

    result = rag.answer_question(question, legislations, branches=branches or None,
                                 session_id=req.session_id or None)
    print(f"[chat] Returning answer ({len(result.get('answer',''))} chars), "
          f"{len(result.get('sources',[]))} sources, "
          f"{len(result.get('followups',[]))} followups")
    return ChatResponse(
        answer=result.get("answer", ""),
        sources=result.get("sources", []),
        followups=result.get("followups", []),
    )


@app.get("/api/sessions")
def list_sessions_endpoint():
    from history import list_sessions
    return list_sessions()


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages_endpoint(session_id: str):
    from history import get_session_messages
    return {"messages": get_session_messages(session_id)}


@app.delete("/api/sessions/{session_id}")
def delete_session_endpoint(session_id: str):
    from history import delete_session
    delete_session(session_id)
    return {"deleted": session_id}


@app.post("/api/hot-sheet/parse")
async def parse_hot_sheet(req: HotSheetRequest):
    """
    Fetch and parse a hot sheet URL.
    Returns entries with indexed/unindexed classification.
    """
    url = req.url.strip()
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid URL — must start with http(s)://")

    print(f"\n[hot-sheet] Parsing {url}")
    try:
        result = await hs.fetch_and_parse(url)
    except Exception as e:
        print(f"[hot-sheet] ERROR {type(e).__name__}: {e}")
        raise HTTPException(status_code=400, detail=f"Could not fetch hot sheet: {e or type(e).__name__}")

    entries = result["entries"]
    print(f"[hot-sheet] Found {len(entries)} entries, date='{result['date']}'")

    # Cross-reference against indexed legislations
    indexed_ids = {l["id"] for l in rag.get_available_legislations()}

    # Preserve order while deduplicating
    indexed: list[str] = []
    unindexed: list[str] = []
    seen_bases: set[str] = set()
    for entry in entries:
        base = entry["base_file"]
        if base not in seen_bases:
            seen_bases.add(base)
            if base in indexed_ids:
                indexed.append(base)
            else:
                unindexed.append(base)

    return {
        "date":      result["date"],
        "entries":   entries,
        "indexed":   indexed,
        "unindexed": unindexed,
    }


@app.post("/api/hot-sheet/load", response_model=HotSheetLoadStarted)
async def load_hot_sheet(req: HotSheetLoadRequest, background_tasks: BackgroundTasks):
    """
    Kick off a background job that downloads every council file in a hot sheet
    into DOCS_PATH/{hs_id}/{full_id}/ and indexes the whole folder as one
    ChromaDB collection keyed by hs_id (e.g. 'HS-2026-05-19').
    """
    if not req.entries:
        raise HTTPException(status_code=400, detail="No entries provided")

    hs_id = date_to_hs_id(req.date)
    print(f"\n[hot-sheet/load] Starting load for {hs_id} ({len(req.entries)} entries)")

    # If already indexed, return 409
    existing = [l["id"] for l in rag.get_available_legislations()]
    if hs_id in existing:
        raise HTTPException(
            status_code=409,
            detail=f"'{hs_id}' is already indexed.",
        )

    job_id = str(uuid.uuid4())
    ingest_jobs[job_id] = {
        "job_id": job_id,
        "council_file": hs_id,
        "status": "downloading",
        "message": f"Starting download for {hs_id}…",
    }

    background_tasks.add_task(
        _run_hot_sheet_load, job_id, hs_id, req.date, req.entries
    )
    print(f"[hot-sheet/load] Job {job_id} started for {hs_id}")
    return HotSheetLoadStarted(job_id=job_id, hs_id=hs_id)
