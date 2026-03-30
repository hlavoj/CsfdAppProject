import os
from typing import Optional

ADDON_URL = os.getenv("ADDON_URL", "http://127.0.0.1:7000")


def _quality(width: Optional[int]) -> str:
    if not width:
        return ""
    if width >= 3840: return "4K"
    if width >= 1920: return "1080p"
    if width >= 1280: return "720p"
    if width >= 720:  return "480p"
    return "SD"


def _audio(tracks: list[dict]) -> str:
    parts = []
    for t in tracks:
        lang = t.get("language") or ""
        fmt  = t.get("format") or ""
        ch   = t.get("channels")
        ch_s = f" {ch}ch" if ch else ""
        if lang == "CZE":
            parts.append(f"CZ {fmt}{ch_s}".strip())
        elif lang == "ENG":
            parts.append(f"EN {fmt}{ch_s}".strip())
    return " / ".join(parts)


def format_stream(result: dict, episode_label: Optional[str] = None) -> dict:
    detail  = result.get("file_detail") or {}
    tracks  = detail.get("audio_tracks") or []
    quality = _quality(detail.get("width"))
    audio   = _audio(tracks)
    size_gb = result["size"] / 1_073_741_824
    prob    = result.get("match_probability", 0)
    has_cz  = any(t.get("language") == "CZE" for t in tracks)

    # Short label in stream picker
    name_parts = [p for p in [episode_label, quality, "CZ" if has_cz else None] if p]
    name = " • ".join(name_parts) or "Stream"

    # Multi-line description
    meta = []
    if audio:   meta.append(audio)
    meta.append(f"{size_gb:.1f} GB")
    if prob:    meta.append(f"{prob}% match")
    desc_lines = [result["name"], " • ".join(meta)]
    if result.get("ai_reasoning"):
        desc_lines.append(result["ai_reasoning"])

    return {
        "url": f"{ADDON_URL}/stream-redirect/{result['ident']}",
        "name": name,
        "description": "\n".join(desc_lines),
        "behaviorHints": {
            "notWebReady": True,
            "videoSize": result["size"],
            "filename": result["name"],
        },
    }


def format_streams(results: list[dict], season: Optional[int] = None, episode: Optional[int] = None) -> list[dict]:
    # ADDON_URL may not be set at import time, read it fresh each call
    global ADDON_URL
    ADDON_URL = os.getenv("ADDON_URL", "http://127.0.0.1:7000")
    episode_label = f"S{season:02d}E{episode:02d}" if season is not None and episode is not None else None
    return [format_stream(r, episode_label) for r in results if r.get("url")]
