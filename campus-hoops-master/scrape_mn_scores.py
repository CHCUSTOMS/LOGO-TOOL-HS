"""
scrape_mn_scores.py — Minnesota-Scores.net adapter

Scrapes QRF ratings and section standings from https://www.minnesota-scores.net
to supplement MaxPreps data for MN boys basketball.

What this provides per team:
  • MSHSL section assignment  (section_id / section_name)
  • Section W/L record        (conf_wins / conf_losses, record_type="section")
  • Overall W/L record        (ovr_wins / ovr_losses)
  • QRF quality rating        (rating) — raw float from /qrf/{season}/overall
  • Logo URL when present     (logo_url)

QRF source
----------
The raw QRF float values are on /qrf/{season}/overall (one table, all classes).
Section-standings pages only show section QRF *ranks*, not values — so those
pages are used only for section assignments and W/L records.

Outer-merge notes
-----------------
Some teams appear on mn-scores but NOT MaxPreps (small / new programs).
Some MaxPreps teams are not on mn-scores (data gaps on either side).
This adapter returns only what mn-scores has; scrape_hs_leagues.py decides
how to handle unmatched teams.

TODO: section-as-conference reorganisation
  Once this data is used, a future pass should reorganise MN output so that
  MSHSL sections become the "conferences" in the data pack (sections determine
  playoff seeding, which maps better to Campus Hoops tournament structure).
  That requires restructuring conf_teams_map in scrape_state.
"""

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from supplements import TeamSupplement, norm_name, merge_supplement

BASE_URL   = "https://www.minnesota-scores.net"
SPORT_PATH = "boys-sports/basketball"
CACHE_DIR  = Path(".scrape_cache")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":         "https://www.minnesota-scores.net/",
}

# Classes in descending length order — important for exact-match checks
_CLASSES = ["AAAA", "AAA", "AA", "A"]

# Known starting page ID for section-standings (2025-2026 = 136)
_STANDINGS_START = 136


# ── Session ────────────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


# ── Section ID helpers ─────────────────────────────────────────────────────────

def _section_key(cls: str, num: int, suffix: str = "") -> str:
    """Canonical conference-id string used in the data pack."""
    key = f"mn_section_{num}{cls.lower()}"
    return f"{key}_{suffix.lower()}" if suffix else key


def _section_display(cls: str, num: int, suffix: str = "") -> str:
    label = f"Section {num}{cls}"
    return f"{label} {suffix.capitalize()}" if suffix else label


# ── Overall QRF page ───────────────────────────────────────────────────────────

def _parse_qrf_overall_page(html: str) -> dict[str, float]:
    """
    Parse /qrf/{season}/overall → {norm_name: raw_qrf_float}.
    Table columns: Rank | Team / Record | Class | QRF
    """
    soup   = BeautifulSoup(html, "html.parser")
    result: dict[str, float] = {}

    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        team_col = next((i for i, h in enumerate(headers) if "team" in h), None)
        qrf_col  = next((i for i, h in enumerate(headers) if h == "qrf"), None)
        if team_col is None or qrf_col is None:
            continue

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(team_col, qrf_col):
                continue
            raw = cells[team_col].get_text(" ", strip=True)
            # Strip " (W-L)" record suffix
            name = re.sub(r"\s*\(\d+-\d+\)\s*$", "", raw).strip()
            # Strip elimination/clinch prefix: "e-Lanesboro" → "Lanesboro"
            name = re.sub(r"^[a-z]-", "", name).strip()
            if not name or len(name) < 2:
                continue
            try:
                qrf = float(cells[qrf_col].get_text(" ", strip=True))
            except ValueError:
                continue
            result[norm_name(name)] = qrf

    return result


def fetch_qrf(
    season:  str,
    session: requests.Session | None = None,
) -> dict[str, float]:
    """
    Fetch overall QRF float values for the season.
    Returns {norm_name → raw QRF float}.  Results are cached per season.
    """
    cache_file = CACHE_DIR / f"mn_qrf_{season.replace('-', '_')}.json"
    CACHE_DIR.mkdir(exist_ok=True)
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    s   = session or _session()
    url = f"{BASE_URL}/{SPORT_PATH}/qrf/{season}/overall"
    try:
        r = s.get(url, timeout=15)
    except requests.RequestException as exc:
        print(f"  [warn] mn-scores QRF fetch failed: {exc}")
        return {}
    if r.status_code != 200:
        print(f"  [warn] mn-scores QRF HTTP {r.status_code} for season {season}")
        return {}

    result = _parse_qrf_overall_page(r.text)
    print(f"    mn-scores QRF overall ({season}): {len(result)} teams")
    if result:
        cache_file.write_text(json.dumps(result))
    return result


# ── Section-standings probe ────────────────────────────────────────────────────

