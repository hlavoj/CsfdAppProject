# MediaSourceFinder — Service Documentation

FastAPI service that accepts a movie ID, fetches metadata from OMDB or TMDB,
searches Webshare.cz for matching Czech video files, ranks results with a
Python scorer (AI fallback for ambiguous cases), and returns stream URLs with
full file metadata.

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
| `limit`    | int    | no       | Results to return, default `10`, max `20`          |

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
GET /search?tmdb_id=27205&limit=10
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
                   │  dedup by ident + by (size, normalised name)
                   │  drop non-video extensions
                   ▼
┌──────────────────────────────────────────────────────┐
│ 3. Python scorer → top 15 sent to AI if ambiguous    │
│    (see Python Scorer section below)                 │
│    score spread < 15 across top N → AI fallback      │
└──────────────────┬───────────────────────────────────┘
                   │
                   ├─ scores spread ≥ 15 → Python result, done (0 ms)
                   │
                   └─ scores spread < 15 → OpenRouter gemini-2.0-flash
                                           re-ranks top 15 candidates
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
5. **Dedup + filter** — two layers:
   - By `ident` (primary)
   - By `(size, normalised_name[:40])` (secondary) — catches the same file uploaded multiple times under different idents
   - Drop non-video extensions
6. **Python scorer + optional AI fallback** (`gemini.py`) — see section below
7. **Parallel fetch** — for top N results only: `POST /api/file_link/` + `POST /api/file_info/` in parallel
8. **Return** assembled `SearchResponse`

## Timing (typical, cached Webshare token)

| Step | Time |
|------|------|
| Movie metadata | ~200–900 ms |
| Webshare search ×2 parallel | ~500–2000 ms |
| Python scorer | ~0–1 ms |
| AI fallback (only when ambiguous) | ~500–2000 ms |
| file_link + file_info ×N parallel | ~700–2000 ms |
| **Total (Python-only)** | **~1.5–3 s** |
| **Total (with AI fallback)** | **~3–5 s** |

Timing log written to `logs/timing.log` on every request.

## Webshare Password Hashing

```python
from passlib.hash import md5_crypt
import hashlib
mc = md5_crypt.using(salt=salt).hash(password)   # Unix md5crypt
wsh = hashlib.sha1(mc.encode()).hexdigest()        # SHA1 of result
```

Plain `MD5(password+salt)` does NOT work — Webshare requires the full Unix md5crypt algorithm.

## Python Scorer (gemini.py)

Primary ranking — fast, deterministic, zero API cost.
All filename matching uses **normalised names** (separators `._-` replaced with spaces)
so `iron-man-3-2013.mkv` matches title `iron man 3` correctly.

### Scoring weights

| Signal | Points |
|--------|--------|
| Title EN exact match in filename | +15 |
| Title CZ exact match in filename | +12 |
| **No title match at all** | **−30** (filters wrong movies from fuzzy Webshare results) |
| Sequel number mismatch (Iron Man 2 when looking for Iron Man) | −20 |
| Year in filename | +8 |
| Czech audio: `dabing` / `dab` keyword | +8 |
| Czech audio: ` cz ` label | +10 |
| Czech audio: `czech` keyword | +7 |
| Subtitles-only: `titulky` | −10 |
| Subtitles-only: `subs` (without `dabing`) | −5 |
| Source: `remux` | +10 |
| Source: `blu-ray` / `bdrip` | +6 |
| Source: `web-dl` | +4 |
| Source: `web-rip` | +2 |
| Resolution: `2160p` / `4k` / `uhd` | +8 |
| Resolution: `1080p` | +5 |
| Resolution: `720p` | +2 |
| Audio codec: `truehd` / `dts-hd` | +3 |
| Audio codec: `dts` | +2 |
| Audio codec: `ac3` / `aac` | +1 |
| File size ≥ 20 GB | +4 |
| File size ≥ 10 GB | +3 |
| File size ≥ 5 GB | +2 |
| File size ≥ 2 GB | +1 |
| Positive votes (capped at 3) | +2 each |
| Negative votes | −3 each |
| Series SxxExx exact match | +15 |
| Series wrong SxxExx | −25 |
| Series no episode notation | −8 |

### AI fallback (gemini-2.0-flash)

Called only when **score spread across top-N < 15** — meaning Python can't confidently
separate the candidates. Uses `google/gemini-2.0-flash-001` via OpenRouter.

- To change model: update `MODEL` constant in `gemini.py`
- To change ambiguity threshold: update `AMBIGUITY_THRESHOLD` constant
- Interface: `rank_results(movie, candidates, limit) -> list[dict]`

### Sequel number detection

Detects when a filename has a different sequel number than the target movie:
- Target `Iron Man` (no number) + filename `Iron Man 2` → −20 (number > 1)
- Target `Iron Man 3` + filename `Iron Man 2` → −20 (number mismatch)
- Target `Iron Man` + filename `Iron Man 1` → no penalty (1 = first film, same movie)

### No-title-match penalty

Webshare's fuzzy search returns phonetically similar titles (e.g. "Ip Man", "Yes Man"
when searching "Iron Man"). These score high on year + CZ audio alone. The −30 penalty
for no title match pushes them well below correct results.

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
