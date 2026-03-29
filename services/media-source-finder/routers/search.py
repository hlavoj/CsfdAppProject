import asyncio
import logging
import time
from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from models.media_source import SearchResponse, MovieInfo, StreamResult, FileDetail, AudioTrack
from services.omdb import get_movie_info as omdb_get_movie_info
from services.tmdb import get_movie_info as tmdb_get_movie_info, get_series_info as tmdb_get_series_info
from services.webshare import search_videos, get_file_link, get_file_info
from services.gemini import rank_results

router = APIRouter()
logger = logging.getLogger("timing")

VIDEO_EXTENSIONS = {"mkv", "mp4", "avi", "mov", "m4v", "wmv", "ts", "mpg", "mpeg"}


def _ms(start: float) -> str:
    return f"{(time.perf_counter() - start) * 1000:7.0f} ms"


async def _resolve_movie(
    imdb_id: Optional[str],
    tmdb_id: Optional[str],
    csfd_id: Optional[str],
    season: Optional[int],
    episode: Optional[int],
) -> MovieInfo:
    provided = sum(x is not None for x in [imdb_id, tmdb_id, csfd_id])
    if provided != 1:
        raise HTTPException(status_code=400, detail="Provide exactly one of: imdb_id, tmdb_id, csfd_id")
    if csfd_id is not None:
        raise HTTPException(status_code=501, detail="CSFD lookup is not yet implemented")
    try:
        if imdb_id:
            return await omdb_get_movie_info(imdb_id)
        if tmdb_id:
            if season is not None and episode is not None:
                return await tmdb_get_series_info(tmdb_id, season, episode)
            return await tmdb_get_movie_info(tmdb_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _is_video(item: dict) -> bool:
    ext = item["name"].rsplit(".", 1)[-1].lower() if "." in item["name"] else ""
    return ext in VIDEO_EXTENSIONS


async def _safe_file_details(ident: str) -> tuple[Optional[str], Optional[dict]]:
    url_result, info_result = await asyncio.gather(
        get_file_link(ident),
        get_file_info(ident),
        return_exceptions=True,
    )
    url = url_result if not isinstance(url_result, Exception) else None
    info = info_result if not isinstance(info_result, Exception) else None
    return url, info


def _build_file_detail(info: Optional[dict]) -> Optional[FileDetail]:
    if not info:
        return None
    return FileDetail(
        video_codec=info.get("video_codec"),
        width=info.get("width"),
        height=info.get("height"),
        fps=info.get("fps"),
        bitrate_kbps=info.get("bitrate_kbps"),
        duration_seconds=info.get("duration_seconds"),
        audio_tracks=[
            AudioTrack(
                format=t.get("format"),
                channels=t.get("channels"),
                language=t.get("language"),
            )
            for t in (info.get("audio_tracks") or [])
        ],
    )


@router.get("/search", response_model=SearchResponse)
async def search(
    imdb_id: Optional[str] = Query(None, description="IMDB ID, e.g. tt1375666"),
    tmdb_id: Optional[str] = Query(None, description="TMDB ID, e.g. 27205"),
    csfd_id: Optional[str] = Query(None, description="CSFD ID (not yet implemented)"),
    season: Optional[int] = Query(None, description="Season number (for series)"),
    episode: Optional[int] = Query(None, description="Episode number (for series)"),
    limit: int = Query(5, ge=1, le=20, description="Number of results to return"),
):
    t_total = time.perf_counter()
    query_id = imdb_id or tmdb_id or csfd_id
    logger.info(f"─── /search  id={query_id}  s={season}  e={episode}  limit={limit} ───────────────────────────────")

    # 1. Movie/episode metadata
    t = time.perf_counter()
    movie = await _resolve_movie(imdb_id, tmdb_id, csfd_id, season, episode)
    logger.info(f"  1. metadata ({movie.source}, {movie.media_type})                  {_ms(t)}")

    # 2. Build search queries
    if movie.media_type == "series" and movie.season is not None:
        sxex = f"S{movie.season:02d}E{movie.episode:02d}"
        cz_query = f"{movie.title} {sxex} CZ"
        en_query = f"{movie.original_title} {sxex}"
    else:
        cz_query = f"{movie.title} {movie.year} CZ"
        en_query = f"{movie.original_title} {movie.year} CZ"

    # 3. Two parallel Webshare searches
    t = time.perf_counter()
    try:
        results_cz, results_en = await asyncio.gather(
            search_videos(cz_query, limit=20),
            search_videos(en_query, limit=20),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Webshare search failed: {e}")
    logger.info(f"  2. webshare search x2 parallel                {_ms(t)}  ({len(results_cz)}+{len(results_en)} results)")

    # 4. Deduplicate + filter
    seen: set[str] = set()
    candidates: list[dict] = []
    for item in results_cz + results_en:
        if item["ident"] not in seen and _is_video(item):
            seen.add(item["ident"])
            candidates.append(item)
    logger.info(f"  3. dedup+filter                               {len(candidates)} unique candidates")

    if not candidates:
        logger.info(f"  TOTAL                                         {_ms(t_total)}  (0 results)")
        return SearchResponse(query=f"{cz_query} / {en_query}", movie=movie, results=[])

    # 5. AI ranking (pre-filter happens inside rank_results)
    from services.gemini import AI_CANDIDATE_LIMIT
    t = time.perf_counter()
    try:
        ranked = await rank_results(movie, candidates, limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI ranking failed: {e}")
    sent = min(len(candidates), AI_CANDIDATE_LIMIT)
    logger.info(f"  4. gemini ranking (prefilter {len(candidates)}→{sent}, top {limit})  {_ms(t)}")

    # 6. Parallel file_link + file_info for top N
    t = time.perf_counter()
    candidate_map = {c["ident"]: c for c in candidates}
    details = await asyncio.gather(*[_safe_file_details(r["ident"]) for r in ranked])
    logger.info(f"  5. file_link + file_info x{len(ranked)} parallel          {_ms(t)}")

    # 7. Assemble
    results: list[StreamResult] = []
    for ai_result, (url, info) in zip(ranked, details):
        if url is None:
            continue
        base = candidate_map[ai_result["ident"]]
        results.append(StreamResult(
            ident=ai_result["ident"],
            name=base["name"],
            size=base["size"],
            url=url,
            positive_votes=base["positive_votes"],
            negative_votes=base["negative_votes"],
            match_probability=int(ai_result.get("match_probability", 0)),
            ai_reasoning=ai_result.get("reasoning", ""),
            file_detail=_build_file_detail(info),
        ))

    logger.info(f"  TOTAL                                         {_ms(t_total)}  ({len(results)} results returned)")

    return SearchResponse(query=f"{cz_query} / {en_query}", movie=movie, results=results)


@router.get("/file-link/{ident}")
async def file_link_endpoint(ident: str):
    try:
        url = await get_file_link(ident)
        return {"url": url}
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
