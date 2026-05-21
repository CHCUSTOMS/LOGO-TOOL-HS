"""
Campus Hoops Utility — Streamlit UI

Run with:
    streamlit run app.py
"""

import datetime
import io
import json
import math
import os
import shutil
import tempfile
import zipfile
from collections import deque

import pandas as pd
import requests
import streamlit as st
from better_profanity import profanity as _profanity
from PIL import Image, ImageDraw

import database as db
import verifier as vf
from recruiting import POSITION_ABBR, RecruitingPool, fmt_height
from save_loader import SaveFile

_profanity.load_censor_words()

st.set_page_config(
    page_title="Campus Hoops Mod Utility",
    layout="wide",
    menu_items={},
)
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
.stDeployButton {display: none !important;}
[data-testid="stToolbarActions"] {display: none !important;}
</style>
""", unsafe_allow_html=True)

# Set to True to show only the Data Pack page and hide save-file tooling.
DATAPACK_ONLY = True

st.title("Campus Hoops Mod Utility")
st.subheader("Developed by Nalex13")


# ================================================================== helpers

def _remove_white_bg(
    png_bytes: bytes,
    tolerance: int = 30,
    remove_enclosed: bool = False,
    enclosed_stroke: bool = False,
) -> bytes:
    """Remove background from a logo PNG.

    Pass 1 (always): BFS from every edge pixel whose colour matches the auto-detected
    background colour (median of all 4 border edges).  More robust than corner-only
    flood-fill and handles non-white solid backgrounds (e.g. purple).

    Pass 2 (opt-in): find near-white connected islands that are fully enclosed by
    non-transparent pixels and make them transparent too.  Useful for letter-counter
    holes (inside an O, P, R, etc.) but will also erase intentional enclosed white
    (e.g. white text inside a coloured banner), so it is off by default.

    enclosed_stroke (opt-in, requires remove_enclosed): after erasing enclosed regions,
    paint a 1-pixel white border around the newly transparent holes so the interior
    edges have a clean outline rather than a hard cut.
    """
    import numpy as np

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.array(img, dtype=np.uint8)
    h, w = arr.shape[:2]

    # Detect background colour from the median of all four border edges
    border = np.concatenate([
        arr[0, :, :3],
        arr[h - 1, :, :3],
        arr[1:h - 1, 0, :3],
        arr[1:h - 1, w - 1, :3],
    ])
    bg = np.median(border, axis=0).astype(np.int16)

    # Build a boolean mask: pixels whose colour is within `tolerance` of bg
    bg_mask = (
        (arr[:, :, 3] > 0) &
        (np.abs(arr[:, :, 0].astype(np.int16) - bg[0]) <= tolerance) &
        (np.abs(arr[:, :, 1].astype(np.int16) - bg[1]) <= tolerance) &
        (np.abs(arr[:, :, 2].astype(np.int16) - bg[2]) <= tolerance)
    )

    # Pass 1: BFS from every edge pixel that matches the background colour
    removed = np.zeros((h, w), dtype=bool)
    queue: deque = deque()
    for x in range(w):
        for y in [0, h - 1]:
            if bg_mask[y, x] and not removed[y, x]:
                removed[y, x] = True
                queue.append((y, x))
    for y in range(1, h - 1):
        for x in [0, w - 1]:
            if bg_mask[y, x] and not removed[y, x]:
                removed[y, x] = True
                queue.append((y, x))
    while queue:
        cy, cx = queue.popleft()
        arr[cy, cx, 3] = 0
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = cy + dy, cx + dx
            if 0 <= ny < h and 0 <= nx < w and not removed[ny, nx] and bg_mask[ny, nx]:
                removed[ny, nx] = True
                queue.append((ny, nx))

    # Pass 2 (opt-in): remove enclosed near-white islands (letter counter holes)
    if remove_enclosed:
        nw_thresh = 255 - tolerance
        nw_mask = (
            (arr[:, :, 3] > 0) &
            (arr[:, :, 0] >= nw_thresh) &
            (arr[:, :, 1] >= nw_thresh) &
            (arr[:, :, 2] >= nw_thresh)
        )
        visited   = arr[:, :, 3] == 0   # transparent pixels already handled
        all_erased = np.zeros((h, w), dtype=bool) if enclosed_stroke else None
        ys, xs = np.where(nw_mask & ~visited)
        for sy, sx in zip(ys.tolist(), xs.tolist()):
            if visited[sy, sx]:
                continue
            component: list[tuple[int, int]] = []
            stack = [(sy, sx)]
            visited[sy, sx] = True
            touches_transparent = False
            while stack:
                cy, cx = stack.pop()
                component.append((cy, cx))
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w:
                        if arr[ny, nx, 3] == 0:
                            touches_transparent = True
                        elif not visited[ny, nx] and nw_mask[ny, nx]:
                            visited[ny, nx] = True
                            stack.append((ny, nx))
            if not touches_transparent:
                for cy, cx in component:
                    arr[cy, cx, 3] = 0
                    if all_erased is not None:
                        all_erased[cy, cx] = True

        # Optional 1px white interior border around each erased hole
        if all_erased is not None and all_erased.any():
            from PIL import ImageFilter
            erased_img  = Image.fromarray(all_erased.astype(np.uint8) * 255, "L")
            dilated     = np.array(erased_img.filter(ImageFilter.MaxFilter(3))) > 0
            border_ring = dilated & ~all_erased & (arr[:, :, 3] > 0)
            arr[border_ring, 0] = 255
            arr[border_ring, 1] = 255
            arr[border_ring, 2] = 255
            arr[border_ring, 3] = 255

    out = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(out, format="PNG")
    return out.getvalue()


def _has_white_stroke(png_bytes: bytes, threshold: int = 240) -> bool:
    """Return True if the outermost content pixels are already predominantly white.

    Erodes the alpha mask by 1px and inspects the exposed ring — if >85% of those
    pixels are near-white the image almost certainly already has a white border.
    """
    import numpy as np
    from PIL import ImageFilter
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.array(img)
    a   = arr[:, :, 3]
    if not (a > 0).any():
        return False
    a_eroded   = np.array(Image.fromarray(a, "L").filter(ImageFilter.MinFilter(3)))
    outer_ring = (a > 0) & (a_eroded == 0)
    if not outer_ring.any():
        return False
    ring_rgb   = arr[outer_ring, :3]
    frac_white = float(np.mean(
        (ring_rgb[:, 0] >= threshold) &
        (ring_rgb[:, 1] >= threshold) &
        (ring_rgb[:, 2] >= threshold)
    ))
    return frac_white > 0.85


def _add_white_stroke(png_bytes: bytes, radius: int = 1) -> bytes:
    """Add a white border of `radius` pixels around all non-transparent content.
    Returns the original bytes unchanged if a white border already exists.
    """
    from PIL import ImageFilter
    if _has_white_stroke(png_bytes):
        return png_bytes
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    _, _, _, a = img.split()
    a_dilated = a.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    white = Image.new("RGBA", img.size, (255, 255, 255, 0))
    white.putalpha(a_dilated)
    result = Image.alpha_composite(white, img)
    out = io.BytesIO()
    result.save(out, format="PNG")
    return out.getvalue()


def _pad_to_square(png_bytes: bytes) -> bytes:
    """Expand a logo to a square canvas by adding transparent padding, centered."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    w, h = img.size
    size = max(w, h)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(img, ((size - w) // 2, (size - h) // 2))
    out = io.BytesIO()
    square.save(out, format="PNG")
    return out.getvalue()


def _is_clean(text: str) -> bool:
    return not _profanity.contains_profanity(text)


def _find_save_root(extracted_dir: str) -> str:
    contents = os.listdir(extracted_dir)
    if len(contents) == 1:
        candidate = os.path.join(extracted_dir, contents[0])
        if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, "meta.json")):
            return candidate
    return extracted_dir


def _mark_dirty(page_key: str):
    st.session_state.setdefault("dirty_pages", set()).add(page_key)
    # Invalidate cached download bytes whenever a new edit is made
    st.session_state.pop("download_bytes", None)


def _coerce_val(new_val, original):
    """Coerce a value from a DataFrame cell to match the type of the original JSON value."""
    if original is None:
        return None if (isinstance(new_val, float) and math.isnan(new_val)) else new_val
    if isinstance(original, bool):
        if isinstance(new_val, float) and math.isnan(new_val):
            return original
        return bool(new_val)
    if isinstance(original, int):
        if isinstance(new_val, float) and math.isnan(new_val):
            return original
        try:
            return int(new_val)
        except (ValueError, TypeError):
            return original
    if isinstance(new_val, float) and math.isnan(new_val):
        return None
    return new_val


# ================================================================== coaches

_COACHES_DISPLAY_COLS = [
    "firstName", "lastName", "age", "experience", "almaMater",
    "offensivePreference", "defensivePreference",
    "coachPrestige", "offenseRating", "defenseRating", "recruitingRating", "developmentRating",
    "careerWins", "careerLosses", "championships", "conferenceChampionships",
    "finalFours", "tournamentAppearances",
    "hallOfFamersCoached", "playersDrafted",
    "nationalCoachOfYearCount", "confCoachOfYearCount",
    "yearsAtSchool", "consecutiveMissedTournaments",
    "isHallOfFame", "isWarned", "wearsGlasses",
    "hairStyle", "facialHairStyle",
    "skinTone", "hairColor", "shirtColor", "tieColor",
    "coachingPoints",
    "id",
]
_COACHES_LOCKED = {"id"}


def _coaches_to_df(coaches: list[dict]) -> pd.DataFrame:
    rows = [{col: c.get(col) for col in _COACHES_DISPLAY_COLS} for c in coaches]
    return pd.DataFrame(rows)


def _coaches_df_to_list(raw: list[dict], df: pd.DataFrame) -> list[dict]:
    edits = {row["id"]: row.to_dict() for _, row in df.iterrows()}
    result = []
    for c in raw:
        cid = c.get("id", "")
        if cid in edits:
            merged = dict(c)
            for k, v in edits[cid].items():
                if k in merged:
                    merged[k] = _coerce_val(v, merged[k])
            result.append(merged)
        else:
            result.append(c)
    return result


def _coaches_col_cfg() -> dict:
    pref_options = ["drive", "motion", "highLow", "Princeton", "dribbleDrive", "postalUp", "spread"]
    def_options = ["manToMan", "zone32", "zone23", "zone22", "matchup", "trapping"]
    hair_options = ["short", "medium", "long", "buzz", "afro", "part", "bald", "mohawk", "cornrows"]
    return {
        "id":                    st.column_config.TextColumn("ID", disabled=True),
        "offensivePreference":   st.column_config.SelectboxColumn("Off. Pref", options=pref_options),
        "defensivePreference":   st.column_config.SelectboxColumn("Def. Pref", options=def_options),
        "hairStyle":             st.column_config.SelectboxColumn("Hair Style", options=hair_options),
        "coachPrestige":         st.column_config.NumberColumn("Prestige", min_value=1, max_value=99),
        "offenseRating":         st.column_config.NumberColumn("Off Rtg", min_value=1, max_value=99),
        "defenseRating":         st.column_config.NumberColumn("Def Rtg", min_value=1, max_value=99),
        "recruitingRating":      st.column_config.NumberColumn("Rec Rtg", min_value=1, max_value=99),
        "developmentRating":     st.column_config.NumberColumn("Dev Rtg", min_value=1, max_value=99),
        "isHallOfFame":          st.column_config.CheckboxColumn("HOF"),
        "isWarned":              st.column_config.CheckboxColumn("Warned"),
        "wearsGlasses":          st.column_config.CheckboxColumn("Glasses"),
    }


# ================================================================== players

_PLAYERS_DISPLAY_COLS = [
    "firstName", "lastName", "year", "jerseyNumber",
    "teamId", "homeState", "hometown", "highSchool",
    "height", "weight",
    "_pos", "ovr_PG", "ovr_SG", "ovr_SF", "ovr_PF", "ovr_C", "overallRating", "potentialRating",
    "insideShooting", "midRangeShooting", "outsideShooting",
    "handling", "passing", "rebounding",
    "perimeterDefense", "interiorDefense", "stealing", "blocking",
    "loyalty", "ambition", "playingTimeDesire", "homeAttachment", "morale",
    "isInjured", "isRedshirted", "hasUsedRedshirt", "draftProjection",
    "id",
]
_PLAYERS_LOCKED = {"id", "teamId", "_team_idx", "overallRating",
                   "ovr_PG", "ovr_SG", "ovr_SF", "ovr_PF", "ovr_C"}
