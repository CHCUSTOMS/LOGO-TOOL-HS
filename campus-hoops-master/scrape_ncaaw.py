"""
scrape_ncaaw.py — Women's D-1 NCAA Basketball pack generator

Produces:
    ncaa_packs/Campus Hoops 2027 D-1 NCAAW Teams 68 NCAAT.json

Sources:
  1. ESPN JSON API       -> D-I conference rosters + team IDs -> logoUrl
  2. Sports-Reference CSVs (manually downloaded) -> OSRS -> offenseRating,
                           DSRS -> defenseRating, SRS -> prestige history
  3. Men's D-1 JSON     -> primaryColor, secondaryColor, state,
                           pipelineStates, abbreviation fallback

CSV prep (do once per season):
  For each year in PRESTIGE_YEARS, visit:
    https://www.sports-reference.com/cbb/seasons/women/{year}-ratings.html
  Click "Share & more" -> "Get table as CSV", save as:
    ncaa_packs/sr_women_ratings/{year}.csv
  (e.g. 2026.csv = 2025-26 season)

Usage:
    python scrape_ncaaw.py
"""

import csv
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

# -- Config ---------------------------------------------------------------------

CURRENT_YEAR   = 2026   # Sports-Reference ending year for the active season
PRESTIGE_YEARS = list(range(2026, 2006, -1))   # 2026..2007 (20 years)
PRESTIGE_DECAY = 0.90   # each older year worth 90% of the next newer year

SR_CSV_DIR  = Path("ncaa_packs/sr_women_ratings")
MEN_JSON    = Path("ncaa_packs/Campus Hoops 2027 D-1 Teams 68 NCAAT.json")
OUTPUT_JSON = Path("ncaa_packs/Campus Hoops 2027 D-1 NCAAW Teams 68 NCAAT.json")
CACHE_DIR   = Path(".scrape_cache")

ESPN_API   = "https://site.api.espn.com/apis/site/v2/sports/basketball/womens-college-basketball"
LOGO_TMPL  = "https://a.espncdn.com/combiner/i?img=/i/teamlogos/ncaa/500/{}.png"

DEFAULT_PRIMARY   = "#004B8D"
DEFAULT_SECONDARY = "#FFFFFF"

# -- Women's-specific metadata --------------------------------------------------

META = {
    "name": "2027 women's teams",
    "version": 1,
    "author": "nalex13",
    "description": "D-I women's basketball teams with 2026-2027 conference alignment and 68-team NCAAW Tournament",
}

BRANDING = {
    "nationalTournament": "NCAA Tournament",
    "roundNames": {
        "firstFour": "First Four",
        "roundOf64": "Round of 64",
        "roundOf32": "Round of 32",
        "sweet16": "Sweet Sixteen",
        "elite8": "Elite Eight",
        "finalFour": "Final Four",
        "championship": "National Championship",
    },
    "regionNames": ["Albany", "Greenville", "Portland", "Spokane"],
    "hallOfFame": "Women's Basketball Hall of Fame",
}

AWARDS = {
    "mvp":             "Wade Trophy",
    "dpoy":            "Naismith Defensive Player of the Year",
    "freshman":        "WBCA Freshman of the Year",
    "mostImproved":    "Most Improved Player",
    "sixthMan":        "Sixth Player of the Year",
    "coachOfYear":     "WBCA Coach of the Year",
    "tournamentMop":   "Most Outstanding Player",
    "allAmerican":     "WBCA All-American",
    "allConference":   "All-Conference",
    "allTournament":   "All-Tournament Team",
    "positionOfYear":  "Position of the Year",
    "nationalChampion": "National Champion",
    "confChampion":    "Conference Champion",
}

HS_AWARDS = {
    "nationalPoy":   "Naismith Prep Player of the Year",
    "allAmerican":   "McDonald's All-American",
    "statePoy":      "Gatorade State Player of the Year",
    "eliteShowcase": "USA Basketball U18 Women's National Team Tryout",
}

