from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, redirect, abort, request
from services.cache import TTLCache
from services.tmdb import get_tmdb_id, get_tmdb_tv_id
from services.media_finder import search_streams, get_file_link
from services.formatter import format_streams

app = Flask(__name__)
_cache: TTLCache = TTLCache(ttl_seconds=600)

MANIFEST = {
    "id": "com.csfdapp.mediasourcefinder",
    "version": "1.1.0",
    "name": "MediaSource CZ",
    "description": "Czech streams from Webshare.cz, AI-ranked",
    "types": ["movie", "series"],
    "catalogs": [],
    "resources": [
        {"name": "stream", "types": ["movie", "series"], "idPrefixes": ["tt"]}
    ],
}


@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


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

    cached = _cache.get(video_id)
    if cached is not None:
        print(f"Cache hit: {video_id}")
        return jsonify({"streams": cached, "cacheMaxAge": 600})

    print(f"Resolving streams for {video_id} ({content_type}) ...")

    if content_type == "series":
        imdb_id, season, episode = _parse_series_id(video_id)
        print(f"  Series: {imdb_id} S{season:02d}E{episode:02d}" if season else f"  Series: {imdb_id} (no S/E)")
        tmdb_id = get_tmdb_tv_id(imdb_id)
        print(f"  TMDB TV ID: {tmdb_id}")
        results = search_streams(tmdb_id=tmdb_id, season=season, episode=episode) if tmdb_id else []
        streams = format_streams(results, season=season, episode=episode)
    else:
        imdb_id = video_id
        season, episode = None, None
        tmdb_id = get_tmdb_id(imdb_id)
        print(f"  TMDB ID: {tmdb_id}")
        results = search_streams(tmdb_id=tmdb_id) if tmdb_id else search_streams(imdb_id=imdb_id)
        streams = format_streams(results)

    print(f"  Got {len(results)} results from MediaFinder")
    _cache.set(video_id, streams)
    return jsonify({"streams": streams, "cacheMaxAge": 600})


@app.route("/stream-redirect/<ident>")
def stream_redirect(ident: str):
    url = get_file_link(ident)
    if not url:
        abort(404)
    return redirect(url, 302)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7000, debug=False)