_POSITIONS = ["pointGuard", "shootingGuard", "smallForward", "powerForward", "center"]
_POS_ABBR = {"pointGuard": "PG", "shootingGuard": "SG", "smallForward": "SF", "powerForward": "PF", "center": "C"}
_POS_FULL  = {v: k for k, v in _POS_ABBR.items()}

_OVERALL_WEIGHTS: dict[str, dict[str, float]] = {
    "pointGuard":    {"insideShooting": 0.06000, "midRangeShooting": 0.09177, "outsideShooting": 0.15249,
                      "handling": 0.18166, "passing": 0.11590, "rebounding": 0.05474,
                      "perimeterDefense": 0.14540, "interiorDefense": 0.03898,
                      "stealing": 0.13874, "blocking": 0.02011},
    "shootingGuard": {"insideShooting": 0.07861, "midRangeShooting": 0.11310, "outsideShooting": 0.16610,
                      "handling": 0.10927, "passing": 0.08359, "rebounding": 0.08987,
                      "perimeterDefense": 0.13850, "interiorDefense": 0.06549,
                      "stealing": 0.10712, "blocking": 0.04920},
    "smallForward":  {"insideShooting": 0.10165, "midRangeShooting": 0.10281, "outsideShooting": 0.09833,
                      "handling": 0.10143, "passing": 0.10058, "rebounding": 0.09899,
                      "perimeterDefense": 0.12965, "interiorDefense": 0.09719,
                      "stealing": 0.09671, "blocking": 0.07663},
    "powerForward":  {"insideShooting": 0.13298, "midRangeShooting": 0.09296, "outsideShooting": 0.06812,
                      "handling": 0.04660, "passing": 0.11227, "rebounding": 0.16776,
                      "perimeterDefense": 0.05282, "interiorDefense": 0.17282,
                      "stealing": 0.04770, "blocking": 0.10691},
    "center":        {"insideShooting": 0.16471, "midRangeShooting": 0.06129, "outsideShooting": 0.02221,
                      "handling": 0.02046, "passing": 0.13663, "rebounding": 0.20500,
                      "perimeterDefense": 0.02427, "interiorDefense": 0.18199,
                      "stealing": 0.03286, "blocking": 0.15051},
}


def _calc_overall(position: str, row: dict) -> int:
    weights = _OVERALL_WEIGHTS.get(position) or _OVERALL_WEIGHTS["smallForward"]
    return int(round(sum(w * float(row.get(s) or 0) for s, w in weights.items())))


_SKIP_COMPUTED = {"_team_name", "_team_idx", "_pos", "ovr_PG", "ovr_SG", "ovr_SF", "ovr_PF", "ovr_C"}


def _teams_to_players_df(teams: list[dict]) -> pd.DataFrame:
    rows = []
    for idx, team in enumerate(teams):
        team_name = team.get("name") or team.get("teamId") or team.get("id") or str(idx)
        for p in (team.get("players") or []):
            row = {col: p.get(col) for col in _PLAYERS_DISPLAY_COLS if col not in _SKIP_COMPUTED}
            row["_team_name"] = team_name
            row["_team_idx"] = idx
            pos = p.get("position", "")
            row["position"] = pos
            row["_pos"] = _POS_ABBR.get(pos, "")
            for pk, cn in [("pointGuard", "ovr_PG"), ("shootingGuard", "ovr_SG"),
                           ("smallForward", "ovr_SF"), ("powerForward", "ovr_PF"), ("center", "ovr_C")]:
                row[cn] = _calc_overall(pk, p)
            rows.append(row)
    df = pd.DataFrame(rows)
    # Enforce column order: _PLAYERS_DISPLAY_COLS first (all columns including computed),
    # then internal-only columns at the end (dropped before showing the editor).
    ordered = _PLAYERS_DISPLAY_COLS + ["_team_name", "_team_idx", "position"]
    return df.reindex(columns=ordered)


def _players_df_to_teams(teams: list[dict], df: pd.DataFrame) -> list[dict]:
    edits = {}
    for _, row in df.iterrows():
        pid = row.get("id")
        if pid:
            edits[pid] = row.to_dict()

    result = []
    for team in teams:
        new_players = []
        for p in (team.get("players") or []):
            pid = p.get("id")
            if pid and pid in edits:
                merged = dict(p)
                for k, v in edits[pid].items():
                    if k.startswith("_"):
                        continue
                    if k in merged:
                        merged[k] = _coerce_val(v, merged[k])
                new_players.append(merged)
            else:
                new_players.append(p)
        new_team = dict(team)
        new_team["players"] = new_players
        result.append(new_team)
    return result


def _players_col_cfg() -> dict:
    return {
        "id":              st.column_config.TextColumn("ID", disabled=True),
        "teamId":          st.column_config.TextColumn("Team ID", disabled=True),
        "_pos":            st.column_config.SelectboxColumn("Pos", options=list(_POS_ABBR.values())),
        "year":            st.column_config.NumberColumn("Year", min_value=1, max_value=5),
        "height":          st.column_config.NumberColumn("Ht (in)", min_value=60, max_value=96),
        "weight":          st.column_config.NumberColumn("Wt (lbs)", min_value=100, max_value=400),
        "overallRating":   st.column_config.NumberColumn("OVR", min_value=0, max_value=99, disabled=True),
        "ovr_PG":          st.column_config.NumberColumn("PG OVR", disabled=True),
        "ovr_SG":          st.column_config.NumberColumn("SG OVR", disabled=True),
        "ovr_SF":          st.column_config.NumberColumn("SF OVR", disabled=True),
        "ovr_PF":          st.column_config.NumberColumn("PF OVR", disabled=True),
        "ovr_C":           st.column_config.NumberColumn("C OVR", disabled=True),
        "potentialRating": st.column_config.NumberColumn("POT", min_value=0, max_value=99),
        "insideShooting":  st.column_config.NumberColumn("INS", min_value=0, max_value=99),
        "midRangeShooting":st.column_config.NumberColumn("MID", min_value=0, max_value=99),
        "outsideShooting": st.column_config.NumberColumn("OUT", min_value=0, max_value=99),
        "handling":        st.column_config.NumberColumn("HND", min_value=0, max_value=99),
        "passing":         st.column_config.NumberColumn("PAS", min_value=0, max_value=99),
        "rebounding":      st.column_config.NumberColumn("REB", min_value=0, max_value=99),
        "perimeterDefense":st.column_config.NumberColumn("PDef", min_value=0, max_value=99),
        "interiorDefense": st.column_config.NumberColumn("IDef", min_value=0, max_value=99),
        "stealing":        st.column_config.NumberColumn("STL", min_value=0, max_value=99),
        "blocking":        st.column_config.NumberColumn("BLK", min_value=0, max_value=99),
        "isInjured":       st.column_config.CheckboxColumn("Injured"),
        "isRedshirted":    st.column_config.CheckboxColumn("RS"),
        "hasUsedRedshirt": st.column_config.CheckboxColumn("RS Used"),
    }


# ================================================================== conferences

def _conferences_to_df(conferences: list, power_conferences: list, teams: list | None = None) -> pd.DataFrame:
    power_set = set(power_conferences or [])
    id_map: dict[str, str] = {}
    for t in (teams or []):
        conf_name = t.get("conference")
        conf_id = t.get("conferenceId")
        if conf_name and conf_id and conf_name not in id_map:
            id_map[conf_name] = conf_id
    return pd.DataFrame([
        {"conference": name, "conferenceId": id_map.get(name, ""), "is_power": name in power_set}
        for name in (conferences or [])
    ])


def _conferences_df_to_lists(df: pd.DataFrame) -> tuple[list, list, dict]:
    all_c = df["conference"].tolist()
    power_c = df[df["is_power"] == True]["conference"].tolist()
    conf_id_map: dict[str, str] = {}
    if "conferenceId" in df.columns:
        conf_id_map = {
            row["conference"]: row["conferenceId"]
            for _, row in df.iterrows()
            if row.get("conference") and row.get("conferenceId")
        }
    return all_c, power_c, conf_id_map


# ================================================================== teams

_TEAMS_SCALAR_COLS = [
    "name", "mascot", "abbreviation", "state",
    "conference", "conferenceId", "isPowerConference",
    "offensiveScheme", "defensiveScheme",
    "prestige", "startingPrestige", "offenseRating", "defenseRating", "expectedWins",
    "teamColor", "secondaryColor", "nilBudget", "isUserControlled",
    "wins", "losses", "conferenceWins", "conferenceLosses",
]
_TEAMS_LOCKED = {"id", "wins", "losses", "conferenceWins", "conferenceLosses"}


def _teams_to_df(teams: list[dict]) -> pd.DataFrame:
    rows = []
    for t in teams:
        row = {col: t.get(col) for col in _TEAMS_SCALAR_COLS}
        row["pipelineStates"] = ", ".join(t.get("pipelineStates") or [])
        row["rivalTeamIds"] = ", ".join(str(x) for x in (t.get("rivalTeamIds") or []))
        row["id"] = t.get("id")
        row["coachId"] = t.get("coachId")
        rows.append(row)
    return pd.DataFrame(rows)


def _teams_df_to_list(raw: list[dict], df: pd.DataFrame) -> list[dict]:
    edits = {row["id"]: row.to_dict() for _, row in df.iterrows()}
    result = []
    for t in raw:
        tid = t.get("id", "")
        if tid not in edits:
            result.append(t)
            continue
        merged = dict(t)
        edit = edits[tid]
        for k, v in edit.items():
            if k == "pipelineStates":
                merged["pipelineStates"] = [s.strip() for s in str(v).split(",") if s.strip()] if v else []
            elif k == "rivalTeamIds":
                merged["rivalTeamIds"] = [s.strip() for s in str(v).split(",") if s.strip()] if v else []
            elif k in merged and not isinstance(merged.get(k), (dict, list)):
                merged[k] = _coerce_val(v, merged[k])
        result.append(merged)
    return result


def _teams_col_cfg(conference_options: list[str], coach_options: list[str]) -> dict:
    off_schemes = ["drive", "motion", "highLow", "Princeton", "dribbleDrive", "postalUp", "spread"]
    def_schemes = ["manToMan", "zone32", "zone23", "zone22", "matchup", "trapping"]
    return {
        "id":               st.column_config.TextColumn("ID", disabled=True),
        "conference":       st.column_config.SelectboxColumn("Conference", options=conference_options),
        "conferenceId":     st.column_config.TextColumn("Conf ID", disabled=True),
        "offensiveScheme":  st.column_config.SelectboxColumn("Off Scheme", options=off_schemes),
        "defensiveScheme":  st.column_config.SelectboxColumn("Def Scheme", options=def_schemes),
        "_coach_name":      st.column_config.SelectboxColumn("Coach", options=coach_options),
        "coachId":          st.column_config.TextColumn("Coach ID", disabled=True),
        "isPowerConference":st.column_config.CheckboxColumn("Power"),
        "isUserControlled": st.column_config.CheckboxColumn("User"),
        "prestige":         st.column_config.NumberColumn("Prestige", min_value=1, max_value=99),
        "startingPrestige": st.column_config.NumberColumn("StartPres", min_value=1, max_value=99),
        "offenseRating":    st.column_config.NumberColumn("OffRtg", min_value=1, max_value=99),
        "defenseRating":    st.column_config.NumberColumn("DefRtg", min_value=1, max_value=99),
        "expectedWins":     st.column_config.NumberColumn("ExpW", min_value=0, max_value=50),
        "wins":             st.column_config.NumberColumn("W", disabled=True),
        "losses":           st.column_config.NumberColumn("L", disabled=True),
        "conferenceWins":   st.column_config.NumberColumn("CW", disabled=True),
        "conferenceLosses": st.column_config.NumberColumn("CL", disabled=True),
    }


# ================================================================== download builder

