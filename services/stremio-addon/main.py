from dotenv import load_dotenv
load_dotenv()

from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, abort, request, Response
import requests as req_lib
from services.cache import TTLCache
from services.tmdb import get_tmdb_id, get_tmdb_id_and_year, get_tmdb_tv_id
from services.media_finder import search_streams, get_file_link
from services.formatter import format_streams, format_refresh_stream
from services.db import init_db, cache_get, cache_set, cache_increment_hit, cache_delete, get_catalogs, get_catalog_items

app = Flask(__name__)
_cache: TTLCache = TTLCache(ttl_seconds=600)

try:
    init_db()
except Exception as e:
    print(f"DB init failed (will retry on first request): {e}")

@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


@app.route("/manifest.json")
def manifest():
    catalogs = get_catalogs()
    return jsonify({
        "id": "com.csfdapp.mediasourcefinder",
        "version": "1.4.0",
        "name": "MediaSource CZ",
        "description": "Czech streams from Webshare.cz, AI-ranked",
        "types": ["movie", "series"],
        "catalogs": [
            {"type": c["type"], "id": c["id"], "name": c["name"]}
            for c in catalogs
        ],
        "resources": [
            {"name": "stream",  "types": ["movie", "series"], "idPrefixes": ["tt"]},
            {"name": "catalog", "types": ["movie", "series"]},
        ],
    })


@app.route("/catalog/<content_type>/<path:catalog_id>.json")
def catalog(content_type: str, catalog_id: str):
    # Strip trailing .json if path capture included it
    catalog_id = catalog_id.removesuffix(".json")

    cache_key = f"catalog:{catalog_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return jsonify({"metas": cached})

    imdb_ids = get_catalog_items(catalog_id)
    metas = [{"id": imdb_id, "type": content_type} for imdb_id in imdb_ids]

    _cache.set(cache_key, metas)
    return jsonify({"metas": metas})


def _parse_series_id(video_id: str):
    """Parse Stremio series ID format: tt1234567:season:episode -> (imdb_id, season, episode)"""
    parts = video_id.split(":")
    if len(parts) == 3:
        try:
            return parts[0], int(parts[1]), int(parts[2])
        except ValueError:
            pass
    return video_id, None, None


@app.route("/stream/<content_type>/<path:video_id>.json")
def stream(content_type: str, video_id: str):
    if content_type not in ("movie", "series"):
        return jsonify({"streams": [], "cacheMaxAge": 0})

    # L1: in-memory cache (10 min) — avoids hitting postgres on rapid re-opens
    cached_l1 = _cache.get(video_id)
    if cached_l1 is not None:
        print(f"L1 cache hit: {video_id}")
        return jsonify({"streams": cached_l1, "cacheMaxAge": 0})

    # L2: postgres cache
    cached_l2 = cache_get(video_id)
    if cached_l2 is not None:
        age_days = (datetime.now(timezone.utc) - cached_l2["cached_at"]).days
        print(f"L2 cache hit: {video_id} (age {age_days}d, hits {cached_l2['hit_count']})")
        cache_increment_hit(video_id)
        refresh = format_refresh_stream(video_id, cached_l2["cached_at"], cached_l2["hit_count"] + 1)
        streams = cached_l2["results"] + [refresh]
        _cache.set(video_id, streams)
        return jsonify({"streams": streams, "cacheMaxAge": 0})

    # Cache miss — fetch fresh
    print(f"Cache miss, fetching: {video_id} ({content_type}) ...")
    movie_year = None

    if content_type == "series":
        imdb_id, season, episode = _parse_series_id(video_id)
        print(f"  Series: {imdb_id} S{season:02d}E{episode:02d}" if season else f"  Series: {imdb_id} (no S/E)")
        tmdb_id = get_tmdb_tv_id(imdb_id)
        print(f"  TMDB TV ID: {tmdb_id}")
        results = search_streams(tmdb_id=tmdb_id, season=season, episode=episode) if tmdb_id else []
        streams = format_streams(results, season=season, episode=episode)
    else:
        imdb_id = video_id
        tmdb_id, movie_year = get_tmdb_id_and_year(imdb_id)
        print(f"  TMDB ID: {tmdb_id}, year: {movie_year}")
        results = search_streams(tmdb_id=tmdb_id) if tmdb_id else search_streams(imdb_id=imdb_id)
        streams = format_streams(results)

    print(f"  Got {len(results)} results from MediaFinder")

    # Store in postgres (only if we got results)
    if streams:
        cache_set(video_id, results, streams, movie_year)

    # Always append refresh button
    refresh = format_refresh_stream(video_id, datetime.now(timezone.utc), 0)
    streams_with_refresh = streams + [refresh]

    _cache.set(video_id, streams_with_refresh)
    return jsonify({"streams": streams_with_refresh, "cacheMaxAge": 0})


@app.route("/refresh/<path:video_id>")
def refresh_cache(video_id: str):
    deleted = cache_delete(video_id)
    _cache.delete(video_id)
    status = "deleted" if deleted else "not found (already fresh)"
    print(f"Cache refresh triggered for {video_id}: {status}")
    return (
        f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Cache cleared</title>
<style>body{{font-family:sans-serif;text-align:center;padding:60px;background:#1a1a2e;color:#eee}}
h1{{color:#e94560}}p{{color:#aaa}}</style></head>
<body>
<h1>🔄 Cache cleared</h1>
<p>Go back to Stremio and reopen the movie or episode.<br>Fresh results will be fetched.</p>
<p style="font-size:0.8em;color:#555">{video_id} — {status}</p>
</body></html>""",
        200,
        {"Content-Type": "text/html"},
    )


@app.route("/stream-redirect/<ident>")
def stream_redirect(ident: str):
    url = get_file_link(ident)
    if not url:
        abort(404)
    return redirect(url, 302)


@app.route("/stream-proxy/<ident>")
def stream_proxy(ident: str):
    """
    Proxy video bytes from Webshare CDN through this server.
    - Fixes IP-restricted Webshare URLs (CDN URL is always fetched by VPS IP)
    - Upgrades HTTP Webshare streams to HTTPS for web browsers
    - Supports Range requests so seeking works
    """
    url = get_file_link(ident)
    if not url:
        abort(404)

    # Forward Range header from player (needed for seeking)
    headers = {}
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    upstream = req_lib.get(url, headers=headers, stream=True, timeout=10)

    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": upstream.headers.get("Content-Type", "video/x-matroska"),
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            response_headers[h] = upstream.headers[h]

    def generate():
        for chunk in upstream.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(generate(), status=upstream.status_code, headers=response_headers)


if __name__ == "__main__":
    import os
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "7000"))
    app.run(host=host, port=port, debug=False)