# -- ESPN conference abbreviation -> JSON id ------------------------------------
# Keyed by ESPN web-API child["abbreviation"] (lowercase).

ESPN_CONF_MAP: dict[str, str] = {
    "acc":      "acc",
    "aeast":    "americaEast",
    "american": "americanAthletic",
    "a-sun":    "asun",
    "atl10":    "a10",
    "big10":    "bigTen",
    "big12":    "big12",
    "bige":     "bigEast",
    "bsky":     "bigSky",
    "bsou":     "bigSouth",
    "bigw":     "bigWest",
    "col":      "caa",
    "usa":      "conferenceUSA",
    "hor":      "horizon",
    "ivy":      "ivy",
    "maac":     "maac",
    "midam":    "mac",
    "meac":     "meac",
    "mvc":      "mvc",
    "mwest":    "mountainWest",
    "neast":    "nec",
    "ovc":      "ovc",
    "pat":      "patriot",
    "sec":      "sec",
    "south":    "southern",
    "land":     "southland",
    "swac":     "swac",
    "summ":     "summit",
    "belt":     "sunBelt",
    "wcc":      "wcc",
    "wac":      "uac",
}


# -- ESPN -> Sports-Reference name aliases --------------------------------------
# ESPN uses short/official school abbreviations; SR spells names out in full.
# Keys are _norm(espn_location); values are _norm(sr_school_name).

_ESPN_SR_ALIASES: dict[str, str] = {
    "uconn":                "connecticut",
    "lsu":                  "louisiana state",
    "ole miss":             "mississippi",
    "usc":                  "southern california",
    "unlv":                 "nevada las vegas",
    "ualbany":              "albany ny",
    "uic":                  "illinois chicago",
    "umbc":                 "maryland baltimore county",
    "vcu":                  "virginia commonwealth",
    "smu":                  "southern methodist",
    "tcu":                  "texas christian",
    "byu":                  "brigham young",
    "ul monroe":            "louisiana monroe",
    "ut rio grande valley": "texas rio grande valley",
    "hawai i":              "hawaii",   # apostrophe stripped by _norm
    "pitt":                 "pittsburgh",
    "umass":                "massachusetts",
    "unc":                  "north carolina",
    "uncw":                 "north carolina wilmington",
    "uncg":                 "north carolina greensboro",
    "utsa":                 "texas san antonio",
    "utep":                 "texas el paso",
    "fiu":                  "florida international",
    "fau":                  "florida atlantic",
    "wku":                  "western kentucky",
    "niu":                  "northern illinois",
    "siu":                  "southern illinois",
    "siue":                 "southern illinois edwardsville",
    "lmu":                  "loyola marymount",
    "csu bakersfield":      "california state bakersfield",
    "csu fullerton":        "california state fullerton",
    "csu northridge":       "california state northridge",
    "csun":                 "california state northridge",
    "csuf":                 "california state fullerton",
    "csub":                 "california state bakersfield",
    "sfbay":                "san francisco",
    "ucr":                  "california riverside",
    "ucd":                  "california davis",
    "ucsb":                 "california santa barbara",
    "ucsd":                 "california san diego",
    "iupui":                "indiana university purdue university indianapolis",
    "njit":                 "new jersey institute technology",
    "uta":                  "texas arlington",
    "utc":                  "tennessee chattanooga",
    "utm":                  "tennessee martin",
    "etsu":                 "east tennessee state",
    "sfpa":                 "saint francis pennsylvania",
    "ipfw":                 "purdue fort wayne",
    "app state":            "appalachian state",
    "se louisiana":         "southeastern louisiana",
    "loyola maryland":      "loyola md",
    "southern miss":        "southern mississippi",
    "miami":                "miami fl",
}

# ESPN women's name -> men's JSON norm, for cases where men's team uses a
# different name/abbreviation than SR (so _ESPN_SR_ALIASES value won't match).
_ESPN_MEN_ALIASES: dict[str, str] = {
    "ualbany":              "albany",
    "ut rio grande valley": "utrgv",
}


# -- Utilities ------------------------------------------------------------------

