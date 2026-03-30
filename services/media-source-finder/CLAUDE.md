# MediaSourceFinder — Service Documentation

FastAPI service that accepts a movie ID, fetches metadata from OMDB or TMDB,
searches Webshare.cz for matching Czech video files, ranks results via AI,
and returns direct stream URLs with full file metadata.

## Directory Layout

```
media-source-finder/
├── main.py                        # FastAPI app, lifespan (Webshare pre-auth), timing logger setup
├── requirements.txt               # fastapi, uvicorn, httpx, python-dotenv, pydantic, passlib
├── .env                           # Secrets — gitignored, never committed
├── .env.example                   # Template for .env
├── logs/
│   └── timing.log                 # Per-request step timing (gitignored, auto-created)
├── routers/
│   └── search.py                  # GET /search — orchestrates full flow
├── services/
│   ├── omdb.py                    # OMDB API client (lookup by IMDB ID)
│   ├── tmdb.py                    # TMDB API client (lookup by TMDB ID, Czech titles)
│   ├── webshare.py                # Webshare.cz client (auth, search, file_link, file_info)
│   └── gemini.py                  # AI ranking via OpenRouter (provider-agnostic interface)
└── models/
    └── media_source.py            # Pydantic request/response models
```

## Environment Variables (.env)

| Variable              | Description                               |
|-----------------------|-------------------------------------------|
| `WEBSHARE_USERNAME`   | Webshare.cz username                      |
| `WEBSHARE_PASSWORD`   | Webshare.cz password                      |
| `OMDB_API_KEY`        | OMDB API key (omdbapi.com)                |
| `TMDB_API_KEY`        | TMDB API key (themoviedb.org)             |
| `GEMINI_API_KEY`      | Google Gemini API key (unused, kept for reference) |
| `OPENROUTER_API_KEY`  | OpenRouter API key (active AI provider)   |

## Running

```bash
cd services/media-source-finder
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

Avoid port 8000 on Windows — it is commonly reserved by the OS.

## API

### `GET /search`

| Parameter  | Type   | Required | Description                                        |
|------------|--------|----------|----------------------------------------------------|
| `imdb_id`  | string | one of   | IMDB ID, e.g. `tt1375666`                          |
| `tmdb_id`  | string | one of   | TMDB ID (movie or TV show), e.g. `27205`           |
| `csfd_id`  | string | one of   | CSFD ID — returns **501** for now                  |
| `season`   | int    | no       | Season number — required for series with `tmdb_id` |
| `episode`  | int    | no       | Episode number — required for series with `tmdb_id`|
| `limit`    | int    | no       | Results to return, default `5`, max `20`           |

Exactly one of `imdb_id`, `tmdb_id`, `csfd_id` must be provided.

For TV series pass `tmdb_id` (TV show ID) + `season` + `episode`.

#### Examples

```bash
# Movies
curl "http://localhost:8080/search?tmdb_id=27205&limit=3"
curl "http://localhost:8080/search?imdb_id=tt1375666&limit=5"

# TV Series episodes
curl "http://localhost:8080/search?tmdb_id=1396&season=1&episode=1&limit=3"   # Breaking Bad S01E01
curl "http://localhost:8080/search?tmdb_id=60574&season=2&episode=1&limit=3"  # Peaky Blinders S02E01
```

#### Response

```json
{
  "query": "Počátek 2010 CZ / Inception 2010 CZ",
  "movie": {
    "title": "Počátek",
    "original_title": "Inception",
    "year": "2010",
    "source_id": "27205",
    "source": "tmdb",
    "runtime_minutes": 148,
    "genres": ["Akční", "Sci-Fi"]
  },
  "results": [
    {
      "ident": "QWeGqB9oNA",
      "name": "Počátek   Inception (2010)(CZ ENG)[1080pHD][Remux].mkv",
      "size": 35611706730,
      "url": "http://vip.6.dl.wsfiles.cz/...",
      "positive_votes": 1,
      "negative_votes": 0,
      "match_probability": 98,
      "ai_reasoning": "Perfect title/year, CZ+ENG audio, 1080p Remux, community vote.",
      "file_detail": {
        "video_codec": "VC1",
        "width": 1920,
        "height": 1080,
        "fps": 23.976,
        "bitrate_kbps": 32053,
        "duration_seconds": 8889,
        "audio_tracks": [
          { "format": "AC3", "channels": 6, "language": "CZE" },
          { "format": "DTS", "channels": 6, "language": "ENG" }
        ]
      }
    }
  ]
}
```

## Service Flow

```
GET /search?tmdb_id=27205&limit=5
        │
        ▼
┌──────────────────────────────────────────────────────┐
│ 1. Validate — exactly one of imdb_id/tmdb_id/csfd_id │
└──────────────────┬───────────────────────────────────┘
                   │
          ┌────────┴────────┐
          │                 │
     imdb_id            tmdb_id (+ season/episode for series)
          │                 │
          ▼                 ▼
      OMDB API          TMDB API
    (EN title+year)  (CZ+EN titles, runtime,
                      genres, episode title)
          │                 │
          └────────┬────────┘
                   │  MovieInfo built
                   ▼
