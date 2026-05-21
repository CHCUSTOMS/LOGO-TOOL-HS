"""
Recruiting pool domain logic for Campus Hoops save files.

Usage:
    from save_loader import SaveFile
    from recruiting import RecruitingPool

    save = SaveFile("saint_francis_pa_season_6_...")
    pool = RecruitingPool(save.get("season.recruitingPool"))

    pool.filter(position="SF", min_rating=85)
    pool.filter(min_stars=4, state="KY")
    pool.filter(is_transfer=True, min_rating=80)
    pool.top(20)
    pool.by_position()
    df = pool.to_df()
"""

from __future__ import annotations

import pandas as pd

POSITION_ABBR = {
    "pointGuard": "PG",
    "shootingGuard": "SG",
    "smallForward": "SF",
    "powerForward": "PF",
    "center": "C",
}

POSITION_FULL = {v: k for k, v in POSITION_ABBR.items()}

SKILL_FIELDS = [
    "insideShooting",
    "midRangeShooting",
    "outsideShooting",
    "handling",
    "passing",
    "rebounding",
    "perimeterDefense",
    "interiorDefense",
    "stealing",
    "blocking",
]

PERSONALITY_FIELDS = ["loyalty", "ambition", "playingTimeDesire", "homeAttachment"]


def fmt_height(inches: int | None) -> str:
    """Friendly display: 6'7\" """
    if inches is None:
        return ""
    return f"{inches // 12}'{inches % 12}\""




def abbr(position: str) -> str:
    return POSITION_ABBR.get(position, position)


class RecruitingPool:
    def __init__(self, raw: list[dict]):
        if not isinstance(raw, list):
            raise ValueError("Expected a list of recruit dicts")
        self._raw = raw

    def __len__(self) -> int:
        return len(self._raw)

    # ------------------------------------------------------------------ filtering

    def filter(
        self,
        *,
        position: str | list[str] | None = None,
        min_rating: int | None = None,
        max_rating: int | None = None,
        min_potential: int | None = None,
        max_potential: int | None = None,
        min_stars: int | None = None,
        max_stars: int | None = None,
        min_height: int | None = None,  # inches
        max_height: int | None = None,  # inches
        state: str | list[str] | None = None,
        recruit_type: str | None = None,  # 'highSchool' or 'transferPortal'
        is_transfer: bool | None = None,
        min_ranking: int | None = None,
        max_ranking: int | None = None,
        interested_in: str | None = None,  # school id in interestedSchools
        is_scouted: bool | None = None,
        is_late_bloomer: bool | None = None,
        is_generational: bool | None = None,
    ) -> list[dict]:
        """
        Filter the recruiting pool by any combination of criteria.
        Position accepts abbreviations (e.g. 'SF') or full names (e.g. 'smallForward').
        State accepts standard two-letter abbreviations (e.g. 'KY').

        Returns a plain list of matching recruit dicts.
        """
        # Normalize positions to full internal names
        if position is not None:
            if isinstance(position, str):
                position = [position]
            position = [POSITION_FULL.get(p, p) for p in position]

        if state is not None and isinstance(state, str):
            state = [state]

        results = []
        for r in self._raw:
            if position and r.get("position") not in position:
                continue
            if min_rating is not None and (r.get("rating") or 0) < min_rating:
                continue
            if max_rating is not None and (r.get("rating") or 0) > max_rating:
                continue
            if min_potential is not None and (r.get("potential") or 0) < min_potential:
                continue
            if max_potential is not None and (r.get("potential") or 0) > max_potential:
                continue
            if min_stars is not None and (r.get("generatedStarRating") or 0) < min_stars:
                continue
            if max_stars is not None and (r.get("generatedStarRating") or 0) > max_stars:
                continue
            if min_height is not None and (r.get("height") or 0) < min_height:
                continue
            if max_height is not None and (r.get("height") or 0) > max_height:
                continue
            if state and r.get("homeState") not in state:
                continue
            if recruit_type and r.get("type") != recruit_type:
                continue
            if is_transfer is not None and r.get("isTransferPortal") != is_transfer:
                continue
            if min_ranking is not None and (r.get("ranking") or 9999) > min_ranking:
                continue
            if max_ranking is not None and (r.get("ranking") or 9999) > max_ranking:
                continue
            if interested_in and interested_in not in (r.get("interestedSchools") or []):
                continue
            if is_scouted is not None and r.get("isScouted") != is_scouted:
                continue
            if is_late_bloomer is not None and r.get("isLateBloomer") != is_late_bloomer:
                continue
            if is_generational is not None and r.get("isGenerational") != is_generational:
                continue
            results.append(r)

        return results

    # ------------------------------------------------------------------ views

    def top(self, n: int = 25, sort_by: str = "rating") -> list[dict]:
        """Return the top N recruits sorted by a field (default: rating)."""
        sorted_recruits = sorted(self._raw, key=lambda r: r.get(sort_by) or 0, reverse=True)
        return sorted_recruits[:n]

    def by_position(self) -> dict[str, list[dict]]:
        """Group all recruits by position abbreviation."""
        groups: dict[str, list[dict]] = {pos: [] for pos in POSITION_ABBR.values()}
        groups["Other"] = []
        for r in self._raw:
            pos = abbr(r.get("position", ""))
            groups.get(pos, groups["Other"]).append(r)
        return groups

    # ------------------------------------------------------------------ dataframe

    def to_df(self, recruits: list[dict] | None = None) -> pd.DataFrame:
        """
        Convert recruits to a display-ready DataFrame.
        Pass a filtered list, or omit to use the full pool.
        """
        source = recruits if recruits is not None else self._raw
        rows = []
        for r in source:
            rows.append({
                "name": f"{r.get('firstName', '')} {r.get('lastName', '')}".strip(),
                "pos": abbr(r.get("position", "")),
                "stars": r.get("generatedStarRating"),
                "rating": r.get("rating"),
                "potential": r.get("potential"),
                "ranking": r.get("ranking"),
                "pos_rank": r.get("positionRanking"),
                "height": fmt_height(r.get("height")),
                "weight": r.get("weight"),
                "state": r.get("homeState"),
                "hometown": r.get("hometown"),
                "type": "Transfer" if r.get("isTransferPortal") else "HS",
                "scouted": r.get("isScouted"),
                "late_bloomer": r.get("isLateBloomer"),
                "generational": r.get("isGenerational"),
                "interest": r.get("interest"),
                "schools": len(r.get("interestedSchools") or []),
                "loyalty": r.get("loyalty"),
                "ambition": r.get("ambition"),
                "playing_time_desire": r.get("playingTimeDesire"),
                "home_attachment": r.get("homeAttachment"),
                "awards": r.get("hsAward"),
                "inside": r.get("insideShooting"),
                "mid": r.get("midRangeShooting"),
                "outside": r.get("outsideShooting"),
                "handling": r.get("handling"),
                "passing": r.get("passing"),
                "rebounding": r.get("rebounding"),
                "perim_def": r.get("perimeterDefense"),
                "int_def": r.get("interiorDefense"),
                "stealing": r.get("stealing"),
                "blocking": r.get("blocking"),
            })
        return pd.DataFrame(rows)