_STOP_WORDS = {"university", "of", "at", "the", "and", "&", "a", "in"}
_ST_RE      = re.compile(r"\bst\.?\s+", re.I)
_STRIP_RE   = re.compile(r"[^a-z0-9 ]")
_WS_RE      = re.compile(r"\s+")

# Words that are part of "Lady X" mascots — kept in women's name, stripped for
# slug generation.
_LADY_RE = re.compile(r"\blady\b", re.I)


def _norm(name: str) -> str:
    # Strip accents (é→e, etc.) so diacritics don't break exact matching
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = _ST_RE.sub("saint ", s.strip())
    s = _STRIP_RE.sub(" ", s.lower())
    return _WS_RE.sub(" ", s).strip()


def _slugify(name: str) -> str:
    s = _LADY_RE.sub("", name)
    s = _norm(s)
    return re.sub(r"\s+", "-", s).strip("-")


def _token_overlap(a: str, b: str) -> float:
    ta = {w for w in a.split() if w not in _STOP_WORDS and len(w) > 1}
    tb = {w for w in b.split() if w not in _STOP_WORDS and len(w) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _to_offense_defense(norm: float) -> int:
    return max(50, min(99, round(50 + norm * 49)))


def _to_prestige(norm: float) -> int:
    return max(25, min(95, round(25 + norm * 70)))


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    mn, mx = min(values), max(values)
    span = mx - mn
    if span < 1e-9:
        return [0.5] * len(values)
    return [(v - mn) / span for v in values]


# -- Sports-Reference CSV loading -----------------------------------------------

def _load_sr_csv(path: Path) -> list[dict]:
    """
    Parse one SR ratings CSV.  Handles the two header row variants SR exports:
      - single header row (clean export)
      - first row may contain repeated headers or rank='' separator rows

    Returns list of {school, srs, osrs, dsrs} with floats where available.
    """
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = None
        for raw in reader:
            if not any(raw):
                continue
            # Detect header row: contains "School" or "school"
            joined_lower = " ".join(raw).lower()
            if header is None:
                if "school" in joined_lower:
                    header = [c.strip().lower() for c in raw]
                continue
            # Skip repeated header rows embedded in data
            if "school" in joined_lower or raw[0].strip().lower() in ("rk", "rank", ""):
                continue
            row = dict(zip(header, [c.strip() for c in raw]))
            school = row.get("school") or row.get("team") or ""
            if not school:
                continue
            # Strip footnote markers SR sometimes appends (e.g. "Connecticut*")
            school = re.sub(r"[*†‡]", "", school).strip()

            def _f(key: str) -> float | None:
                for k in (key, key.upper(), key.lower()):
                    v = row.get(k, "").strip()
                    if v and v not in ("", "—"):
                        try:
                            return float(v)
                        except ValueError:
                            pass
                return None

            rows.append({
                "school": school,
                "norm":   _norm(school),
                "srs":    _f("srs"),
                "osrs":   _f("osrs"),
                "dsrs":   _f("dsrs"),
            })
    return rows


def load_all_sr(csv_dir: Path) -> dict[int, dict[str, dict]]:
    """
    Load all yearly CSVs from csv_dir.
    Returns {year: {norm_school: row_dict}}.
    """
    result: dict[int, dict[str, dict]] = {}
    for path in sorted(csv_dir.glob("*.csv")):
        try:
            year = int(path.stem)
        except ValueError:
            print(f"  [warn] SR CSV: unexpected filename {path.name} — expected YYYY.csv, skipping")
            continue
        rows = _load_sr_csv(path)
        by_norm: dict[str, dict] = {}
        for r in rows:
            by_norm[r["norm"]] = r
        result[year] = by_norm
        print(f"  SR {year}: {len(by_norm)} teams loaded")
    return result


# -- ESPN API -------------------------------------------------------------------

_ESPN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_ESPN_HEADERS)
    return s


def _map_conf(espn_abbrev: str) -> str | None:
    """Map ESPN conference abbreviation (lowercased) to the JSON conference id."""
    if not espn_abbrev:
        return None
    return ESPN_CONF_MAP.get(espn_abbrev.lower().strip())


