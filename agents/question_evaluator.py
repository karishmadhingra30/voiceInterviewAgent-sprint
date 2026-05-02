"""Score generated interview questions against a human ground-truth interview.

Loads ``prompts/evaluator.txt``, calls Claude as judge, and returns an
``EvaluationResult`` with per-dimension scores and optimizer-facing feedback.
"""

import asyncio
import json
import logging
import os
import re
import sys
from json import JSONDecodeError
from pathlib import Path

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

from agents.question_generator import QuestionSet

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


class DimensionScore(BaseModel):
    """One rubric dimension with score, feedback, and concrete examples."""

    name: str
    score: float
    feedback: str
    examples: list[str]


class EvaluationResult(BaseModel):
    """Structured judge output for the question-generation optimizer."""

    total_score: float
    passed: bool
    dimensions: list[DimensionScore]
    strengths: list[str]
    weaknesses: list[str]
    prompt_suggestions: list[str]
    iteration: int
    errors: list[str] = Field(default_factory=list)


def _anthropic_message_text(message: object) -> str:
    """Concatenate text blocks from an Anthropic Messages API ``message``."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


def load_evaluator_prompt() -> str:
    """Load ``prompts/evaluator.txt`` for the judge system prompt."""
    path = PROMPTS_DIR / "evaluator.txt"
    return path.read_text(encoding="utf-8")


def _failed_result(iteration: int, errors: list[str]) -> EvaluationResult:
    """Return a zero-score result when evaluation cannot complete."""
    return EvaluationResult(
        total_score=0.0,
        passed=False,
        dimensions=[],
        strengths=[],
        weaknesses=[],
        prompt_suggestions=[],
        iteration=iteration,
        errors=errors,
    )


def _result_from_response_text(response_text: str, iteration: int) -> EvaluationResult:
    """Extract JSON from ``response_text``, inject metadata, validate as ``EvaluationResult``."""
    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")
    data = json.loads(match.group())
    data["iteration"] = iteration
    data["errors"] = []
    data["passed"] = float(data.get("total_score", 0.0)) >= 0.80
    result = EvaluationResult(**data)
    result.passed = result.total_score >= 0.80
    return result


async def evaluate_questions(
    question_set: QuestionSet,
    ground_truth_transcript: str,
    iteration: int = 0,
) -> EvaluationResult:
    """Call Claude to score ``question_set`` vs ground truth; retry with backoff.

    Uses 30s sleep after ``anthropic.RateLimitError``, otherwise 2s then 4s between
    attempts. Returns ``EvaluationResult`` with rubric dimensions and suggestions,
    or a zero-score result with ``errors`` if the API key is missing or all
    attempts fail.
    """
    system_prompt = load_evaluator_prompt()
    user_message = f"""
GENERATED QUESTIONS TO EVALUATE:
{json.dumps(question_set.questions, indent=2)}

SIGNAL TARGETS CLAIMED:
{json.dumps(question_set.signal_targets, indent=2)}

GROUND TRUTH — ACTUAL HUMAN INTERVIEW (2nd Meeting):
{ground_truth_transcript[:8000]}

Evaluate the generated questions against the rubric.
"""

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        msg = "ANTHROPIC_API_KEY is not set"
        logger.error("[ERROR] %s", msg)
        return _failed_result(iteration, [msg])

    client = AsyncAnthropic(api_key=api_key)
    last_error: str | None = None
    backoff_seconds = [2, 4]

    for attempt in range(3):
        logger.info(
            "[LLM-CALL] model=claude-sonnet-4-5 attempt=%s/3 system_len=%s user_len=%s",
            attempt + 1,
            len(system_prompt),
            len(user_message),
        )
        try:
            message = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            response_text = _anthropic_message_text(message)
            return _result_from_response_text(response_text, iteration)
        except ValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "[WARNING] evaluate_questions attempt %s ValidationError: %s",
                attempt + 1,
                exc,
            )
        except (JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            logger.warning(
                "[WARNING] evaluate_questions attempt %s parse error: %s",
                attempt + 1,
                exc,
            )
        except anthropic.RateLimitError as exc:
            last_error = str(exc)
            logger.warning(
                "[WARNING] evaluate_questions attempt %s RateLimitError: %s",
                attempt + 1,
                exc,
            )
            if attempt < 2:
                logger.info("[WARNING] Waiting 30s for rate limit before retry")
                await asyncio.sleep(30)
            continue
        except Exception as exc:
            last_error = str(exc)
            logger.exception(
                "[ERROR] evaluate_questions attempt %s failed",
                attempt + 1,
            )
        if attempt < 2:
            await asyncio.sleep(backoff_seconds[attempt])

    err_list = [f"Failed after 3 attempts: {last_error or 'unknown'}"]
    logger.error("[ERROR] evaluate_questions exhausted retries: %s", last_error)
    return _failed_result(iteration, err_list)


async def main() -> None:
    """End-to-end smoke test: LastMile fixture → generate questions → evaluate."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from agents.question_generator import generate_questions, load_initial_prompt
    from services.crawler_service import scrape_linkedin
    from services.input_parser import extract_urls, load_ground_truth, parse_input_folder
    from services.website_reader import read_website

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parsed = parse_input_folder("tests/fixtures/LastMile")
    urls = extract_urls(parsed.context)
    ground_truth = load_ground_truth("tests/fixtures/LastMile")

    linkedin = await scrape_linkedin(
        urls["linkedin_urls"][0] if urls["linkedin_urls"] else "",
        context_fallback=parsed.context,
    )
    website = await read_website(
        urls["website_urls"][0] if urls["website_urls"] else ""
    )
    prompt = load_initial_prompt()
    question_set = await generate_questions(
        transcript=parsed.transcript,
        context=parsed.context,
        linkedin_raw=linkedin.raw_text,
        website_raw=website.raw_text,
        current_prompt=prompt,
        iteration=0,
    )
    print("[OK] Waiting 20s for rate limit window to reset...")
    await asyncio.sleep(20)
    result = await evaluate_questions(question_set, ground_truth, 0)

    print(f"\n{'='*50}")
    print(f"EVALUATION SCORE: {result.total_score:.2f}")
    print(f"PASSED: {result.passed}")
    print(f"{'='*50}")
    for dim in result.dimensions:
        print(f"  [{dim.score:.2f}] {dim.name}: {dim.feedback}")
    print(f"\nSTRENGTHS: {result.strengths}")
    print(f"WEAKNESSES: {result.weaknesses}")
    print(f"SUGGESTIONS: {result.prompt_suggestions}")


if __name__ == "__main__":
    asyncio.run(main())
