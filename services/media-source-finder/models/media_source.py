from pydantic import BaseModel
from typing import Optional


class MovieInfo(BaseModel):
    title: str                          # Localized title (Czech for TMDB, English for OMDB)
    original_title: str                 # Original release title
    year: str
    source_id: str                      # imdb_id or tmdb_id value
    source: str                         # "omdb" | "tmdb"
    runtime_minutes: Optional[int] = None
    genres: list[str] = []


class AudioTrack(BaseModel):
    format: Optional[str] = None
    channels: Optional[int] = None
    language: Optional[str] = None


class FileDetail(BaseModel):
    video_codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    bitrate_kbps: Optional[int] = None
    duration_seconds: Optional[int] = None
    audio_tracks: list[AudioTrack] = []


class StreamResult(BaseModel):
    ident: str
    name: str
    size: int
    url: str
    positive_votes: int
    negative_votes: int
    match_probability: int              # 0–100 from AI
    ai_reasoning: str
    file_detail: Optional[FileDetail] = None


class SearchResponse(BaseModel):
    query: str
    movie: MovieInfo
    results: list[StreamResult]
