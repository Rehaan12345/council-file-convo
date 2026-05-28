from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

from llm import get_response
from ingest import collection_name

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "chroma_db")))
TOP_K = 5

_collections: dict = {}
_answer_cache: dict[str, dict] = {}
_answer_cache_legs: dict[str, set] = {}   # legislation → set of cache keys

_chroma_client: chromadb.PersistentClient | None = None
_chroma_lock = threading.Lock()
_collections_lock = threading.Lock()


def _cache_key(question: str, legislation: str) -> str:
    normalized = question.lower().strip()
    return hashlib.sha256(f"{legislation}:{normalized}".encode()).hexdigest()


def get_chroma_client() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        with _chroma_lock:
            if _chroma_client is None:
                _chroma_client = chromadb.PersistentClient(path=str(DB_PATH))
    return _chroma_client


def _get_collection(legislation: str):
    if legislation in _collections:
        return _collections[legislation]
    with _collections_lock:
        if legislation not in _collections:
            coll_name = collection_name(legislation)
            print(f"[rag] Loading ChromaDB collection '{coll_name}' from {DB_PATH}...")
            ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
            _collections[legislation] = get_chroma_client().get_collection(
                coll_name, embedding_function=ef
            )
            print(f"[rag] Loaded '{coll_name}' ({_collections[legislation].count()} chunks)")
    return _collections[legislation]


def get_chunk_count(legislation: str) -> int:
    try:
        return _get_collection(legislation).count()
    except Exception as e:
        print(f"[rag] ERROR getting chunk count for '{legislation}': {e}")
        return 0


def get_available_legislations() -> list[dict]:
    """Return [{id, chunks}] for every indexed legislation, sorted by id."""
    try:
        client = get_chroma_client()
        result = []
        for col in client.list_collections():
            if col.name.startswith("leg_"):
                # leg_17_0090 → 17-0090   leg_17_0090_S4 → 17-0090-S4
                leg_id = col.name.removeprefix("leg_").replace("_", "-")
                result.append({"id": leg_id, "chunks": col.count()})
        return sorted(result, key=lambda x: x["id"])
    except Exception as e:
        print(f"[rag] ERROR listing legislations: {e}")
        return []


def invalidate_collection_cache(legislation: str):
    """Drop a cached collection handle so it gets reloaded after re-ingestion."""
    with _collections_lock:
        _collections.pop(legislation, None)
    # Clear answer cache entries for this legislation
    for key in _answer_cache_legs.pop(legislation, set()):
        _answer_cache.pop(key, None)
    print(f"[rag] Invalidated collection cache for '{legislation}'")


def delete_legislation(legislation: str, docs_path: Path) -> int:
    """
    Remove a legislation's ChromaDB collection, clear all caches, and delete
    its docs folder from disk.  Returns the number of chunks that were indexed.
    """
    from ingest import collection_name as _col_name

    coll_name = _col_name(legislation)
    client = get_chroma_client()

    # Count before deleting so we can report it
    try:
        col = client.get_collection(coll_name)
        chunk_count = col.count()
    except Exception:
        chunk_count = 0

    # Delete ChromaDB collection
    try:
        client.delete_collection(coll_name)
        print(f"[rag] Deleted ChromaDB collection '{coll_name}' ({chunk_count} chunks)")
    except Exception as e:
        print(f"[rag] WARNING: could not delete collection '{coll_name}': {e}")

    # Clear in-memory caches
    invalidate_collection_cache(legislation)

    # Remove docs folder from disk
    leg_folder = docs_path / legislation
    if leg_folder.exists():
        import shutil
        shutil.rmtree(leg_folder)
        print(f"[rag] Deleted docs folder {leg_folder}")
    else:
        print(f"[rag] Docs folder {leg_folder} not found — skipping")

    return chunk_count


def answer_question(
    question: str,
    legislations: list[str],
    branches: dict[str, list[str]] | None = None,
    session_id: str | None = None,
) -> dict:
    """
    Query one or more legislation collections, merge results by relevance,
    and return an LLM answer grounded in the retrieved chunks.

    `branches` maps legislation_id → list of branch subfolder names to filter to
    (empty list or missing key = no filter = all branches).
    """
    branches = branches or {}
    leg_ids = sorted(legislations)
    branch_key = "|".join(f"{l}:{','.join(sorted(branches.get(l, [])))}" for l in leg_ids)
    print(f"[rag] Query for legislations {leg_ids}: '{question}'" +
          (f" [branches: {branches}]" if branches else ""))

    # App-level cache — skip when the session has history (each turn has unique context)
    key = _cache_key(question + "|legs:" + ",".join(leg_ids) + "|branches:" + branch_key,
                     "__multi__")
    use_cache = not session_id
    if not use_cache and session_id:
        from history import load_recent
        use_cache = len(load_recent(session_id)) == 0  # cache only on first turn

    if use_cache and key in _answer_cache:
        print(f"[rag] Cache HIT — returning stored answer (0 tokens used)")
        return _answer_cache[key]

    print(f"[rag] Cache MISS — querying {len(leg_ids)} collection(s), top {TOP_K} each...")

    # Query each collection and collect (distance, doc, metadata) tuples
    all_results: list[tuple[float, str, dict]] = []
    for leg in leg_ids:
        try:
            collection = _get_collection(leg)
        except Exception as e:
            print(f"[rag] WARNING: could not load collection for '{leg}': {e}")
            continue

        leg_branches = branches.get(leg)
        where = {"subfolder": {"$in": leg_branches}} if leg_branches else None
        res = collection.query(
            query_texts=[question],
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
            **({"where": where} if where else {}),
        )
        if res["documents"] and res["documents"][0]:
            for doc, meta, dist in zip(
                res["documents"][0], res["metadatas"][0], res["distances"][0]
            ):
                all_results.append((dist, doc, meta))

    # Sort globally by distance (lower = more relevant), keep top TOP_K per leg
    all_results.sort(key=lambda x: x[0])
    top = all_results[: TOP_K * len(leg_ids)]

    chunks = [{"text": doc, "source": meta.get("source", "unknown")}
              for _, doc, meta in top]

    print(f"[rag] Retrieved {len(chunks)} chunks total:")
    for i, c in enumerate(chunks):
        print(f"  [{i+1}] {c['source']} — {len(c['text'].split())} words")

    print(f"[rag] Sending to LLM...")
    result = get_response(question, chunks, leg_ids, session_id=session_id)

    if use_cache:
        _answer_cache[key] = result
    if use_cache:
        for leg in leg_ids:
            _answer_cache_legs.setdefault(leg, set()).add(key)
    print(f"[rag] Answer stored in cache (cache size: {len(_answer_cache)} entries)")
    return result
