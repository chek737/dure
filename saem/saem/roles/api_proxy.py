"""api_proxy: authenticated external-facing proxy in front of the vLLM head.

TODO: port camp-73's api_proxy.py (Bearer key check against api_keys.txt,
httpx streaming relay to get_llm_backend()'s url, GET /health
unauthenticated, every request/response appended to api_log.jsonl for the
future fine-tuning pipeline). Note this role is the one exception that must
be reachable from outside the internal network, so its port stays bound to
the 22/443-only security group (hence the default port 443) — every other
role only ever talks over 192.168.0.x.
"""
from __future__ import annotations

from typing import Optional

import uvicorn
from fastapi import FastAPI

from saem.common.config import get_llm_backend

app = FastAPI(title="saem-api-proxy")


@app.get("/health")
def health():
    return {"status": "ok"}


def run(port: Optional[int] = None) -> None:
    uvicorn.run(app, host="0.0.0.0", port=port or 443)
