import json
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg


def _conn():
    return psycopg.connect(os.getenv("POSTGRES_URL", ""))


def _ttl_days(movie_year: Optional[int]) -> int:
    """Movies released this year get 3-day TTL (new rips appear quickly).
    Everything else (older movies + series episodes) gets 14 days."""
    if movie_year is None:
        return 14
    return 3 if movie_year >= datetime.now().year else 14


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stream_cache (
                video_id   TEXT PRIMARY KEY,
                results    JSONB NOT NULL,
                movie_year INTEGER,
                cached_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                hit_count  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    print("DB: stream_cache table ready")


def cache_get(video_id: str) -> Optional[dict]:
    """Return cached row if still within TTL, else None."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT results, movie_year, cached_at, hit_count FROM stream_cache WHERE video_id = %s",
                (video_id,),
            ).fetchone()
        if row is None:
            return None
        results, movie_year, cached_at, hit_count = row
        age_days = (datetime.now(timezone.utc) - cached_at).days
        if age_days >= _ttl_days(movie_year):
            return None  # expired — treat as miss
        return {"results": results, "movie_year": movie_year, "cached_at": cached_at, "hit_count": hit_count}
    except Exception as e:
        print(f"DB cache_get error: {e}")
        return None


def cache_set(video_id: str, results: list, movie_year: Optional[int]) -> None:
    try:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO stream_cache (video_id, results, movie_year, cached_at, hit_count)
                VALUES (%s, %s::jsonb, %s, NOW(), 0)
                ON CONFLICT (video_id) DO UPDATE
                    SET results = EXCLUDED.results,
                        movie_year = EXCLUDED.movie_year,
                        cached_at = NOW(),
                        hit_count = 0
                """,
                (video_id, json.dumps(results), movie_year),
            )
            conn.commit()
    except Exception as e:
        print(f"DB cache_set error: {e}")


def cache_increment_hit(video_id: str) -> None:
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE stream_cache SET hit_count = hit_count + 1 WHERE video_id = %s",
                (video_id,),
            )
            conn.commit()
    except Exception as e:
        print(f"DB cache_increment_hit error: {e}")


def cache_delete(video_id: str) -> bool:
    try:
        with _conn() as conn:
            result = conn.execute(
                "DELETE FROM stream_cache WHERE video_id = %s",
                (video_id,),
            )
            conn.commit()
            return result.rowcount > 0
    except Exception as e:
        print(f"DB cache_delete error: {e}")
        return False
