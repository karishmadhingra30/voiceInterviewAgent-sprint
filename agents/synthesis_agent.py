"""
Phase 3 — Synthesis Agent

Reads the full Vapi interview transcript against the briefing_doc.
Produces output.md: scorecard + structured notes for Weida's pipeline.
"""

import asyncio
import json
import logging
import os
import traceback
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SYNTHESIZER_PROMPT_PATH = "prompts/synthesizer.txt"

SIGNAL_TYPES = [
    "funding",
    "product_world_first",
    "product_world_best",
    "traction_revenue",
    "traction_growth",
    "traction_quality",
    "founder_uniqueness",
    "insight_contrarian",
]


async def run_synthesis_agent(transcript: str, briefing_doc: dict) -> str:
    """
    Synthesize interview transcript + briefing doc into output.md string.
    """
    logger.info("[OK] Synthesis agent starting")
    try:
        client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        system = _load_prompt()

        company = briefing_doc.get("company_name", "the company")
        founder = briefing_doc.get("founder_name", "the founder")
        angles = briefing_doc.get("hypothesized_angles", [])
        known_facts = briefing_doc.get("known_facts", [])

        prompt = f"""You are synthesizing the results of an AI-conducted interview for PressClub.

COMPANY: {company}
FOUNDER: {founder}

BRIEFING DOC — HYPOTHESIZED ANGLES:
{json.dumps(angles, indent=2)}

BRIEFING DOC — KNOWN FACTS (do not re-surface as new findings):
{json.dumps(known_facts, indent=2)}

INTERVIEW TRANSCRIPT:
{transcript}

Produce output.md with these exact sections:

## Interview Summary
One paragraph overview of what was covered.

## Newsworthy Scorecard
For each signal type below, mark it as `confirmed`, `unconfirmed`, or `needs follow-up`.
Include a one-line evidence quote from the transcript (or "no evidence found").

Signals: {', '.join(SIGNAL_TYPES)}

## Structured Interview Notes
Key findings organized by signal type. Only include signals with evidence.

## Recommended Pitch Angle
The 1-2 strongest signals worth pursuing, with specific data points and recommended framing.

## Transcript (Timestamped)
The full interview transcript as provided.

IMPORTANT: Do not fabricate facts not present in the transcript. Mark anything uncertain as `needs follow-up`."""

        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )

        output = response.content[0].text

        logger.info("[OK] Claude response received, saving output")

        output_path = Path("data/outputs") / f"{briefing_doc.get('session_id', 'unknown')}_output.md"
        await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(output_path.write_text, output, encoding="utf-8")
        logger.info("[OK] Synthesis saved to %s", output_path)

        return output
    except Exception:
        logger.error(
            "[ERROR] Synthesis agent failed\n%s",
            traceback.format_exc(),
        )
        raise



def _load_prompt() -> str:
    try:
        return open(SYNTHESIZER_PROMPT_PATH).read()
    except FileNotFoundError:
        return "You are a synthesis agent for PressClub. Produce accurate, pipeline-ready interview output."