def fetch_espn_teams(
    session: requests.Session | None = None,
    season:  int = CURRENT_YEAR,
) -> dict[str, dict]:
    """
    Fetch all D-I women's teams via ESPN standings web API, grouped by conference.
    Returns {espn_id_str: {espn_id, name, mascot, abbrev, conf_id, conf_name}}.
    """
    cache_file = CACHE_DIR / f"espn_ncaaw_teams_{season}.json"
    CACHE_DIR.mkdir(exist_ok=True)
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    s = session or _session()
    teams: dict[str, dict] = {}
    unknown_confs: list[str] = []

    url = "https://site.web.api.espn.com/apis/v2/sports/basketball/womens-college-basketball/standings"
    params = {
        "region": "us", "lang": "en", "contentorigin": "espn",
        "type": "0", "level": "3", "sort": "leaguestandings",
        "season": season,
    }
    try:
        r = s.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"  [error] ESPN standings fetch failed: {exc}")
        return {}

    for conf_group in data.get("children", []):
        conf_abbrev  = conf_group.get("abbreviation", "")
        conf_display = conf_group.get("name", "")
        conf_id = _map_conf(conf_abbrev)
        if conf_id is None:
            unknown_confs.append(f"{conf_abbrev} ({conf_display})")
            conf_id = conf_abbrev.lower() or "unknown"

        for entry in conf_group.get("standings", {}).get("entries", []):
            team = entry.get("team", {})
            eid  = str(team.get("id", ""))
            if not eid:
                continue
            # location = school name,  name = mascot/nickname in this API
            location = team.get("location", "")
            mascot   = team.get("name", "")
            display  = team.get("displayName", "")
            abbrev   = team.get("abbreviation", "")
            teams[eid] = {
                "espn_id":   eid,
                "name":      location,
                "mascot":    mascot,
                "display":   display,
                "abbrev":    abbrev,
                "conf_id":   conf_id,
                "conf_name": conf_display,
            }
        time.sleep(0.05)

    if unknown_confs:
        print(f"  [warn] ESPN confs not in ESPN_CONF_MAP: {unknown_confs}")

    print(f"  ESPN: {len(teams)} women's D-I teams fetched")
    if teams:
        cache_file.write_text(json.dumps(teams, indent=2))
    return teams


# -- Men's JSON -----------------------------------------------------------------

