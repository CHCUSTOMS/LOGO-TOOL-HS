"""
compare_mshsl.py
----------------
Compare school names and mascots from MaxPreps (mn.json) against MSHSL.org.
Prints rows where either the school name or mascot doesn't match exactly.

Usage:
    python compare_mshsl.py

Output:
    compare_mshsl_results.csv  — full mismatch table
    (also printed to console)

Caches MSHSL page fetches to .scrape_cache/mshsl_schools.json so re-runs
don't re-scrape the whole site.
"""

import csv
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

MN_JSON      = Path("hs_packs/mn.json")
OUTPUT_CSV   = Path("compare_mshsl_results.csv")
CACHE_FILE   = Path(".scrape_cache/mshsl_schools.json")
BASE_URL     = "https://www.mshsl.org"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://www.mshsl.org/",
}

_STOP = re.compile(
    r"\b(high|school|senior|academy|area|community|christian|charter|home|of|the)\b",
    re.IGNORECASE,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _norm(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = _STOP.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


def _slug_to_display(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


# ── MSHSL listing scrape ───────────────────────────────────────────────────────

def scrape_listing(session: requests.Session) -> dict[str, dict]:
    """
    Returns {slug: {"display_name": str, "norm": str}}
    for every school on the MSHSL /schools listing.
    """
    result: dict[str, dict] = {}
    slug_re = re.compile(r"^/schools/([^/?#]+)$")
    page = 0
    while True:
        url = f"{BASE_URL}/schools?page={page}"
        try:
            r = session.get(url, timeout=15)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        links = [a for a in soup.find_all("a", href=True) if slug_re.match(a["href"])]
        if not links:
            break
        for a in links:
            slug = slug_re.match(a["href"]).group(1)
            if "home-school" in slug:
                continue
            # Prefer the link text as display name; fall back to slug
            display = a.get_text(" ", strip=True) or _slug_to_display(slug)
            if not display:
                display = _slug_to_display(slug)
            result[slug] = {
                "display_name": display,
                "norm":         _norm(display),
            }
        print(f"  listing page {page}: {len(links)} schools")
        page += 1
        time.sleep(0.15)
    return result


# ── MSHSL school page scrape ───────────────────────────────────────────────────

def scrape_school_page(slug: str, session: requests.Session) -> dict:
    """
    Fetch one school page and return {"name": str, "mascot": str | None}.
    Looks for the mascot in: page <title>, <h1>/<h2>, meta description.
    """
    url = f"{BASE_URL}/schools/{slug}"
    try:
        r = session.get(url, timeout=15)
    except requests.RequestException:
        return {"name": _slug_to_display(slug), "mascot": None}
    if r.status_code != 200:
        return {"name": _slug_to_display(slug), "mascot": None}

    soup = BeautifulSoup(r.text, "html.parser")

    # School name: prefer h1, fall back to title
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = h1.get_text(" ", strip=True)
    if not name and soup.title:
        name = soup.title.string or ""
        name = re.split(r"\s*[|\-–]\s*", name)[0].strip()

    # Mascot: look for it after the school name in title/h1/h2
    mascot = None
    candidates = []
    if soup.title and soup.title.string:
        candidates.append(soup.title.string)
    for tag in soup.find_all(["h1", "h2", "h3"]):
        candidates.append(tag.get_text(" ", strip=True))
    # Meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        candidates.append(meta["content"])

    # Pattern: "School Name Mascots" or "School Name - Mascots"
    for text in candidates:
        m = re.search(r"[-–|]\s*([A-Z][a-zA-Z\s]+?)(?:\s*[-–|]|$)", text)
        if m:
            candidate = m.group(1).strip()
            # Skip if it looks like a site name rather than a mascot
            if candidate.lower() not in ("mshsl", "minnesota state high school league", "home"):
                mascot = candidate
                break

    return {"name": name or _slug_to_display(slug), "mascot": mascot}


# ── Cache ──────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    CACHE_FILE.parent.mkdir(exist_ok=True)
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ── Matching ───────────────────────────────────────────────────────────────────

def best_match(norm_team: str, mshsl_schools: dict[str, dict]) -> str | None:
    """Return the best-matching MSHSL slug for a normalised MaxPreps team name."""
    # 1. Exact norm match
    for slug, info in mshsl_schools.items():
        if info["norm"] == norm_team:
            return slug
    # 2. Subset token match
    team_tokens = set(norm_team.split())
    best_slug, best_score = None, 0
    for slug, info in mshsl_schools.items():
        school_tokens = set(info["norm"].split())
        inter = team_tokens & school_tokens
        shorter = min(len(team_tokens), len(school_tokens))
        if shorter >= 2 and len(inter) == shorter:
            if shorter > best_score:
                best_score = shorter
                best_slug = slug
    return best_slug


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Load MaxPreps data
    data = json.loads(MN_JSON.read_text(encoding="utf-8"))
    mp_teams = [
        {"id": t["id"], "name": t["name"], "mascot": t.get("mascot", "")}
        for t in data.get("teams", [])
    ]
    print(f"MaxPreps teams: {len(mp_teams)}")

    session = _session()
    cache   = load_cache()

    # Scrape MSHSL listing (or use cache)
    if "listing" not in cache:
        print("Scraping MSHSL school listing…")
        cache["listing"] = scrape_listing(session)
        save_cache(cache)
    mshsl_schools = cache["listing"]
    print(f"MSHSL schools: {len(mshsl_schools)}")

    # Scrape individual pages (cached per slug)
    if "pages" not in cache:
        cache["pages"] = {}

    results = []
    for i, team in enumerate(mp_teams):
        norm = _norm(team["name"])
        slug = best_match(norm, mshsl_schools)

        if slug is None:
            results.append({
                "id":           team["id"],
                "mp_name":      team["name"],
                "mp_mascot":    team["mascot"],
                "mshsl_name":   "",
                "mshsl_mascot": "",
                "issue":        "NO MSHSL MATCH",
            })
            continue

        # Fetch school page if not cached
        if slug not in cache["pages"]:
            print(f"  [{i+1}/{len(mp_teams)}] fetching {slug}…")
            cache["pages"][slug] = scrape_school_page(slug, session)
            if (i + 1) % 20 == 0:
                save_cache(cache)
            time.sleep(0.2)

        page_data = cache["pages"][slug]
        mshsl_name   = page_data["name"]
        mshsl_mascot = page_data["mascot"] or ""

        name_match   = team["name"].strip().lower() == mshsl_name.strip().lower()
        mascot_match = team["mascot"].strip().lower() == mshsl_mascot.strip().lower()

        if not name_match or not mascot_match:
            issues = []
            if not name_match:
                issues.append("name")
            if not mascot_match:
                issues.append("mascot")
            results.append({
                "id":           team["id"],
                "mp_name":      team["name"],
                "mp_mascot":    team["mascot"],
                "mshsl_name":   mshsl_name,
                "mshsl_mascot": mshsl_mascot,
                "issue":        "+".join(issues),
            })

    save_cache(cache)

    # Output
    print(f"\n{'─'*90}")
    print(f"{'ID':<40} {'ISSUE':<12} {'MP NAME / MASCOT':<35} {'MSHSL NAME / MASCOT'}")
    print(f"{'─'*90}")
    for r in results:
        mp   = f"{r['mp_name']} / {r['mp_mascot']}"
        ms   = f"{r['mshsl_name']} / {r['mshsl_mascot']}" if r["mshsl_name"] else "—"
        print(f"{r['id']:<40} {r['issue']:<12} {mp:<35} {ms}")

    print(f"\n{len(results)} mismatches out of {len(mp_teams)} teams.")

    # Save CSV
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id","mp_name","mp_mascot","mshsl_name","mshsl_mascot","issue"])
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved → {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