def _apply_all_edits(save: SaveFile):
    """Flush all in-memory DataFrame edits back into save.session."""
    edits = st.session_state.get("page_edits", {})
    raw = st.session_state.get("raw_data", {})

    # Apply conferences first so team conference dropdowns reference a valid list
    conf_id_map: dict[str, str] = {}
    if "conferences" in edits:
        all_c, power_c, conf_id_map = _conferences_df_to_lists(edits["conferences"])
        save.set("season.conferences", all_c)
        save.set("season.powerConferences", power_c)

    # Teams and players both write to season.teams — apply together
    if "teams" in edits or "players" in edits:
        teams = save.get("season.teams") or []
        if "teams" in edits and "teams" in raw:
            teams = _teams_df_to_list(teams, edits["teams"])
        if "players" in edits:
            teams = _players_df_to_teams(teams, edits["players"])
        save.set("season.teams", teams)

    # Propagate conferenceId changes last so they're authoritative over team-level edits
    if conf_id_map:
        teams = save.get("season.teams") or []
        for team in teams:
            conf_name = team.get("conference")
            if conf_name and conf_name in conf_id_map:
                team["conferenceId"] = conf_id_map[conf_name]
        save.set("season.teams", teams)

    if "coaches" in edits and "coaches" in raw:
        save.set("season.coaches", _coaches_df_to_list(raw["coaches"], edits["coaches"]))

    if "recruiting" in edits and "recruiting" in raw:
        edits_by_id = {row.get("id"): row.to_dict()
                       for _, row in edits["recruiting"].iterrows() if row.get("id")}
        result = []
        for r in raw["recruiting"]:
            rid = r.get("id")
            if rid and rid in edits_by_id:
                merged = dict(r)
                for k, v in edits_by_id[rid].items():
                    if k in merged:
                        merged[k] = _coerce_val(v, merged[k])
                result.append(merged)
            else:
                result.append(r)
        save.set("season.recruitingPool", result)

    modified_sections = sorted(k for k in ["conferences", "teams", "players", "coaches", "recruiting"] if k in edits)
    if modified_sections:
        save.set("_modded", {
            "tool": "campus-hoops-mod-utility",
            "modifiedSections": modified_sections,
            "modifiedAt": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })


def _csv_download_btn(df: pd.DataFrame, filename: str, label: str = "Download CSV"):
    st.download_button(label, df.to_csv(index=False).encode(), filename, "text/csv",
                       use_container_width=True)


def _csv_upload_widget(page_key: str, reference_df: pd.DataFrame, key: str):
    """Show a CSV uploader; on new file upload, store result in page_edits and mark dirty."""
    uploaded_csv = st.file_uploader("Upload CSV to overwrite", type="csv", key=key)
    if uploaded_csv is not None:
        # Only process when the file actually changes (prevents re-applying on every rerun)
        file_id = f"{uploaded_csv.name}_{uploaded_csv.size}"
        last_id_key = f"_csv_last_id_{key}"
        if st.session_state.get(last_id_key) != file_id:
            try:
                new_df = pd.read_csv(uploaded_csv)
                for col in new_df.columns:
                    if col in reference_df.columns:
                        try:
                            new_df[col] = new_df[col].astype(reference_df[col].dtype)
                        except Exception:
                            pass
                st.session_state.setdefault("page_edits", {})[page_key] = new_df
                _mark_dirty(page_key)
                st.session_state[last_id_key] = file_id
                st.success(f"Loaded {len(new_df)} rows from CSV.")
            except Exception as e:
                st.error(f"Could not read CSV: {e}")


# ================================================================== sidebar

if DATAPACK_ONLY:
    page = "Data Pack"
else:
    page = st.sidebar.radio(
        "Page",
        ["Data Pack", "Conferences", "Teams", "Rosters", "Coaches", "Recruiting Pool"],
    )

if not DATAPACK_ONLY:
    with st.sidebar:
        st.header("Save File")
        uploaded = st.file_uploader("Upload save (.campushoops or .zip)")

        if uploaded:
            if not zipfile.is_zipfile(uploaded):
                st.error("That file doesn't appear to be a valid save export.")
                st.stop()
            uploaded.seek(0)
            raw_zip_bytes = uploaded.read()

            upload_key = f"{uploaded.name}_{uploaded.size}"
            if st.session_state.get("upload_key") != upload_key:
                old_dir = st.session_state.get("temp_dir")
                if old_dir and os.path.exists(old_dir):
                    shutil.rmtree(old_dir, ignore_errors=True)

                temp_dir = tempfile.mkdtemp()
                with zipfile.ZipFile(io.BytesIO(raw_zip_bytes)) as zf:
                    zf.extractall(temp_dir)

                save_root = _find_save_root(temp_dir)
                st.session_state["upload_key"] = upload_key
                st.session_state["temp_dir"] = temp_dir
                st.session_state["save"] = SaveFile(save_root)
                st.session_state["raw_zip"] = raw_zip_bytes
                st.session_state["page_edits"] = {}
                st.session_state["raw_data"] = {}
                st.session_state["dirty_pages"] = set()
                st.session_state.pop("download_bytes", None)
                st.session_state.pop("logo_edits", None)
                st.session_state.pop("existing_logos", None)

        if "save" in st.session_state:
            save: SaveFile = st.session_state["save"]
            st.caption(
                f"**{save.meta.get('teamName')}** — Season {save.meta.get('seasonYear')}\n\n"
                f"Last saved: {save.meta.get('lastSaved', '')[:10]}"
            )

            st.divider()

            dirty = st.session_state.get("dirty_pages", set())
            if dirty:
                st.warning(f"Unsaved edits on: {', '.join(sorted(dirty))}. "
                           "Download your file before refreshing.")

            if st.button("Build .campushoops", use_container_width=True,
                         help="Applies all edits and compresses the save file for download."):
                with st.spinner("Building…"):
                    _apply_all_edits(save)
                    st.session_state["download_bytes"] = save.to_campushoops_bytes(
                        st.session_state["raw_zip"],
                        logo_overrides=st.session_state.get("logo_edits") or None,
                    )

            if "download_bytes" in st.session_state:
                team_id = save.meta.get("teamId", "save")
                st.download_button(
                    "Download .campushoops",
                    st.session_state["download_bytes"],
                    file_name=f"modified_{team_id}.campushoops",
                    mime="application/zip",
                    use_container_width=True,
                    type="primary",
                )
        else:
            st.info("Upload your save file to begin.")


# ================================================================== Recruiting Pool

def render_recruiting():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Recruiting Pool")
    save: SaveFile = st.session_state["save"]
    upload_key = st.session_state["upload_key"]

    if "pool" not in st.session_state or st.session_state.get("pool_key") != upload_key:
        raw_pool = save.get("season.recruitingPool") or []
        st.session_state["pool"] = raw_pool
        st.session_state["pool_key"] = upload_key

    pool = RecruitingPool(st.session_state["pool"])
    st.caption(f"{len(pool)} recruits in pool")

    tab_view, tab_edit = st.tabs(["View / Filter", "Edit"])

    # ---- View tab (read-only, with filters) ----
    with tab_view:
        with st.sidebar:
            st.header("Filters")
            selected_positions = st.multiselect("Position", list(POSITION_ABBR.values()))
            rating_range = st.slider("Rating", 0, 99, (0, 99))
            potential_range = st.slider("Potential", 0, 99, (0, 99))
            _hl = [fmt_height(h) for h in range(60, 91)]
            _hs = st.select_slider("Height", options=_hl, value=(fmt_height(60), fmt_height(90)))
            height_range = (60 + _hl.index(_hs[0]), 60 + _hl.index(_hs[1]))
            fc1, fc2 = st.columns(2)
            min_stars = fc1.selectbox("Min ★", [0, 1, 2, 3, 4, 5])
            max_ranking = fc2.number_input("Max rank", min_value=1, value=1600)
            fc3, fc4 = st.columns(2)
            state_filter = fc3.text_input("State", "").upper().strip() or None
            recruit_type = fc4.selectbox("Type", ["All", "HS", "Xfer"])
            type_map = {"All": None, "HS": "highSchool", "Xfer": "transferPortal"}
            fc5, fc6 = st.columns(2)
            scouted_filter = fc5.selectbox("Scouted", ["All", "Yes", "No"])
            scouted_map = {"All": None, "Yes": True, "No": False}
            sort_by = fc6.selectbox("Sort by", [
                "rating", "potential", "ranking", "generatedStarRating", "height",
                "insideShooting", "midRangeShooting", "outsideShooting",
                "handling", "passing", "rebounding",
                "perimeterDefense", "interiorDefense", "stealing", "blocking",
            ])

        filtered = pool.filter(
            position=selected_positions if selected_positions else None,
            min_rating=rating_range[0], max_rating=rating_range[1],
            min_potential=potential_range[0], max_potential=potential_range[1],
            min_height=height_range[0], max_height=height_range[1],
            min_stars=min_stars, max_ranking=max_ranking,
            state=state_filter,
            recruit_type=type_map[recruit_type],
            is_scouted=scouted_map[scouted_filter],
        )
        filtered_sorted = sorted(filtered, key=lambda r: r.get(sort_by) or 0, reverse=True)
        df_view = pool.to_df(filtered_sorted)
        st.caption(f"Showing {len(df_view)} recruits")

        col_cfg = {
            "height": st.column_config.TextColumn(
                "Height", help="Use 'Sort by → height' sidebar for correct ordering"),
            "stars": st.column_config.NumberColumn("Stars", format="%d ⭐"),
        }
        core_cols = ["name", "pos", "stars", "rating", "potential", "ranking",
                     "pos_rank", "height", "weight", "state", "hometown", "type",
                     "scouted", "late_bloomer", "generational", "schools", "awards"]
        skill_cols = ["inside", "mid", "outside", "handling", "passing",
                      "rebounding", "perim_def", "int_def", "stealing", "blocking"]
        personality_cols = ["loyalty", "ambition", "playing_time_desire", "home_attachment"]

        sub1, sub2, sub3 = st.tabs(["Overview", "Skills", "Personality"])
        with sub1:
            st.dataframe(df_view[core_cols], column_config=col_cfg,
                         use_container_width=True, hide_index=True)
        with sub2:
            st.dataframe(df_view[["name", "pos", "rating", "height"] + skill_cols],
                         column_config=col_cfg, use_container_width=True, hide_index=True)
        with sub3:
            st.info(
                "**Personality as recruiting propensity**\n\n"
                "- **Loyalty** — prefers staying close to home\n"
                "- **Home Attachment** — weights distance from hometown heavily\n"
                "- **Playing Time Desire** — will prioritize where he's most likely to start\n"
                "- **Ambition** — chases prestige; easier to flip toward higher-ranked programs"
            )
            st.dataframe(df_view[["name", "pos", "rating"] + personality_cols],
                         use_container_width=True, hide_index=True)

    # ---- Edit tab ----
    with tab_edit:
        page_key = "recruiting"
        raw_pool = st.session_state["pool"]

        if page_key not in st.session_state.get("page_edits", {}):
            # Build editable df from raw pool (scalar fields only)
            skip = {"interestedSchools", "awards", "seasonLogs", "startOfSeasonAttributes"}
            sample = raw_pool[0] if raw_pool else {}
            edit_cols = [k for k, v in sample.items()
                         if not isinstance(v, (dict, list)) and k not in skip]
            rows = [{c: r.get(c) for c in edit_cols} for r in raw_pool]
            edit_df = pd.DataFrame(rows)
            st.session_state.setdefault("page_edits", {})[page_key] = edit_df
            st.session_state.setdefault("raw_data", {})[page_key] = raw_pool

        current_df = st.session_state["page_edits"][page_key]
        locked = {c for c in current_df.columns if c.lower().endswith("id") or c == "id"}
        col_cfg_edit = {c: st.column_config.Column(disabled=True) for c in locked}

        edited = st.data_editor(
            current_df,
            column_config=col_cfg_edit,
            use_container_width=True,
            num_rows="dynamic",
            key=f"editor_{page_key}",
        )

        if not edited.equals(current_df):
            st.session_state["page_edits"][page_key] = edited
            _mark_dirty(page_key)

        c1, c2 = st.columns(2)
        with c1:
            _csv_download_btn(edited, "recruiting_pool.csv")
        with c2:
            _csv_upload_widget(page_key, current_df, key="csv_up_recruiting")


# ================================================================== Coaches

