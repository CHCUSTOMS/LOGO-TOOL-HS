"""
Shared data model and merge logic for supplemental team data sources.

Each adapter (scrape_mn_scores, scrape_mshsl, etc.) returns:
    dict[normalised_name → TeamSupplement]

The merge layer combines multiple supplements before they're applied to
the MaxPreps skeleton in scrape_hs_leagues.py.
"""

import re
from dataclasses import dataclass


@dataclass
class TeamSupplement:
    """Normalised team data from a secondary source."""

    # Win-loss records — None means the source didn't provide this field.
    ovr_wins:    int | None = None   # overall season W
    ovr_losses:  int | None = None   # overall season L
    # conf_wins/losses = within-group record.
    # record_type tells you which group: "conference" (same as MaxPreps conf),
    # "section" (MSHSL playoff section), or "unknown".
    conf_wins:   int | None = None
    conf_losses: int | None = None
    record_type: str = "unknown"     # "conference" | "section" | "unknown"

    # Source's own quality metric (e.g. QRF from mn-scores).
    # Scale is source-specific; callers normalise before use.
    rating:      float | None = None

    # Logo URL from this source (fully-qualified https://).
    logo_url:    str | None = None

    # MSHSL section placement (populated by mn-scores adapter).
    section_id:   str | None = None   # canonical key, e.g. "mn_section_3a"
    section_name: str | None = None   # display, e.g.  "Section 3A"

    source: str = ""


# ── Record helpers ─────────────────────────────────────────────────────────────

def better_record(
    a_w: int | None, a_l: int | None,
    b_w: int | None, b_l: int | None,
) -> tuple[int | None, int | None]:
    """Return the (w, l) pair with more total games; prefer b on tie."""
    a_total = (a_w or 0) + (a_l or 0)
    b_total = (b_w or 0) + (b_l or 0)
    return (b_w, b_l) if b_total >= a_total else (a_w, a_l)


def merge_supplement(base: TeamSupplement, overlay: TeamSupplement) -> TeamSupplement:
    """
    Merge two supplements:
      - Records: take whichever has more total games.
      - record_type: overlay wins if set.
      - Other scalar fields: overlay wins if not None/empty.
    """
    ovr_w,  ovr_l  = better_record(base.ovr_wins,  base.ovr_losses,
                                    overlay.ovr_wins,  overlay.ovr_losses)
    conf_w, conf_l = better_record(base.conf_wins, base.conf_losses,
                                    overlay.conf_wins, overlay.conf_losses)
    return TeamSupplement(
        ovr_wins=ovr_w,
        ovr_losses=ovr_l,
        conf_wins=conf_w,
        conf_losses=conf_l,
        record_type=overlay.record_type if overlay.record_type != "unknown" else base.record_type,
        rating=overlay.rating if overlay.rating is not None else base.rating,
        logo_url=overlay.logo_url or base.logo_url,
        section_id=overlay.section_id or base.section_id,
        section_name=overlay.section_name or base.section_name,
        source="+".join(filter(None, [base.source, overlay.source])),
    )


# ── Name normalisation (shared across all adapters) ────────────────────────────

_ST = re.compile(r"\bst\.?\s+", re.I)          # "St. " / "St " → "saint "
_STRIP = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")


def norm_name(raw: str) -> str:
    """
    Normalise a school name for cross-source matching.
    - Expands "St." → "saint"
    - Strips punctuation
    - Lowercases and collapses whitespace
    """
    s = raw.strip()
    s = _ST.sub("saint ", s)
    s = _STRIP.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()
