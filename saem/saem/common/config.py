"""Shared config knobs, overridable via environment variables.

Keeping these here (instead of hardcoded per-script, as the original
nohup scripts did) is what lets one role module run correctly on any
node without editing code per-VM.
"""
from __future__ import annotations

import os

AGENT_PORT = int(os.environ.get("SAEM_AGENT_PORT", "9999"))
QDRANT_URL = os.environ.get("SAEM_QDRANT_URL", "http://127.0.0.1:6333")
EMBEDDING_MODEL = os.environ.get(
    "SAEM_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2"
)
GATEWAY_SEARCH_THRESHOLD = float(os.environ.get("SAEM_SEARCH_THRESHOLD", "0.40"))


def get_llm_backend() -> tuple[str, str]:
    """(url, model) of whichever dure GPU-cluster backend head last pushed to
    this node via `saem head register-backend`. Falls back to the env vars
    below (useful for local testing, or the very first node before head has
    registered anything) — but the registry is the source of truth so a new
    GPU head (camp1, a 235B replacement, ...) can be swapped in fleet-wide
    with one `saem head register-backend` call, no code/config edits here.
    """
    from saem.common.state import read_backend

    backend = read_backend()
    if backend:
        return backend["url"], backend["model"]
    return (
        os.environ.get("SAEM_VLLM_URL", "http://192.168.0.228:8000"),
        os.environ.get("SAEM_VLLM_MODEL_NAME", "qwen3-235b"),
    )
