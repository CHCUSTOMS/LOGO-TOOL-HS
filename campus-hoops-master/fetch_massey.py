"""
fetch_massey.py — Scrape Massey Ratings for HS basketball
https://masseyratings.com/hsbb{year}/{state}/ratings

Uses undetected-chromedriver (non-headless, off-screen) to bypass Cloudflare.
Results cached to .scrape_cache/massey_{state}_{year}.json.

Table structure:
  Headers: Team | Rec | Rat | Pwr | Off | Def | HFA | SoS
  Each numeric cell: main text = column rank, <div class="detail"> = actual value.
  Off / Def: higher = better for both.
  Rat: overall power rating — higher = better, used for prestige history.

Public API:
    fetch_massey_state_year(state, year)  → dict[norm_key, MasseyRating]
    fetch_massey_state(state)             → same, for CURRENT_YEAR
    normalize_massey(ratings)             → (off_norm, def_norm, ovr_norm)
    lookup(team_name, norm_dict)          → float | None  (fuzzy name match)
    lookup_name(team_name, name_map)      → str           (matched display name)
    close_driver()                        → quit the shared Selenium driver

Usage:
    import fetch_massey
    current = fetch_massey.fetch_massey_state("mn")
    hist    = fetch_massey.fetch_massey_state_year("mn", 2022)
    fetch_massey.close_driver()
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

CURRENT_YEAR  = 2026
CACHE_DIR     = Path(".scrape_cache")
_URL_TMPL     = "https://masseyratings.com/hsbb{year}/{state}/ratings"
_CF_WAIT_S    = 30   # max seconds to wait for Cloudflare JS challenge to clear
_SETTLE_S     = 2    # extra settle pause after challenge clears

# Full state names as appended by Massey to team names (e.g. "WayzataMinnesota").
# Used as a fallback when no <a> anchor is present in the team cell.
_STATE_FULL_NAMES: dict[str, str] = {
    "al":"Alabama",      "ak":"Alaska",        "az":"Arizona",       "ar":"Arkansas",
    "ca":"California",   "co":"Colorado",      "ct":"Connecticut",   "de":"Delaware",
    "fl":"Florida",      "ga":"Georgia",       "hi":"Hawaii",        "id":"Idaho",
    "il":"Illinois",     "in":"Indiana",       "ia":"Iowa",          "ks":"Kansas",
    "ky":"Kentucky",     "la":"Louisiana",     "me":"Maine",         "md":"Maryland",
    "ma":"Massachusetts","mi":"Michigan",      "mn":"Minnesota",     "ms":"Mississippi",
    "mo":"Missouri",     "mt":"Montana",       "ne":"Nebraska",      "nv":"Nevada",
    "nh":"New Hampshire","nj":"New Jersey",    "nm":"New Mexico",    "ny":"New York",
    "nc":"North Carolina","nd":"North Dakota", "oh":"Ohio",          "ok":"Oklahoma",
    "or":"Oregon",       "pa":"Pennsylvania",  "ri":"Rhode Island",  "sc":"South Carolina",
    "sd":"South Dakota", "tn":"Tennessee",     "tx":"Texas",         "ut":"Utah",
    "vt":"Vermont",      "va":"Virginia",      "wa":"Washington",    "wv":"West Virginia",
    "wi":"Wisconsin",    "wy":"Wyoming",
}

_driver = None   # singleton Selenium driver, reused across all fetches


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class MasseyRating:
    name:    str
    offense: float   # raw Massey Off rating (higher = better)
    defense: float   # raw Massey Def rating (higher = better)
    overall: float   # raw Massey Rat rating (higher = better; used for prestige)
    rank:    int = 0  # page rank (1-based row position)


# ── Name helpers ──────────────────────────────────────────────────────────────

def _norm_key(name: str) -> str:
    """Lowercase alphanum+space normalisation for fuzzy matching.
    Strips common school-type words so 'Jefferson High School', 'Jefferson High',
    and 'Jefferson' all produce the same key.
    """
    n = name.lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    # Strip school-type suffix words so "Oakwood Academy" and "Oakwood Acad" both → "oakwood".
    # "prep" is intentionally NOT stripped — it's meaningful in names like "Clarke Prep"
    # and stripping it causes collisions (e.g. "Southern Prep" ↔ "Southern Acad" → both "southern").
    for word in ("academy", "acad", "school", "hs", "charter", "magnet"):
        n = re.sub(rf"\b{re.escape(word)}\b", " ", n)
    # Strip trailing "high" only (e.g. "Jefferson High" → "jefferson")
    n = re.sub(r"\bhigh\s*$", " ", n.rstrip())
    return " ".join(n.split())


def _strip_state(raw: str, state: str) -> str:
    """Remove state full-name suffix Massey appends to team names."""
    full = _STATE_FULL_NAMES.get(state.lower(), "")
    if full and raw.endswith(full):
        return raw[: -len(full)].strip()
    return raw.strip()


# ── Selenium driver ───────────────────────────────────────────────────────────

def _get_driver():
    global _driver
    if _driver is not None:
        return _driver

    import undetected_chromedriver as uc

    opts = uc.ChromeOptions()
    opts.add_argument("--window-position=-10000,-10000")
    opts.add_argument("--window-size=1280,900")
    _driver = uc.Chrome(options=opts, version_main=148)
    return _driver


def close_driver():
    """Quit the shared Selenium driver. Call once when all fetches are done."""
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


# ── Table parsing ─────────────────────────────────────────────────────────────

def _col_index(headers: list[str], candidates: list[str]) -> int | None:
    for cand in candidates:
        for i, h in enumerate(headers):
            if h.strip().lower() == cand:
                return i
    return None


def _cell_rating(cell) -> float:
    """
    Extract the actual rating value from a Massey cell.
    Main text = column rank; <div class="detail"> = actual value.
    """
    detail = cell.find(class_="detail")
    if detail:
        txt = detail.get_text(strip=True).replace(",", "")
    else:
        txt = cell.get_text(separator=" ", strip=True).replace(",", "")
    try:
        return float(txt)
    except ValueError:
        return 0.0


def _cell_rank(cell) -> int:
    """Extract the rank integer (main cell text, excluding the detail div)."""
    import copy
    c = copy.copy(cell)
    detail = c.find(class_="detail")
    if detail:
        detail.extract()
    try:
        return int(c.get_text(strip=True))
    except ValueError:
        return 0


def _parse_table(html: str, state: str) -> dict[str, "MasseyRating"]:
    from bs4 import BeautifulSoup
    import copy

    soup   = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    table  = max(tables, key=lambda t: len(t.find_all("tr")), default=None) if tables else None
    if table is None:
        return {}

    rows = table.find_all("tr")
    if not rows:
        return {}

    header_cells = rows[0].find_all(["th", "td"])
    hdrs = [c.get_text(strip=True) for c in header_cells]

    name_idx = _col_index(hdrs, ["team", "school", "name"])
    ovr_idx  = _col_index(hdrs, ["rat", "rating", "rtg", "rate", "overall"])
    off_idx  = _col_index(hdrs, ["off", "offense", "o", "oe"])
    def_idx  = _col_index(hdrs, ["def", "defense", "d", "de"])
    if name_idx is None:
        name_idx = 0

    result: dict[str, MasseyRating] = {}

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        name_cell   = cells[name_idx] if name_idx < len(cells) else None
        name_anchor = name_cell.find("a") if name_cell else None
        name_raw    = (name_anchor.get_text(strip=True) if name_anchor
                       else (name_cell.get_text(strip=True) if name_cell else ""))
        if not name_raw or name_raw.lower().startswith("correlation"):
            continue

        name = _strip_state(name_raw, state)
        if not name:
            continue

        def _fval(idx):
            return _cell_rating(copy.copy(cells[idx])) if idx is not None and idx < len(cells) else 0.0

        ovr       = _fval(ovr_idx)
        off       = _fval(off_idx)
        def_      = _fval(def_idx)
        page_rank = _cell_rank(cells[ovr_idx]) if ovr_idx is not None and ovr_idx < len(cells) else 0

        if ovr == 0.0 and (off != 0.0 or def_ != 0.0):
            ovr = (off + def_) / 2.0

        key = _norm_key(name)
        if key:
            result[key] = MasseyRating(name=name, offense=off, defense=def_, overall=ovr, rank=page_rank)

    return result


# ── Core fetch ────────────────────────────────────────────────────────────────

def fetch_massey_state_year(state: str, year: int) -> dict[str, MasseyRating]:
    """
    Return {normalized_name: MasseyRating} for the given state and season year.
    Year is the ending year of the season: 2026 = 2025-26, 2022 = 2021-22, etc.
    Results cached to .scrape_cache/massey_{state}_{year}.json.
    Returns empty dict on failure or when the page has no data.
    """
    state = state.lower()
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"massey_{state}_{year}.json"

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            # Re-apply current _norm_key from the stored display name so cached data
            # stays consistent even when _norm_key stripping rules change.
            return {
                _norm_key(v["name"]): MasseyRating(
                    name=v["name"], offense=v["offense"], defense=v["defense"],
                    overall=v["overall"], rank=v.get("rank", 0),
                )
                for k, v in data.items()
                if v.get("name")
            }
        except Exception:
            cache_file.unlink(missing_ok=True)

    url = _URL_TMPL.format(year=year, state=state)
    print(f"    [massey] Fetching {url}…", flush=True)

    try:
        from selenium.webdriver.support.ui import WebDriverWait

        driver = _get_driver()
        driver.get(url)

        WebDriverWait(driver, _CF_WAIT_S).until(
            lambda d: "moment" not in d.title.lower()
        )
        time.sleep(_SETTLE_S)

        result = _parse_table(driver.page_source, state)

    except Exception as exc:
        print(f"    [massey] Failed {state}/{year}: {exc}")
        return {}

    if result:
        cache_file.write_text(
            json.dumps(
                {k: {"name": v.name, "offense": v.offense, "defense": v.defense,
                     "overall": v.overall, "rank": v.rank}
                 for k, v in result.items()},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"    [massey] {state}/{year}: {len(result)} teams cached")
    else:
        # Page loaded but had no data — write an empty sentinel so we don't retry
        cache_file.write_text("{}", encoding="utf-8")
        print(f"    [massey] {state}/{year}: page loaded, no data")

    return result


def fetch_massey_state(state: str) -> dict[str, MasseyRating]:
    """Convenience wrapper: fetch the current season (CURRENT_YEAR)."""
    return fetch_massey_state_year(state, CURRENT_YEAR)


# ── Normalization helpers ─────────────────────────────────────────────────────

def normalize_massey(
    ratings: dict[str, MasseyRating],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """
    Normalize Off, Def, and Rat to [0, 1] independently.
    Returns (off_norm, def_norm, ovr_norm), each keyed by normalized name.
    Higher raw value → higher normalized value (higher is better for all three).
    """
    if not ratings:
        return {}, {}, {}

    def _scale(vals: dict[str, float]) -> dict[str, float]:
        mn, mx = min(vals.values()), max(vals.values())
        span = mx - mn or 1.0
        return {k: (v - mn) / span for k, v in vals.items()}

    return (
        _scale({k: r.offense for k, r in ratings.items()}),
        _scale({k: r.defense for k, r in ratings.items()}),
        _scale({k: r.overall for k, r in ratings.items()}),
    )


def normalize_rat_prestige(ratings: dict[str, "MasseyRating"]) -> dict[str, float]:
    """
    Normalize Rat (overall) to [25, 95] within a single year's pool.
    Returns {norm_key: float} where float ∈ [25.0, 95.0].
    Per-year normalization neutralizes Massey scale changes across seasons so
    that a weighted average of these values reflects historical strength fairly.
    """
    if not ratings:
        return {}
    vals = {k: r.overall for k, r in ratings.items()}
    mn, mx = min(vals.values()), max(vals.values())
    span = mx - mn or 1.0
    return {k: 25.0 + (v - mn) / span * 70.0 for k, v in vals.items()}


# ── Name variant expansion (for lookup) ──────────────────────────────────────

# Common abbreviated ↔ full-word pairs in school names
_ABBREV_EXPAND   = {
    "st":   "saint",
    "mt":   "mount",
    "ft":   "fort",
    "chr":  "christian",
    "bapt": "baptist",
    "luth": "lutheran",
    "intl": "international",
    "co":   "county",
    "mtn":  "mountain",
    "val":  "valley",
}
_ABBREV_CONTRACT = {v: k for k, v in _ABBREV_EXPAND.items()}


def _name_variants(key: str) -> list[str]:
    """
    Return a list of alternative normalised forms of a school name to catch
    abbreviation mismatches (e.g. 'st cloud' ↔ 'saint cloud').
    Only the first matching token in the name is substituted per variant.
    """
    variants: list[str] = [key]
    tokens = key.split()
    for i, tok in enumerate(tokens):
        for mapping in (_ABBREV_EXPAND, _ABBREV_CONTRACT):
            if tok in mapping:
                new_tokens = tokens[:i] + [mapping[tok]] + tokens[i + 1:]
                variants.append(" ".join(new_tokens))
    return variants


def _fuzzy_score(a: str, b: str) -> float:
    """
    Best similarity score between two normalised school names.
    Uses SequenceMatcher on both the original and token-sorted strings so that
    word-order differences (e.g. 'de lasalle' vs 'lasalle de') are handled.
    Falls back to rapidfuzz.token_sort_ratio when the library is available.
    """
    try:
        from rapidfuzz import fuzz
        return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0
    except ImportError:
        import difflib
        direct = difflib.SequenceMatcher(None, a, b).ratio()
        a_s = " ".join(sorted(a.split()))
        b_s = " ".join(sorted(b.split()))
        sorted_ = difflib.SequenceMatcher(None, a_s, b_s).ratio()
        return max(direct, sorted_)


# ── Fuzzy lookup ──────────────────────────────────────────────────────────────

def _match_key(team_name: str, candidates: dict) -> str | None:
    """
    Find the best-matching normalized key in candidates for team_name.
    Works on any dict keyed by norm_key (float values, str values, etc.).
    Matching priority:
      1. Exact key match + abbreviation variants
      2. Best fuzzy match ≥ 0.90 (single-token) or ≥ 0.82 (multi-token)
    Returns the matched key string, or None.
    """
    if not candidates:
        return None

    key = _norm_key(team_name)

    # Pass 1: exact match + abbreviation variants
    for variant in _name_variants(key):
        if variant in candidates:
            return variant

    # Pass 2: fuzzy matching — expand variants on both sides so abbreviation
    # differences ("chr" vs "christian") don't deflate the similarity score.
    key_variants    = _name_variants(key)
    key_tokens      = set(key.split())
    expanded_tokens = key_tokens | {t for v in key_variants for t in v.split()}
    n_key           = len(key_tokens)
    threshold       = 0.90 if n_key == 1 else 0.82

    best_key, best_score = None, 0.0
    for candidate in candidates:
        if n_key > 1 and not (set(candidate.split()) & expanded_tokens):
            continue
        cand_variants = _name_variants(candidate)
        score = max(
            _fuzzy_score(kv, cv)
            for kv in key_variants
            for cv in cand_variants
        )
        if score > best_score:
            best_score = score
            best_key   = candidate

    return best_key if best_score >= threshold else None


def lookup(team_name: str, norm_dict: dict[str, float]) -> float | None:
    """
    Look up a team's score from a Massey norm dict by fuzzy name matching.
    team_name is the MaxPreps display name.

    Matching priority:
      1. Exact key match
      2. Abbreviation-expanded / contracted variants (st ↔ saint, mt ↔ mount …)
      3. Best fuzzy match via SequenceMatcher (or rapidfuzz if installed),
         accepting scores ≥ 0.90 for single-token names, ≥ 0.82 for multi-token.
    """
    key = _match_key(team_name, norm_dict)
    return norm_dict[key] if key is not None else None


def lookup_name(team_name: str, name_map: dict[str, str]) -> str:
    """Return the Massey display name for team_name, or '' if no match found."""
    key = _match_key(team_name, name_map)
    return name_map[key] if key is not None else ""


# ── CLI test mode ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    args   = sys.argv[1:]
    states = [a for a in args if not a.isdigit()] or ["mn"]
    year   = int(next((a for a in args if a.isdigit()), CURRENT_YEAR))

    for st in states:
        print(f"\n=== {st.upper()} / {year} ===")
        data = fetch_massey_state_year(st, year)
        if data:
            off_n, def_n, ovr_n = normalize_massey(data)
            sample = sorted(data.items(), key=lambda x: x[1].rank)[:15]
            print(f"  {'Rk':>3}  {'Team':<35}  {'Off':>7}  {'Def':>7}  {'Rat':>6}  {'off_n':>6}  {'def_n':>6}  {'rat_n':>6}")
            for k, r in sample:
                print(f"  {r.rank:>3}  {r.name:<35}  {r.offense:>7.2f}  {r.defense:>7.2f}  "
                      f"{r.overall:>6.2f}  {off_n.get(k,0):>6.3f}  {def_n.get(k,0):>6.3f}  {ovr_n.get(k,0):>6.3f}")
            print(f"  … {len(data)} total teams")
        else:
            print("  (no data)")

    close_driver()
