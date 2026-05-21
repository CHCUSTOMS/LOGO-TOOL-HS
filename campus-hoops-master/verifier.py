"""
Challenge verification engine — reads from history.db for all historical checks.

Table reference (from history.db schema exploration):
  coach_career          — one row per coach per season; championship/final_four/made_tournament flags
  user_season_recaps    — one row per user season; team_id shows team history
  user_coach_rating_history — one row per user season; coach_prestige at each point
  user_coach_recruiting_classes — every player the user recruited
  team_seasons          — all teams all seasons; prestige at any point in time
"""

from __future__ import annotations
import sqlite3
from save_loader import SaveFile

USER_COACH_ID = "user_coach"

# ------------------------------------------------------------------ condition metadata

CONDITION_LABELS = {
    "start_team_id":           "Must start with a specific team",
    "max_start_prestige":      "Starting team prestige must be ≤ X",
    "must_win_championship":   "Must win at least one national championship",
    "min_championships":       "Must win at least N national championships",
    "must_make_tournament":    "Must make the NCAA tournament at least once",
    "min_tournament_appearances": "Must make tournament at least N times",
    "must_make_final_four":    "Must reach the Final Four at least once",
    "single_team_only":        "Must never change teams",
    "max_seasons":             "Must complete within N seasons",
    "max_recruit_rating":      "No recruited player rated above X",
}

CONDITION_HELP = {
    "start_team_id":           "Team ID of the team you must begin your career with (e.g. 'saint_francis_pa')",
    "max_start_prestige":      "Your team's prestige in your first season must be at or below this value (1–99)",
    "must_win_championship":   "Your coach career must include at least one national championship",
    "min_championships":       "Minimum number of national championships won across your career",
    "must_make_tournament":    "Must have at least one NCAA tournament appearance",
    "min_tournament_appearances": "Minimum number of NCAA tournament appearances",
    "must_make_final_four":    "Must have at least one Final Four appearance",
    "single_team_only":        "You must coach only one program — no team changes allowed",
    "max_seasons":             "The save's current season year must be at or below this number",
    "max_recruit_rating":      "No recruited player in your history may be rated above this value",
}


# ------------------------------------------------------------------ verifiers

def _verify_start_team_id(save: SaveFile, conn: sqlite3.Connection, expected: str) -> dict:
    row = conn.execute(
        "SELECT team_id FROM user_season_recaps ORDER BY season_year ASC LIMIT 1"
    ).fetchone()
    actual = row["team_id"] if row else save.meta.get("teamId", "")
    passed = actual == expected
    return {
        "passed": passed,
        "label": f"Must start with team: {expected}",
        "detail": f"Started with '{actual}'" if not passed else f"✓ {actual}",
    }


def _verify_max_start_prestige(save: SaveFile, conn: sqlite3.Connection, expected: int) -> dict:
    row = conn.execute(
        "SELECT coach_prestige FROM user_coach_rating_history ORDER BY season_year ASC LIMIT 1"
    ).fetchone()
    actual = row["coach_prestige"] if row else 999
    passed = actual <= int(expected)
    return {
        "passed": passed,
        "label": f"Starting prestige ≤ {expected}",
        "detail": f"Starting prestige was {actual}",
    }


def _verify_must_win_championship(save: SaveFile, conn: sqlite3.Connection, expected: bool) -> dict:
    if not expected:
        return {"passed": True, "label": "No championship required", "detail": "N/A"}
    count = conn.execute(
        "SELECT COUNT(*) as n FROM coach_career WHERE coach_id = ? AND championship = 1",
        (USER_COACH_ID,)
    ).fetchone()["n"]
    passed = count > 0
    return {
        "passed": passed,
        "label": "Must win at least one national championship",
        "detail": f"{count} championship(s) found in career history",
    }


def _verify_min_championships(save: SaveFile, conn: sqlite3.Connection, expected: int) -> dict:
    count = conn.execute(
        "SELECT COUNT(*) as n FROM coach_career WHERE coach_id = ? AND championship = 1",
        (USER_COACH_ID,)
    ).fetchone()["n"]
    passed = count >= int(expected)
    return {
        "passed": passed,
        "label": f"Must win at least {expected} national championship(s)",
        "detail": f"{count} championship(s) found in career history",
    }


def _verify_must_make_tournament(save: SaveFile, conn: sqlite3.Connection, expected: bool) -> dict:
    if not expected:
        return {"passed": True, "label": "Tournament appearance not required", "detail": "N/A"}
    count = conn.execute(
        "SELECT COUNT(*) as n FROM coach_career WHERE coach_id = ? AND made_tournament = 1",
        (USER_COACH_ID,)
    ).fetchone()["n"]
    passed = count > 0
    return {
        "passed": passed,
        "label": "Must make the NCAA tournament at least once",
        "detail": f"{count} tournament appearance(s) found",
    }


