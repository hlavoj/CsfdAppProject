import os
import httpx
from models.media_source import MovieInfo

TMDB_BASE = "https://api.themoviedb.org/3"


async def get_movie_info(tmdb_id: str) -> MovieInfo:
    api_key = os.getenv("TMDB_API_KEY")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TMDB_BASE}/movie/{tmdb_id}",
            params={"api_key": api_key, "language": "cs"},
        )
        if resp.status_code == 404:
            raise ValueError(f"TMDB: no movie found for id={tmdb_id}")
        resp.raise_for_status()
        data = resp.json()

    genres = [g["name"] for g in data.get("genres") or []]
    runtime = data.get("runtime") or None
    year = (data.get("release_date") or "")[:4]
    title = data.get("title") or data.get("original_title")
    original_title = data.get("original_title", title)

    return MovieInfo(
        title=title,
        original_title=original_title,
        year=year,
        source_id=str(data["id"]),
        source="tmdb",
        runtime_minutes=runtime,
        genres=genres,
    )
