from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from models.media_source import SearchResponse, MovieInfo, StreamResult
from services.omdb import get_movie_info
from services.webshare import search_videos, get_file_link

router = APIRouter()


@router.get("/search", response_model=SearchResponse)
async def search(
    imdb_id: Optional[str] = Query(None, description="IMDB ID, e.g. tt1375666"),
    csfd_id: Optional[str] = Query(None, description="CSFD ID (not yet implemented)"),
):
    if csfd_id is not None:
        raise HTTPException(status_code=501, detail="CSFD lookup is not yet implemented")

    if not imdb_id:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of: imdb_id or csfd_id",
        )

    # 1. Fetch movie metadata
    try:
        movie = await get_movie_info(imdb_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    query = f"{movie.title} {movie.year}"

    # 2. Search Webshare
    try:
        raw_results = await search_videos(query)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Webshare search failed: {e}")

    # 3. Resolve file links
    results: list[StreamResult] = []
    for item in raw_results:
        try:
            url = await get_file_link(item["ident"])
            results.append(StreamResult(
                ident=item["ident"],
                name=item["name"],
                size=item["size"],
                url=url,
            ))
        except RuntimeError:
            # Skip files we can't get a link for
            continue

    return SearchResponse(query=query, movie=movie, results=results)
