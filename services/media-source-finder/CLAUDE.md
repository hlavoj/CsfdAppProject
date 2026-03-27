# MediaSourceFinder — Service Documentation

Lightweight FastAPI service that accepts an IMDB ID, fetches movie metadata via OMDB,
searches Webshare.cz for matching video files, and returns direct stream URLs.

## Directory Layout

```
media-source-finder/
├── main.py                  # FastAPI app entry point, lifespan, router mount
├── requirements.txt         # Python dependencies
├── .env                     # Secrets — gitignored, never committed
├── .env.example             # Template for .env
├── routers/
│   └── search.py            # GET /search?imdb_id=...  (or csfd_id — 501 stub)
├── services/
│   ├── omdb.py              # OMDB API client
│   └── webshare.py          # Webshare.cz client (auth, search, file_link)
└── models/
    └── media_source.py      # Pydantic response models
```

## Environment Variables (.env)

| Variable             | Description                  |
|----------------------|------------------------------|
| `WEBSHARE_USERNAME`  | Webshare.cz username         |
| `WEBSHARE_PASSWORD`  | Webshare.cz password         |
| `OMDB_API_KEY`       | OMDB API key                 |

## Running

```bash
cd services/media-source-finder
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## API

### `GET /search`

| Parameter  | Required | Description                         |
|------------|----------|-------------------------------------|
| `imdb_id`  | one of   | IMDB ID, e.g. `tt1375666`           |
| `csfd_id`  | one of   | CSFD ID — returns **501** for now   |

#### Example

```bash
curl "http://localhost:8000/search?imdb_id=tt1375666"
```

#### Response

```json
{
  "query": "Inception 2010",
  "movie": { "title": "Inception", "year": "2010", "imdb_id": "tt1375666" },
  "results": [
    {
      "ident": "abc123",
      "name": "Inception.2010.1080p.mkv",
      "size": 8589934592,
      "url": "https://cdn.webshare.cz/..."
    }
  ]
}
```

## Service Flow

1. Validate query params (exactly one of `imdb_id` / `csfd_id`)
2. **OMDB** — fetch `Title` + `Year` → build search query `"{Title} {Year}"`
3. **Webshare auth** — `POST /api/salt/` → hash password → `POST /api/login/` → WST token
   - Token cached as module-level variable in `webshare.py`, re-auth on FATAL response
4. **Webshare search** — `POST /api/search/` with `what`, `category=video`, `limit=20`, `wst`
5. **File links** — `POST /api/file_link/` per result → direct download URL
6. Return assembled JSON

## Password Hashing (Webshare)

```python
import hashlib
md5 = hashlib.md5(f"{password}{salt}".encode()).hexdigest()
sha1 = hashlib.sha1(md5.encode()).hexdigest()
```

## Error Codes

| Code | Meaning                              |
|------|--------------------------------------|
| 400  | Neither `imdb_id` nor `csfd_id` given |
| 404  | OMDB found no movie for the given ID |
| 501  | `csfd_id` lookup not yet implemented |
| 502  | Webshare search/auth failure          |
