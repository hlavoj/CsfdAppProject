"""
Periodic sync of Disney+ and Netflix kids catalog content from TMDB watch providers.
Runs in a background thread — first run 30s after startup, then every 7 days.

TMDB provider IDs: Disney+=337, Netflix=8
TMDB genre IDs:   Animation=16, Family=10751
"""
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from services.db import sync_streaming_catalog

TMDB_BASE = "https://api.themoviedb.org/3"
REFRESH_INTERVAL_SECONDS = 7 * 24 * 3600   # 7 days
DISCOVER_PAGES = 4                           # 4 pages × 20 = up to 80 titles per catalog
KIDS_GENRES = "16|10751"                     # Animation OR Family (| = OR on TMDB)

# Catalogs to sync — order here controls manifest position (starting at position 2,
# after the manually curated Kids Movies/Series at positions 0 and 1).
# Series listed first because user cares more about them.
STREAMING_CATALOGS = [
    {"id": "disney_kids_series",  "name": "Disney+ Kids Series CZ",  "type": "series", "provider": 337, "position": 2},
    {"id": "netflix_kids_series", "name": "Netflix Kids Series CZ",  "type": "series", "provider": 8,   "position": 3},
    {"id": "disney_kids_movies",  "name": "Disney+ Kids Movies CZ",  "type": "movie",  "provider": 337, "position": 4},
    {"id": "netflix_kids_movies", "name": "Netflix Kids Movies CZ",  "type": "movie",  "provider": 8,   "position": 5},
]


def _api_key() -> str:
    return os.getenv("TMDB_API_KEY", "")


def _fetch_tmdb_ids(provider_id: int, media_type: str) -> list[int]:
    """Query TMDB Discover for titles on a given provider in CZ, sorted by popularity."""
    endpoint = "tv" if media_type == "series" else "movie"
    tmdb_ids: list[int] = []
    seen: set[int] = set()

    with httpx.Client(timeout=10) as client:
        for page in range(1, DISCOVER_PAGES + 1):
            try:
                r = client.get(
                    f"{TMDB_BASE}/discover/{endpoint}",
                    params={
                        "api_key": _api_key(),
                        "with_watch_providers": provider_id,
                        "watch_region": "CZ",
                        "with_genres": KIDS_GENRES,
                        "sort_by": "popularity.desc",
                        "page": page,
                    },
                )
                r.raise_for_status()
                for item in r.json().get("results", []):
                    tid = item["id"]
                    if tid not in seen:
                        seen.add(tid)
                        tmdb_ids.append(tid)
            except Exception as e:
                print(f"Catalog sync: TMDB discover page {page} failed: {e}")

    return tmdb_ids


def _tmdb_to_imdb(tmdb_id: int, media_type: str) -> str | None:
    """Resolve a TMDB movie/TV ID to an IMDB ID via external_ids endpoint."""
    endpoint = "tv" if media_type == "series" else "movie"
    try:
        with httpx.Client(timeout=8) as client:
            r = client.get(
                f"{TMDB_BASE}/{endpoint}/{tmdb_id}/external_ids",
                params={"api_key": _api_key()},
            )
            r.raise_for_status()
            imdb_id = r.json().get("imdb_id")
            return imdb_id if imdb_id and imdb_id.startswith("tt") else None
    except Exception:
        return None


def _resolve_imdb_ids(tmdb_ids: list[int], media_type: str) -> list[str]:
    """Convert TMDB IDs to IMDB IDs in parallel. Preserves popularity order."""
    results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_tmdb_to_imdb, tid, media_type): tid for tid in tmdb_ids}
        for future in as_completed(futures):
            tid = futures[future]
            results[tid] = future.result()

    # Rebuild in original order, dropping any that had no IMDB ID
    return [results[tid] for tid in tmdb_ids if results.get(tid)]


def refresh_streaming_catalogs() -> None:
    """Fetch current Disney+/Netflix kids content from TMDB and update catalog_items."""
    print("Catalog sync: starting refresh of streaming catalogs...")

    for cat in STREAMING_CATALOGS:
        try:
            print(f"Catalog sync: fetching '{cat['name']}'...")
            tmdb_ids = _fetch_tmdb_ids(cat["provider"], cat["type"])
            print(f"Catalog sync:   {len(tmdb_ids)} TMDB IDs found, resolving to IMDB IDs...")
            imdb_ids = _resolve_imdb_ids(tmdb_ids, cat["type"])
            print(f"Catalog sync:   {len(imdb_ids)} IMDB IDs resolved")
            sync_streaming_catalog(cat["id"], cat["name"], cat["type"], cat["position"], imdb_ids)
        except Exception as e:
            print(f"Catalog sync: failed for '{cat['name']}': {e}")

    print("Catalog sync: done")


def start_background_sync() -> None:
    """Launch a daemon thread that refreshes streaming catalogs every 7 days."""
    def _loop():
        time.sleep(30)   # let Flask finish starting before hammering TMDB
        while True:
            refresh_streaming_catalogs()
            time.sleep(REFRESH_INTERVAL_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="catalog-sync")
    t.start()
    print("Catalog sync: background thread started (first run in 30s)")
