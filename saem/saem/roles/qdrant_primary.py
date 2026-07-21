"""qdrant_primary: run the Qdrant binary this node owns.

TODO: port the actual startup logic used on camp-57 (pinned to v1.12.6 for
GLIBC 2.35 compatibility, binary at ~/qdrant/qdrant) in place of the
placeholder subprocess call below.
"""
from __future__ import annotations

import subprocess
from typing import Optional

QDRANT_BINARY = "/root/qdrant/qdrant"


def run(port: Optional[int] = None) -> None:
    subprocess.run([QDRANT_BINARY], check=True)
