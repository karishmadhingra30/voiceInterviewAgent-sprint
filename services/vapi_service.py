"""Vapi REST API: create and configure voice assistants for PressClub interviews."""

from __future__ import annotations

# stdlib
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

# third party
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)

VAPI_API_URL = "https://api.vapi.ai"


## Pydantic Models


class VapiAssistantConfig(BaseModel):
    """Configuration sent to Vapi API to create an assistant."""

    name: str
    model: dict = Field(default_factory=dict)
    voice: dict = Field(default_factory=dict)
    firstMessage: str = ""
    systemPrompt: str = ""
    stopSpeakingPlan: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class VapiAssistantResponse(BaseModel):
    """Response from Vapi API after creating an assistant."""

    id: str
    name: str
    errors: list[str] = Field(default_factory=list)


def build_system_prompt(briefing_doc: dict) -> str:
    """
    Convert briefing doc into a structured system prompt for the Vapi assistant.
    Order: persona → behavioral rules → strategy → reference materials (lowest priority).
    """
    known_facts = briefing_doc.get("known_facts", []) or []
    questions = briefing_doc.get("questions", []) or []
    known_block = (
        "\n".join(f"- {f}" for f in known_facts) if known_facts else "(none)"
    )
    questions_block = "\n".join(
        f"{i + 1}. {q}" for i, q in enumerate(questions)
    )

    strategy = briefing_doc.get("interview_strategy", "") or ""

    return f"""You are Riley, a senior tech journalist with 10 years covering B2B SaaS and deep tech startups. You are conducting a 30-minute interview with a startup founder to extract newsworthy angles for a PR campaign. Keep your tone warm but persistent.

INTERVIEW CONDUCT (follow these at all times):
- Ask ONE question at a time.
- Never accept vague quantifiers like "a lot" or "many" — always ask for the exact figure or a concrete detail.
- If the founder gives a vague answer, say: "Can you give me the exact number on that?"
- If the founder rambles for more than 30 seconds, say: "That's fascinating — let me focus us on [specific point]"
- Always acknowledge the answer briefly before the next question.
- After you have asked all questions from your question bank, ask: "Is there anything else you think a journalist covering your space would find remarkable?"
- When reading dollar amounts or statistics aloud, always spell them out naturally as a human would speak them. For example: $6 million not $6,000,000. Say "six million dollars" not "dollar sign six million". Say "13,800" as "thirteen thousand eight hundred". Never read punctuation or symbols aloud.
- Never say placeholder text such as "publication name", "[publication]", or similar — if reference materials contain those, rephrase in plain speech (e.g. "outlets that cover your space", "journalists") without inventing or naming a specific publication unless a real name is given in context.

INTERVIEW STRATEGY:
{strategy}

=== REFERENCE MATERIALS ===

WHAT YOU ALREADY KNOW (do not ask about these):
{known_block}

YOUR QUESTION BANK (ask in order, probe deeper on vague answers):
Never accept "a lot" or "many" — always ask for the exact figure.

{questions_block}
"""


def build_assistant_payload(briefing_doc: dict, session_id: str) -> dict:
    """Build the complete Vapi API payload dict (JSON-serializable)."""
    company = briefing_doc.get("company_folder", "your company")
    first_message = (
        f"Hi! I'm Riley from PressClub. "
        f"Thanks so much for joining today. I've done my research on "
        f"{company} and I'm really excited to dig in. "
        f"I have about 12 questions prepared — we should be able to "
        f"cover everything in 30 minutes. Ready to get started?"
    )

    return {
        "name": f"PressClub-{session_id}",
        "model": {
            "provider": "google",
            "model": "gemini-2.0-flash",
            "systemPrompt": build_system_prompt(briefing_doc),
            "temperature": 0.7,
            "maxTokens": 1000
        },
        "voice": {
            "provider": "vapi",
            "voiceId": "Elliot",
        },
        "firstMessage": first_message,
        "stopSpeakingPlan": {
            "numWords": 5,
            "voiceSeconds": 0.3,
            "backoffSeconds": 1.0,
        },
        "metadata": {
            "session_id": session_id,
            "company_folder": briefing_doc.get("company_folder", ""),
        },
    }