def probe_class_page_ids(
    season: str,
    session: requests.Session | None = None,
) -> dict[str, int]:
    """
    Find the numeric page ID for each class by inspecting h3/h4 headings on
    section-standings pages.  Each page covers all sections for one class and
    uses headings like "Section 1A Standings" or "Section 1AAAA Standings".

    Returns {"A": 136, "AA": 137, "AAA": 138, "AAAA": 139} (example).
    """
    cache_file = CACHE_DIR / f"mn_scores_ids_{season.replace('-', '_')}.json"
    CACHE_DIR.mkdir(exist_ok=True)
    if cache_file.exists():
        return json.loads(cache_file.read_text())

    s      = session or _session()
    result: dict[str, int] = {}

    probe_range = (
        list(range(_STANDINGS_START, _STANDINGS_START + 80))
        + list(range(max(80, _STANDINGS_START - 60), _STANDINGS_START))
    )
    for sid in probe_range:
        if len(result) == len(_CLASSES):
            break
        url = f"{BASE_URL}/{SPORT_PATH}/section-standings/{season}/{sid}"
        try:
            r = s.get(url, timeout=10)
        except requests.RequestException:
            time.sleep(0.1)
            continue
        if r.status_code != 200:
            time.sleep(0.05)
            continue

        # Determine class from h3/h4 headings like "Section 1A Standings".
        # A{1,4} is greedy — correctly captures "AAAA" in "Section 1AAAA".
        soup       = BeautifulSoup(r.text, "html.parser")
        page_class = None
        for tag in soup.find_all(["h3", "h4"]):
            text = tag.get_text(" ", strip=True)
            m    = re.search(r"Section\s+\d+(A{1,4})", text, re.IGNORECASE)
            if m:
                suffix = m.group(1).upper()
                for cls in _CLASSES:
                    if suffix == cls:
                        page_class = cls
                        break
                if page_class:
                    break

        if page_class and page_class not in result:
            result[page_class] = sid
            print(f"    mn-scores standings id {sid} → Class {page_class}")

        time.sleep(0.15)

    if result:
        cache_file.write_text(json.dumps(result))
    if len(result) < len(_CLASSES):
        missing = [c for c in _CLASSES if c not in result]
        print(f"  [warn] mn-scores: could not find standings page IDs for classes: {missing}")

    return result


# ── HTML parsing (section-standings) ──────────────────────────────────────────

_WL_RE   = re.compile(r"^(\d+)-(\d+)$")
_SEC_RE  = re.compile(r"[Ss]ection\s+(\d+)(?:\s*[-–]\s*(\w+))?")


