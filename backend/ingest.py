"""
Run this once (or after adding new PDFs) to build/rebuild the search indexes.
Creates one ChromaDB collection per top-level legislation folder.

Usage:
  python backend/ingest.py              # ingest all legislations
  python backend/ingest.py 17-0090     # ingest one specific legislation
"""
import os
import sys
from pathlib import Path

import pdfplumber
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv

load_dotenv()

DOCS_PATH = os.getenv("DOCS_PATH", "/Users/rehaananjaria/Visic/CouncilFiles")
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "chroma_db")))
CHUNK_SIZE = 400  # words
OVERLAP = 50      # words


def collection_name(legislation_id: str) -> str:
    """Turn '17-0090' into a valid ChromaDB collection name."""
    return f"leg_{legislation_id.replace('-', '_')}"


def chunk_text(text: str, source: str) -> list[dict]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunks.append({"text": " ".join(words[start:end]), "source": source})
        if end == len(words):
            break
        start += CHUNK_SIZE - OVERLAP
    return chunks


def ingest_legislation(legislation_folder: Path, client: chromadb.PersistentClient,
                        ef: embedding_functions.SentenceTransformerEmbeddingFunction):
    legislation_id = legislation_folder.name  # e.g. "17-0090"
    coll_name = collection_name(legislation_id)

    pdf_files = sorted(legislation_folder.rglob("*.pdf"))
    if not pdf_files:
        print(f"  WARNING: No PDFs found in {legislation_folder}")
        return 0

    sub_count = len({p.parent for p in pdf_files})
    print(f"\n[{legislation_id}] {len(pdf_files)} PDFs across {sub_count} sub-files → collection '{coll_name}'")

    # Wipe and recreate so re-runs are safe
    try:
        client.delete_collection(coll_name)
    except Exception:
        pass
    collection = client.create_collection(coll_name, embedding_function=ef)

    all_ids, all_texts, all_metadatas = [], [], []

    for pdf_path in pdf_files:
        subfolder = pdf_path.parent.name
        source_label = f"{subfolder}/{pdf_path.name}"
        print(f"  Parsing {source_label}...")
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
        except Exception as e:
            print(f"    WARNING: Could not parse {source_label}: {e}")
            continue

        if not text:
            print(f"    WARNING: No text extracted from {source_label}")
            continue

        for i, chunk in enumerate(chunk_text(text, source_label)):
            all_ids.append(f"{subfolder}__{pdf_path.stem}_{i}")
            all_texts.append(chunk["text"])
            all_metadatas.append({
                "source": source_label,
                "subfolder": subfolder,
                "filename": pdf_path.name,
                "legislation": legislation_id,
            })

    # Insert in batches of 500 (ChromaDB limit)
    for i in range(0, len(all_ids), 500):
        collection.add(
            ids=all_ids[i:i + 500],
            documents=all_texts[i:i + 500],
            metadatas=all_metadatas[i:i + 500],
        )

    print(f"  ✓ Indexed {len(all_ids)} chunks from {len(pdf_files)} files.")
    return len(all_ids)


def ingest(target: str | None = None):
    docs_path = Path(DOCS_PATH)
    if not docs_path.exists():
        print(f"ERROR: DOCS_PATH not found: {docs_path}")
        sys.exit(1)

    # Each immediate subfolder of CouncilFiles is one legislation set
    leg_folders = sorted(
        f for f in docs_path.iterdir()
        if f.is_dir() and not f.name.startswith(".")
    )

    if not leg_folders:
        print(f"ERROR: No legislation folders found in {docs_path}")
        sys.exit(1)

    if target:
        leg_folders = [f for f in leg_folders if f.name == target]
        if not leg_folders:
            print(f"ERROR: Legislation folder '{target}' not found in {docs_path}")
            sys.exit(1)

    print(f"Found {len(leg_folders)} legislation folder(s): {[f.name for f in leg_folders]}")

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path=str(DB_PATH))

    total_chunks = 0
    for folder in leg_folders:
        total_chunks += ingest_legislation(folder, client, ef)

    print(f"\nAll done. {total_chunks} total chunks indexed.")
    print(f"Index saved to: {DB_PATH}")


if __name__ == "__main__":
    target_leg = sys.argv[1] if len(sys.argv) > 1 else None
    ingest(target_leg)
