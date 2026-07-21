"""crawler: web fallback search used when Qdrant score is below threshold.

TODO: port camp-18's crawler.py (ddgs + trafilatura; remember trafilatura
needs lxml_html_clean installed separately or import fails silently and the
service never comes up).
"""
from __future__ import annotations

from typing import Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="saem-crawler")


class CrawlRequest(BaseModel):
    query: str


@app.post("/crawl")
def crawl(req: CrawlRequest):
    # TODO: replace with real ddgs + trafilatura pipeline
    return {"query": req.query, "results": []}


def run(port: Optional[int] = None) -> None:
    uvicorn.run(app, host="0.0.0.0", port=port or 9200)