def render_coaches():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Coaches")
    save: SaveFile = st.session_state["save"]
    page_key = "coaches"

    if page_key not in st.session_state.get("page_edits", {}):
        raw = save.get("season.coaches") or []
        st.session_state.setdefault("raw_data", {})[page_key] = raw
        st.session_state.setdefault("page_edits", {})[page_key] = _coaches_to_df(raw)

    current_df = st.session_state["page_edits"][page_key]
    st.caption(f"{len(current_df)} coaches")

    col_cfg = _coaches_col_cfg()
    for c in _COACHES_LOCKED:
        col_cfg[c] = st.column_config.TextColumn(c, disabled=True)

    edited = st.data_editor(
        current_df,
        column_config=col_cfg,
        use_container_width=True,
        num_rows="dynamic",
        key="editor_coaches",
    )

    if not edited.equals(current_df):
        st.session_state["page_edits"][page_key] = edited
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        _csv_download_btn(edited, "coaches.csv")
    with c2:
        _csv_upload_widget(page_key, current_df, key="csv_up_coaches")


# ================================================================== Rosters


@st.fragment
def _players_editor_fragment(page_key: str, selected: str):
    """Isolated fragment so stat edits rerun only this section, preserving page scroll.

    We pass a frozen base to data_editor so the component never receives new data
    mid-session (which would reset table scroll). Edits accumulate as a delta on
    top of the frozen base; OVR is recomputed in page_edits on every edit. The
    frozen base resets (showing updated OVR) when switching teams or clicking ↺.
    """
    full_df = st.session_state["page_edits"][page_key]

    if selected == "All Teams":
        current_slice = full_df.drop(columns=["_team_idx", "_team_name", "position"], errors="ignore").copy()
    else:
        current_slice = (
            full_df[full_df["_team_name"] == selected]
            .drop(columns=["_team_idx", "_team_name", "position"], errors="ignore")
            .copy()
        )

    # Frozen base: initialised on first visit to this team selection, then held
    # constant so the data_editor component never gets new props mid-edit.
    base_key = f"_editor_base_{selected}"
    if base_key not in st.session_state:
        st.session_state[base_key] = current_slice

    editor_base = st.session_state[base_key]

    cap_col, btn_col = st.columns([12, 1])
    cap_col.caption(
        f"{len(current_slice)} players" + ("" if selected == "All Teams" else f" on {selected}")
    )
    if btn_col.button("↺", help="Refresh OVR column (resets table scroll)", key=f"refresh_ovr_{selected}"):
        st.session_state[base_key] = current_slice
        st.rerun(scope="fragment")

    edited_display = st.data_editor(
        editor_base,
        column_config=_players_col_cfg(),
        use_container_width=True,
        num_rows="dynamic",
        key=f"editor_players_{selected}",
    )

    if not edited_display.equals(editor_base):
        id_to_edit = edited_display.set_index("id").to_dict("index")
        new_full = full_df.copy()
        for idx, row in new_full.iterrows():
            pid = row.get("id")
            if pid and pid in id_to_edit:
                for col, val in id_to_edit[pid].items():
                    new_full.at[idx, col] = val
        new_full["position"] = new_full["_pos"].map(_POS_FULL).fillna(new_full["position"])
        for pk, cn in [("pointGuard", "ovr_PG"), ("shootingGuard", "ovr_SG"),
                       ("smallForward", "ovr_SF"), ("powerForward", "ovr_PF"), ("center", "ovr_C")]:
            new_full[cn] = new_full.apply(lambda r, p=pk: _calc_overall(p, r.to_dict()), axis=1)
        new_full["overallRating"] = new_full.apply(
            lambda r: _calc_overall(r.get("position", ""), r.to_dict()), axis=1
        )
        st.session_state["page_edits"][page_key] = new_full
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        fname = f"roster_{selected.replace(' ', '_')}.csv" if selected != "All Teams" else "all_players.csv"
        _csv_download_btn(edited_display, fname)
    with c2:
        upload_key = f"csv_up_players_{selected}"
        last_key = f"_csv_last_id_{upload_key}"
        prev_last = st.session_state.get(last_key)
        _csv_upload_widget(page_key, editor_base, key=upload_key)
        if st.session_state.get(last_key) != prev_last:
            st.session_state.pop(base_key, None)
            st.rerun(scope="fragment")


def render_rosters():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Rosters")
    save: SaveFile = st.session_state["save"]
    page_key = "players"

    if page_key not in st.session_state.get("page_edits", {}):
        teams = save.get("season.teams") or []
        full_df = _teams_to_players_df(teams)
        full_df["overallRating"] = full_df.apply(
            lambda r: _calc_overall(r.get("position", ""), r.to_dict()), axis=1
        )
        st.session_state.setdefault("page_edits", {})[page_key] = full_df

    full_df: pd.DataFrame = st.session_state["page_edits"][page_key]

    # Ensure meta columns exist (e.g. after CSV upload without them)
    if "_team_name" not in full_df.columns:
        full_df["_team_name"] = full_df.get("teamId", "unknown")
        st.session_state["page_edits"][page_key] = full_df
    if "_team_idx" not in full_df.columns:
        full_df["_team_idx"] = 0
        st.session_state["page_edits"][page_key] = full_df

    team_names = sorted(full_df["_team_name"].dropna().unique().tolist())
    selected = st.selectbox("Team", ["All Teams"] + team_names)

    _players_editor_fragment(page_key, selected)


# ================================================================== Teams

def render_teams():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Teams")
    save: SaveFile = st.session_state["save"]
    page_key = "teams"

    if page_key not in st.session_state.get("page_edits", {}):
        raw = save.get("season.teams") or []
        st.session_state.setdefault("raw_data", {})[page_key] = raw
        st.session_state.setdefault("page_edits", {})[page_key] = _teams_to_df(raw).sort_values(
            ["conference", "name"], key=lambda s: s.str.lower(), na_position="last", ignore_index=True
        )

    current_df: pd.DataFrame = st.session_state["page_edits"][page_key]

    # Build coach name ↔ id maps from live edits (or raw save data)
    coaches_edits = st.session_state.get("page_edits", {}).get("coaches")
    if coaches_edits is not None:
        raw_coaches_list = coaches_edits.to_dict("records")
    else:
        raw_coaches_list = save.get("season.coaches") or []
    coach_name_map = {
        c.get("id", ""): f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        for c in raw_coaches_list if c.get("id")
    }
    coach_id_map = {name: cid for cid, name in coach_name_map.items() if name}
    coach_options = [""] + sorted(coach_id_map.keys())

    # Add _coach_name display column if not yet present
    if "_coach_name" not in current_df.columns:
        current_df = current_df.copy()
        current_df["_coach_name"] = current_df["coachId"].map(coach_name_map).fillna("")
        st.session_state["page_edits"][page_key] = current_df

    # Use live conference list (may have been edited on Conferences page)
    conf_edits = st.session_state.get("page_edits", {}).get("conferences")
    if conf_edits is not None:
        all_conferences = sorted(conf_edits["conference"].dropna().tolist())
    else:
        all_conferences = sorted(save.get("season.conferences") or [])

    col_cfg = _teams_col_cfg(all_conferences, coach_options)

    conf_filter = st.selectbox("Filter by conference", ["All Teams"] + all_conferences)
    if conf_filter != "All Teams":
        display_df = current_df[current_df["conference"] == conf_filter].copy()
    else:
        display_df = current_df

    st.caption(
        f"{len(display_df)} team(s) shown — edit conference assignments, schemes, prestige, and colors."
    )

    edited = st.data_editor(
        display_df,
        column_config=col_cfg,
        use_container_width=True,
        num_rows="fixed",
        key=f"editor_teams_{conf_filter}",
    )

    # Merge edits back into full DataFrame
    if not edited.equals(display_df):
        id_to_edit = edited.set_index("id").to_dict("index")
        new_full = current_df.copy()
        for idx, row in new_full.iterrows():
            tid = row.get("id")
            if tid and tid in id_to_edit:
                for col, val in id_to_edit[tid].items():
                    new_full.at[idx, col] = val
        # Sync coachId from the coach name selectbox
        new_full["coachId"] = new_full["_coach_name"].map(coach_id_map).fillna(new_full["coachId"])
        st.session_state["page_edits"][page_key] = new_full
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        fname = f"teams_{conf_filter.replace(' ', '_')}.csv"
        _csv_download_btn(edited, fname)
    with c2:
        _csv_upload_widget(page_key, current_df, key=f"csv_up_teams_{conf_filter}")

    # ---- Logo management ----
    with st.expander("Team Logos"):
        raw_zip = st.session_state.get("raw_zip", b"")

        # Cache logos from the extracted save folder (read once per save upload)
        if "existing_logos" not in st.session_state:
            existing: dict[str, bytes] = {}
            logos_dir = os.path.join(str(save.folder), "logos")
            if os.path.isdir(logos_dir):
                for _fname in os.listdir(logos_dir):
                    if _fname.lower().endswith(".png"):
                        _tid = _fname[:-4]
                        with open(os.path.join(logos_dir, _fname), "rb") as _f:
                            existing[_tid] = _f.read()
            st.session_state["existing_logos"] = existing
        existing_logos: dict[str, bytes] = st.session_state.get("existing_logos", {})

        if "logo_edits" not in st.session_state:
            st.session_state["logo_edits"] = {}
        logo_edits: dict[str, bytes] = st.session_state["logo_edits"]

        team_rows = [
            (row["id"], row.get("name") or row["id"])
            for _, row in current_df.iterrows()
            if row.get("id")
        ]
        if not team_rows:
            st.caption("No teams loaded.")
        else:
            team_name_map = {tid: name for tid, name in team_rows}
            selected_logo_tid = st.selectbox(
                "Team",
                options=[tid for tid, _ in team_rows],
                format_func=lambda tid: f"{team_name_map.get(tid, tid)} ({tid})",
                key="logo_team_select",
            )

            col_img, col_up = st.columns([1, 3])
            current_logo = logo_edits.get(selected_logo_tid) or existing_logos.get(selected_logo_tid)
            with col_img:
                if current_logo:
                    # Composite onto a dark background so transparency is visible
                    _prev = Image.open(io.BytesIO(current_logo)).convert("RGBA")
                    _bg   = Image.new("RGBA", _prev.size, (60, 60, 60, 255))
                    _bg.paste(_prev, mask=_prev.split()[3])
                    _buf  = io.BytesIO()
                    _bg.save(_buf, format="PNG")
                    st.image(_buf.getvalue(), width=80)
                    st.caption("staged" if selected_logo_tid in logo_edits else "from save")
                else:
                    st.caption("No logo")

            with col_up:
                remove_bg = st.checkbox(
                    "Remove background", value=True, key=f"logo_rmbg_{selected_logo_tid}",
                    help="BFS from all image edges using the auto-detected background colour. Handles white, off-white, and solid-colour backgrounds.",
                )
                remove_enclosed = st.checkbox(
                    "Remove enclosed white (letter counters)", value=False,
                    key=f"logo_enc_{selected_logo_tid}",
                    help="Also removes near-white regions fully enclosed by non-transparent pixels (e.g. the hole inside an O, P, or R). Don't use if the logo has intentional white inside a coloured shape.",
                )
                uploaded_logo = st.file_uploader(
                    "Upload PNG", type=["png"], key=f"logo_up_{selected_logo_tid}",
                )
                if uploaded_logo is not None:
                    raw_bytes = uploaded_logo.getvalue()
                    if remove_bg:
                        raw_bytes = _remove_white_bg(raw_bytes, remove_enclosed=remove_enclosed)
                    logo_edits[selected_logo_tid] = raw_bytes
                    st.session_state["logo_edits"] = logo_edits
                if selected_logo_tid in logo_edits:
                    if st.button("Remove staged logo", key=f"logo_rm_{selected_logo_tid}"):
                        del logo_edits[selected_logo_tid]
                        st.session_state["logo_edits"] = logo_edits
                        st.rerun()

            st.divider()
            rmbg_all_enclosed = st.checkbox(
                "Remove enclosed white (letter counters)", value=False, key="logo_rmbg_all_enc",
                help="See per-logo option above.",
            )
            if st.button("Remove background from all logos", key="logo_rmbg_all"):
                all_logos = {**existing_logos, **logo_edits}
                for tid, logo_bytes in all_logos.items():
                    logo_edits[tid] = _remove_white_bg(logo_bytes, remove_enclosed=rmbg_all_enclosed)
                st.session_state["logo_edits"] = logo_edits
                st.rerun()

            if logo_edits:
                names = ", ".join(team_name_map.get(tid, tid) for tid in logo_edits)
                st.info(f"{len(logo_edits)} logo(s) staged for export: {names}")


