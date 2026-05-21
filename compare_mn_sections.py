"""
compare_mn_sections.py
----------------------
Two reports in one run:

  1. SECTION MISMATCHES
     For every team matched between MaxPreps (mn.json) and minnesota-scores.net,
     flag cases where the section assignment disagrees.
     East/West subdivisions on mn-scores are ignored — only class + number compared.

  2. MN-SCORES ONLY
     Teams that appear on minnesota-scores.net but could not be matched to any
     MaxPreps team in mn.json.

Usage:
    python compare_mn_sections.py

Output:
    compare_mn_sections_mismatches.csv
    compare_mn_scores_only.csv
    (also printed to console)
"""

import csv
import json
import re
from pathlib import Path

import scrape_mn_scores
from supplements import norm_name

MN_JSON              = Path("hs_packs/mn.json")
OUT_MISMATCHES       = Path("compare_mn_sections_mismatches.csv")
OUT_MN_SCORES_ONLY   = Path("compare_mn_scores_only.csv")
SEASON               = "2025-2026"


# ── Section key parsing ────────────────────────────────────────────────────────

def _parse_maxpreps_conf(conf_id: str) -> tuple[str, int] | None:
    """'aaaa-section-7'  →  ('AAAA', 7)"""
    m = re.match(r"^(a{1,4})-section-(\d+)$", conf_id or "", re.IGNORECASE)
    return (m.group(1).upper(), int(m.group(2))) if m else None


def _parse_mn_scores_section(section_id: str) -> tuple[str, int] | None:
    """'mn_section_7aaaa' or 'mn_section_7aaaa_east'  →  ('AAAA', 7)"""
    m = re.match(r"^mn_section_(\d+)(a{1,4})(?:_\w+)?$", section_id or "")
    return (m.group(2).upper(), int(m.group(1))) if m else None


def _section_label(cls: str, num: int) -> str:
    return f"Section {num} {cls}"


# ── Name matching ──────────────────────────────────────────────────────────────

def _token_match(norm_a: str, norm_b: str) -> bool:
    """True if one name's tokens are a subset of the other's (≥2 tokens)."""
    ta, tb = set(norm_a.split()), set(norm_b.split())
    shorter = min(len(ta), len(tb))
    return shorter >= 2 and len(ta & tb) == shorter


def find_mp_team(mn_norm: str, mp_by_norm: dict) -> dict | None:
    """Look up a MaxPreps team by normalised mn-scores name."""
    if mn_norm in mp_by_norm:
        return mp_by_norm[mn_norm]
    for mp_norm, team in mp_by_norm.items():
        if _token_match(mn_norm, mp_norm):
            return team
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Load MaxPreps data from mn.json
    data = json.loads(MN_JSON.read_text(encoding="utf-8"))

    # Build conference id → name map
    conf_name = {c["id"]: c["name"] for c in data.get("conferences", [])}

    # Build {norm_name → team dict} for MaxPreps
    mp_by_norm: dict[str, dict] = {}
    for t in data.get("teams", []):
        key = norm_name(t["name"])
        mp_by_norm[key] = {
            "id":        t["id"],
            "name":      t["name"],
            "mascot":    t.get("mascot", ""),
            "conf_id":   t.get("conferenceId", ""),
            "conf_name": conf_name.get(t.get("conferenceId", ""), ""),
        }

    print(f"MaxPreps teams: {len(mp_by_norm)}")

    # Fetch mn-scores data
    print(f"Fetching mn-scores ({SEASON})…")
    mn_supp = scrape_mn_scores.fetch(SEASON)
    print(f"mn-scores teams: {len(mn_supp)}")

    mismatches   = []
    mn_only      = []

    for mn_norm, supp in mn_supp.items():
        mp = find_mp_team(mn_norm, mp_by_norm)

        if mp is None:
            mn_only.append({
                "mn_scores_name":    supp.section_name and mn_norm or mn_norm,
                "section":           supp.section_name or "",
                "section_id":        supp.section_id or "",
            })
            continue

        mp_key = _parse_maxpreps_conf(mp["conf_id"])
        ms_key = _parse_mn_scores_section(supp.section_id or "")

        if mp_key != ms_key:
            mp_label = _section_label(*mp_key) if mp_key else mp["conf_name"] or mp["conf_id"]
            ms_label = supp.section_name or supp.section_id or "?"
            mismatches.append({
                "id":          mp["id"],
                "name":        mp["name"],
                "mascot":      mp["mascot"],
                "mp_section":  mp_label,
                "ms_section":  ms_label,
            })

    # ── Print mismatches ───────────────────────────────────────────────────────
    print(f"\n{'─'*95}")
    print(f"SECTION MISMATCHES  ({len(mismatches)})")
    print(f"{'─'*95}")
    if mismatches:
        print(f"{'ID':<42} {'MP SECTION':<22} {'MN-SCORES SECTION'}")
        print(f"{'─'*95}")
        for r in mismatches:
            print(f"{r['id']:<42} {r['mp_section']:<22} {r['ms_section']}")
    else:
        print("  (none — all matched teams agree on section)")

    # ── Print mn-scores only ───────────────────────────────────────────────────
    print(f"\n{'─'*95}")
    print(f"TEAMS ON MN-SCORES NOT IN MAXPREPS  ({len(mn_only)})")
    print(f"{'─'*95}")
    if mn_only:
        print(f"{'NORMALISED NAME':<45} {'SECTION'}")
        print(f"{'─'*95}")
        for r in sorted(mn_only, key=lambda x: x["section"]):
            print(f"{r['mn_scores_name']:<45} {r['section']}")
    else:
        print("  (none)")

    # ── Save CSVs ──────────────────────────────────────────────────────────────
    with OUT_MISMATCHES.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id","name","mascot","mp_section","ms_section"])
        w.writeheader()
        w.writerows(mismatches)
    print(f"\nSaved → {OUT_MISMATCHES}  ({len(mismatches)} rows)")

    with OUT_MN_SCORES_ONLY.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["mn_scores_name","section","section_id"])
        w.writeheader()
        w.writerows(mn_only)
    print(f"Saved → {OUT_MN_SCORES_ONLY}  ({len(mn_only)} rows)")


if __name__ == "__main__":
    main()
