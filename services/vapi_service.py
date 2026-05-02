"""Vapi REST API: assistant creation and configuration (stub for sprint)."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def create_vapi_assistant(briefing_doc: dict, session_id: str) -> str:
    """
    Create a Vapi assistant loaded with the briefing doc.

    Production: POST to Vapi with model config, system prompt from briefing_doc,
    and assistant/call metadata including session_id for webhooks.

    Returns:
        Assistant id for the Vapi browser SDK.
    """
    fake_id = f"stub-assistant-{session_id}"
    logger.info(
        "[OK] Vapi assistant stub session=%s assistant_id=%s (attach metadata in prod)",
        session_id,
        fake_id,
    )
    return fake_id
