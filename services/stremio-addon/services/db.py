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


def _quality(width: Optional[int]) -> Optional[str]:
    if not width:
        return None
    if width >= 3840: return "4K"
    if width >= 1920: return "1080p"
    if width >= 1280: return "720p"
    if width >= 720:  return "480p"
    return "SD"


def init_db() -> None:
    with _conn() as conn:
        # Stream result cache
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stream_cache (
                id                SERIAL PRIMARY KEY,
                video_id          TEXT NOT NULL,
                movie_year        INTEGER,
                ident             TEXT NOT NULL,
                filename          TEXT,
                quality           TEXT,
                has_cz            BOOLEAN,
                audio_langs       TEXT,
                size_bytes        BIGINT,
                match_probability INTEGER,
                stream_json       JSONB NOT NULL,
                cached_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                hit_count         INTEGER NOT NULL DEFAULT 0,
                UNIQUE (video_id, ident)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_cache_video_id ON stream_cache (video_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_cache_quality  ON stream_cache (quality)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_cache_has_cz   ON stream_cache (has_cz)")

        # Catalog definitions — one row per category
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catalogs (
                id       TEXT PRIMARY KEY,
                name     TEXT NOT NULL,
                type     TEXT NOT NULL CHECK (type IN ('movie', 'series')),
                position INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Catalog items — one row per movie/series in a category
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catalog_items (
                id         SERIAL PRIMARY KEY,
                catalog_id TEXT NOT NULL REFERENCES catalogs(id) ON DELETE CASCADE,
                imdb_id    TEXT NOT NULL,
                position   INTEGER NOT NULL DEFAULT 0,
                UNIQUE (catalog_id, imdb_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_items_catalog ON catalog_items (catalog_id, position)")

        # Seed default catalogs (skip if already exist)
        conn.execute("""
            INSERT INTO catalogs (id, name, type, position) VALUES
                ('kids_movies', 'Kids Movies', 'movie',  0),
                ('kids_series', 'Kids Series', 'series', 1)
            ON CONFLICT (id) DO NOTHING
        """)

        # Seed kids movies
        conn.execute("""
            INSERT INTO catalog_items (catalog_id, imdb_id, position) VALUES
                ('kids_movies', 'tt0114709', 0),   -- Toy Story
                ('kids_movies', 'tt0110357', 1),   -- The Lion King
                ('kids_movies', 'tt0266543', 2),   -- Finding Nemo
                ('kids_movies', 'tt0126029', 3),   -- Shrek
                ('kids_movies', 'tt0317219', 4),   -- The Incredibles
                ('kids_movies', 'tt0198781', 5),   -- Monsters, Inc.
                ('kids_movies', 'tt2294629', 6),   -- Frozen
                ('kids_movies', 'tt0910970', 7),   -- WALL-E
                ('kids_movies', 'tt1049413', 8),   -- Up
                ('kids_movies', 'tt2948356', 9),   -- Zootopia
                ('kids_movies', 'tt2380307', 10),  -- Coco
                ('kids_movies', 'tt3521164', 11),  -- Moana
                ('kids_movies', 'tt0241527', 12),  -- Harry Potter 1
                ('kids_movies', 'tt4633694', 13),  -- Spider-Man: Into the Spider-Verse
                ('kids_movies', 'tt8946378', 14)   -- Encanto
            ON CONFLICT (catalog_id, imdb_id) DO NOTHING
        """)

        # Seed kids series
        conn.execute("""
            INSERT INTO catalog_items (catalog_id, imdb_id, position) VALUES
                ('kids_series', 'tt0417299', 0),   -- Avatar: The Last Airbender
                ('kids_series', 'tt1865718', 1),   -- Gravity Falls
                ('kids_series', 'tt0852863', 2),   -- Phineas and Ferb
                ('kids_series', 'tt1305826', 3),   -- Adventure Time
                ('kids_series', 'tt0206512', 4),   -- SpongeBob SquarePants
                ('kids_series', 'tt0426769', 5),   -- Peppa Pig
                ('kids_series', 'tt4786824', 6),   -- Miraculous Ladybug
                ('kids_series', 'tt0168366', 7),   -- Pokémon
                ('kids_series', 'tt3112940', 8),   -- Paw Patrol
                ('kids_series', 'tt7539996', 9),   -- Bluey
                ('kids_series', 'tt0799922', 10),  -- Wizards of Waverly Place
                ('kids_series', 'tt0844357', 11),  -- iCarly
                ('kids_series', 'tt1751105', 12),  -- My Little Pony: Friendship Is Magic
                ('kids_series', 'tt0115316', 13),  -- Dexter's Laboratory
                ('kids_series', 'tt0182630', 14)   -- Scooby-Doo on Zombie Island (classic)
            ON CONFLICT (catalog_id, imdb_id) DO NOTHING
        """)

        conn.commit()
    print("DB: all tables ready")


def get_catalogs() -> list[dict]:
    """Return all catalog definitions ordered by position."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT id, name, type FROM catalogs ORDER BY position"
            ).fetchall()
        return [{"id": r[0], "name": r[1], "type": r[2]} for r in rows]
    except Exception as e:
        print(f"DB get_catalogs error: {e}")
        return []


def get_catalog_items(catalog_id: str) -> list[str]:
    """Return ordered list of imdb_ids for a catalog."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT imdb_id FROM catalog_items WHERE catalog_id = %s ORDER BY position",
                (catalog_id,),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"DB get_catalog_items error: {e}")
        return []


def cache_get(video_id: str) -> Optional[dict]:
    """Return cached streams for video_id if within TTL, else None."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT stream_json, movie_year, cached_at, hit_count
                   FROM stream_cache
                   WHERE video_id = %s
                   ORDER BY match_probability DESC NULLS LAST""",
                (video_id,),
            ).fetchall()
        if not rows:
            return None
        _, movie_year, cached_at, hit_count = rows[0]
        age_days = (datetime.now(timezone.utc) - cached_at).days
        if age_days >= _ttl_days(movie_year):
            return None  # expired — treat as miss
        streams = [row[0] for row in rows]
        return {"results": streams, "movie_year": movie_year, "cached_at": cached_at, "hit_count": hit_count}
    except Exception as e:
        print(f"DB cache_get error: {e}")
        return None


def cache_set(video_id: str, results: list, streams: list, movie_year: Optional[int]) -> None:
    """Store one row per stream. results[i] is raw MediaFinder output, streams[i] is the
    formatted Stremio stream object. They correspond by index (same filter applied)."""
    try:
        filtered_results = [r for r in results if r.get("url")]
        with _conn() as conn:
            # Delete old entries for this video first (full refresh)
            conn.execute("DELETE FROM stream_cache WHERE video_id = %s", (video_id,))
            for raw, stream in zip(filtered_results, streams):
                detail = raw.get("file_detail") or {}
                tracks = detail.get("audio_tracks") or []
                has_cz = any(t.get("language") == "CZE" for t in tracks)
                audio_langs = " / ".join(
                    t.get("language", "") for t in tracks if t.get("language")
                )
                conn.execute(
                    """
                    INSERT INTO stream_cache
                        (video_id, movie_year, ident, filename, quality, has_cz,
                         audio_langs, size_bytes, match_probability, stream_json, cached_at, hit_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), 0)
                    """,
                    (
                        video_id,
                        movie_year,
                        raw.get("ident"),
                        raw.get("name"),
                        _quality(detail.get("width")),
                        has_cz,
                        audio_langs or None,
                        raw.get("size"),
                        raw.get("match_probability"),
                        json.dumps(stream),
                    ),
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
