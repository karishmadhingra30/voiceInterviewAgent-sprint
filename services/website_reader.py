"""Fetch company websites and extract structured PR-oriented fields via Claude."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from json import JSONDecodeError
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class WebsiteContent(BaseModel):
    """Structured website extraction result; ``raw_text`` is always filled by this module."""

    url: str
    company_name: str
    product_description: str
    key_claims: list[str]
    metrics_found: list[str]
    customers_mentioned: list[str]
    funding_mentions: list[str]
    raw_text: str = Field(default="")
    errors: list[str]


def _anthropic_message_text(message: object) -> str:
    """Return concatenated text from an Anthropic Messages API ``message`` object."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


async def fetch_website_text(url: str) -> str:
    """Download ``url`` with httpx (async), strip markup, return first 8000 chars or ``\"\"``.

    On network/HTML errors logs ``[ERROR]`` and returns empty string.
    """
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=15,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:8000]
    except Exception as exc:
        logger.error("[ERROR] fetch_website_text failed for %s: %s", url, exc)
        return ""


def _parse_llm_json(response_text: str, raw_text: str) -> WebsiteContent:
    """Extract JSON object from ``response_text``, validate, and set ``raw_text`` from Python."""
    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")
    result = WebsiteContent.model_validate_json(match.group())
    result.raw_text = raw_text
    return result


async def _call_claude_analyze(
    client: AsyncAnthropic,
    url: str,
    raw_text: str,
    system_prompt: str,
) -> WebsiteContent:
    """Single Claude request + JSON parse; raises on API or parse failure."""
    user_message = f"URL: {url}\n\nCONTENT:\n{raw_text}"
    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    response_text = _anthropic_message_text(message)
    return _parse_llm_json(response_text, raw_text)


async def analyze_website(url: str, raw_text: str) -> WebsiteContent:
    """Run Claude with retries (2s, 4s backoff); on total failure return ``WebsiteContent`` with errors.

    Does not raise after exhausted retries; pipeline callers always get a model instance.
    """
    system_prompt = (PROMPTS_DIR / "website_reader.txt").read_text()
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    last_exception: Exception | None = None

    for attempt in range(1, 4):
        logger.info("[LLM-CALL] url=%s attempt=%s", url, attempt)
        try:
            return await _call_claude_analyze(client, url, raw_text, system_prompt)
        except (ValidationError, JSONDecodeError, ValueError) as exc:
            last_exception = exc
        except Exception as exc:
            last_exception = exc
        if attempt < 3:
            await asyncio.sleep(2**attempt)

    logger.error("[ERROR] analyze_website failed after 3 attempts: %s", last_exception)
    return WebsiteContent(
        url=url,
        company_name="",
        product_description="",
        key_claims=[],
        metrics_found=[],
        customers_mentioned=[],
        funding_mentions=[],
        raw_text=raw_text,
        errors=[f"Claude analysis failed after 3 attempts: {str(last_exception)}"],
    )


async def read_website(url: str) -> WebsiteContent:
    """Fetch ``url``, analyze with Claude, return ``WebsiteContent`` (never raises for LLM failure)."""
    if url == "":
        return WebsiteContent(
            url="",
            company_name="",
            product_description="",
            key_claims=[],
            metrics_found=[],
            customers_mentioned=[],
            funding_mentions=[],
            raw_text="",
            errors=["No URL provided"],
        )
    raw_text = await fetch_website_text(url)
    if not raw_text:
        return WebsiteContent(
            url=url,
            company_name="",
            product_description="",
            key_claims=[],
            metrics_found=[],
            customers_mentioned=[],
            funding_mentions=[],
            raw_text="",
            errors=["Failed to fetch website content"],
        )
    return await analyze_website(url, raw_text)


async def main() -> None:
    """CLI: fetch and analyze one URL (default https://www.lastmile-ed.org)."""
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.lastmile-ed.org"
    print(f"[OK] Fetching: {url}")
    result = await read_website(url)
    print(f"\ncompany_name:        {result.company_name}")
    print(f"product_description: {result.product_description}")
    print(f"key_claims:          {result.key_claims}")
    print(f"metrics_found:       {result.metrics_found}")
    print(f"customers_mentioned: {result.customers_mentioned}")
    print(f"funding_mentions:    {result.funding_mentions}")
    print(f"raw_text length:     {len(result.raw_text)} chars")
    if result.errors:
        print(f"[WARNING] errors: {result.errors}")
    else:
        print(f"[OK] No errors")


if __name__ == "__main__":
    asyncio.run(main())
