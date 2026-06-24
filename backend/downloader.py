"""
Downloads PDFs for a council file directly from the LA City Clerk's online
records and saves them locally so ingest.py can index them.

Each council file has a record page that links its PDFs from /onlinedocs/:
  https://cityclerk.lacity.org/lacityclerkconnect/index.cfm?fa=ccfi.viewrecord&cfnumber=19-0036
A non-existent file (or branch) returns the page with zero PDF links.

Supports two modes:
  - Single file:  download_and_extract("22-0100", ...)
  - All branches: download_all_branches("26-0900", last_branch=15, ...)
                  → saves each Sx into DOCS_PATH/26-0900/26-0900-Sx/
"""
import asyncio
import re
from pathlib import Path

import httpx

CFMS_BASE = "https://cityclerk.lacity.org"
RECORD_URL = CFMS_BASE + "/lacityclerkconnect/index.cfm?fa=ccfi.viewrecord&cfnumber={cf}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Council file PDFs are linked as /onlinedocs/<year>/<name>.pdf
_PDF_RE = re.compile(r"/onlinedocs/[^\"'\s]+?\.pdf", re.IGNORECASE)


async def _fetch_pdf_urls(cf: str, client: httpx.AsyncClient) -> list[str]:
    """Fetch the council file record page and return unique absolute PDF URLs."""
    resp = await client.get(RECORD_URL.format(cf=cf), headers=_HEADERS)
    resp.raise_for_status()
    urls = [CFMS_BASE + path for path in _PDF_RE.findall(resp.text)]
    return list(dict.fromkeys(urls))  # dedupe, preserve order


async def probe_branch_exists(base_file: str, n: int) -> bool:
    """Return True if {base_file}-Sn has PDFs, False otherwise."""
    cf = f"{base_file}-S{n}"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            return len(await _fetch_pdf_urls(cf, client)) > 0
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


async def download_and_extract(
    council_file: str,
    docs_path: Path,
    dest_folder_override: Path | None = None,
    cleanup: bool = False,
    job_status: dict | None = None,
) -> tuple[Path, int]:
    """
    Download every PDF linked from `council_file`'s record page into
    `dest_folder_override` if given, otherwise `docs_path / council_file /`.

    Returns (dest_folder, pdf_count).
    """
    dest_folder = dest_folder_override or (docs_path / council_file)
    dest_folder.mkdir(parents=True, exist_ok=True)

    def _set(msg: str):
        if job_status:
            job_status["message"] = msg
        print(f"[downloader] {msg}")

    _set(f"Connecting to city clerk archive for {council_file}...")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        urls = await _fetch_pdf_urls(council_file, client)
        if not urls:
            _set(f"No PDFs found for {council_file}")
            return dest_folder, 0

        count = 0
        for i, url in enumerate(urls, 1):
            _set(f"Downloading {council_file}... {i}/{len(urls)}")
            try:
                resp = await client.get(url, headers=_HEADERS)
                resp.raise_for_status()
            except Exception as e:
                print(f"[downloader] WARNING: failed to download {url}: {e}")
                continue
            (dest_folder / url.rsplit("/", 1)[-1]).write_bytes(resp.content)
            count += 1

    _set(f"Downloaded {count} PDFs for {council_file}")
    print(f"[downloader] Saved to {dest_folder}")

    if cleanup:
        import shutil
        shutil.rmtree(dest_folder)
        print(f"[downloader] Cleaned up {dest_folder}")

    return dest_folder, count


async def download_single_pdf(
    url: str,
    dest_folder: Path,
    job_status: dict | None = None,
) -> tuple[Path, int]:
    """
    Download one specific PDF from a direct URL into `dest_folder`.
    Used by the "Single PDF" flow where the user pastes a single onlinedocs link.

    Returns (dest_folder, pdf_count) — pdf_count is 1 on success, 0 on failure.
    """
    dest_folder.mkdir(parents=True, exist_ok=True)

    def _set(msg: str):
        if job_status:
            job_status["message"] = msg
        print(f"[downloader] {msg}")

    filename = url.rsplit("/", 1)[-1].split("?")[0]
    _set(f"Downloading {filename}...")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url, headers=_HEADERS)
        resp.raise_for_status()
        content = resp.content

    (dest_folder / filename).write_bytes(content)
    _set(f"Downloaded {filename}")
    print(f"[downloader] Saved to {dest_folder}")
    return dest_folder, 1


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
