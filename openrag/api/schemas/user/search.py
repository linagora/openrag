from pydantic import BaseModel


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = 5