def load_men_json(path: Path) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    """
    Load men's D-1 JSON.
    Returns (by_norm, by_abbrev, conferences).
    by_norm   keyed by _norm(team name)
    by_abbrev keyed by team abbreviation uppercase
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    by_norm:   dict[str, dict] = {}
    by_abbrev: dict[str, dict] = {}
    for t in data.get("teams", []):
        by_norm[_norm(t["name"])] = t
        abbrev = t.get("abbreviation", "").upper().strip()
        if abbrev:
            by_abbrev[abbrev] = t
    return by_norm, by_abbrev, data.get("conferences", [])


# -- Name matching --------------------------------------------------------------

def _match_sr(espn_name: str, sr_by_norm: dict[str, dict]) -> dict | None:
    """
    Match an ESPN school name to a Sports-Reference school entry.
    SR spells names out fully (e.g. "Connecticut") while ESPN uses short forms
    ("UConn"). _ESPN_SR_ALIASES bridges the most common divergences.
    """
    en = _norm(espn_name)

    # 1. Exact norm match
    if en in sr_by_norm:
        return sr_by_norm[en]

    # 2. Pre-built alias (ESPN short name -> SR full name)
    alias = _ESPN_SR_ALIASES.get(en)
    if alias and alias in sr_by_norm:
        return sr_by_norm[alias]

    # 3. Token-overlap fuzzy match
    best, best_score = None, 0.0
    for sn, row in sr_by_norm.items():
        score = _token_overlap(en, sn)
        if score > best_score:
            best_score = score
            best = row

    return best if best_score >= 0.5 else None


def _match_men(
    espn_name: str,
    men_by_norm: dict[str, dict],
    men_by_abbrev: dict[str, dict] | None = None,
    espn_abbrev: str = "",
) -> dict | None:
    """Match an ESPN women's school name to a men's JSON team entry."""
    en = _norm(espn_name)

    # 1. Exact norm match
    if en in men_by_norm:
        return men_by_norm[en]

    # 2. Men-specific aliases (ESPN name -> men's norm, differs from SR norm)
    men_alias = _ESPN_MEN_ALIASES.get(en)
    if men_alias and men_alias in men_by_norm:
        return men_by_norm[men_alias]

    # 3. Reuse ESPN->SR aliases since men's norm keys usually match SR norms
    alias = _ESPN_SR_ALIASES.get(en)
    if alias and alias in men_by_norm:
        return men_by_norm[alias]

    # 3. ESPN abbreviation -> men's abbreviation (handles FAU/FIU/USF/LIU etc.)
    if espn_abbrev and men_by_abbrev:
        hit = men_by_abbrev.get(espn_abbrev.upper())
        if hit:
            return hit

    # 4. Token-overlap fuzzy match (last resort — needs score > 0.5 to avoid
    #    false positives from generic shared tokens like "state" or "florida")
    best, best_score = None, 0.0
    for mn, entry in men_by_norm.items():
        score = _token_overlap(en, mn)
        if score > best_score:
            best_score = score
            best = entry

    return best if best_score > 0.5 else None


# -- Rating calculations --------------------------------------------------------

def compute_ratings(
    espn_teams: dict[str, dict],
    sr_current: dict[str, dict],
    sr_all: dict[int, dict[str, dict]],
) -> dict[str, tuple[int, int]]:
    """
    Returns {espn_id: (offenseRating, defenseRating)}.

    Normalization bounds are derived from ALL years in sr_all so current
    ratings are in historical context — the best team only earns 99 if it's
    truly the best team across the full lookback window.
    Teams without SR data get (65, 65) as a conservative default.
    """
    # Build global OSRS/DSRS bounds from all available years
    all_osrs: list[float] = []
    all_dsrs: list[float] = []
    for yr_data in sr_all.values():
        for row in yr_data.values():
            if row["osrs"] is not None:
                all_osrs.append(row["osrs"])
            if row["dsrs"] is not None:
                all_dsrs.append(row["dsrs"])

    o_min, o_max = (min(all_osrs), max(all_osrs)) if all_osrs else (0.0, 1.0)
    d_min, d_max = (min(all_dsrs), max(all_dsrs)) if all_dsrs else (0.0, 1.0)
    o_span = o_max - o_min or 1.0
    d_span = d_max - d_min or 1.0

    rated: dict[str, tuple[int, int]] = {}
    for eid, team in espn_teams.items():
        row = _match_sr(team["name"], sr_current)
        if row and row["osrs"] is not None and row["dsrs"] is not None:
            o_norm = (row["osrs"] - o_min) / o_span
            d_norm = (row["dsrs"] - d_min) / d_span
            rated[eid] = (_to_offense_defense(o_norm), _to_offense_defense(d_norm))
        else:
            rated[eid] = (65, 65)

    return rated


def _prestige_weights(available_years: list[int]) -> dict[int, float]:
    """
    Compute geometric decay weights for the years we actually have data for.
    Weight for year index i (0=most recent) = PRESTIGE_DECAY^i, then normalized
    so all weights sum to 1.
    """
    sorted_years = sorted(available_years, reverse=True)
    raw = {yr: PRESTIGE_DECAY ** i for i, yr in enumerate(sorted_years)}
    total = sum(raw.values())
    return {yr: w / total for yr, w in raw.items()}


def compute_prestige(espn_teams: dict[str, dict],
                     sr_all: dict[int, dict[str, dict]]) -> dict[str, int]:
    """
    Returns {espn_id: prestige} via weighted SRS average across available years.

    Per-year SRS is normalized to [0,1] within that year's pool.
    Weights follow geometric decay (PRESTIGE_DECAY) so older years contribute
    less but aren't negligible.
    Final scores are end-normalized across all teams to fill [25, 95] so the
    full range is always utilized.
    """
    # Pre-normalize each available year's SRS pool
    year_norms: dict[int, dict[str, float]] = {}
    for year in PRESTIGE_YEARS:
        rows = sr_all.get(year, {})
        if not rows:
            continue
        vals = [r["srs"] for r in rows.values() if r["srs"] is not None]
        if not vals:
            continue
        mn, mx = min(vals), max(vals)
        span = mx - mn
        year_norms[year] = {
            n: (r["srs"] - mn) / span if span > 1e-9 else 0.5
            for n, r in rows.items()
            if r["srs"] is not None
        }

    weights = _prestige_weights(list(year_norms.keys()))

    # Compute weighted score for each team
    raw_scores: dict[str, float] = {}
    for eid, team in espn_teams.items():
        total_w = total_s = 0.0
        for year, weight in weights.items():
            yn = year_norms[year]
            row = _match_sr(team["name"], sr_all.get(year, {}))
            if row:
                srs_norm = yn.get(row["norm"])
                if srs_norm is not None:
                    total_s += srs_norm * weight
                    total_w += weight
        raw_scores[eid] = total_s / total_w if total_w > 1e-9 else None

    # End-normalize across all teams so [25, 95] is fully utilized
    scored = {eid: s for eid, s in raw_scores.items() if s is not None}
    if scored:
        s_min = min(scored.values())
        s_max = max(scored.values())
        s_span = s_max - s_min or 1.0
    else:
        s_min = s_span = 1.0

    result: dict[str, int] = {}
    for eid in espn_teams:
        s = raw_scores.get(eid)
        if s is not None:
            result[eid] = _to_prestige((s - s_min) / s_span)
        else:
            result[eid] = 45

    return result


# -- ID generation --------------------------------------------------------------

def _team_id(espn_name: str, men_match: dict | None) -> str:
    """
    Use the same id as the men's entry when available; otherwise slugify the
    ESPN school name (Lady prefix stripped for slug stability).
    """
    if men_match:
        return men_match["id"]
    return _slugify(espn_name)


# -- Conferences list -----------------------------------------------------------

def build_conferences(men_confs: list[dict],
                      used_conf_ids: set[str]) -> list[dict]:
    """
    Start from the men's conference list; keep all confs used by women's teams.
    Any conf id not in the men's list gets a conservative default entry.
    """
    by_id = {c["id"]: c for c in men_confs}
    result = []
    for cid in sorted(used_conf_ids):
        if cid in by_id:
            result.append(by_id[cid])
        else:
            print(f"  [warn] conference '{cid}' not in men's JSON — adding with defaults")
            result.append({
                "id":              cid,
                "name":            cid,
                "abbreviation":    cid.upper()[:4],
                "isPower":         False,
                "logoUrl":         None,
                "prestigeFloor":   25,
                "prestigeCeiling": 70,
            })
    return result


# -- Main -----------------------------------------------------------------------

def main():
    # -- Load sources ----------------------------------------------------------
    print("Loading Sports-Reference CSVs…")
    if not SR_CSV_DIR.exists():
        print(f"  [error] CSV directory not found: {SR_CSV_DIR}")
        print("  Create the directory and add yearly CSVs (2021.csv … 2026.csv).")
        return
    sr_all = load_all_sr(SR_CSV_DIR)
    if not sr_all:
        print("  [error] No CSVs found in", SR_CSV_DIR)
        return
    sr_current = sr_all.get(CURRENT_YEAR, {})
    if not sr_current:
        print(f"  [warn] No SR data for current year {CURRENT_YEAR}")

    print("\nLoading men's JSON for color/state/pipeline fallback…")
    men_by_norm, men_by_abbrev, men_confs = load_men_json(MEN_JSON)
    print(f"  {len(men_by_norm)} men's teams loaded")

    print("\nFetching ESPN women's D-I teams…")
    session     = _session()
    espn_teams  = fetch_espn_teams(session)
    if not espn_teams:
        print("[error] ESPN fetch returned no teams. Check API or run again.")
        return

    # -- Compute ratings --------------------------------------------------------
    print("\nComputing ratings and prestige…")
    ratings  = compute_ratings(espn_teams, sr_current, sr_all)
    prestige = compute_prestige(espn_teams, sr_all)

    # -- Build team entries -----------------------------------------------------
    print("\nBuilding team entries…")
    teams        = []
    used_conf_ids: set[str] = set()
    no_sr_match  = []
    no_men_match = []

    for eid, team in sorted(espn_teams.items(), key=lambda x: x[1]["name"]):
        conf_id = team["conf_id"]
        if conf_id == "unknown":
            print(f"  [skip] {team['display']} — unknown conference, excluded")
            continue
        used_conf_ids.add(conf_id)

        men = _match_men(team["name"], men_by_norm, men_by_abbrev, team.get("abbrev", ""))
        if men is None:
            no_men_match.append(team["display"])

        sr_row = _match_sr(team["name"], sr_current)
        if sr_row is None:
            no_sr_match.append(team["display"])

        off_r, def_r = ratings.get(eid, (65, 65))
        pres         = prestige.get(eid, 45)

        # ID: reuse men's id when the school exists in both files
        team_id = _team_id(team["name"], men)

        # Mascot: use ESPN's women's mascot (may differ from men, e.g. Lady Vols)
        mascot = team.get("mascot") or (men["mascot"] if men else "")

        # Abbrev: use ESPN's abbreviation; fall back to men's
        abbrev = team.get("abbrev") or (men.get("abbreviation", "") if men else "")

        # Colors: inherit from men's entry when available
        primary   = men["primaryColor"]   if men else DEFAULT_PRIMARY
        secondary = men["secondaryColor"] if men else DEFAULT_SECONDARY

        # State / pipeline: inherit from men's
        state    = men.get("state", "")          if men else ""
        pipeline = men.get("pipelineStates", []) if men else []

        logo_url = LOGO_TMPL.format(eid)

        teams.append({
            "id":              team_id,
            "name":            team["name"],
            "mascot":          mascot,
            "abbreviation":    abbrev,
            "conferenceId":    conf_id,
            "primaryColor":    primary,
            "secondaryColor":  secondary,
            "offenseRating":   off_r,
            "defenseRating":   def_r,
            "prestige":        pres,
            "state":           state,
            "pipelineStates":  pipeline,
            "logoUrl":         logo_url,
        })

    # -- Build conferences ------------------------------------------------------
    conferences = build_conferences(men_confs, used_conf_ids)

    # -- Report gaps -----------------------------------------------------------
    if no_sr_match:
        print(f"\n  [info] {len(no_sr_match)} teams with no SR match "
              f"(defaulted to 65/65 ratings):")
        for n in sorted(no_sr_match)[:20]:
            print(f"    {n}")
        if len(no_sr_match) > 20:
            print(f"    … and {len(no_sr_match)-20} more")

    if no_men_match:
        print(f"\n  [info] {len(no_men_match)} teams not in men's JSON "
              f"(defaulted colors/state):")
        for n in sorted(no_men_match)[:20]:
            print(f"    {n}")
        if len(no_men_match) > 20:
            print(f"    … and {len(no_men_match)-20} more")

    # -- Assemble output --------------------------------------------------------
    # Carry over non-team sections from the men's file
    men_data = json.loads(MEN_JSON.read_text(encoding="utf-8"))

    output = {
        "meta":        META,
        "branding":    BRANDING,
        "awards":      AWARDS,
        "hsAwards":    HS_AWARDS,
        "rules":       men_data.get("rules", {}),
        "conferences": conferences,
        "teams":       teams,
    }

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n{'-'*60}")
    print(f"  Teams written:      {len(teams)}")
    print(f"  Conferences:        {len(conferences)}")
    print(f"  SR current-year:    {len(sr_current)} teams")
    print(f"  Saved -> {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
