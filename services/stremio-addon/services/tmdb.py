import os
from typing import Optional
import httpx

TMDB_BASE = "https://api.themoviedb.org/3"


def get_tmdb_id(imdb_id: str) -> Optional[str]:
    """Look up TMDB movie ID from an IMDB ID. Returns None on failure."""
    api_key = os.getenv("TMDB_API_KEY")
    try:
        with httpx.Client(timeout=8) as client:
            r = client.get(
                f"{TMDB_BASE}/find/{imdb_id}",
                params={"api_key": api_key, "external_source": "imdb_id"},
            )
            r.raise_for_status()
            results = r.json().get("movie_results", [])
            if results:
                return str(results[0]["id"])
    except Exception as e:
        print(f"TMDB movie lookup failed for {imdb_id}: {e}")
    return None


def get_tmdb_id_and_year(imdb_id: str) -> tuple[Optional[str], Optional[int]]:
    """Look up TMDB movie ID and release year from an IMDB ID."""
    api_key = os.getenv("TMDB_API_KEY")
    try:
        with httpx.Client(timeout=8) as client:
            r = client.get(
                f"{TMDB_BASE}/find/{imdb_id}",
                params={"api_key": api_key, "external_source": "imdb_id"},
            )
            r.raise_for_status()
            results = r.json().get("movie_results", [])
            if results:
                tmdb_id = str(results[0]["id"])
                year = None
                rd = results[0].get("release_date", "")
                if rd and len(rd) >= 4:
                    try:
                        year = int(rd[:4])
                    except ValueError:
                        pass
                return tmdb_id, year
    except Exception as e:
        print(f"TMDB movie lookup failed for {imdb_id}: {e}")
    return None, None


def get_tmdb_tv_id(imdb_id: str) -> Optional[str]:
    """Look up TMDB TV show ID from an IMDB ID. Returns None on failure."""
    api_key = os.getenv("TMDB_API_KEY")
    try:
        with httpx.Client(timeout=8) as client:
            r = client.get(
                f"{TMDB_BASE}/find/{imdb_id}",
                params={"api_key": api_key, "external_source": "imdb_id"},
            )
            r.raise_for_status()
            results = r.json().get("tv_results", [])
            if results:
                return str(results[0]["id"])
    except Exception as e:
        print(f"TMDB TV lookup failed for {imdb_id}: {e}")
    return None
