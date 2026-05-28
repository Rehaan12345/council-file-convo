"""
Fetches and parses an LA City Clerk "Council and Committee Referral Hot Sheet"
HTML page, extracting council file numbers and titles.

Example URL:
  https://ens.lacity.org/clk/referralmemo/clkreferralmemo9197529_05192026.htm

Extraction strategy: regex on cfnumber= query params in <a href> links,
then pair each with the nearest <strong>Title:</strong> that follows it.
"""
import asyncio
import re
import subprocess
from datetime import datetime
from html import unescape

# Matches cfnumber=XX-XXXX or cfnumber=XX-XXXX-SN (case-insensitive)
_CF_RE = re.compile(r'cfnumber=([0-9]{2}-[0-9]{4}(?:-S\d+)?)', re.IGNORECASE)

# Matches the title text immediately after <strong>Title:</strong>
_TITLE_RE = re.compile(r'<strong[^>]*>\s*Title:\s*</strong>([^<]+)', re.IGNORECASE)

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def date_to_hs_id(date: str) -> str:
    """Convert a hot sheet date string to a legislation ID.

    'May 19, 2026' → 'HS-2026-05-19'
    Falls back to today's date if parsing fails.
    """
    m = re.match(r'(\w+)\s+(\d{1,2}),\s+(\d{4})', date.strip())
    if m:
        month = _MONTH_MAP.get(m.group(1).lower())
        if month:
            return f"HS-{m.group(3)}-{month}-{m.group(2).zfill(2)}"
    return f"HS-{datetime.now().strftime('%Y-%m-%d')}"


# Date pattern in the page header e.g. "May 19, 2026"
_DATE_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+\d{1,2},\s+\d{4}',
    re.IGNORECASE,
)


async def fetch_and_parse(url: str) -> dict:
    """
    Fetch a hot sheet URL and return:
    {
        "date": "May 19, 2026",
        "entries": [
            {
                "full_id":   "26-0900-S10",   # as it appears in the hot sheet
                "base_file": "26-0900",        # ChromaDB collection key
                "branch":    "26-0900-S10",    # branch subfolder (null if no -Sx)
                "title":     "Street lighting assessment..."
            },
            ...
        ]
    }
    Entries are deduplicated by full_id and returned in document order.
    """
    # ens.lacity.org resets TLS connections from Python 3.13's OpenSSL.
    # curl uses the system SSL stack and works reliably, so shell out to it.
    def _fetch() -> str:
        result = subprocess.run(
            ["curl", "-s", "-L", "--insecure", "-A", "Mozilla/5.0",
             "--max-time", "20", url],
            capture_output=True, text=True, timeout=25,
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr.strip()}")
        return result.stdout

    loop = asyncio.get_event_loop()
    html = await loop.run_in_executor(None, _fetch)

    # Extract date from page (first match)
    date_match = _DATE_RE.search(html)
    date = date_match.group(0) if date_match else ""

    # Collect positions of all cfnumber= occurrences
    cf_matches = list(_CF_RE.finditer(html))

    # Collect positions of all Title: occurrences
    title_matches = list(_TITLE_RE.finditer(html))

    seen: set[str] = set()
    entries: list[dict] = []

    for cf_match in cf_matches:
        full_id = cf_match.group(1).strip()
        if full_id in seen:
            continue
        seen.add(full_id)

        # Parse base_file and branch from full_id
        branch_match = re.match(r'^(.+)-(S\d+)$', full_id, re.IGNORECASE)
        if branch_match:
            base_file = branch_match.group(1)
            branch: str | None = full_id
        else:
            base_file = full_id
            branch = None

        # Find the first title that appears after this cfnumber in the HTML
        title = ""
        cf_pos = cf_match.start()
        for t_match in title_matches:
            if t_match.start() > cf_pos:
                title = unescape(t_match.group(1).strip())
                break

        entries.append({
            "full_id":   full_id,
            "base_file": base_file,
            "branch":    branch,
            "title":     title,
        })

    return {"date": date, "entries": entries}