# ================================================================== Conferences

def render_conferences():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return

    st.header("Conferences")
    save: SaveFile = st.session_state["save"]
    page_key = "conferences"

    if page_key not in st.session_state.get("page_edits", {}):
        conferences = save.get("season.conferences") or []
        power = save.get("season.powerConferences") or []
        teams = save.get("season.teams") or []
        df = _conferences_to_df(conferences, power, teams).sort_values(
            "conference", key=lambda s: s.str.lower(), na_position="last", ignore_index=True
        )
        st.session_state.setdefault("page_edits", {})[page_key] = df

    current_df = st.session_state["page_edits"][page_key]
    st.caption(f"{len(current_df)} conferences — edit names, IDs, or toggle power conference status.")

    col_cfg = {
        "conference":   st.column_config.TextColumn("Conference Name"),
        "conferenceId": st.column_config.TextColumn("Conference ID"),
        "is_power":     st.column_config.CheckboxColumn("Power Conference"),
    }

    edited = st.data_editor(
        current_df,
        column_config=col_cfg,
        use_container_width=True,
        num_rows="dynamic",
        key="editor_conferences",
    )

    if not edited.equals(current_df):
        st.session_state["page_edits"][page_key] = edited
        _mark_dirty(page_key)

    c1, c2 = st.columns(2)
    with c1:
        _csv_download_btn(edited, "conferences.csv")
    with c2:
        _csv_upload_widget(page_key, current_df, key="csv_up_conferences")


# ================================================================== Leaderboard (hidden from nav)

