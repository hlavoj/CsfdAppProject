"""
Ranking service for Webshare video candidates.

Primary:  rich Python scorer — fast, deterministic, no API cost.
Fallback: OpenRouter LLM — only called when Python scores are too close to
          distinguish confidently (spread between top-N scores < AMBIGUITY_THRESHOLD).

Interface: rank_results(movie, candidates, limit) -> list[dict]
"""
import os
import json
import re
import httpx
from models.media_source import MovieInfo

_SXEX_RE = re.compile(r's(\d{1,2})e(\d{1,2})', re.IGNORECASE)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MODEL = "google/gemini-2.0-flash-001"

# How many top Python-scored candidates to send to AI when fallback triggers
AI_CANDIDATE_LIMIT = 15

# If the score spread across the top-N results is below this,
# Python can't confidently separate them — call AI to break ties.
AMBIGUITY_THRESHOLD = 15


# ---------------------------------------------------------------------------
# Python scorer
# ---------------------------------------------------------------------------

def _trailing_num(title: str) -> int | None:
    """Extract trailing sequel number from a title, e.g. 'Iron Man 3' → 3, 'Iron Man' → None."""
    m = re.search(r'\b(\d+)\s*$', title.strip())
    return int(m.group(1)) if m else None


def _sequel_penalty(title: str, name: str, target_num: int | None) -> int:
    """
    Return a penalty when the filename has a different sequel number than the target.
    e.g. searching for 'Iron Man' (no number): 'Iron Man 2.mkv' → -20
         searching for 'Iron Man 3': 'Iron Man 2.mkv' → -20
    No penalty when file has '1' and target has no number (same movie, just labeled).
    """
    idx = name.find(title)
    if idx < 0:
        return 0
    after = name[idx + len(title):].lstrip(' ._-(')
    m = re.match(r'^(\d+)', after)
    if not m:
        return 0
    file_num = int(m.group(1))
    if file_num > 20:          # not a sequel number (could be year, resolution, etc.)
        return 0
    if target_num is None and file_num > 1:
        return -20             # "Iron Man 2" when looking for "Iron Man"
    if target_num is not None and file_num != target_num:
        return -20             # "Iron Man 2" when looking for "Iron Man 3"
    return 0


def _score(c: dict, movie: MovieInfo) -> int:
    name = c["name"].lower()
    s = 0

    # --- Title match --------------------------------------------------------
    title_en = (movie.original_title or "").lower().strip()
    title_cz = (movie.title or "").lower().strip()
    target_num_en = _trailing_num(title_en)
    target_num_cz = _trailing_num(title_cz)

    title_matched = False
    if title_en and title_en in name:
        s += 15
        s += _sequel_penalty(title_en, name, target_num_en)
        title_matched = True
    if title_cz and title_cz != title_en and title_cz in name:
        s += 12
        s += _sequel_penalty(title_cz, name, target_num_cz)
        title_matched = True

    # No title match at all → likely a wrong movie returned by fuzzy Webshare search
    if not title_matched:
        s -= 30

    # --- Year ---------------------------------------------------------------
    if movie.year and movie.year in name:  s += 8

    # --- Czech audio signals ------------------------------------------------
    if "dabing" in name or " dab " in name or ".dab." in name:  s += 8
    # CZ label patterns: " cz " / ".cz." / "(cz)" / "-cz-" / filename ends with .cz
    if re.search(r'(?:^|[\s._(-])cz(?:[\s._)\[]|$)', name):    s += 10
    elif ".cz" in name:                                          s += 5
    if "czech" in name:                                          s += 7
    # Penalise subtitles-only releases
    if "titulky" in name:                                        s -= 10
    if re.search(r'\bsubs?\b', name) and "dabing" not in name:  s -= 5

    # --- Source / encode quality --------------------------------------------
    if "remux" in name:                                          s += 10
    elif re.search(r'blu.?ray|bdrip|brrip', name):               s += 6
    elif re.search(r'web.?dl', name):                            s += 4
    elif re.search(r'web.?rip', name):                           s += 2
    elif "hdtv" in name:                                         s += 1

    # --- Resolution ---------------------------------------------------------
    if re.search(r'2160p|4k|uhd', name):                         s += 8
    elif "1080" in name:                                         s += 5
    elif "720" in name:                                          s += 2

    # --- Audio codec --------------------------------------------------------
    if re.search(r'truehd|dts.hd|dts-hd', name):                s += 3
    elif "dts" in name:                                          s += 2
    elif re.search(r'ac3|dd5|aac', name):                        s += 1

    # --- File size as quality proxy (GB) ------------------------------------
    size_gb = c.get("size", 0) / 1_073_741_824
    if size_gb >= 20:    s += 4
    elif size_gb >= 10:  s += 3
    elif size_gb >= 5:   s += 2
    elif size_gb >= 2:   s += 1

    # --- Community votes ----------------------------------------------------
    s += min(c.get("positive_votes", 0), 3) * 2   # cap bonus at +6
    s -= c.get("negative_votes", 0) * 3

    # --- Series: SxxExx match -----------------------------------------------
    if movie.media_type == "series" and movie.season is not None and movie.episode is not None:
        target_s = f"s{movie.season:02d}e{movie.episode:02d}"
        target_alt = f"s{movie.season}e{movie.episode}"
        found = [(int(m.group(1)), int(m.group(2))) for m in _SXEX_RE.finditer(name)]
        if found:
            if any(f == (movie.season, movie.episode) for f in found):
                s += 15   # exact episode match
            else:
                s -= 25   # wrong episode — near-exclude
        else:
            if target_s in name or target_alt in name:
                s += 15   # matched via simpler pattern
            else:
                s -= 8    # no episode notation at all

    return s


