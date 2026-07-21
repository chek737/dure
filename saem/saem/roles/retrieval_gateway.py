"""retrieval_gateway: search Qdrant, fall back to crawler, ask the LLM.

TODO: port the real /ask and /vibecutter/ask logic from camp-59's
gateway.py (query_points() on qdrant-client>=1.18, THRESH-based branch to
mode:repo / mode:web / mode:none, source citations in the response) here.
This stub only wires up the FastAPI app + uvicorn entrypoint so `saem
register --role retrieval_gateway` has something real to start.

The final LLM call goes to whichever dure backend head last registered
(`saem head register-backend`) — same 192.168.0.x network, plain HTTP:
    url, model = get_llm_backend()
    httpx.post(f"{url}/v1/chat/completions",
               json={"model": model, "messages": [...]})
"""
from __future__ import annotations

from typing import Optional

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from saem.common.config import GATEWAY_SEARCH_THRESHOLD, QDRANT_URL, get_llm_backend

app = FastAPI(title="saem-retrieval-gateway")


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
def ask(req: AskRequest):
    # TODO: replace with real query_points() + LLM call
    return {
        "question": req.question,
        "mode": "none",
        "top_score": 0.0,
        "sources": [],
        "answer": "not implemented yet",
    }


def run(port: Optional[int] = None) -> None:
    uvicorn.run(app, host="0.0.0.0", port=port or 9000)
