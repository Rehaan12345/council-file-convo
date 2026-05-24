"""
Fetches and parses an LA City Clerk "Council and Committee Referral Hot Sheet"
HTML page, extracting council file numbers and titles.

Example URL:
  https://ens.lacity.org/clk/referralmemo/clkreferralmemo9197529_05192026.htm

Extraction strategy: regex on cfnumber= query params in <a href> links,
then pair each with the nearest <strong>Title:</strong> that follows it.
"""
import re
from html import unescape

import httpx

# Matches cfnumber=XX-XXXX or cfnumber=XX-XXXX-SN (case-insensitive)
_CF_RE = re.compile(r'cfnumber=([0-9]{2}-[0-9]{4}(?:-S\d+)?)', re.IGNORECASE)

# Matches the title text immediately after <strong>Title:</strong>
_TITLE_RE = re.compile(r'<strong[^>]*>\s*Title:\s*</strong>([^<]+)', re.IGNORECASE)

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
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    html = resp.text

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
