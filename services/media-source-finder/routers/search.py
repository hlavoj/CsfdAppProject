import asyncio
import logging
import os
import re
import time
from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from models.media_source import SearchResponse, MovieInfo, StreamResult, FileDetail, AudioTrack
from services.omdb import get_movie_info as omdb_get_movie_info
from services.tmdb import get_movie_info as tmdb_get_movie_info, get_series_info as tmdb_get_series_info
from services.webshare import search_videos as ws_search, get_file_link as ws_file_link, get_file_info
from services import fastshare
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
    limit: int = Query(10, ge=1, le=20, description="Number of results to return"),
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

    # 3. Search — FastShare first, Webshare fallback
    fs_enabled = bool(os.getenv("FASTSHARE_USERNAME") and os.getenv("FASTSHARE_PASSWORD"))
    source = "webshare"
    t = time.perf_counter()

    raw_results: list[dict] = []
    if fs_enabled:
        try:
            fs_cz, fs_en = await asyncio.gather(
                fastshare.search_videos(cz_query, limit=20),
                fastshare.search_videos(en_query, limit=20),
            )
            raw_results = fs_cz + fs_en
            source = "fastshare"
            logger.info(f"  2. fastshare search x2 parallel              {_ms(t)}  ({len(fs_cz)}+{len(fs_en)} results)")
        except Exception as e:
            logger.info(f"  2. fastshare search FAILED ({e}), falling back to webshare")

    if not raw_results:
        try:
            ws_cz, ws_en = await asyncio.gather(
                ws_search(cz_query, limit=20),
                ws_search(en_query, limit=20),
            )
            raw_results = ws_cz + ws_en
            source = "webshare"
            logger.info(f"  2. webshare search x2 parallel               {_ms(t)}  ({len(ws_cz)}+{len(ws_en)} results)")
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=f"Webshare search failed: {e}")

    # 4. Deduplicate + filter
    # Primary dedup: by ident. Secondary: by (size, normalised name prefix) to catch
    # the same file uploaded multiple times under different idents.
    seen_idents: set[str] = set()
    seen_content: set[tuple] = set()
    candidates: list[dict] = []
    for item in raw_results:
        if item["ident"] in seen_idents or not _is_video(item):
            continue
        # Normalise name: strip extension, collapse separators, take first 40 chars
        norm = re.sub(r'\.\w{2,4}$', '', item["name"]).lower()
        norm = re.sub(r'[\s._\-]+', ' ', norm)[:40].strip()
        content_key = (item["size"], norm)
        if content_key in seen_content:
            continue
        seen_idents.add(item["ident"])
        seen_content.add(content_key)
        candidates.append(item)
    logger.info(f"  3. dedup+filter [{source}]                    {len(candidates)} unique candidates")

    if not candidates:
        logger.info(f"  TOTAL                                         {_ms(t_total)}  (0 results)")
        return SearchResponse(query=f"{cz_query} / {en_query}", movie=movie, results=[])

    # 5. Ranking (Python scorer, AI fallback when scores are ambiguous)
    from services.gemini import AI_CANDIDATE_LIMIT, AMBIGUITY_THRESHOLD
    t = time.perf_counter()
    try:
        ranked = await rank_results(movie, candidates, limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ranking failed: {e}")
    sent = min(len(candidates), AI_CANDIDATE_LIMIT)
    logger.info(f"  4. ranking (python+ai fallback, {len(candidates)}→{sent}→{limit})   {_ms(t)}")

    # 6. Parallel file_link + file_info for top N (Webshare only; FastShare idents are self-contained)
    t = time.perf_counter()
    candidate_map = {c["ident"]: c for c in candidates}

    if source == "fastshare":
        # FastShare: URL already in candidate, partial file_detail from search response
        details = [(c["url"], None) for c in [candidate_map[r["ident"]] for r in ranked]]
        logger.info(f"  5. fastshare — no file_link/file_info calls needed  {_ms(t)}")
    else:
        details = await asyncio.gather(*[_safe_file_details(r["ident"]) for r in ranked])
        logger.info(f"  5. file_link + file_info x{len(ranked)} parallel          {_ms(t)}")

    # 7. Assemble
    results: list[StreamResult] = []
    for ai_result, (url, info) in zip(ranked, details):
        if url is None:
            continue
        base = candidate_map[ai_result["ident"]]
        # FastShare: build partial FileDetail from resolution/duration parsed at search time
        if source == "fastshare" and info is None:
            info = {}
            if base.get("_width"):
                info["width"] = base["_width"]
                info["height"] = base["_height"]
            if base.get("_duration"):
                info["duration_seconds"] = base["_duration"]
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
        if ident.startswith("fs_"):
            url = await fastshare.get_file_link(ident)
        else:
            url = await ws_file_link(ident)
        if not url:
            raise HTTPException(status_code=404, detail="File not found")
        return {"url": url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
