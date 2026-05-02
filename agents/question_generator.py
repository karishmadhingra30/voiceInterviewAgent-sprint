"""Generate interview questions from company context for newsworthy PR angles.

Loads a journalist system prompt, sends transcript + context + LinkedIn + website
to Claude, and returns a structured ``QuestionSet`` for the optimizer to refine.
"""

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic import ValidationError
import os, asyncio, sys, logging, json, re
from pathlib import Path
from json import JSONDecodeError

load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_FALLBACK_PROMPT = """You are a senior tech journalist interviewing a startup founder.
Generate 10 interview questions to find newsworthy angles.
Cover all 5 signal types: funding, product, traction,
founder background, and industry insights.
Ask for specific numbers and quotable statements.
Respond ONLY with valid JSON:
{
  "questions": [],
  "signal_targets": []
}"""


class QuestionSet(BaseModel):
    """LLM output: interview questions aligned to PR signal types and run metadata."""

    questions: list[str]
    signal_targets: list[str]
    iteration: int
    prompt_version: str
    errors: list[str] = Field(default_factory=list)


def _anthropic_message_text(message: object) -> str:
    """Concatenate text blocks from an Anthropic Messages API ``message``."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


def load_initial_prompt() -> str:
    """Load ``prompts/question_generator.txt``; if missing, return a minimal JSON prompt."""
    path = PROMPTS_DIR / "question_generator.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    logger.warning("[WARNING] Missing %s; using built-in minimal prompt", path)
    return _FALLBACK_PROMPT


def _build_user_message(
    transcript: str,
    context: str,
    linkedin_raw: str,
    website_raw: str,
) -> str:
    """Assemble the user message with all research sources."""
    return f"""
SALES TRANSCRIPT:
{transcript}

CONTEXT / BACKGROUND:
{context}

LINKEDIN DATA:
{linkedin_raw if linkedin_raw else "Not available"}

WEBSITE DATA:
{website_raw if website_raw else "Not available"}

Generate interview questions now.
"""


def _questionset_from_response_text(
    response_text: str,
    iteration: int,
    prompt_version: str,
) -> QuestionSet:
    """Extract JSON from ``response_text``, inject metadata, validate as ``QuestionSet``."""
    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")
    data = json.loads(match.group())
    data["iteration"] = iteration
    data["prompt_version"] = prompt_version
    data["errors"] = []
    return QuestionSet(**data)


async def generate_questions(
    transcript: str,
    context: str,
    linkedin_raw: str,
    website_raw: str,
    current_prompt: str,
    iteration: int = 0,
) -> QuestionSet:
    """Call Claude with retries (2s / 4s backoff); return ``QuestionSet`` or one with ``errors`` set."""
    prompt_version = str(hash(current_prompt))[:8]
    user_message = _build_user_message(transcript, context, linkedin_raw, website_raw)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        msg = "ANTHROPIC_API_KEY is not set"
        logger.error("[ERROR] %s", msg)
        return QuestionSet(
            questions=[],
            signal_targets=[],
            iteration=iteration,
            prompt_version=prompt_version,
            errors=[msg],
        )

    client = AsyncAnthropic(api_key=api_key)
    last_error: str | None = None
    backoff_seconds = [2, 4]

    for attempt in range(3):
        logger.info(
            "[LLM-CALL] model=claude-sonnet-4-5 attempt=%s/3 system_len=%s user_len=%s",
            attempt + 1,
            len(current_prompt),
            len(user_message),
        )
        try:
            message = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1500,
                system=current_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            response_text = _anthropic_message_text(message)
            return _questionset_from_response_text(
                response_text, iteration, prompt_version
            )
        except ValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "[WARNING] generate_questions attempt %s ValidationError: %s",
                attempt + 1,
                exc,
            )
        except (JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            logger.warning(
                "[WARNING] generate_questions attempt %s parse error: %s",
                attempt + 1,
                exc,
            )
        except Exception as exc:
            last_error = str(exc)
            logger.exception(
                "[ERROR] generate_questions attempt %s failed",
                attempt + 1,
            )
        if attempt < 2:
            await asyncio.sleep(backoff_seconds[attempt])

    err_list = [f"Failed after 3 attempts: {last_error or 'unknown'}"]
    logger.error("[ERROR] generate_questions exhausted retries: %s", last_error)
    return QuestionSet(
        questions=[],
        signal_targets=[],
        iteration=iteration,
        prompt_version=prompt_version,
        errors=err_list,
    )


async def main() -> None:
    """Smoke-test: LastMile fixture, LinkedIn + website fetch, question generation."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from services.input_parser import extract_urls, parse_input_folder
    from services.crawler_service import scrape_linkedin
    from services.website_reader import read_website

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parsed = parse_input_folder("tests/fixtures/LastMile")
    urls = extract_urls(parsed.context)

    linkedin = await scrape_linkedin(
        urls["linkedin_urls"][0] if urls["linkedin_urls"] else "",
        context_fallback=parsed.context,
    )
    website = await read_website(
        urls["website_urls"][0] if urls["website_urls"] else ""
    )

    prompt = load_initial_prompt()
    result = await generate_questions(
        transcript=parsed.transcript,
        context=parsed.context,
        linkedin_raw=linkedin.raw_text,
        website_raw=website.raw_text,
        current_prompt=prompt,
        iteration=0,
    )
    print(f"[OK] Generated {len(result.questions)} questions")
    for i, q in enumerate(result.questions, 1):
        target = result.signal_targets[i - 1] if i <= len(result.signal_targets) else "?"
        print(f"  {i}. [{target.upper()}] {q}")
    if result.errors:
        print(f"[WARNING] {result.errors}")


if __name__ == "__main__":
    asyncio.run(main())
