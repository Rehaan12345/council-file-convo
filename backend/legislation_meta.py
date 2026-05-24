"""
Generates and persists human-readable metadata for each indexed council file.

Metadata is stored in legislation_meta.json next to this file so it survives
server restarts.  On first ingest of a new file, generate_and_save_meta() is
called automatically to produce:

  subtitle  – one-line description shown in the tab bar and page title
  context   – 2-3 sentence blurb injected into the LLM system prompt
  starters  – four suggested questions shown on the welcome screen
"""
import json
import os
import re
from pathlib import Path

import anthropic
import chromadb
from chromadb.utils import embedding_functions

META_PATH = Path(os.getenv("META_PATH", str(Path(__file__).parent / "legislation_meta.json")))
DB_PATH   = Path(os.getenv("DB_PATH",   str(Path(__file__).parent / "chroma_db")))

# Seed values for council files that pre-exist before auto-generation was added.
_SEEDS: dict[str, dict] = {
    "17-0090": {
        "subtitle": "Affordable housing & local hire — South & Southeast Los Angeles",
        "context": (
            "Council File 17-0090 and all of its related sub-files (S1 through S33). "
            "These documents cover affordable housing requirements, local hire initiatives, "
            "and community development in South and Southeast Los Angeles."
        ),
        "starters": [
            "What is this legislation about?",
            "How could this affect South LA residents?",
            "What did the final ordinance actually change?",
            "When was this passed and who supported it?",
        ],
    },
    "24-0011": {
        "subtitle": "Street services & tree trimming — Council District 3",
        "context": (
            "Council File 24-0011 and all of its related sub-files (S1 through S35). "
            "These documents cover street services, supplemental tree trimming, and "
            "related Bureau of Street Services actions in Council District 3."
        ),
        "starters": [
            "What is this legislation about?",
            "What tree trimming services does this cover?",
            "How much funding was approved and where does it come from?",
            "Which council district does this affect?",
        ],
    },
    "26-0900": {
        "subtitle": "Street lighting assessment districts — Los Angeles",
        "context": (
            "Council File 26-0900 and all of its related sub-files. "
            "These documents cover street lighting assessment districts across multiple areas of Los Angeles. "
            "They include ordinances establishing and modifying lighting districts, property owner ballot "
            "proceedings under California's Proposition 218, Board of Public Works notifications, and "
            "weighted ballot processes that determine whether assessments are imposed on affected properties."
        ),
        "starters": [
            "What is this legislation about?",
            "Which neighborhoods or areas are affected by these street lighting districts?",
            "How does the property owner ballot process work?",
            "What happens if property owners vote no on the assessment?",
        ],
    },
}


# ── Persistence ───────────────────────────────────────────────────────────────

def load_meta() -> dict:
    """Return the full metadata dict, seeded with hardcoded defaults."""
    data: dict = dict(_SEEDS)
    if META_PATH.exists():
        try:
            with open(META_PATH) as f:
                stored = json.load(f)
            data.update(stored)   # stored values override seeds
        except Exception as e:
            print(f"[meta] WARNING: could not read {META_PATH}: {e}")
    return data


def save_meta(meta: dict):
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"[meta] Saved {META_PATH} ({len(meta)} entries)")


def get_meta(legislation_id: str) -> dict | None:
    return load_meta().get(legislation_id)


# ── Generation ────────────────────────────────────────────────────────────────

def generate_and_save_meta(legislation_id: str) -> dict:
    """
    Retrieve the top representative chunks from the indexed collection, ask
    Claude Haiku to produce a subtitle, context blurb, and starter questions,
    then persist the result.  Returns the generated dict.
    """
    from ingest import collection_name

    print(f"[meta] Generating metadata for '{legislation_id}'...")

    # Pull representative chunks from ChromaDB
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    client = chromadb.PersistentClient(path=str(DB_PATH))
    try:
        collection = client.get_collection(collection_name(legislation_id), embedding_function=ef)
    except Exception as e:
        print(f"[meta] ERROR: could not load collection for '{legislation_id}': {e}")
        return {}

    results = collection.query(
        query_texts=["What is this legislation about? What does it cover?"],
        n_results=5,
        include=["documents", "metadatas"],
    )

    chunks = []
    if results["documents"] and results["documents"][0]:
        for doc, m in zip(results["documents"][0], results["metadatas"][0]):
            chunks.append(f"[Source: {m.get('source', 'unknown')}]\n{doc}")
    context_text = "\n\n---\n\n".join(chunks)

    prompt = f"""You are helping build a public-facing app that lets LA residents ask questions about city council legislation.

Below are document excerpts from Council File {legislation_id}. Based only on these excerpts, return ONLY a JSON object with this exact shape (no markdown, no extra text):

{{
  "subtitle": "A short description under 10 words — like 'Street lighting assessment districts — Los Angeles'",
  "context": "2-3 sentences describing what this legislation covers. Written for an AI assistant that will answer resident questions.",
  "starters": [
    "What is this legislation about?",
    "A specific question a resident could ask (answerable from these documents)",
    "Another specific question a resident could ask",
    "A fourth specific question a resident could ask"
  ]
}}

Document excerpts:
{context_text}"""

    anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        generated = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[meta] WARNING: could not parse response for '{legislation_id}' — using fallback")
        generated = {
            "subtitle": "LA City Council legislation",
            "context": f"Council File {legislation_id}.",
            "starters": [
                "What is this legislation about?",
                "Who introduced this and when?",
                "What does this change or authorize?",
                "How does this affect residents?",
            ],
        }

    # Guarantee "What is this legislation about?" is always first
    starters = generated.get("starters", [])
    starters = ["What is this legislation about?"] + [
        s for s in starters if s != "What is this legislation about?"
    ]
    generated["starters"] = starters[:4]

    meta = load_meta()
    meta[legislation_id] = generated
    save_meta(meta)

    print(f"[meta] '{legislation_id}' → {generated['subtitle']}")
    return generated


def ensure_meta(legislation_id: str) -> dict:
    """Return existing meta if present, otherwise generate it."""
    existing = get_meta(legislation_id)
    if existing:
        return existing
    return generate_and_save_meta(legislation_id)
