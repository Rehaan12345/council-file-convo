"""
Downloads PDFs for a council file from the scrape-cf API and extracts them
locally so ingest.py can index them.

Supports two modes:
  - Single file:  download_and_extract("22-0100", ...)
  - All branches: download_all_branches("26-0900", last_branch=15, ...)
                  → extracts each Sx into DOCS_PATH/26-0900/26-0900-Sx/
"""
import asyncio
import io
import zipfile
from pathlib import Path

import httpx

SCRAPE_API = "https://scrape-cf.vercel.app"


async def probe_branch_exists(base_file: str, n: int) -> bool:
    """Return True if {base_file}-Sn has PDFs, False if the API returns 404."""
    cf = f"{base_file}-S{n}"
    url = f"{SCRAPE_API}/council-files/{cf}/pdf-links"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return False
            data = resp.json()
            return data.get("unique_pdf_count", 0) > 0
        except Exception:
            return False


async def discover_last_branch(base_file: str, known_min: int, job_status: dict | None = None) -> int:
    """
    Find the highest Sx that has PDFs, starting from known_min (confirmed to exist).
    Uses exponential doubling to bracket the upper bound, then binary search.
    Much faster than linear scan for files with many branches (e.g. 183 → ~15 probes).
    Returns the last valid branch number.
    """
    def _set(msg: str):
        if job_status:
            job_status["message"] = msg
        print(f"[downloader] {msg}")

    _set(f"Finding last branch of {base_file} (starting from S{known_min})...")

    # Phase 1: exponential doubling to bracket the upper bound
    lo = known_min   # confirmed to exist
    step = 1
    while await probe_branch_exists(base_file, lo + step):
        lo += step
        step *= 2
    hi = lo + step   # first confirmed non-existent

    _set(f"Narrowing down: between S{lo} and S{hi}...")

    # Phase 2: binary search within [lo, hi)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if await probe_branch_exists(base_file, mid):
            lo = mid
        else:
            hi = mid

    _set(f"Last branch of {base_file} is S{lo} ({lo} branches total)")
    return lo


async def _extract_zip_to(zip_bytes: bytes, dest_folder: Path) -> int:
    """Extract PDFs from a zip into dest_folder. Returns pdf count."""
    dest_folder.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".pdf"):
                pdf_name = Path(name).name
                (dest_folder / pdf_name).write_bytes(zf.read(name))
                count += 1
    return count


async def download_and_extract(
    council_file: str,
    docs_path: Path,
    dest_folder_override: Path | None = None,
    cleanup: bool = False,
    job_status: dict | None = None,
) -> tuple[Path, int]:
    """
    Download the zip of PDFs for `council_file` and extract into
    `dest_folder_override` if given, otherwise `docs_path / council_file /`.

    Returns (dest_folder, pdf_count).
    """
    url = f"{SCRAPE_API}/council-files/{council_file}/pdfs"
    dest_folder = dest_folder_override or (docs_path / council_file)
    dest_folder.mkdir(parents=True, exist_ok=True)

    def _set(msg: str):
        if job_status:
            job_status["message"] = msg
        print(f"[downloader] {msg}")

    _set(f"Connecting to city clerk archive for {council_file}...")

    zip_buffer = io.BytesIO()
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            downloaded = 0
            async for chunk in response.aiter_bytes(chunk_size=65536):
                zip_buffer.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded / total * 100)
                    _set(f"Downloading {council_file}... {pct}%")

    _set(f"Extracting PDFs for {council_file}...")
    pdf_count = await _extract_zip_to(zip_buffer.getvalue(), dest_folder)
    _set(f"Extracted {pdf_count} PDFs for {council_file}")
    print(f"[downloader] Saved to {dest_folder}")

    if cleanup:
        import shutil
        shutil.rmtree(dest_folder)
        print(f"[downloader] Cleaned up {dest_folder}")

    return dest_folder, pdf_count


async def download_all_branches(
    base_file: str,
    last_branch: int,
    docs_path: Path,
    job_status: dict | None = None,
    concurrency: int = 8,
) -> tuple[Path, int]:
    """
    Download S1 through S{last_branch} for base_file concurrently.
    Each branch is extracted into docs_path / base_file / {base_file}-Sx /
    so ingest_legislation() can read them all with proper subfolder citations.
    Branches already on disk (non-empty folder) are skipped.

    Returns (base_folder, total_pdf_count).
    """
    base_folder = docs_path / base_file
    base_folder.mkdir(parents=True, exist_ok=True)

    completed = 0
    total_pdfs = 0
    sem = asyncio.Semaphore(concurrency)

    def _set(msg: str):
        if job_status:
            job_status["message"] = msg
        print(f"[downloader] {msg}")

    async def download_one(n: int):
        nonlocal completed, total_pdfs
        branch = f"{base_file}-S{n}"
        dest = base_folder / branch

        # Skip branches already downloaded
        existing = list(dest.rglob("*.pdf")) if dest.exists() else []
        if existing:
            total_pdfs += len(existing)
            completed += 1
            _set(f"Downloading branches… {completed}/{last_branch} (S{n} cached)")
            return

        async with sem:
            try:
                _, count = await download_and_extract(
                    council_file=branch,
                    docs_path=docs_path,
                    dest_folder_override=dest,
                    job_status=None,   # suppress per-branch messages
                )
                total_pdfs += count
                print(f"[downloader] {branch}: {count} PDFs")
            except Exception as e:
                print(f"[downloader] WARNING: {branch} failed — {e}")
            completed += 1
            _set(f"Downloading branches… {completed}/{last_branch}")

    await asyncio.gather(*[download_one(n) for n in range(1, last_branch + 1)])
    _set(f"Downloaded all {last_branch} branches of {base_file} ({total_pdfs} PDFs total)")
    return base_folder, total_pdfs