def _python_rank(candidates: list[dict], movie: MovieInfo, limit: int) -> list[tuple[int, dict]]:
    """Return (score, candidate) pairs sorted descending, limited to top AI_CANDIDATE_LIMIT."""
    scored = sorted(
        [(_score(c, movie), c) for c in candidates],
        key=lambda x: x[0],
        reverse=True,
    )
    return scored[:AI_CANDIDATE_LIMIT]


def _is_ambiguous(scored: list[tuple[int, dict]], limit: int) -> bool:
    """True when top-N Python scores are too close to rank confidently."""
    top = scored[:min(limit, len(scored))]
    if len(top) < 2:
        return False
    return (top[0][0] - top[-1][0]) < AMBIGUITY_THRESHOLD


def _to_ranked(scored: list[tuple[int, dict]], limit: int) -> list[dict]:
    """Convert (score, candidate) pairs to result dicts with normalised match_probability."""
    top = scored[:limit]
    if not top:
        return []
    max_s = top[0][0]
    results = []
    for s, c in top:
        if max_s > 0:
            prob = max(10, min(95, int(s / max_s * 85) + 10))
        else:
            prob = max(10, min(50, 50 + s))   # for zero/negative scores
        results.append({
            "ident": c["ident"],
            "match_probability": prob,
            "reasoning": f"python score {s}",
        })
    return results


# ---------------------------------------------------------------------------
# AI fallback (OpenRouter)
# ---------------------------------------------------------------------------

def _build_prompt(movie: MovieInfo, candidates: list[dict], limit: int) -> str:
    candidates_json = json.dumps(
        [{"ident": c["ident"], "name": c["name"], "votes": f"+{c['positive_votes']}/-{c['negative_votes']}"}
         for c in candidates],
        ensure_ascii=False,
    )
    runtime_line = f", {movie.runtime_minutes} min" if movie.runtime_minutes else ""

    if movie.media_type == "series" and movie.season is not None:
        sxex = f"S{movie.season:02d}E{movie.episode:02d}"
        ep_title = f" — {movie.episode_title}" if movie.episode_title else ""
        target = (
            f"{movie.title} / {movie.original_title} "
            f"{sxex}{ep_title} ({movie.year}{runtime_line})"
        )
        hints = (
            f"TV series episode {sxex}. SxxExx match is most critical — wrong episode = 0%. "
            f"Czech dubbing preferred. '&' becomes 'a' in Czech, diacritics often stripped."
        )
    else:
        target = f"{movie.title} / {movie.original_title} ({movie.year}{runtime_line})"
        hints = (
            f"Czech dubbing preferred, subtitles acceptable. "
            f"'CZ dabing'=dubbed, '&' becomes 'a' in Czech titles, diacritics often stripped. "
            f"Quality: Remux>BluRay>WEB-DL. Resolution: 2160p>1080p>720p."
        )

    return (
        f"Rank these Webshare video files for: {target}. {hints}\n"
        f"Files: {candidates_json}\n"
        f"Return JSON array of top {limit} sorted best-first: "
        f'[{{"ident":"...","match_probability":85,"reasoning":"short"}}]. '
        f"match_probability is 0-100 int. JSON only, no markdown."
    )


def _parse_response(text: str) -> list[dict]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    ranked = json.loads(text.strip())
    # Normalise: model sometimes returns a single dict or wrapped object
    if isinstance(ranked, dict):
        if "ident" in ranked:
            ranked = [ranked]
        else:
            ranked = next((v for v in ranked.values() if isinstance(v, list)), [])
    return ranked


async def _ai_rank(movie: MovieInfo, candidates: list[dict], limit: int) -> list[dict]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": _build_prompt(movie, candidates, limit)}],
                "temperature": 0.1,
                "max_tokens": 800,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()

    text = resp.json()["choices"][0]["message"]["content"]
    ranked = _parse_response(text)

    # Guard against hallucinated idents
    valid_idents = {c["ident"] for c in candidates}
    ranked = [r for r in ranked if r.get("ident") in valid_idents]
    ranked.sort(key=lambda x: x.get("match_probability", 0), reverse=True)
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def rank_results(movie: MovieInfo, candidates: list[dict], limit: int) -> list[dict]:
    """
    Score candidates with Python heuristics.
    Falls back to AI (gemini-2.0-flash) only when top scores are ambiguous.
    Returns list of {ident, match_probability, reasoning}.
    """
    scored = _python_rank(candidates, movie, limit)

    if _is_ambiguous(scored, limit):
        top_candidates = [c for _, c in scored]
        try:
            ai_results = await _ai_rank(movie, top_candidates, limit)
            if ai_results:
                return ai_results
        except Exception as e:
            print(f"AI fallback failed ({e}), using Python scorer")

    return _to_ranked(scored, limit)