def _fmt_playtime(seconds: int | None) -> str:
    if not seconds:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def render_leaderboard():
    st.header("Leaderboard")
    try:
        challenges = db.get_active_challenges()
    except Exception as e:
        st.error(f"Could not load challenges: {e}")
        return
    if not challenges:
        st.info("No challenges yet.")
        return
    challenge_names = [c["name"] for c in challenges]
    selected_name = st.selectbox("Select a challenge", challenge_names)
    challenge = next(c for c in challenges if c["name"] == selected_name)
    st.markdown(f"**{challenge['name']}**")
    if challenge.get("description"):
        st.caption(challenge["description"])
    with st.expander("Challenge conditions"):
        for key, val in challenge.get("conditions", {}).items():
            st.markdown(f"- **{vf.CONDITION_LABELS.get(key, key)}**: {val}")
    st.divider()
    entries = db.get_leaderboard(challenge["id"])
    if not entries:
        st.info("No verified submissions yet.")
        return
    rows = []
    for i, e in enumerate(entries, 1):
        w, l = e.get("career_wins") or 0, e.get("career_losses") or 0
        rows.append({
            "#": i, "Username": e["username"], "Coach": e.get("coach_name") or "—",
            "Team": e["team_name"], "Seasons": e.get("seasons_played") or "—",
            "W": w, "L": l,
            "Win %": f"{w / (w + l) * 100:.1f}%" if (w + l) > 0 else "—",
            "Play Time": _fmt_playtime(e.get("play_time_seconds")),
            "Submitted": str(e.get("submitted_at", ""))[:10],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ================================================================== Submit (hidden)

def render_submit():
    if "save" not in st.session_state:
        st.info("Upload a save file to use this page.")
        return
    st.header("Submit to Challenge")
    save: SaveFile = st.session_state["save"]
    try:
        challenges = db.get_active_challenges()
    except Exception as e:
        st.error(f"Could not load challenges: {e}")
        return
    if not challenges:
        st.info("No active challenges.")
        return
    username = st.text_input("Your leaderboard username", max_chars=32)
    challenge = next(c for c in challenges
                     if c["name"] == st.selectbox("Challenge", [c["name"] for c in challenges]))
    if challenge.get("description"):
        st.caption(challenge["description"])
    if not username.strip():
        st.warning("Enter a username to continue.")
        return
    if not _is_clean(username):
        st.error("Please choose an appropriate username.")
        return
    st.divider()
    conditions = challenge.get("conditions", {})
    results = vf.verify(save, conditions)
    career = vf.get_career_stats(save)
    all_ok = vf.all_passed(results)
    for key, r in results.items():
        icon = "✅" if r["passed"] else "❌"
        st.markdown(f"{icon} **{r['label']}** — {r['detail']}")
    st.divider()
    if not all_ok:
        st.error("Save does not meet all challenge conditions.")
        return
    st.success("All conditions passed!")
    st.caption(f"Coach: **{career['coach_name']}** — {career['seasons_played']} seasons — "
               f"{career['career_wins']}W / {career['career_losses']}L ({career['win_pct']}%)")
    if st.button("Submit to Leaderboard", type="primary"):
        db.upsert_submission(
            username=username.strip(), challenge_id=challenge["id"],
            team_name=save.meta.get("teamName", ""), team_id=save.meta.get("teamId", ""),
            seasons_played=career["seasons_played"],
            play_time_seconds=save.meta.get("playTimeSeconds", 0),
            verified=True, conditions_met=results,
            coach_name=career["coach_name"],
            career_wins=career["career_wins"], career_losses=career["career_losses"],
        )
        st.success("Submitted!")
        st.balloons()


# ================================================================== Create Challenge (hidden)

def render_create_challenge():
    st.header("Create a Challenge")
    creator = st.text_input("Your name / username", max_chars=32)
    name = st.text_input("Challenge name", max_chars=80)
    description = st.text_area("Description", max_chars=500)
    st.divider()
    st.subheader("Conditions")
    if "draft_conditions" not in st.session_state:
        st.session_state["draft_conditions"] = {}
    conditions = st.session_state["draft_conditions"]
    _BOOL = {"must_win_championship", "must_make_tournament", "must_make_final_four", "single_team_only"}
    _TEXT = {"start_team_id"}
    _PRESTIGE = {"max_start_prestige", "max_recruit_rating"}
    _COUNT = {"min_championships", "min_tournament_appearances", "max_seasons"}
    with st.expander("Add a condition", expanded=not conditions):
        ctype = st.selectbox("Condition type", list(vf.CONDITION_LABELS.keys()),
                             format_func=lambda k: vf.CONDITION_LABELS[k])
        st.caption(vf.CONDITION_HELP.get(ctype, ""))
        if ctype in _BOOL:
            cval = True
            st.info("This condition is either required or not — add it to require it.")
        elif ctype in _TEXT:
            cval = st.text_input("Team ID", placeholder="saint_francis_pa")
        elif ctype in _PRESTIGE:
            cval = st.slider("Value", min_value=1, max_value=99, value=50)
        elif ctype in _COUNT:
            cval = st.number_input("Value", min_value=1, value=1)
        else:
            cval = None
        if st.button("Add condition"):
            if cval not in (None, "", 0):
                conditions[ctype] = cval
                st.session_state["draft_conditions"] = conditions
                st.rerun()
    if conditions:
        st.markdown("**Current conditions:**")
        to_remove = None
        for key, val in conditions.items():
            col_l, col_r = st.columns([5, 1])
            col_l.markdown(f"- **{vf.CONDITION_LABELS.get(key, key)}**: {val}")
            if col_r.button("Remove", key=f"rm_{key}"):
                to_remove = key
        if to_remove:
            del conditions[to_remove]
            st.rerun()
    else:
        st.info("No conditions added — fully honor system.")
    st.divider()
    if not (creator.strip() and name.strip()):
        st.warning("Enter your name and challenge name to publish.")
        return
    if not all(_is_clean(t) for t in [creator, name, description]):
        st.error("Please keep content appropriate.")
        return
    if st.button("Publish Challenge", type="primary"):
        try:
            db.create_challenge(created_by=creator.strip(), name=name.strip(),
                                description=description.strip(), conditions=conditions)
            st.session_state["draft_conditions"] = {}
            st.success(f"Challenge '{name}' published!")
        except Exception as e:
            st.error(f"Failed to publish: {e}")


# ================================================================== Data Pack

_DP_TEAM_COLS = [
    "id", "name", "mascot", "abbreviation", "conferenceId", "state", "pipelineStates",
    "offenseRating", "defenseRating", "prestige", "primaryColor", "secondaryColor", "logoUrl",
    "nilProfile", "nilTier", "nilMin", "nilBase", "nilMax",
]
_DP_CONF_COLS = ["id", "name", "abbreviation", "isPower", "prestigeFloor", "prestigeCeiling", "logoUrl", "revShareTier"]
_NIL_TIERS    = ["", "S", "A", "B", "C", "D"]
_NIL_PROFILES = [
    "", "blueBloodTrad", "nilPowerhouse", "bbFocused",
    "bbFocusedNonP5", "academicLowDonor",
]
_REV_SHARE_TIERS = ["", "S", "A", "B", "C", "D", "E", "F"]


def _dp_teams_to_df(teams: list[dict]) -> pd.DataFrame:
    rows = []
    for t in teams:
        rows.append({
            "id":             t.get("id", ""),
            "name":           t.get("name", ""),
            "mascot":         t.get("mascot", ""),
            "abbreviation":   t.get("abbreviation", ""),
            "conferenceId":   t.get("conferenceId", ""),
            "state":          t.get("state", ""),
            "pipelineStates": ", ".join(t.get("pipelineStates") or []),
            "offenseRating":  t.get("offenseRating", 75),
            "defenseRating":  t.get("defenseRating", 75),
            "prestige":       t.get("prestige", 50),
            "primaryColor":   t.get("primaryColor") or "",
            "secondaryColor": t.get("secondaryColor") or "",
            "logoUrl":        t.get("logoUrl") or "",
            "nilProfile":     t.get("nilProfile") or "",
            "nilTier":        t.get("nilTier") or "",
            "nilMin":         t.get("nilMin"),
            "nilBase":        t.get("nilBase"),
            "nilMax":         t.get("nilMax"),
        })
    return pd.DataFrame(rows)


def _dp_df_to_teams(df: pd.DataFrame, original: list[dict]) -> list[dict]:
    orig_by_id = {t.get("id"): t for t in original}

    def _nan(v):
        return v is None or (isinstance(v, float) and math.isnan(v))

    def _str(v, default=""):
        return default if _nan(v) else str(v).strip() or default

    def _int(v, default):
        if _nan(v):
            return default
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return default

    result = []
    for _, row in df.iterrows():
        tid = _str(row.get("id"))
        orig = orig_by_id.get(tid, {})
        ps_raw = _str(row.get("pipelineStates"))
        nil_profile = _str(row.get("nilProfile")) or None
        nil_tier    = _str(row.get("nilTier")) or None
        nil_min     = _int(row.get("nilMin"), None)
        nil_base    = _int(row.get("nilBase"), None)
        nil_max     = _int(row.get("nilMax"), None)
        entry = {
            "id":             tid,
            "name":           _str(row.get("name")),
            "mascot":         _str(row.get("mascot")),
            "abbreviation":   _str(row.get("abbreviation")),
            "conferenceId":   _str(row.get("conferenceId")),
            "primaryColor":   _str(row.get("primaryColor")) or orig.get("primaryColor"),
            "secondaryColor": _str(row.get("secondaryColor")) or orig.get("secondaryColor"),
            "offenseRating":  _int(row.get("offenseRating"), 75),
            "defenseRating":  _int(row.get("defenseRating"), 75),
            "prestige":       _int(row.get("prestige"), 50),
            "state":          _str(row.get("state")),
            "pipelineStates": [s.strip() for s in ps_raw.split(",") if s.strip()],
            "logoUrl":        _str(row.get("logoUrl")) or _str(orig.get("logoUrl")),
        }
        if nil_profile is not None: entry["nilProfile"] = nil_profile
        if nil_tier    is not None: entry["nilTier"]    = nil_tier
        if nil_min     is not None: entry["nilMin"]     = nil_min
        if nil_base    is not None: entry["nilBase"]    = nil_base
        if nil_max     is not None: entry["nilMax"]     = nil_max
        result.append(entry)
    return result


def _dp_confs_to_df(conferences: list[dict]) -> pd.DataFrame:
    rows = []
    for c in conferences:
        cg = c.get("conferenceGames")
        rows.append({
            "id":               c.get("id", ""),
            "name":             c.get("name", ""),
            "abbreviation":     c.get("abbreviation", ""),
            "isPower":          bool(c.get("isPower", False)),
            "hasTournament":    bool(c.get("hasTournament", True)),
            "conferenceGames":  int(cg) if cg is not None else None,
            "prestigeFloor":    c.get("prestigeFloor", 35),
            "prestigeCeiling":  c.get("prestigeCeiling", 85),
            "logoUrl":          c.get("logoUrl") or "",
            "revShareTier":     c.get("revShareTier") or "",
        })
    return pd.DataFrame(rows)


def _dp_recalc_conf_prestige(confs_df: pd.DataFrame, teams_df: pd.DataFrame) -> pd.DataFrame:
    """
    Recalculate prestigeFloor and prestigeCeiling for each conference based on
    the current prestige values of its teams.

    Floor   = ROUND(MAX(15,  70% * min_prestige) / 5, 0) * 5
    Ceiling = ROUND(MIN(95, 115% * max_prestige) / 5, 0) * 5
    """
    if "conferenceId" not in teams_df.columns or "prestige" not in teams_df.columns:
        return confs_df

    pres = pd.to_numeric(teams_df["prestige"], errors="coerce")
    tmp  = teams_df[["conferenceId"]].copy()
    tmp["prestige"] = pres
    stats = (
        tmp.dropna(subset=["conferenceId", "prestige"])
        .groupby("conferenceId")["prestige"]
        .agg(["min", "max"])
    )

    df = confs_df.copy()
    for idx, row in df.iterrows():
        cid = row.get("id")
        if cid and cid in stats.index:
            mn = stats.loc[cid, "min"]
            mx = stats.loc[cid, "max"]
            df.at[idx, "prestigeFloor"]   = int(round(max(15, 0.70 * mn) / 5) * 5)
            df.at[idx, "prestigeCeiling"] = int(round(min(95, 1.15 * mx) / 5) * 5)
    return df


def _dp_df_to_confs(df: pd.DataFrame) -> list[dict]:
    result = []
    for _, row in df.iterrows():
        cg  = row.get("conferenceGames")
        rst = str(row.get("revShareTier") or "").strip() or None
        entry = {
            "id":              str(row.get("id", "")),
            "name":            str(row.get("name", "")),
            "abbreviation":    str(row.get("abbreviation", "")),
            "isPower":         bool(row.get("isPower", False)),
            "hasTournament":   bool(row.get("hasTournament", True)),
            "conferenceGames": int(cg) if pd.notna(cg) and cg is not None else None,
            "prestigeFloor":   int(row.get("prestigeFloor") or 35),
            "prestigeCeiling": int(row.get("prestigeCeiling") or 85),
            "logoUrl":         row.get("logoUrl") or None,
        }
        if rst is not None:
            entry["revShareTier"] = rst
        result.append(entry)
    return result


def _dp_csv_upload(state_key: str, reference_df: pd.DataFrame, upload_key: str):
    """CSV uploader that writes directly into a dp_* session state key."""
    uploaded_csv = st.file_uploader("Upload CSV to overwrite", type="csv", key=upload_key)
    if uploaded_csv is not None:
        file_id = f"{uploaded_csv.name}_{uploaded_csv.size}"
        last_key = f"_csv_last_id_{upload_key}"
        if st.session_state.get(last_key) != file_id:
            try:
                new_df = pd.read_csv(uploaded_csv)
                for col in new_df.select_dtypes(include="object").columns:
                    new_df[col] = new_df[col].fillna("")
                for col in new_df.columns:
                    if col in reference_df.columns:
                        try:
                            new_df[col] = new_df[col].astype(reference_df[col].dtype)
                        except Exception:
                            pass
                st.session_state[state_key] = new_df
                st.session_state[last_key] = file_id
                st.success(f"Loaded {len(new_df)} rows from CSV.")
            except Exception as e:
                st.error(f"Could not read CSV: {e}")


def _post_to_pastebin(content: str, api_key: str, title: str = "") -> str:
    resp = requests.post("https://pastebin.com/api/api_post.php", data={
        "api_dev_key":          api_key,
        "api_option":           "paste",
        "api_paste_code":       content,
        "api_paste_name":       title,
        "api_paste_format":     "json",
        "api_paste_private":    "1",   # unlisted — accessible via URL, not searchable
        "api_paste_expire_date":"N",   # never expire
    }, timeout=15)
    resp.raise_for_status()
    result = resp.text.strip()
    if result.startswith("Bad API request"):
        raise ValueError(result)
    return result


def _dp_load(url_or_raw: str | None = None, file_bytes: bytes | None = None):
    """Parse a data pack from a URL string or raw bytes; store into session state."""
    if url_or_raw:
        paste_id = url_or_raw.strip().rstrip("/").split("/")[-1]
        raw_url = f"https://pastebin.com/raw/{paste_id}"
        resp = requests.get(raw_url, timeout=10)
        resp.raise_for_status()
        data = json.loads(resp.text)
    else:
        data = json.loads(file_bytes.decode("utf-8"))
    st.session_state["dp_raw"] = data
    st.session_state.pop("dp_teams", None)
    st.session_state.pop("dp_confs", None)
    st.session_state.pop("_dp_confs_base", None)
    st.session_state.pop("dp_json_output", None)
    st.session_state.pop("dp_paste_url", None)


def render_data_pack():
    st.header("Data Pack Editor")
    st.caption(
        "Import a data pack from Pastebin/JSON, edit, "
        "then post the updated pack back to Pastebin and drop the URL into Campus Hoops."
    )

    # ------------------------------------------------------------------ 1. Import
    st.subheader("1 · Import")
    with st.expander("Start from existing data pack", expanded="dp_raw" not in st.session_state):
        col_url, col_btn = st.columns([5, 1])
        url_input = col_url.text_input(
            "Pastebin URL", placeholder="https://pastebin.com/abc123",
            label_visibility="collapsed", key="dp_url_input",
        )
        if col_btn.button("Fetch", use_container_width=True):
            if url_input.strip():
                try:
                    _dp_load(url_or_raw=url_input.strip())
                    st.success(
                        f"Loaded: **{st.session_state['dp_raw'].get('meta', {}).get('name', 'data pack')}**"
                    )
                except Exception as e:
                    st.error(f"Could not fetch: {e}")
            else:
                st.warning("Enter a Pastebin URL first.")

        st.caption("— or —")
        uploaded_json = st.file_uploader("Upload JSON file", type="json", key="dp_json_upload")
        if uploaded_json is not None:
            file_id = f"{uploaded_json.name}_{uploaded_json.size}"
            if st.session_state.get("dp_upload_id") != file_id:
                try:
                    _dp_load(file_bytes=uploaded_json.read())
                    st.session_state["dp_upload_id"] = file_id
                    st.success(
                        f"Loaded: **{st.session_state['dp_raw'].get('meta', {}).get('name', 'data pack')}**"
                    )
                except Exception as e:
                    st.error(f"Could not parse JSON: {e}")

    if "dp_raw" not in st.session_state:
        return

    raw: dict = st.session_state["dp_raw"]

    # Show loaded pack summary
    meta = raw.get("meta", {})
    st.info(
        f"**{meta.get('name', '—')}** · v{meta.get('version', 1)} · by {meta.get('author', '—')}  \n"
        f"{meta.get('description', '')}"
    )

    # Lazy-init editable DataFrames
    if "dp_confs" not in st.session_state:
        st.session_state["dp_confs"] = _dp_confs_to_df(raw.get("conferences", []))
    if "dp_teams" not in st.session_state:
        st.session_state["dp_teams"] = _dp_teams_to_df(raw.get("teams", []))

    # ------------------------------------------------------------------ 2. Edit
    st.subheader("2 · Edit")
    tab_meta, tab_branding, tab_awards, tab_rules, tab_confs, tab_teams = st.tabs(
        ["Meta", "Branding", "Awards", "Rules", "Conferences", "Teams"]
    )

    with tab_meta:
        settings = raw.get("settings", {})

        st.markdown("**Meta**")
        new_name     = st.text_input("Pack name",    value=meta.get("name", ""))
        new_author   = st.text_input("Author",       value=meta.get("author", ""))
        new_version  = st.number_input("Version", value=int(meta.get("version", 1)), min_value=1, step=1)
        new_desc     = st.text_area("Description",   value=meta.get("description", ""), height=80)
        new_nil_era  = st.checkbox("NIL Era", value=bool(meta.get("nilEra", False)))

        st.markdown("**Settings**")
        new_starting_year = st.number_input(
            "Starting year", value=int(settings.get("startingYear", 2026)),
            min_value=1900, max_value=2100, step=1,
        )

        if st.button("Save Meta & Settings"):
            st.session_state["dp_raw"] = {
                **raw,
                "meta": {
                    "name": new_name, "author": new_author,
                    "version": new_version, "description": new_desc,
                    "nilEra": new_nil_era,
                },
                "settings": {
                    **settings,
                    "startingYear": new_starting_year,
                },
            }
            st.success("Meta & Settings updated.")

    with tab_branding:
        branding   = raw.get("branding", {})
        rn         = branding.get("roundNames", {})
        regions    = branding.get("regionNames", ["East", "West", "South", "Midwest"])

        b_natl = st.text_input("National Tournament name", value=branding.get("nationalTournament", ""))
        b_hof  = st.text_input("Hall of Fame name",        value=branding.get("hallOfFame", ""))

        st.markdown("**Round Names**")
        _round_keys = ["firstFour","roundOf64","roundOf32","sweet16","elite8","finalFour","championship"]
        _round_lbls = ["First Four","Round of 64","Round of 32","Sweet Sixteen","Elite Eight","Final Four","Championship"]
        b_rounds = {}
        rn_c1, rn_c2 = st.columns(2)
        for i, (k, lbl) in enumerate(zip(_round_keys, _round_lbls)):
            with (rn_c1 if i % 2 == 0 else rn_c2):
                b_rounds[k] = st.text_input(lbl, value=rn.get(k, ""), key=f"br_rn_{k}")

        st.markdown("**Region Names**")
        reg_cols = st.columns(4)
        b_regions = []
        for i in range(4):
            with reg_cols[i]:
                b_regions.append(st.text_input(
                    f"Region {i+1}", value=regions[i] if i < len(regions) else "", key=f"br_reg_{i}"
                ))

        if st.button("Save Branding"):
            st.session_state["dp_raw"] = {
                **st.session_state["dp_raw"],
                "branding": {
                    "nationalTournament": b_natl,
                    "roundNames": b_rounds,
                    "regionNames": [r for r in b_regions if r],
                    "hallOfFame": b_hof,
                },
            }
            st.success("Branding updated.")

    with tab_awards:
        awards    = raw.get("awards", {})
        hs_awards = raw.get("hsAwards", {})

        st.markdown("**Awards**")
        _aw_keys = ["mvp","dpoy","freshman","mostImproved","sixthMan","coachOfYear",
                    "tournamentMop","allAmerican","allConference","allTournament",
                    "positionOfYear","nationalChampion","confChampion"]
        _aw_lbls = {
            "mvp":"Player of the Year","dpoy":"Defensive POY","freshman":"Freshman Award",
            "mostImproved":"Most Improved","sixthMan":"Sixth Man","coachOfYear":"Coach of the Year",
            "tournamentMop":"Tournament MOP","allAmerican":"All-American",
            "allConference":"All-Conference","allTournament":"All-Tournament",
            "positionOfYear":"Position of the Year","nationalChampion":"National Champion",
            "confChampion":"Conference Champion",
        }
        new_awards = {}
        aw_c1, aw_c2 = st.columns(2)
        for i, k in enumerate(_aw_keys):
            with (aw_c1 if i % 2 == 0 else aw_c2):
                new_awards[k] = st.text_input(_aw_lbls[k], value=awards.get(k, ""), key=f"aw_{k}")

        st.markdown("**High School Awards**")
        _hs_keys = ["nationalPoy","allAmerican","statePoy","eliteShowcase"]
        _hs_lbls = {
            "nationalPoy":"National POY","allAmerican":"All-American",
            "statePoy":"State POY","eliteShowcase":"Elite Showcase",
        }
        new_hs = {}
        hs_c1, hs_c2 = st.columns(2)
        for i, k in enumerate(_hs_keys):
            with (hs_c1 if i % 2 == 0 else hs_c2):
                new_hs[k] = st.text_input(_hs_lbls[k], value=hs_awards.get(k, ""), key=f"hs_{k}")

        if st.button("Save Awards"):
            st.session_state["dp_raw"] = {
                **st.session_state["dp_raw"],
                "awards": new_awards,
                "hsAwards": new_hs,
            }
            st.success("Awards updated.")

    with tab_rules:
        rules      = raw.get("rules", {})
        sched      = rules.get("schedule", {})
        ct         = rules.get("conferenceTournaments", {})
        nt         = rules.get("nationalTournament", {})
        pi         = nt.get("playIn", {})
        bb         = nt.get("bubble", {})
        bk         = nt.get("bracket", {})
        rn_teams   = nt.get("roundNamesByRemainingTeams", {})

        st.markdown("**Schedule**")
        sc1, sc2 = st.columns(2)
        r_games      = sc1.number_input("Games per team",          value=int(sched.get("gamesPerTeam", 32)),          min_value=1)
        r_conf_games = sc2.number_input("Default conference games", value=int(sched.get("defaultConferenceGames", 18)), min_value=1, max_value=r_games)

        st.markdown("**Conference Tournaments**")
        ct1, ct2, ct3, ct4 = st.columns(4)
        r_ct_enabled    = ct1.checkbox("Enabled",        value=bool(ct.get("enabled", True)),       key="r_ct_en")
        r_ct_autobid    = ct2.checkbox("Winner auto bid", value=bool(ct.get("winnerAutoBid", True)), key="r_ct_ab")
        r_ct_qualifiers = ct3.text_input("Qualifiers",   value=str(ct.get("defaultQualifiers", "all")), key="r_ct_q")
        r_ct_bracket    = ct4.selectbox("Bracket type",  ["singleElimination"],
                                         index=0 if ct.get("bracketType","singleElimination") == "singleElimination" else 1,
                                         key="r_ct_br")

        st.markdown("**National Tournament**")
        nt1, nt2, nt3 = st.columns(3)
        r_nt_enabled    = nt1.checkbox("Enabled",    value=bool(nt.get("enabled", True)),  key="r_nt_en")
        r_nt_field      = nt2.number_input("Field size",         value=int(nt.get("fieldSize", 68)),         min_value=1)
        r_nt_main       = nt3.number_input("Main bracket size",  value=int(nt.get("mainBracketSize", 64)),   min_value=1, max_value=r_nt_field)
        na1, na2, na3 = st.columns(3)
        r_nt_autobid_src   = na1.text_input("Auto bid source",    value=str(nt.get("autoBidSource", "")))
        r_nt_autobid_confs = na2.text_input("Auto bid confs",     value=str(nt.get("autoBidConferences", "all")))
        r_nt_atlarge_src   = na3.text_input("At-large bid source",value=str(nt.get("atLargeBidSource", "")))

        st.markdown("*Play-In*")
        pi1, pi2, pi3 = st.columns(3)
        r_pi_enabled = pi1.checkbox("Enabled",         value=bool(pi.get("enabled", True)),     key="r_pi_en")
        r_pi_autobid = pi2.number_input("Auto bid teams", value=int(pi.get("autoBidTeams", 4)), min_value=0)
        r_pi_atlarge = pi3.number_input("At-large teams", value=int(pi.get("atLargeTeams", 4)), min_value=0)

        st.markdown("*Bubble*")
        bb1, bb2, bb3 = st.columns(3)
        r_bb_enabled  = bb1.checkbox("Enabled",         value=bool(bb.get("enabled", True)),      key="r_bb_en")
        r_bb_lastin   = bb2.number_input("Last in",     value=int(bb.get("lastInCount", 4)),      min_value=0)
        r_bb_firstout = bb3.number_input("First out",   value=int(bb.get("firstOutCount", 4)),    min_value=0)

        st.markdown("*Bracket*")
        bk1, bk2 = st.columns(2)
        r_bk_type    = bk1.selectbox("Type", ["singleElimination"],
                                      index=0 if bk.get("type","singleElimination") == "singleElimination" else 1,
                                      key="r_bk_ty")
        r_bk_regions = bk2.number_input("Regions", value=int(bk.get("regions", 4)), min_value=1)

        st.markdown("*Round Names by Remaining Teams*")
        rn_df_default = [{"teams_remaining": k, "round_name": ""}
                         for k in ["68","64","32","16","8","4","2"]]
        rn_df = pd.DataFrame([
            {"teams_remaining": k, "round_name": v} for k, v in rn_teams.items()
        ] if rn_teams else rn_df_default)
        edited_rn_df = st.data_editor(
            rn_df,
            column_config={
                "teams_remaining": st.column_config.TextColumn("Teams Remaining"),
                "round_name":      st.column_config.TextColumn("Round Name"),
            },
            use_container_width=True, num_rows="dynamic", key="rn_editor",
        )
        new_rn = {
            str(row["teams_remaining"]): str(row["round_name"])
            for _, row in edited_rn_df.iterrows()
            if row.get("teams_remaining") and row.get("round_name")
        }

        if st.button("Save Rules"):
            st.session_state["dp_raw"] = {
                **st.session_state["dp_raw"],
                "rules": {
                    "schedule": {
                        "gamesPerTeam": r_games,
                        "defaultConferenceGames": r_conf_games,
                    },
                    "conferenceTournaments": {
                        "enabled": r_ct_enabled,
                        "defaultQualifiers": r_ct_qualifiers,
                        "bracketType": r_ct_bracket,
                        "winnerAutoBid": r_ct_autobid,
                    },
                    "nationalTournament": {
                        "enabled": r_nt_enabled,
                        "fieldSize": r_nt_field,
                        "mainBracketSize": r_nt_main,
                        "autoBidSource": r_nt_autobid_src,
                        "autoBidConferences": r_nt_autobid_confs,
                        "atLargeBidSource": r_nt_atlarge_src,
                        "playIn": {
                            "enabled": r_pi_enabled,
                            "autoBidTeams": r_pi_autobid,
                            "atLargeTeams": r_pi_atlarge,
                        },
                        "bubble": {
                            "enabled": r_bb_enabled,
                            "lastInCount": r_bb_lastin,
                            "firstOutCount": r_bb_firstout,
                        },
                        "bracket": {
                            "type": r_bk_type,
                            "regions": r_bk_regions,
                        },
                        "roundNamesByRemainingTeams": new_rn,
                    },
                },
            }
            st.success("Rules updated.")

    with tab_confs:
        # Recalculate floor/ceiling from current team prestiges on every visit
        _recalced = _dp_recalc_conf_prestige(
            st.session_state["dp_confs"], st.session_state["dp_teams"]
        )
        if not _recalced.equals(st.session_state["dp_confs"]):
            st.session_state["dp_confs"] = _recalced
            st.session_state.pop("_dp_confs_base", None)

        confs_df = st.session_state["dp_confs"]
        st.caption(f"{len(confs_df)} conferences")
        _info_col, _btn_col = st.columns([6, 1])
        with _info_col:
            st.info(
                "**Prestige Floor and Ceiling are auto-recalculated** whenever team conference "
                "assignments change on the Teams tab.  \n"
                "**Floor** = 70% of the min team prestige · "
                "**Ceiling** = 115% of the max team prestige · "
                "Both capped between 15–95 and rounded to the nearest 5.",
                icon="ℹ️",
            )
        with _btn_col:
            if st.button("Refresh", key="dp_confs_refresh", use_container_width=True,
                         help="Force-recalculate prestige floor/ceiling from current team data."):
                _refreshed = _dp_recalc_conf_prestige(
                    st.session_state["dp_confs"], st.session_state["dp_teams"]
                )
                st.session_state["dp_confs"] = _refreshed
                st.session_state.pop("_dp_confs_base", None)
                st.rerun()
        _max_games = raw.get("rules", {}).get("schedule", {}).get("gamesPerTeam")
        conf_col_cfg = {
            "id":               st.column_config.TextColumn("ID"),
            "isPower":          st.column_config.CheckboxColumn("Power"),
            "hasTournament":    st.column_config.CheckboxColumn("Tournament"),
            "conferenceGames":  st.column_config.NumberColumn(
                "Conf Games", min_value=1,
                **( {"max_value": int(_max_games)} if _max_games else {} ),
            ),
            "prestigeFloor":    st.column_config.NumberColumn("Pres Floor",   min_value=1, max_value=99),
            "prestigeCeiling":  st.column_config.NumberColumn("Pres Ceiling", min_value=1, max_value=99),
            "revShareTier":     st.column_config.SelectboxColumn("Rev Share", options=_REV_SHARE_TIERS),
            "logoUrl":          st.column_config.LinkColumn("Logo URL", display_text="link"),
        }
        _confs_base_key = "_dp_confs_base"
        if _confs_base_key not in st.session_state:
            st.session_state[_confs_base_key] = confs_df.copy()
        editor_base_confs = st.session_state[_confs_base_key]

        edited_confs = st.data_editor(
            editor_base_confs, column_config=conf_col_cfg,
            use_container_width=True, num_rows="dynamic", key="dp_confs_editor",
        )
        if not edited_confs.equals(editor_base_confs):
            st.session_state["dp_confs"] = edited_confs

        c1, c2 = st.columns(2)
        with c1:
            _csv_download_btn(edited_confs, "datapack_conferences.csv")
        with c2:
            prev_last_confs = st.session_state.get("_csv_last_id_dp_csv_confs")
            _dp_csv_upload("dp_confs", editor_base_confs, "dp_csv_confs")
            if st.session_state.get("_csv_last_id_dp_csv_confs") != prev_last_confs:
                st.session_state.pop(_confs_base_key, None)
                st.rerun()

    with tab_teams:
        teams_df = st.session_state["dp_teams"]

        conf_id_to_name = st.session_state["dp_confs"].set_index("id")["name"].to_dict()
        conf_name_to_id = {v: k for k, v in conf_id_to_name.items()}
        conf_name_options_all = [""] + sorted(conf_name_to_id.keys())

        # Keep _conf_name in sync with conferenceId on every render.
        # An uploaded CSV may have _conf_name but not conferenceId (since we hide that
        # column from the editor/download), so reconstruct it first if missing.
        teams_df = teams_df.copy()
        if "conferenceId" not in teams_df.columns:
            if "_conf_name" in teams_df.columns:
                teams_df["conferenceId"] = teams_df["_conf_name"].map(conf_name_to_id).fillna("")
            else:
                teams_df["conferenceId"] = ""
        teams_df["_conf_name"] = teams_df["conferenceId"].map(conf_id_to_name).fillna("")
        st.session_state["dp_teams"] = teams_df

        us_states = [
            "", "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
            "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM",
            "NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA",
            "WV","WI","WY","DC",
        ]
        teams_col_cfg = {
            "id":             st.column_config.TextColumn("ID"),
            "_conf_name":     st.column_config.SelectboxColumn("Conference", options=conf_name_options_all),
            "state":          st.column_config.SelectboxColumn("State",      options=us_states),
            "offenseRating":  st.column_config.NumberColumn("Off Rtg",  min_value=1, max_value=99),
            "defenseRating":  st.column_config.NumberColumn("Def Rtg",  min_value=1, max_value=99),
            "prestige":       st.column_config.NumberColumn("Prestige", min_value=1, max_value=99),
            "primaryColor":   st.column_config.TextColumn("Primary"),
            "secondaryColor": st.column_config.TextColumn("Secondary"),
            "logoUrl":        st.column_config.LinkColumn("Logo URL", display_text="link"),
            "nilProfile":     st.column_config.SelectboxColumn("NIL Profile", options=_NIL_PROFILES),
            "nilTier":        st.column_config.SelectboxColumn("NIL Tier", options=_NIL_TIERS),
            "nilMin":         st.column_config.NumberColumn("NIL Min", min_value=0),
            "nilBase":        st.column_config.NumberColumn("NIL Base", min_value=0),
            "nilMax":         st.column_config.NumberColumn("NIL Max", min_value=0),
        }

        conf_name_filter_options = ["All"] + sorted(
            conf_id_to_name.get(cid, cid)
            for cid in teams_df["conferenceId"].dropna().unique()
        )
        conf_filter_name = st.selectbox(
            "Filter by conference", conf_name_filter_options, key="dp_teams_conf_filter",
        )
        if conf_filter_name != "All":
            filter_id = conf_name_to_id.get(conf_filter_name, conf_filter_name)
            display_teams = teams_df[teams_df["conferenceId"] == filter_id].copy()
        else:
            filter_id = "All"
            display_teams = teams_df
        # Drop conferenceId from display and move _conf_name before state
        _dt_cols = [c for c in display_teams.columns if c != "conferenceId"]
        if "_conf_name" in _dt_cols and "state" in _dt_cols:
            _dt_cols.remove("_conf_name")
            _dt_cols.insert(_dt_cols.index("state"), "_conf_name")
        display_teams = display_teams[_dt_cols]

        # Frozen base pattern — same fix as Rosters: always pass a stable DataFrame
        # to data_editor so its React component never receives new props mid-edit.
        base_key = f"_dp_teams_base_{filter_id}"
        if base_key not in st.session_state:
            st.session_state[base_key] = display_teams.copy()
        editor_base = st.session_state[base_key]

        st.caption(f"{len(display_teams)} of {len(teams_df)} teams")

        edited_teams = st.data_editor(
            editor_base, column_config=teams_col_cfg,
            use_container_width=True, num_rows="dynamic",
            key=f"dp_teams_editor_{filter_id}",
        )
        if not edited_teams.equals(editor_base):
            id_to_edit = edited_teams.set_index("id").to_dict("index")
            new_full = teams_df.copy()
            conf_changed = False
            for idx, row in new_full.iterrows():
                tid = row.get("id")
                if tid and tid in id_to_edit:
                    for col, val in id_to_edit[tid].items():
                        if col == "_conf_name" and val != row.get("_conf_name"):
                            conf_changed = True
                        new_full.at[idx, col] = val
            # Remove rows deleted in this filter view
            base_ids   = set(editor_base["id"].dropna()) if "id" in editor_base.columns else set()
            edited_ids = set(edited_teams["id"].dropna()) if "id" in edited_teams.columns else set()
            deleted_ids = base_ids - edited_ids
            if deleted_ids:
                new_full = new_full[~new_full["id"].isin(deleted_ids)].reset_index(drop=True)
            new_full["conferenceId"] = new_full["_conf_name"].map(conf_name_to_id).fillna(new_full["conferenceId"])
            st.session_state["dp_teams"] = new_full
            for _k in [k for k in st.session_state if k.startswith("_dp_teams_base_")]:
                del st.session_state[_k]
            if conf_changed:
                st.session_state["dp_confs"] = _dp_recalc_conf_prestige(
                    st.session_state["dp_confs"], new_full
                )
                st.session_state.pop("_dp_confs_base", None)
                st.rerun()

        c1, c2 = st.columns(2)
        with c1:
            _csv_download_btn(edited_teams, f"datapack_teams_{filter_id}.csv")
        with c2:
            prev_last = st.session_state.get(f"_csv_last_id_dp_csv_teams_{filter_id}")
            _dp_csv_upload("dp_teams", teams_df, f"dp_csv_teams_{filter_id}")
            if st.session_state.get(f"_csv_last_id_dp_csv_teams_{filter_id}") != prev_last:
                for _k in [k for k in st.session_state if k.startswith("_dp_teams_base_")]:
                    del st.session_state[_k]
                st.rerun()

    # ------------------------------------------------------------------ 3. Export
    st.subheader("3 · Export")

    if st.button("Build JSON", type="primary"):
        output = dict(raw)
        confs_sorted = _dp_recalc_conf_prestige(
            st.session_state["dp_confs"], st.session_state["dp_teams"]
        ).sort_values("name", key=lambda s: s.str.lower(), na_position="last")
        output["conferences"] = _dp_df_to_confs(confs_sorted)
        conf_id_to_name_export = confs_sorted.set_index("id")["name"].to_dict()
        teams_export = st.session_state["dp_teams"].copy()
        teams_export["_sort_conf"] = teams_export["conferenceId"].map(conf_id_to_name_export).fillna("")
        teams_export = teams_export.sort_values(
            ["_sort_conf", "name"], key=lambda s: s.str.lower(), na_position="last"
        )
        output["teams"]       = _dp_df_to_teams(teams_export, raw.get("teams", []))
        st.session_state["dp_json_output"] = json.dumps(output, indent=2)
        st.session_state.pop("dp_paste_url", None)

    if "dp_json_output" not in st.session_state:
        return

    json_str = st.session_state["dp_json_output"]
    with st.expander(f"JSON output ({len(json_str):,} characters) — copy button is inside ↓"):
        st.code(json_str, language="json")

    st.divider()
    st.subheader("Post to Pastebin")

    # Use site-level key from Streamlit secrets or env var; fall back to user input.
    _site_key = st.secrets.get("PASTEBIN_API_KEY", "") or os.environ.get("PASTEBIN_API_KEY", "")
    if _site_key:
        api_key = _site_key
    else:
        st.caption("No site key configured — enter your own from [pastebin.com/api](https://pastebin.com/api).")
        api_key = st.text_input(
            "Pastebin API key", type="password", key="dp_pastebin_key",
            placeholder="Your dev key from pastebin.com/api",
        )

    default_title = st.session_state["dp_raw"].get("meta", {}).get("name", "Campus Hoops data pack")
    pack_title = st.text_input("Paste title", value=default_title, key="dp_paste_title")

    if st.button("Post to Pastebin →", disabled=not api_key, type="primary"):
        with st.spinner("Posting…"):
            try:
                paste_url = _post_to_pastebin(json_str, api_key, title=pack_title)
                st.session_state["dp_paste_url"] = paste_url
            except Exception as e:
                st.error(f"Pastebin error: {e}")

    if "dp_paste_url" in st.session_state:
        st.success("Posted! Copy this URL into Campus Hoops to load your data pack:")
        st.code(st.session_state["dp_paste_url"])

    # ------------------------------------------------------------------ 4. Logo Lab
    st.divider()
    st.subheader("4 · Logo Lab")

    tab_fetch, tab_process = st.tabs(["Fetch from URLs", "Process Local PNGs"])

    with tab_fetch:
        st.caption("Fetch logos from each team's `logoUrl` for a given state and download as a ZIP ready to push to GitHub (`logos/{state}/slug.png`).")
        _teams_for_logos = st.session_state.get("dp_teams")
        if _teams_for_logos is not None and not _teams_for_logos.empty:
            _states_available = sorted(_teams_for_logos["state"].dropna().unique().tolist())
            _logo_state = st.selectbox(
                "State", options=_states_available, key="dp_logo_state",
            )
            _dl_remove_bg = st.checkbox(
                "Remove background", value=True, key="dp_logo_dl_rmbg",
                help="BFS from all image edges using the auto-detected background colour.",
            )
            _dl_remove_enc = st.checkbox(
                "Remove enclosed white (letter counters)", value=False, key="dp_logo_dl_enc",
                help="Also removes near-white regions fully enclosed by non-transparent pixels. "
                     "Don't use if logos have intentional white inside a coloured shape.",
            )
            _dl_enc_stroke = st.checkbox(
                "↳ Add interior white border at removed edges", value=False, key="dp_logo_dl_enc_stroke",
                disabled=not _dl_remove_enc,
                help="After erasing enclosed white holes, paint a 1px white outline around each hole's inner edge.",
            )
            _dl_white_stroke = st.checkbox(
                "Add 1px white border", value=False, key="dp_logo_dl_stroke",
                help="Grow a 1-pixel white outline around all non-transparent content. Skipped automatically if a white border already exists.",
            )
            _dl_pad_square = st.checkbox(
                "Pad to square", value=False, key="dp_logo_dl_pad",
                help="Expand each logo to a square canvas with transparent padding, keeping the image centered.",
            )
            if st.button("Build ZIP", key="dp_logo_zip_btn"):
                _logo_rows = _teams_for_logos[
                    _teams_for_logos["state"] == _logo_state
                ][["id", "logoUrl"]].dropna(subset=["logoUrl"])
                _logo_rows = _logo_rows[_logo_rows["logoUrl"].str.strip() != ""]

                _zip_buf = io.BytesIO()
                _fetched = _skipped = 0
                _prog = st.progress(0.0, text="Fetching logos…")
                _total = len(_logo_rows)

                with zipfile.ZipFile(_zip_buf, "w", zipfile.ZIP_STORED) as _zf:
                    for _idx, (_i, _row) in enumerate(_logo_rows.iterrows()):
                        _prog.progress((_idx + 1) / _total, text=f"{_idx + 1}/{_total} — {_row['id']}")
                        try:
                            _resp = requests.get(_row["logoUrl"], timeout=10)
                            if _resp.status_code == 200:
                                _png = _resp.content
                                if _dl_remove_bg:
                                    _png = _remove_white_bg(_png, remove_enclosed=_dl_remove_enc, enclosed_stroke=_dl_enc_stroke)
                                if _dl_white_stroke:
                                    _png = _add_white_stroke(_png)
                                if _dl_pad_square:
                                    _png = _pad_to_square(_png)
                                _zf.writestr(
                                    f"{_logo_state.lower()}/{_row['id']}.png",
                                    _png,
                                )
                                _fetched += 1
                            else:
                                _skipped += 1
                        except Exception:
                            _skipped += 1

                _prog.empty()
                _zip_buf.seek(0)
                st.download_button(
                    label=f"Download {_logo_state.lower()}_logos.zip  ({_fetched} logos, {_skipped} skipped)",
                    data=_zip_buf,
                    file_name=f"{_logo_state.lower()}_logos.zip",
                    mime="application/zip",
                    key="dp_logo_zip_dl",
                )

    with tab_process:
        st.caption("Upload PNGs, apply adjustments, and download the results as a ZIP. To upload a whole folder, open the picker, navigate to the folder, and press Ctrl+A to select all.")
        _local_files = st.file_uploader(
            "Upload PNGs", type="png", accept_multiple_files=True, key="dp_local_png_upload",
        )
        if _local_files:
            st.caption(f"{len(_local_files)} PNG(s) uploaded.")
            _local_remove_bg = st.checkbox(
                "Remove background", value=False, key="dp_local_rmbg",
                help="BFS from all image edges using the auto-detected background colour.",
            )
            _local_remove_enc = st.checkbox(
                "Remove enclosed white (letter counters)", value=False, key="dp_local_enc",
                help="Also removes near-white regions fully enclosed by non-transparent pixels.",
            )
            _local_enc_stroke = st.checkbox(
                "↳ Add interior white border at removed edges", value=False, key="dp_local_enc_stroke",
                disabled=not _local_remove_enc,
                help="After erasing enclosed white holes, paint a 1px white outline around each hole's inner edge.",
            )
            _local_white_stroke = st.checkbox(
                "Add 1px white border", value=False, key="dp_local_stroke",
                help="Grow a 1-pixel white outline around all non-transparent content. Skipped automatically if a white border already exists.",
            )
            _local_pad_square = st.checkbox(
                "Pad to square", value=True, key="dp_local_pad",
                help="Expand each logo to a square canvas with transparent padding, keeping the image centered.",
            )
            if st.button("Process & Build ZIP", key="dp_local_zip_btn"):
                _local_buf = io.BytesIO()
                _local_prog = st.progress(0.0, text="Processing…")
                _local_total = len(_local_files)
                with zipfile.ZipFile(_local_buf, "w", zipfile.ZIP_STORED) as _zf:
                    for _li, _uf in enumerate(_local_files):
                        _local_prog.progress((_li + 1) / _local_total, text=f"{_li + 1}/{_local_total} — {_uf.name}")
                        _png = _uf.read()
                        if _local_remove_bg:
                            _png = _remove_white_bg(_png, remove_enclosed=_local_remove_enc, enclosed_stroke=_local_enc_stroke)
                        if _local_white_stroke:
                            _png = _add_white_stroke(_png)
                        if _local_pad_square:
                            _png = _pad_to_square(_png)
                        _zf.writestr(_uf.name, _png)
                _local_prog.empty()
                _local_buf.seek(0)
                st.download_button(
                    label=f"Download processed_{_local_total}_logos.zip",
                    data=_local_buf,
                    file_name=f"processed_{_local_total}_logos.zip",
                    mime="application/zip",
                    key="dp_local_zip_dl",
                )


# ================================================================== router

if page == "Recruiting Pool":
    render_recruiting()
elif page == "Coaches":
    render_coaches()
elif page == "Rosters":
    render_rosters()
elif page == "Teams":
    render_teams()
elif page == "Conferences":
    render_conferences()
elif page == "Data Pack":
    render_data_pack()
