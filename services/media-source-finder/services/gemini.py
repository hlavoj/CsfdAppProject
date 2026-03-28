"""
AI ranking service — OpenRouter backend.
Interface: rank_results(movie, candidates, limit) -> list[dict]
Swap the MODEL constant or provider URL to change the underlying LLM.
"""
import os
import json
import re
import httpx
from models.media_source import MovieInfo

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MODEL = "meta-llama/llama-3.1-8b-instruct"

# Max candidates sent to AI — pre-filter reduces noise and token count
AI_CANDIDATE_LIMIT = 15


def _prefilter(candidates: list[dict], movie: MovieInfo) -> list[dict]:
    """
    Score candidates with cheap string heuristics and keep the top AI_CANDIDATE_LIMIT.
    Runs before the AI call to reduce prompt size and response time.
    """
    title_cz = movie.title.lower()
    title_en = movie.original_title.lower()
    year = movie.year

    def score(c: dict) -> int:
        name = c["name"].lower()
        s = 0
        if title_en in name:                                  s += 6
        if title_cz in name:                                  s += 4
        if year in name:                                      s += 3
        if " cz" in name or ".cz" in name or "_cz" in name:  s += 3
        if "dabing" in name or "dab." in name:                s += 2
        if "1080" in name:                                    s += 1
        if "2160" in name or "4k" in name:                    s += 1
        s += c.get("positive_votes", 0)
        s -= c.get("negative_votes", 0) * 2
        return s

    return sorted(candidates, key=score, reverse=True)[:AI_CANDIDATE_LIMIT]


def _build_prompt(movie: MovieInfo, candidates: list[dict], limit: int) -> str:
    candidates_json = json.dumps(
        [{"ident": c["ident"], "name": c["name"], "votes": f"+{c['positive_votes']}/-{c['negative_votes']}"}
         for c in candidates],
        ensure_ascii=False,
    )
    runtime_line = f", {movie.runtime_minutes} min" if movie.runtime_minutes else ""
    return (
        f"Rank these Webshare video files for: {movie.title} / {movie.original_title} "
        f"({movie.year}{runtime_line}). Czech dubbing preferred, subtitles acceptable. "
        f"Hints: 'CZ dabing'=dubbed, '&' becomes 'a' in Czech titles, diacritics often stripped. "
        f"Quality: 2160p>1080p>720p. votes=community trust.\n"
        f"Files: {candidates_json}\n"
        f"Return JSON array of top {limit} sorted best-first: "
        f'[{{"ident":"...","match_probability":85,"reasoning":"short"}}]. '
        f"match_probability is 0-100 int. JSON only."
    )


def _parse_response(text: str) -> list[dict]:
    """Extract JSON array from response, stripping any markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


async def rank_results(movie: MovieInfo, candidates: list[dict], limit: int) -> list[dict]:
    """
    Pre-filters candidates with heuristics, sends top AI_CANDIDATE_LIMIT to OpenRouter.
    Returns top `limit` items as list of {ident, match_probability, reasoning}.
    """
    filtered = _prefilter(candidates, movie)
    api_key = os.getenv("OPENROUTER_API_KEY")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": _build_prompt(movie, filtered, limit)}],
                "temperature": 0.1,
                "max_tokens": 600,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()

    text = resp.json()["choices"][0]["message"]["content"]
    ranked: list[dict] = _parse_response(text)

    # If model returned a wrapped object instead of array, unwrap it
    if isinstance(ranked, dict):
        ranked = next((v for v in ranked.values() if isinstance(v, list)), [])

    # Guard against hallucinated idents
    valid_idents = {c["ident"] for c in filtered}
    ranked = [r for r in ranked if r.get("ident") in valid_idents]

    ranked.sort(key=lambda x: x.get("match_probability", 0), reverse=True)
    return ranked[:limit]
