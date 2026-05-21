"""
Supabase database wrapper for Campus Hoops leaderboard.
Credentials are read from .streamlit/secrets.toml (local)
or Streamlit Cloud secrets (deployed).
"""

from __future__ import annotations
import streamlit as st
from supabase import create_client, Client


@st.cache_resource
def _client() -> Client:
    url = st.secrets.get("supabase_url", "")
    key = st.secrets.get("supabase_key", "")
    if not url or not key:
        raise RuntimeError(
            "Supabase credentials missing. "
            "Add supabase_url and supabase_key to .streamlit/secrets.toml"
        )
    return create_client(url, key)


# ------------------------------------------------------------------ challenges

def get_active_challenges() -> list[dict]:
    return (
        _client().table("challenges")
        .select("*")
        .eq("is_active", True)
        .order("created_at", desc=True)
        .execute()
        .data
    )


def create_challenge(created_by: str, name: str, description: str, conditions: dict) -> dict:
    return (
        _client().table("challenges")
        .insert({
            "created_by": created_by,
            "name": name,
            "description": description,
            "conditions": conditions,
        })
        .execute()
        .data[0]
    )


# ------------------------------------------------------------------ submissions

def get_leaderboard(challenge_id: str) -> list[dict]:
    """Return verified submissions for a challenge, sorted chronologically by submission date."""
    return (
        _client().table("submissions")
        .select("*")
        .eq("challenge_id", challenge_id)
        .eq("verified", True)
        .order("submitted_at", desc=False)
        .execute()
        .data
    )


def upsert_submission(
    username: str,
    challenge_id: str,
    team_name: str,
    team_id: str,
    seasons_played: int,
    play_time_seconds: int,
    verified: bool,
    conditions_met: dict,
    coach_name: str = "",
    career_wins: int = 0,
    career_losses: int = 0,
) -> dict:
    """Insert or overwrite the user's entry for this challenge."""
    return (
        _client().table("submissions")
        .upsert(
            {
                "username": username,
                "challenge_id": challenge_id,
                "team_name": team_name,
                "team_id": team_id,
                "seasons_played": seasons_played,
                "play_time_seconds": play_time_seconds,
                "verified": verified,
                "conditions_met": conditions_met,
                "coach_name": coach_name,
                "career_wins": career_wins,
                "career_losses": career_losses,
            },
            on_conflict="username,challenge_id",
        )
        .execute()
        .data[0]
    )