def _parse_wl(text: str) -> tuple[int, int] | None:
    m = _WL_RE.match(text.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _col_map(headers: list[str]) -> dict[str, int]:
    """
    Map logical column names to their index given a list of header strings.
    Handles both split-column (separate W / L) and combined "W-L" layouts.
    """
    col: dict[str, int] = {}
    for i, raw in enumerate(headers):
        h = raw.strip().lower()
        if "team" in h and "team" not in col:
            col["team"] = i
        elif re.match(r"^(cw|sw|s\.?w\.?|section\s*w(ins?)?)$", h) and "conf_w" not in col:
            col["conf_w"] = i
        elif re.match(r"^(cl|sl|s\.?l\.?|section\s*l(oss(es)?)?)$", h) and "conf_l" not in col:
            col["conf_l"] = i
        elif re.match(r"^section$", h) and "conf_wl" not in col:
            col["conf_wl"] = i
        elif re.match(r"^nc[wl]$", h):
            pass
        elif re.match(r"^(w|ow|wins?)$", h) and "ovr_w" not in col:
            col["ovr_w"] = i
        elif re.match(r"^(l|ol|loss(es)?)$", h) and "ovr_l" not in col:
            col["ovr_l"] = i
        elif re.match(r"^overall$", h) and "ovr_wl" not in col:
            col["ovr_wl"] = i
    return col


def _parse_class_page(html: str, cls: str) -> list[dict]:
    """
    Parse one class page (e.g., Class A — all sections shown together).
    Returns a list of row dicts with keys:
        name, norm, section_id, section_name,
        conf_w, conf_l, ovr_w, ovr_l, logo_url
    (QRF is NOT parsed here — section pages only have section QRF ranks,
     not the raw float values; use fetch_qrf() for actual values.)
    """
    soup  = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []

    current_num    = 0
    current_suffix = ""

    for el in soup.descendants:
        if not hasattr(el, "name") or not el.name:
            continue

        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6", "caption", "strong"):
            text = el.get_text(" ", strip=True)
            m = _SEC_RE.search(text)
            if m:
                current_num    = int(m.group(1))
                current_suffix = (m.group(2) or "").strip()
                continue

        if el.name != "table" or current_num == 0:
            continue

        cells_rows = el.find_all("tr")
        if not cells_rows:
            continue

        header_cells = cells_rows[0].find_all(["th", "td"])
        headers      = [c.get_text(" ", strip=True) for c in header_cells]
        col          = _col_map(headers)

        if "team" not in col and len(headers) >= 2:
            col["team"] = 1

        sid   = _section_key(cls, current_num, current_suffix)
        sname = _section_display(cls, current_num, current_suffix)

        for row in cells_rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]

            joined = " ".join(texts[:4]).lower()
            if any(kw in joined for kw in ("team", "seed", "section", "overall", "qrf")):
                continue

            name     = ""
            logo_url = ""
            tcell    = cells[col.get("team", 1)]
            a_tag    = tcell.find("a")
            name     = (a_tag or tcell).get_text(" ", strip=True)
            img      = tcell.find("img")
            if img and img.get("src", ""):
                src      = img["src"]
                logo_url = (BASE_URL + src) if src.startswith("/") else src

            # Strip leading standings indicators: "e ", "x ", "y " etc.
            name = re.sub(r"^[a-z]\s+", "", name.strip())
            if not name or len(name) < 2 or name.isdigit():
                continue

            conf_w = conf_l = ovr_w = ovr_l = None

            if "conf_wl" in col:
                wl = _parse_wl(texts[col["conf_wl"]])
                if wl:
                    conf_w, conf_l = wl
            else:
                if "conf_w" in col:
                    try: conf_w = int(texts[col["conf_w"]])
                    except ValueError: pass
                if "conf_l" in col:
                    try: conf_l = int(texts[col["conf_l"]])
                    except ValueError: pass

            if "ovr_wl" in col:
                wl = _parse_wl(texts[col["ovr_wl"]])
                if wl:
                    ovr_w, ovr_l = wl
            else:
                if "ovr_w" in col:
                    try: ovr_w = int(texts[col["ovr_w"]])
                    except ValueError: pass
                if "ovr_l" in col:
                    try: ovr_l = int(texts[col["ovr_l"]])
                    except ValueError: pass

            if ovr_w is None and ovr_l is None:
                wl_found = [_parse_wl(t) for t in texts if _parse_wl(t)]
                if len(wl_found) >= 2:
                    conf_w, conf_l = wl_found[0]
                    ovr_w,  ovr_l  = wl_found[-1]
                elif len(wl_found) == 1:
                    ovr_w, ovr_l = wl_found[0]

            rows.append({
                "name":         name,
                "norm":         norm_name(name),
                "section_id":   sid,
                "section_name": sname,
                "conf_w":       conf_w,
                "conf_l":       conf_l,
                "ovr_w":        ovr_w,
                "ovr_l":        ovr_l,
                "logo_url":     logo_url or None,
            })

    return rows


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch(
    season:  str,
    session: requests.Session | None = None,
) -> dict[str, TeamSupplement]:
    """
    Main entry: return {normalised_name → TeamSupplement} for all MN teams
    found on minnesota-scores.net for the given season.

    QRF float values come from /qrf/{season}/overall (one page, all classes).
    Section assignments + W/L records come from section-standings pages.
    The two are merged by normalised team name.

    Outer-merge note: teams present here but absent from MaxPreps are NOT
    added to the data pack automatically.  The caller receives this dict and
    decides how to handle unmatched entries.
    """
    s = session or _session()

    # ── Step 1: QRF float values (primary signal) ──────────────────────────────
    qrf_by_norm = fetch_qrf(season, s)

    # ── Step 2: Section assignments + W/L records ──────────────────────────────
    section_entries: dict[str, dict] = {}
    page_ids = probe_class_page_ids(season, s)

    for cls in ["A", "AA", "AAA", "AAAA"]:
        page_id = page_ids.get(cls)
        if page_id is None:
            continue
        url = f"{BASE_URL}/{SPORT_PATH}/section-standings/{season}/{page_id}"
        try:
            r = s.get(url, timeout=15)
        except requests.RequestException as exc:
            print(f"  [warn] mn-scores Class {cls} standings: request failed — {exc}")
            continue
        if r.status_code != 200:
            print(f"  [warn] mn-scores Class {cls} standings: HTTP {r.status_code}")
            continue

        entries = _parse_class_page(r.text, cls)
        print(f"    mn-scores Class {cls} standings: {len(entries)} teams parsed")

        for e in entries:
            n = e["norm"]
            if not n:
                continue
            if n in section_entries:
                # Keep whichever entry has more data
                existing = section_entries[n]
                if e["ovr_w"] is not None and existing["ovr_w"] is None:
                    section_entries[n] = e
            else:
                section_entries[n] = e

        time.sleep(0.2)

    # ── Step 3: Merge QRF + section info ──────────────────────────────────────
    result: dict[str, TeamSupplement] = {}
    all_norms = set(qrf_by_norm) | set(section_entries)

    for n in all_norms:
        e   = section_entries.get(n, {})
        qrf = qrf_by_norm.get(n)
        supp = TeamSupplement(
            ovr_wins=e.get("ovr_w"),
            ovr_losses=e.get("ovr_l"),
            conf_wins=e.get("conf_w"),
            conf_losses=e.get("conf_l"),
            record_type="section",
            rating=qrf,
            logo_url=e.get("logo_url"),
            section_id=e.get("section_id"),
            section_name=e.get("section_name"),
            source="mn_scores",
        )
        result[n] = supp

    qrf_hits    = sum(1 for s in result.values() if s.rating is not None)
    section_hits = sum(1 for s in result.values() if s.section_id is not None)
    print(f"  mn-scores: {len(result)} unique teams  "
          f"({qrf_hits} with QRF, {section_hits} with section info)")
    return result