def _verify_min_tournament_appearances(save: SaveFile, conn: sqlite3.Connection, expected: int) -> dict:
    count = conn.execute(
        "SELECT COUNT(*) as n FROM coach_career WHERE coach_id = ? AND made_tournament = 1",
        (USER_COACH_ID,)
    ).fetchone()["n"]
    passed = count >= int(expected)
    return {
        "passed": passed,
        "label": f"Must make tournament at least {expected} time(s)",
        "detail": f"{count} tournament appearance(s) found",
    }


def _verify_must_make_final_four(save: SaveFile, conn: sqlite3.Connection, expected: bool) -> dict:
    if not expected:
        return {"passed": True, "label": "Final Four not required", "detail": "N/A"}
    count = conn.execute(
        "SELECT COUNT(*) as n FROM coach_career WHERE coach_id = ? AND final_four = 1",
        (USER_COACH_ID,)
    ).fetchone()["n"]
    passed = count > 0
    return {
        "passed": passed,
        "label": "Must reach the Final Four at least once",
        "detail": f"{count} Final Four appearance(s) found",
    }


def _verify_single_team_only(save: SaveFile, conn: sqlite3.Connection, expected: bool) -> dict:
    if not expected:
        return {"passed": True, "label": "Team changes allowed", "detail": "N/A"}
    rows = conn.execute(
        "SELECT DISTINCT team_id FROM user_season_recaps ORDER BY season_year ASC"
    ).fetchall()
    teams = [r["team_id"] for r in rows]
    passed = len(teams) == 1
    return {
        "passed": passed,
        "label": "Must never change teams",
        "detail": f"✓ Only coached {teams[0]}" if passed else f"Coached {len(teams)} teams: {', '.join(teams)}",
    }


def _verify_max_seasons(save: SaveFile, conn: sqlite3.Connection, expected: int) -> dict:
    actual = save.meta.get("seasonYear", 9999)
    passed = actual <= int(expected)
    return {
        "passed": passed,
        "label": f"Complete within {expected} seasons",
        "detail": f"Currently season {actual}",
    }


def _verify_max_recruit_rating(save: SaveFile, conn: sqlite3.Connection, expected: int) -> dict:
    row = conn.execute(
        "SELECT player_name, overall_rating FROM user_coach_recruiting_classes "
        "WHERE overall_rating > ? ORDER BY overall_rating DESC LIMIT 5",
        (int(expected),)
    ).fetchall()
    passed = len(row) == 0
    violators = ", ".join(f"{r['player_name']} ({r['overall_rating']})" for r in row)
    return {
        "passed": passed,
        "label": f"No recruited player rated above {expected}",
        "detail": f"Violations: {violators}" if row else "None found ✓",
    }


# ------------------------------------------------------------------ career stats helper

def get_career_stats(save: SaveFile) -> dict:
    """Extract coach name, career record, and seasons played from history.db."""
    conn = save.history

    name_row = conn.execute(
        "SELECT coach_name FROM coach_career WHERE coach_id = ? ORDER BY season_year DESC LIMIT 1",
        (USER_COACH_ID,)
    ).fetchone()

    totals = conn.execute(
        "SELECT SUM(wins) as w, SUM(losses) as l FROM coach_career WHERE coach_id = ?",
        (USER_COACH_ID,)
    ).fetchone()
    wins = totals["w"] or 0
    losses = totals["l"] or 0

    seasons_row = conn.execute(
        "SELECT COUNT(*) as n FROM user_season_recaps"
    ).fetchone()

    return {
        "coach_name": name_row["coach_name"] if name_row else "Unknown",
        "career_wins": wins,
        "career_losses": losses,
        "win_pct": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0.0,
        "seasons_played": seasons_row["n"] if seasons_row else 0,
    }


# ------------------------------------------------------------------ registry

_VERIFIERS = {
    "start_team_id":              _verify_start_team_id,
    "max_start_prestige":         _verify_max_start_prestige,
    "must_win_championship":      _verify_must_win_championship,
    "min_championships":          _verify_min_championships,
    "must_make_tournament":       _verify_must_make_tournament,
    "min_tournament_appearances": _verify_min_tournament_appearances,
    "must_make_final_four":       _verify_must_make_final_four,
    "single_team_only":           _verify_single_team_only,
    "max_seasons":                _verify_max_seasons,
    "max_recruit_rating":         _verify_max_recruit_rating,
}


# ------------------------------------------------------------------ public API

def verify(save: SaveFile, conditions: dict) -> dict[str, dict]:
    """Run all conditions against the save using history.db. Returns results keyed by condition."""
    conn = save.history
    results = {}
    for key, value in conditions.items():
        fn = _VERIFIERS.get(key)
        if fn:
            results[key] = fn(save, conn, value)
        else:
            results[key] = {
                "passed": True,
                "label": key,
                "detail": "Not auto-verified (honor system)",
            }
    return results


def all_passed(results: dict) -> bool:
    return all(r.get("passed", False) for r in results.values())