async def create_vapi_assistant(briefing_doc: dict, session_id: str) -> str:
    """
    Updates the pre-built Vapi assistant with session-specific 
    system prompt and metadata. Returns assistant_id.
    """
    assistant_id = os.getenv("VAPI_ASSISTANT_ID")
    if not assistant_id:
        raise RuntimeError("VAPI_ASSISTANT_ID not set in .env")
    
    headers = {
        "Authorization": f"Bearer {os.getenv('VAPI_API_KEY')}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": {
            "provider": "google",
            "model": "gemini-2.0-flash",
            "systemPrompt": build_system_prompt(briefing_doc),
            "temperature": 0.7,
            "maxTokens": 1000
        },
        "metadata": {
            "session_id": session_id,
            "company_folder": briefing_doc.get("company_folder", "")
        }
    }
    
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.patch(
            f"{VAPI_API_URL}/assistant/{assistant_id}",
            headers=headers,
            json=payload
        )
    
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Vapi PATCH failed {response.status_code}: {response.text}"
        )
    
    logger.info("[VAPI] Assistant updated id=%s session=%s",
                assistant_id, session_id)
    return assistant_id


async def delete_vapi_assistant(assistant_id: str) -> bool:
    """DELETE assistant; returns True on success. Never raises."""
    headers = {
        "Authorization": f"Bearer {os.getenv('VAPI_API_KEY')}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{VAPI_API_URL}/assistant/{assistant_id}",
                headers=headers,
                timeout=30,
            )
    except Exception as exc:
        logger.warning(
            "[WARNING] Vapi DELETE assistant failed id=%s: %s",
            assistant_id,
            exc,
        )
        return False

    if response.status_code in (200, 204):
        logger.info("[VAPI] Assistant deleted id=%s", assistant_id)
        return True

    logger.warning(
        "[WARNING] Vapi DELETE assistant failed id=%s status=%s body=%s",
        assistant_id,
        response.status_code,
        response.text,
    )
    return False


async def get_vapi_assistant(assistant_id: str) -> dict:
    """GET assistant details for debugging; returns {} on failure."""
    headers = {
        "Authorization": f"Bearer {os.getenv('VAPI_API_KEY')}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{VAPI_API_URL}/assistant/{assistant_id}",
                headers=headers,
                timeout=30,
            )
    except Exception as exc:
        logger.error("[ERROR] Vapi GET assistant failed id=%s: %s", assistant_id, exc)
        return {}

    if response.status_code != 200:
        logger.error(
            "[ERROR] Vapi GET assistant failed id=%s status=%s body=%s",
            assistant_id,
            response.status_code,
            response.text,
        )
        return {}

    try:
        return response.json()
    except json.JSONDecodeError:
        return {}


async def main() -> None:
    outputs_dir = Path("data/outputs")
    test_briefing = {
        "session_id": "test123",
        "company_folder": "TestCo",
        "questions": [
            "What is your current ARR?",
            "Who are your top 3 customers?",
            "What makes you different from competitors?",
        ],
        "signal_targets": ["traction", "traction", "product"],
        "interview_strategy": "Focus on traction signals.",
        "known_facts": ["Company is in B2B SaaS space"],
        "errors": [],
    }

    briefing_doc = test_briefing
    if outputs_dir.is_dir():
        briefings = sorted(outputs_dir.glob("*_briefing.json"))
        if briefings:
            briefing_path = briefings[0]
            briefing_doc = json.loads(briefing_path.read_text(encoding="utf-8"))
            print(f"[OK] Loaded briefing from {briefing_path}")

    session_id = "test_" + str(uuid.uuid4())[:6]

    print(f"[OK] Creating Vapi assistant for session {session_id}")
    assistant_id = await create_vapi_assistant(briefing_doc, session_id)
    print(f"[OK] Assistant created: {assistant_id}")

    details = await get_vapi_assistant(assistant_id)
    print(f"[OK] Assistant name: {details.get('name')}")
    print(f"[OK] Metadata: {details.get('metadata')}")

    deleted = await delete_vapi_assistant(assistant_id)
    print(f"[OK] Cleaned up: {deleted}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