┌──────────────────────────────────────────────────────┐
│ 2. Webshare auth (cached WST token)                  │
│    POST /api/salt/ → md5crypt+sha1 → POST /api/login/│
└──────────────────┬───────────────────────────────────┘
                   │
        ┌──────────┴──────────┐  parallel
        ▼                     ▼
  Webshare search         Webshare search
  "{title_cz} S02E01 CZ"  "{title_en} S02E01"
  POST /api/search/        POST /api/search/
        │                     │
        └──────────┬──────────┘
                   │  merge + dedup by ident
                   │  drop non-video extensions
                   │  up to 40 candidates
                   ▼
┌──────────────────────────────────────────────────────┐
│ 3. Heuristic pre-filter → top 15                     │
│    title_en +6, title_cz +4, year +3, CZ +3          │
│    SxxExx match +10, wrong episode -20, no ep -5     │
└──────────────────┬───────────────────────────────────┘
                   │
                   ▼
        OpenRouter API (llama-3.1-8b)
        rank_results() → match_probability 0-100%
                   │
                   ▼
┌──────────────────────────────────────────────────────┐
│ 4. Parallel fetch for top N results                  │
│    POST /api/file_link/ → CDN URL                    │
│    POST /api/file_info/ → codec, resolution, audio   │
└──────────────────┬───────────────────────────────────┘
                   │
                   ▼
           SearchResponse JSON
```

**Steps in plain text:**
1. **Validate** — exactly one of `imdb_id` / `tmdb_id` / `csfd_id`
2. **Movie/episode metadata**
   - `imdb_id` → `omdb.py` → OMDB API → English title + year
   - `tmdb_id` (movie) → `tmdb.py` `get_movie_info()` → Czech title, original title, year, runtime, genres
   - `tmdb_id` + `season` + `episode` → `tmdb.py` `get_series_info()` → TV show + episode metadata
3. **Webshare auth** (`webshare.py`) — WST token cached in memory, re-auth on FATAL response
   - `POST /api/salt/` → SHA1(md5crypt(password, salt)) → `POST /api/login/` → `<token>`
4. **Dual parallel search** — two simultaneous `POST /api/search/` calls:
   - Movies: `"{czech_title} {year} CZ"` + `"{original_title} {year} CZ"`
   - Series: `"{show_title_cz} S{s:02d}E{e:02d} CZ"` + `"{show_title_en} S{s:02d}E{e:02d}"`
5. **Dedup + filter** — deduplicate by `ident`, drop non-video extensions → max 40 candidates
6. **Pre-filter** (`gemini.py`) — heuristic scoring → top 15:
   - Movies: title_en(+6), title_cz(+4), year(+3), CZ label(+3), dabing(+2), quality(+1)
   - Series: adds SxxExx exact match(+10), wrong episode penalty(-20), no-episode-notation(-5)
7. **AI ranking** (`gemini.py`) — OpenRouter `llama-3.1-8b-instruct` with `json_object` response mode
   - Returns top N ranked with `match_probability` (0–100) and `reasoning`
   - Series prompt emphasizes SxxExx as the most critical matching signal
8. **Parallel fetch** — for top N results only: `POST /api/file_link/` + `POST /api/file_info/` in parallel
9. **Return** assembled `SearchResponse`

## Timing (typical, cached Webshare token)

| Step | Time |
|------|------|
| Movie metadata | ~200–900 ms |
| Webshare search ×2 parallel | ~500–2000 ms |
| AI ranking (15 candidates) | ~500–4000 ms |
| file_link + file_info ×N parallel | ~1000–4000 ms |
| **Total** | **~5–10 s** |

Timing log written to `logs/timing.log` on every request.

## Webshare Password Hashing

```python
from passlib.hash import md5_crypt
import hashlib
mc = md5_crypt.using(salt=salt).hash(password)   # Unix md5crypt
wsh = hashlib.sha1(mc.encode()).hexdigest()        # SHA1 of result
```

Plain `MD5(password+salt)` does NOT work — Webshare requires the full Unix md5crypt algorithm.

## AI Ranking (gemini.py)

The file is named `gemini.py` for historical reasons but currently uses **OpenRouter**.

- **Model:** `meta-llama/llama-3.1-8b-instruct` via `https://openrouter.ai/api/v1`
- **Interface:** `rank_results(movie, candidates, limit) -> list[dict]`
- To swap provider/model: change `OPENROUTER_BASE` and `MODEL` constants in `gemini.py`
- Pre-filter heuristic scores: title match (+6/+4), year (+3), CZ label (+3), dubbing (+2), resolution (+1), votes

## Webshare file_info Fields

| Field | Notes |
|-------|-------|
| `format` / `video` | Video codec (HEVC, H264, VC1, AVC…) |
| `width` × `height` | Resolution |
| `fps` | Frame rate |
| `bitrate` | Overall bitrate in bps |
| `length` | Duration in seconds |
| `audio/stream[]` | Per-track: format, channels, language (ENG/CZE/…) |
| `positive_votes` / `negative_votes` | Community trust signal |
| `stripe` / `stripe_count` | Sprite sheet URL (10 frames) for filmstrip preview |
| `img` | Single thumbnail URL |

## Error Codes

| Code | Meaning                                       |
|------|-----------------------------------------------|
| 400  | Not exactly one ID param provided             |
| 404  | Movie not found in OMDB/TMDB                  |
| 501  | `csfd_id` lookup not yet implemented          |
| 502  | Webshare search/auth failure or AI failure    |
