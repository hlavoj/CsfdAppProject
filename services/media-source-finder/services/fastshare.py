"""
FastShare.cloud client — primary video source (searched before Webshare).

Auth flow:
  GET /api/api_kodi.php?process=login&login=X&password=Y
  → JSON: {"user": {"hash": "<token>", ...}}
  Session token cached in memory; re-auth on failure.

Search flow:
  GET /api/api_kodi.php?process=search&term=<query>&pagination=200
  → JSON: {"search": {"total": "N", "file": [{...}, ...]}}

File link flow:
  GET <download_url>  Cookie: FASTSHARE=<hash>  follow_redirects=True
  → 302 to CDN URL (pre-signed, no cookie needed for playback)

Ident scheme:
  "fs_" + urlsafe_base64(download_url) — self-contained, survives restarts.
  All FastShare idents start with "fs_"; Webshare idents never do.
"""

import base64
import os
import re
from typing import Optional

import httpx

FASTSHARE_BASE = "https://fastshare.cloud"
_API = f"{FASTSHARE_BASE}/api/api_kodi.php"

_hash: Optional[str] = None   # cached session token


# ---------------------------------------------------------------------------
# Ident helpers
# ---------------------------------------------------------------------------

def _encode_ident(download_url: str) -> str:
    b64 = base64.urlsafe_b64encode(download_url.encode()).decode().rstrip("=")
    return f"fs_{b64}"


def _decode_ident(ident: str) -> Optional[str]:
    if not ident.startswith("fs_"):
        return None
    b64 = ident[3:]
    pad = 4 - len(b64) % 4
    if pad != 4:
        b64 += "=" * pad
    try:
        return base64.urlsafe_b64decode(b64).decode()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _login() -> str:
    global _hash
    username = os.getenv("FASTSHARE_USERNAME", "")
    password = os.getenv("FASTSHARE_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("FASTSHARE_USERNAME / FASTSHARE_PASSWORD not set")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(_API, params={"process": "login", "login": username, "password": password})
        r.raise_for_status()
        data = r.json()
    token = data.get("user", {}).get("hash")
    if not token:
        raise RuntimeError(f"FastShare login failed: {data}")
    _hash = token
    print(f"FastShare: authenticated as {username}")
    return token


async def _ensure_auth() -> str:
    global _hash
    if _hash:
        return _hash
    return await _login()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _parse_resolution(res_str: str) -> tuple[Optional[int], Optional[int]]:
    """Parse '1920x1080 px' → (1920, 1080)."""
    m = re.match(r'(\d+)\s*x\s*(\d+)', res_str or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


async def search_videos(query: str, limit: int = 20) -> list[dict]:
    """
    Search FastShare for video files matching query.
    Returns candidates in the same dict format as webshare.search_videos plus
    extra '_width', '_height', '_duration' keys for building FileDetail.
    """
    global _hash
    token = await _ensure_auth()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(_API, params={
            "process": "search",
            "term": query,
            "pagination": 200,
            "adult": "0",
        }, cookies={"FASTSHARE": token})

        # Re-auth once if session expired
        if r.status_code in (401, 403) or (r.headers.get("content-type", "").startswith("text") and "login" in r.text.lower()):
            _hash = None
            token = await _login()
            r = await client.get(_API, params={
                "process": "search",
                "term": query,
                "pagination": 200,
                "adult": "0",
            }, cookies={"FASTSHARE": token})

        r.raise_for_status()
        data = r.json()

    files = data.get("search", {}).get("file", [])
    results = []
    for f in files:
        dl_url = f.get("download_url", "")
        if not dl_url:
            continue
        name = f.get("filename", "")
        size = 0
        try:
            size = int(f.get("data", {}).get("value", 0) or 0)
        except (ValueError, TypeError):
            pass
        width, height = _parse_resolution(f.get("resolution", ""))
        duration = None
        try:
            duration = int(f.get("duration", {}).get("value", 0) or 0) or None
        except (ValueError, TypeError):
            pass

        results.append({
            "ident": _encode_ident(dl_url),
            "name": name,
            "size": size,
            "url": dl_url,              # truthy value — actual stream URL constructed from ident
            "positive_votes": 0,
            "negative_votes": 0,
            "_width": width,
            "_height": height,
            "_duration": duration,
        })
    return results


# ---------------------------------------------------------------------------
# File link
# ---------------------------------------------------------------------------

async def get_file_link(ident: str) -> Optional[str]:
    """
    Decode download_url from ident, authenticate with FastShare cookie,
    follow the redirect and return the CDN URL (usable without cookies).
    """
    global _hash
    download_url = _decode_ident(ident)
    if not download_url:
        return None

    token = await _ensure_auth()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(download_url, cookies={"FASTSHARE": token})
            final_url = str(r.url)
            # Prefer the CDN URL (different domain from fastshare) from redirect history
            if r.history:
                return final_url
            # No redirect — return URL as-is; may work or may need proxy
            return final_url
    except Exception as e:
        print(f"FastShare get_file_link error: {e}")
        return download_url   # fallback: return original URL
