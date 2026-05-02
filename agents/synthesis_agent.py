"""Post-call synthesis: turns interview transcript into structured notes (stub for sprint)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUTS_DIR = Path("data/outputs")


async def synthesize(session_id: str, transcript: str) -> None:
    """
    Write a minimal markdown report to data/outputs/{session_id}_output.md.

    Full implementation will call Claude with prompts/synthesizer.txt.
    """
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUTS_DIR / f"{session_id}_output.md"
    body = (
        f"# Interview notes (stub)\n\n"
        f"**Session:** `{session_id}`\n\n"
        f"## Transcript\n\n{transcript[:8000]}\n"
    )
    await asyncio.to_thread(path.write_text, body, encoding="utf-8")
    logger.info("[OK] Synthesis stub wrote %s", path)
