import os
from typing import Optional
import httpx


def _base() -> str:
    return os.getenv("MEDIA_FINDER_URL", "http://127.0.0.1:8080")


def search_streams(
    tmdb_id: Optional[str] = None,
    imdb_id: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    limit: int = 10,
) -> list[dict]:
    params: dict = {"limit": limit}
    if tmdb_id:
        params["tmdb_id"] = tmdb_id
    elif imdb_id:
        params["imdb_id"] = imdb_id
    else:
        return []
    if season is not None:
        params["season"] = season
    if episode is not None:
        params["episode"] = episode
    try:
        with httpx.Client(timeout=60) as client:
            r = client.get(f"{_base()}/search", params=params)
            r.raise_for_status()
            return r.json().get("results", [])
    except Exception as e:
        print(f"MediaFinder search error: {e}")
        return []


def get_file_link(ident: str) -> Optional[str]:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{_base()}/file-link/{ident}")
            r.raise_for_status()
            return r.json().get("url")
    except Exception as e:
        print(f"MediaFinder file-link error: {e}")
        return None
