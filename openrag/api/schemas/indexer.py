from pydantic import BaseModel


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = 5  # default to 5 if not provided
