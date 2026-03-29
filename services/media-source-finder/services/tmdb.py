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
        media_type="movie",
    )


async def get_series_info(tmdb_id: str, season: int, episode: int) -> MovieInfo:
    """Fetch TV show + episode metadata from TMDB."""
    api_key = os.getenv("TMDB_API_KEY")
    async with httpx.AsyncClient() as client:
        show_resp, ep_resp = await _fetch_show_and_episode(client, api_key, tmdb_id, season, episode)

    if show_resp.status_code == 404:
        raise ValueError(f"TMDB: no TV show found for id={tmdb_id}")
    show_resp.raise_for_status()
    ep_resp.raise_for_status()

    show = show_resp.json()
    ep = ep_resp.json()

    genres = [g["name"] for g in show.get("genres") or []]
    year = (show.get("first_air_date") or "")[:4]
    title = show.get("name") or show.get("original_name")
    original_title = show.get("original_name", title)
    episode_title = ep.get("name")
    runtime = ep.get("runtime") or None

    return MovieInfo(
        title=title,
        original_title=original_title,
        year=year,
        source_id=str(show["id"]),
        source="tmdb",
        runtime_minutes=runtime,
        genres=genres,
        media_type="series",
        season=season,
        episode=episode,
        episode_title=episode_title,
    )


async def _fetch_show_and_episode(client, api_key, tmdb_id, season, episode):
    import asyncio
    show_coro = client.get(
        f"{TMDB_BASE}/tv/{tmdb_id}",
        params={"api_key": api_key, "language": "cs"},
    )
    ep_coro = client.get(
        f"{TMDB_BASE}/tv/{tmdb_id}/season/{season}/episode/{episode}",
        params={"api_key": api_key, "language": "cs"},
    )
    return await asyncio.gather(show_coro, ep_coro)
