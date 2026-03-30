# Stremio Addon — MediaSource CZ

Stremio addon that bridges the MediaSourceFinder API to Stremio's stream protocol.
For each movie or TV series episode opened in Stremio (identified by IMDB ID), it returns
up to 5 ranked Czech video streams sourced from Webshare.cz.

## Directory Layout

```
stremio-addon/
├── main.py                  # Flask app — manifest, stream handler, redirect
├── requirements.txt         # flask, httpx, python-dotenv
├── .env                     # Secrets (gitignored)
├── .env.example
├── services/
│   ├── cache.py             # In-memory TTL cache (10 min default)
│   ├── tmdb.py              # IMDB ID -> TMDB movie/TV ID lookup
│   ├── media_finder.py      # MediaSourceFinder HTTP client
│   └── formatter.py         # Format results as Stremio stream objects
```

## Environment Variables

| Variable            | Description                                    |
|---------------------|------------------------------------------------|
| `MEDIA_FINDER_URL`  | Base URL of MediaSourceFinder service          |
| `TMDB_API_KEY`      | TMDB API key (for IMDB->TMDB ID resolution)    |
| `ADDON_URL`         | Public/local URL of this addon (for stream-redirect URLs) |

## Running

```bash
cd services/stremio-addon
pip install -r requirements.txt
python main.py
# runs on http://127.0.0.1:7000
```

## Install in Stremio

**Production (VPS):** `https://srv1475341.hstgr.cloud/manifest.json`

**Local dev:**
1. Run both MediaSourceFinder (port from .env) and this addon (port 7000)
2. Open Stremio -> Addons -> paste in URL bar:
   `http://127.0.0.1:7000/manifest.json`
3. Click Install
4. Open any movie or TV series -> Watch -> streams appear in picker

## API Routes

| Route | Description |
|-------|-------------|
| `GET /manifest.json` | Stremio addon manifest |
| `GET /stream/movie/{imdb_id}.json` | Stream list for a movie |
| `GET /stream/series/{imdb_id}:{season}:{episode}.json` | Stream list for a series episode |
| `GET /stream-redirect/{ident}` | Resolves fresh Webshare CDN URL and redirects (302) |

## Series ID Format

Stremio sends series episode IDs as `{imdb_id}:{season}:{episode}`, e.g.:
- `tt2442560:2:1` → Peaky Blinders S02E01
- `tt0903747:5:14` → Breaking Bad S05E14

The addon parses this format, looks up the TMDB TV show ID, and passes season/episode
to MediaSourceFinder which adjusts search queries and AI ranking accordingly.

## Flow — Movies

1. Stremio calls `/stream/movie/tt1375666.json`
2. Check in-memory cache (10 min TTL)
3. On miss: TMDB `find` lookup (`movie_results`) → get TMDB movie ID
4. Call MediaSourceFinder `GET /search?tmdb_id=...&limit=5`
5. Format results: quality label, audio tracks, size, AI match %
6. Return stream objects with `url = /stream-redirect/{ident}`

## Flow — Series

1. Stremio calls `/stream/series/tt2442560:2:1.json`
2. Check in-memory cache (10 min TTL)
3. On miss: TMDB `find` lookup (`tv_results`) → get TMDB TV show ID
4. Call MediaSourceFinder `GET /search?tmdb_id=...&season=2&episode=1&limit=5`
5. Format results: `S02E01 • 1080p • CZ` stream name, with episode label
6. Return stream objects

## Stream Object Format

```json
{
  "url": "http://127.0.0.1:7000/stream-redirect/{ident}",
  "name": "S02E01 • 1080p • CZ",
  "description": "Peaky.Blinders.S02E01.CZ.mkv\nCZ AC3 6ch • 0.7 GB • 100% match\nexact match",
  "behaviorHints": {
    "notWebReady": true,
    "videoSize": 748686391,
    "filename": "Peaky.Blinders.S02E01.CZ.mkv"
  }
}
```

For movies the name omits the episode label: `"1080p • CZ"`

## Caching

Results cached in memory per video_id (IMDB ID for movies, `imdb_id:season:episode` for series) for 10 minutes.
Stream URLs point to `/stream-redirect/` which always fetches a fresh CDN link — so cached results never produce expired URLs.

`cacheMaxAge: 0` is returned in every stream response so Stremio never caches the stream list on its side either.

## Known Behaviour & Hard-Won Lessons

### Black screen on second play (Stremio caching bug)
**Symptom:** First play works fine (even 4K). Second play of the same file shows black screen
with subtitles still changing — audio silent.

**Root cause:** Stremio caches the *final destination URL* (the Webshare CDN URL) after
the first 302 redirect. Webshare CDN tokens expire. On the second play Stremio reuses the
stale CDN URL directly, bypassing `/stream-redirect/`, so the token is expired → black screen.
Subtitles still run because they were fully buffered on the first play.

**Fix applied:**
- `cacheMaxAge: 0` — tells Stremio not to cache the stream list
- `notWebReady: true` — signals that the URL is not a plain HTTP byte stream,
  suppresses web Stremio's inline player (which doesn't handle redirects well)
- `/stream-redirect/{ident}` always fetches a fresh Webshare CDN URL on every play

**Why not proxy?** A `/stream-proxy/` endpoint (routing bytes through VPS) was tested and
worked around the caching issue, but the VPS plan (KVM 1, 4 GB RAM) has limited bandwidth.
Webshare CDN URLs are **not IP-restricted** — confirmed by fetching a CDN URL from a
different IP address (got HTTP 206). Direct redirect is safe and preferred.

### notWebReady: true and web Stremio
`notWebReady: true` causes web Stremio (web.stremio.com) to show an infinite spinner /
"still loading" state. This is expected — web Stremio cannot play non-web-ready streams.
Android TV and desktop Stremio apps handle them fine.

### AI model returns dict instead of array for single result
When the OpenRouter `llama-3.1-8b-instruct` model has only one candidate, it sometimes
returns a single JSON object `{"ident":"...","match_probability":85}` instead of an array.
The fix in `gemini.py` normalises this:
```python
if isinstance(ranked, dict):
    if "ident" in ranked:
        ranked = [ranked]
    else:
        ranked = next((v for v in ranked.values() if isinstance(v, list)), [])
```

### Series pre-filter scoring
Wrong SxxExx notation is penalised heavily so episodes don't cross-contaminate:
- SxxExx exact match in filename: **+10**
- Different SxxExx in filename: **−20** (e.g. S02E02 file when searching S03E02)
- No episode notation at all: **−5**
