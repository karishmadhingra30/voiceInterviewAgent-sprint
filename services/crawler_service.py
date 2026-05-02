"""Scrape LinkedIn profile pages via Oxylabs and structure fields with Claude for PR signals."""

import asyncio
import json
import logging
import os
import re
import sys
from json import JSONDecodeError
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

BRIGHTDATA_TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"
BRIGHTDATA_SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot"
BRIGHTDATA_POLL_INTERVAL = 3  # seconds between status checks
BRIGHTDATA_MAX_POLLS = 20  # max attempts before timeout (60 seconds total)


class LinkedInProfile(BaseModel):
    """Structured LinkedIn extraction; ``url``, ``raw_text``, and ``errors`` are set by this module."""

    url: str
    name: str
    current_role: str
    current_company: str
    location: str
    summary: str
    experience: list[str]
    education: list[str]
    notable_signals: list[str]
    raw_text: str
    errors: list[str]


def _anthropic_message_text(message: object) -> str:
    """Concatenate text blocks from an Anthropic Messages API response ``message``."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


def _parse_linkedin_json(response_text: str, url: str, raw_text: str) -> LinkedInProfile:
    """Extract the first JSON object from ``response_text``, inject URL fields, validate."""
    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in response")

    data = json.loads(match.group())
    data["url"] = url
    data["raw_text"] = raw_text
    data["errors"] = []
    return LinkedInProfile(**data)


async def _call_claude_analyze_linkedin(
    client: AsyncAnthropic,
    url: str,
    raw_text: str,
    system_prompt: str,
) -> LinkedInProfile:
    """Single Claude request plus JSON parse; raises on API or parse failure."""
    user_message = f"LinkedIn URL: {url}\n\nPROFILE DATA:\n{raw_text}"
    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = _anthropic_message_text(message)
    return _parse_linkedin_json(response_text, url, raw_text)

def extract_profile_from_context(context_text: str, url: str) -> LinkedInProfile:
    """
    Fallback when LinkedIn scraping fails.
    Passes context text as summary so research_agent can still
    extract founder info from it via Claude.
    Returns a LinkedInProfile with errors flagged so research_agent
    knows to rely primarily on the transcript.
    NOTE: Synchronous. Safe to call directly — no network I/O.
    """
    return LinkedInProfile(
        url=url,
        name="",
        current_role="",
        current_company="",
        location="",
        summary=context_text.strip(),
        experience=[],
        education=[],
        notable_signals=[],
        raw_text=context_text,
        errors=["LinkedIn scraping unavailable — using context fallback"]
    )

async def scrape_linkedin_raw(url: str) -> str:
    """Trigger Bright Data dataset job and poll snapshot; return up to 6000 chars or ``\"\"``."""
    api_key = os.getenv("BRIGHTDATA_API_KEY")
    dataset_id = os.getenv("BRIGHTDATA_DATASET_ID")
    if not api_key or not dataset_id:
        logger.error(
            "[ERROR] scrape_linkedin_raw missing BRIGHTDATA_API_KEY or BRIGHTDATA_DATASET_ID",
        )
        return ""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    params_trigger = {
        "dataset_id": dataset_id,
        "include_errors": "true",
    }
    body = [{"url": url}]

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                BRIGHTDATA_TRIGGER_URL,
                headers=headers,
                params=params_trigger,
                json=body,
            )
            response.raise_for_status()
            trigger_payload = response.json()
            snapshot_id = trigger_payload.get("snapshot_id")
            if not snapshot_id:
                logger.error("[ERROR] scrape_linkedin_raw no snapshot_id in trigger response")
                return ""
            logger.info("[OK] Bright Data snapshot_id received: %s", snapshot_id)

            poll_url = f"{BRIGHTDATA_SNAPSHOT_URL}/{snapshot_id}"
            poll_params = {"format": "json"}

            for attempt in range(1, BRIGHTDATA_MAX_POLLS + 1):
                if attempt > 1:
                    await asyncio.sleep(BRIGHTDATA_POLL_INTERVAL)

                snap_response = await client.get(
                    poll_url,
                    headers=headers,
                    params=poll_params,
                )
                snap_response.raise_for_status()
                payload = snap_response.json()

                status: str | None = None
                if isinstance(payload, dict):
                    status = payload.get("status")
                elif isinstance(payload, list):
                    status = "ready"

                logger.info(
                    "[OK] Bright Data poll attempt=%s status=%s",
                    attempt,
                    status,
                )

                if status == "failed":
                    logger.error("[ERROR] scrape_linkedin_raw Bright Data snapshot failed")
                    return ""

                if status == "ready":
                    results: list = []
                    if isinstance(payload, list):
                        results = payload
                    elif isinstance(payload, dict):
                        for key in ("data", "results", "snapshot", "items"):
                            val = payload.get(key)
                            if isinstance(val, list):
                                results = val
                                break
                        if not results:
                            results = [payload]
                    profile_data = results[0] if results else {}
                    text = str(profile_data)[:6000]
                    logger.info(
                        "[OK] scrape_linkedin_raw url=%s content_length=%s",
                        url,
                        len(text),
                    )
                    return text

                if status != "running":
                    continue

            logger.error(
                "[ERROR] Bright Data polling timed out after %s attempts",
                BRIGHTDATA_MAX_POLLS,
            )
            return ""
    except Exception as exc:
        logger.error("[ERROR] scrape_linkedin_raw failed for %s: %s", url, exc)
        return ""


