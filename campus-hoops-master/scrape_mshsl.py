"""
Scrape school logos from the Minnesota State High School League (MSHSL) website.
Used as a secondary logo source for MN teams when MaxPreps logos are absent or poor.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.mshsl.org"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.mshsl.org/",
}

# Words stripped from both sides before comparison
_STOP = re.compile(
    r"\b(high|school|senior|academy|area|community|christian|charter|home|of|the)\b",
    re.IGNORECASE,
)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _norm(name: str) -> str:
    """Normalise a school name for matching: lowercase, strip stop-words, collapse spaces."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = _STOP.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


def _slug_to_name(slug: str) -> str:
    """Convert an MSHSL URL slug to a display name (title-case, hyphens → spaces)."""
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


def fetch_school_list(session: requests.Session | None = None) -> dict[str, str]:
    """
    Scrape the paginated /schools listing and return a dict of
    normalised_name -> slug for every MSHSL school.
    Skips home-school entries.
    """
    s = session or _session()
    result: dict[str, str] = {}
    _slug_re = re.compile(r"^/schools/([^/?#]+)$")
    page = 0
    while True:
        url = f"{BASE_URL}/schools?page={page}"
        try:
            r = s.get(url, timeout=15)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        slugs = [
            m.group(1)
            for a in soup.find_all("a", href=True)
            if (m := _slug_re.match(a["href"]))
        ]
        if not slugs:
            break
        for slug in slugs:
            if "home-school" in slug:
                continue
            norm = _norm(_slug_to_name(slug))
            if norm and slug not in result.values():
                result[norm] = slug
        page += 1
        time.sleep(0.15)  # polite crawl rate
    return result


def fetch_logo_url(slug: str, session: requests.Session | None = None) -> str | None:
    """
    Fetch the MSHSL school page for *slug* and return the first logo image URL,
    or None if not found.  Returns a fully-qualified https:// URL.
    """
    s = session or _session()
    url = f"{BASE_URL}/schools/{slug}"
    try:
        r = s.get(url, timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "/sites/default/files/" in src and "/logos/" in src:
            return (BASE_URL + src) if src.startswith("/") else src
    return None


def find_match(team_name: str, school_list: dict[str, str]) -> str | None:
    """
    Given a MaxPreps team name and the MSHSL school list (normalised_name → slug),
    return the best-matching slug or None.

    Priority:
    1. Exact normalised match
    2. One name's normalised tokens are a subset of the other's (handles cases like
       "St. Francis" ↔ "Saint Francis High School")
    """
    norm_team = _norm(team_name)
    if not norm_team:
        return None

    # 1. Exact
    if norm_team in school_list:
        return school_list[norm_team]

    # 2. Subset token match
    team_tokens = set(norm_team.split())
    best_slug: str | None = None
    best_score = 0
    for norm_school, slug in school_list.items():
        school_tokens = set(norm_school.split())
        # Score = intersection size; require all of the shorter set to be present
        inter = team_tokens & school_tokens
        shorter = min(len(team_tokens), len(school_tokens))
        if shorter >= 2 and len(inter) == shorter:
            score = shorter
            if score > best_score:
                best_score = score
                best_slug = slug
    return best_slug
