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

| Parameter  | Type   | Required | Description                          |
|------------|--------|----------|--------------------------------------|
| `imdb_id`  | string | one of   | IMDB ID, e.g. `tt1375666`            |
| `tmdb_id`  | string | one of   | TMDB ID, e.g. `27205`                |
| `csfd_id`  | string | one of   | CSFD ID — returns **501** for now    |
| `limit`    | int    | no       | Results to return, default `5`, max `20` |

Exactly one of `imdb_id`, `tmdb_id`, `csfd_id` must be provided.

#### Examples

```bash
curl "http://localhost:8080/search?tmdb_id=27205&limit=3"
curl "http://localhost:8080/search?imdb_id=tt1375666&limit=5"
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

1. **Validate** — exactly one of `imdb_id` / `tmdb_id` / `csfd_id`
2. **Movie metadata**
   - `imdb_id` → `omdb.py` → OMDB API → English title + year
   - `tmdb_id` → `tmdb.py` → TMDB API (`language=cs`) → Czech title, original title, year, runtime, genres
3. **Webshare auth** (`webshare.py`) — WST token cached in memory, re-auth on FATAL response
   - `POST /api/salt/` → SHA1(md5crypt(password, salt)) → `POST /api/login/` → `<token>`
4. **Dual parallel search** — two simultaneous `POST /api/search/` calls:
   - `"{czech_title} {year} CZ"` → 20 results
   - `"{original_title} {year} CZ"` → 20 results
5. **Dedup + filter** — deduplicate by `ident`, drop non-video extensions → max 40 candidates
6. **Pre-filter** (`gemini.py`) — heuristic scoring (title match, year, CZ label, quality, votes) → top 15
7. **AI ranking** (`gemini.py`) — OpenRouter `llama-3.1-8b-instruct` with `json_object` response mode
   - Returns all 15 ranked with `match_probability` (0–100) and `reasoning`
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
