from pydantic import BaseModel
from typing import Optional


class MovieInfo(BaseModel):
    title: str
    year: str
    imdb_id: str


class StreamResult(BaseModel):
    ident: str
    name: str
    size: int
    url: str


class SearchResponse(BaseModel):
    query: str
    movie: MovieInfo
    results: list[StreamResult]