async def analyze_linkedin(url: str, raw_text: str) -> LinkedInProfile:
    """Run Claude with exponential backoff (2s, 4s); on failure return ``LinkedInProfile`` with errors."""
    system_prompt = (PROMPTS_DIR / "linkedin_analyzer.txt").read_text()
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    last_exception: Exception | None = None

    for attempt in range(1, 4):
        logger.info("[LLM-CALL] url=%s attempt=%s", url, attempt)
        try:
            return await _call_claude_analyze_linkedin(
                client, url, raw_text, system_prompt
            )
        except (ValidationError, JSONDecodeError, ValueError) as exc:
            last_exception = exc
        except Exception as exc:
            last_exception = exc
        if attempt < 3:
            await asyncio.sleep(2**attempt)

    logger.error("[ERROR] analyze_linkedin failed after 3 attempts: %s", last_exception)
    return LinkedInProfile(
        url=url,
        name="",
        current_role="",
        current_company="",
        location="",
        summary="",
        experience=[],
        education=[],
        notable_signals=[],
        raw_text=raw_text,
        errors=[f"Claude analysis failed after 3 attempts: {str(last_exception)}"],
    )


async def scrape_linkedin(url: str, context_fallback: str = "") -> LinkedInProfile:
    """
    Main async entry point for LinkedIn profile data.

    Fallback chain:
      1. Oxylabs scrape → Claude analysis (full profile)
      2. Context text fallback (partial — research_agent uses transcript as primary)
      3. Empty profile with error (pipeline continues, transcript is sole source)

    Args:
        url: LinkedIn profile URL from Context.txt
        context_fallback: raw Context.txt content passed by research_agent
                          used if scraping fails and context has useful info
    """
    # Guard — no URL provided
    if not url:
        logger.warning("[WARNING] scrape_linkedin called with empty URL")
        return LinkedInProfile(
            url="", name="", current_role="", current_company="",
            location="", summary="", experience=[], education=[],
            notable_signals=[], raw_text="",
            errors=["No URL provided"]
        )

    logger.info("[OK] Starting LinkedIn scrape for %s", url)

    # Attempt 1 — Oxylabs scrape
    raw_text = await scrape_linkedin_raw(url)

    if raw_text and "page not found" not in raw_text.lower():
        # Scrape succeeded — run Claude analysis
        return await analyze_linkedin(url, raw_text)

    # Scrape failed or returned error page
    logger.warning("[WARNING] LinkedIn scraping failed for %s", url)

    # Attempt 2 — context fallback
    if context_fallback and len(context_fallback.strip()) > 20:
        logger.warning("[WARNING] Falling back to context text for %s", url)
        return extract_profile_from_context(context_fallback, url)

    # Attempt 3 — both failed, return empty but never crash
    # Research agent will rely solely on transcript for this company
    logger.warning("[WARNING] LinkedIn and context both unavailable for %s", url)
    return LinkedInProfile(
        url=url, name="", current_role="", current_company="",
        location="", summary="", experience=[], education=[],
        notable_signals=[], raw_text="",
        errors=[
            "LinkedIn scraping failed and no context available — "
            "transcript will be primary signal source"
        ]
    )


async def main() -> None:
    """CLI: scrape and analyze one LinkedIn URL (default Ruthe Farmer profile)."""
    url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "https://www.linkedin.com/in/ruthefarmer/"
    )
    print(f"[OK] Scraping LinkedIn: {url}")
    result = await scrape_linkedin(url)
    print(f"\nname:             {result.name}")
    print(f"current_role:     {result.current_role}")
    print(f"current_company:  {result.current_company}")
    print(f"location:         {result.location}")
    print(f"experience:       {result.experience}")
    print(f"notable_signals:  {result.notable_signals}")
    print(f"raw_text length:  {len(result.raw_text)} chars")
    if result.errors:
        print(f"[WARNING] errors: {result.errors}")
    else:
        print("[OK] No errors")


if __name__ == "__main__":
    asyncio.run(main())
