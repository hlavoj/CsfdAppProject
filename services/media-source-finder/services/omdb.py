import os
import httpx
from models.media_source import MovieInfo

OMDB_BASE = "http://www.omdbapi.com/"


async def get_movie_info(imdb_id: str) -> MovieInfo:
    api_key = os.getenv("OMDB_API_KEY")
    async with httpx.AsyncClient() as client:
        resp = await client.get(OMDB_BASE, params={"i": imdb_id, "apikey": api_key})
        resp.raise_for_status()
        data = resp.json()

    if data.get("Response") == "False":
        raise ValueError(f"OMDB error: {data.get('Error', 'Unknown error')}")

    title = data["Title"]
    return MovieInfo(
        title=title,
        original_title=title,
        year=data["Year"],
        source_id=data["imdbID"],
        source="omdb",
    )
