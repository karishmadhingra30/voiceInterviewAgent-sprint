# DEPRECATED — superseded by agents/research_agent.py (DSPy PRResearchAgent).
# DSPy BootstrapFewShot handles optimization natively; this manual generate→evaluate→rewrite
# loop is no longer part of the active pipeline. Kept for reference only.

"""Self-improving loop: generate questions, evaluate, rewrite prompt until quality threshold.

Runs bounded iterations, persists per-iteration artifacts and the best prompt to disk.
"""

import os, asyncio, logging, json
from pathlib import Path

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from agents.question_evaluator import evaluate_questions, EvaluationResult
from agents.question_generator import generate_questions, load_initial_prompt, QuestionSet

load_dotenv()

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

SCORE_THRESHOLD = 0.80
MAX_ITERATIONS = 10
NO_IMPROVEMENT_LIMIT = 3
RESULTS_DIR = Path("tests/optimization_results")


class OptimizationResult(BaseModel):
    """Outcome of a full prompt optimization run."""

    best_score: float
    best_prompt: str
    iterations_run: int
    converged: bool
    score_history: list[float]
    final_questions: list[str]
    errors: list[str]


def _anthropic_message_text(message: object) -> str:
    """Concatenate text blocks from an Anthropic Messages API ``message``."""
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


def _load_prompt_improver_system() -> str:
    """Load ``prompts/prompt_improver.txt`` for the rewriter system prompt."""
    path = PROMPTS_DIR / "prompt_improver.txt"
    return path.read_text(encoding="utf-8")


async def improve_prompt(
    current_prompt: str,
    evaluation: EvaluationResult,
    iteration: int,
) -> str:
    """Ask Claude to rewrite the question-generator prompt using evaluation feedback.

    Returns the new prompt text, or ``current_prompt`` unchanged on failure.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("[ERROR] improve_prompt: ANTHROPIC_API_KEY is not set")
        return current_prompt

    system_prompt = _load_prompt_improver_system()
    user_message = f"""
CURRENT PROMPT:
{current_prompt}

EVALUATION SCORE: {evaluation.total_score:.2f}

WEAKNESSES TO FIX:
{json.dumps(evaluation.weaknesses, indent=2)}

SPECIFIC SUGGESTIONS:
{json.dumps(evaluation.prompt_suggestions, indent=2)}

DIMENSION FEEDBACK:
{json.dumps([{"name": d.name, "score": d.score, "feedback": d.feedback} for d in evaluation.dimensions], indent=2)}

Rewrite the prompt to fix these weaknesses.
"""
    client = AsyncAnthropic(api_key=api_key)
    try:
        logger.info(
            "[LLM-CALL] improve_prompt iteration=%s model=claude-sonnet-4-5 system_len=%s user_len=%s",
            iteration + 1,
            len(system_prompt),
            len(user_message),
        )
        message = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        response_text = _anthropic_message_text(message)
        return response_text.strip()
    except Exception as exc:
        logger.exception("[ERROR] improve_prompt iteration=%s failed: %s", iteration + 1, exc)
        return current_prompt


def save_iteration_result(
    iteration: int,
    question_set: QuestionSet,
    evaluation: EvaluationResult,
    prompt: str,
) -> None:
    """Persist one iteration’s questions, scores, and prompt to JSON under ``RESULTS_DIR``."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "iteration": iteration,
        "score": evaluation.total_score,
        "passed": evaluation.passed,
        "questions": question_set.questions,
        "signal_targets": question_set.signal_targets,
        "weaknesses": evaluation.weaknesses,
        "strengths": evaluation.strengths,
        "prompt_used": prompt,
    }
    out_path = RESULTS_DIR / f"iteration_{iteration}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("[OK] saved iteration file %s", out_path)


def save_best_prompt(prompt: str, score: float) -> None:
    """Write the best prompt and its score to ``RESULTS_DIR``."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "best_prompt.txt").write_text(prompt, encoding="utf-8")
    (RESULTS_DIR / "best_score.txt").write_text(f"{score:.6f}\n", encoding="utf-8")
     # Deploy best prompt to prompts/ so research_agent picks it up automatically
    import shutil
    shutil.copy(
        RESULTS_DIR / "best_prompt.txt",
        PROMPTS_DIR / "question_generator.txt"
    )
    logger.info("[OK] best prompt saved with score %.4f", score)


async def run_optimization(
    transcript: str,
    context: str,
    linkedin_raw: str,
    website_raw: str,
    ground_truth: str,
    initial_prompt: str | None = None,
) -> OptimizationResult:
    """Run generate → evaluate → improve until threshold, plateau, or max iterations."""
    current_prompt = initial_prompt if initial_prompt is not None else load_initial_prompt()
    best_score = 0.0
    best_prompt = current_prompt
    score_history: list[float] = []
    no_improvement_count = 0

    for iteration in range(MAX_ITERATIONS):
        logger.info("[OK] Starting iteration %s/%s", iteration + 1, MAX_ITERATIONS)

        question_set = await generate_questions(
            transcript,
            context,
            linkedin_raw,
            website_raw,
            current_prompt,
            iteration,
        )

        evaluation = await evaluate_questions(
            question_set,
            ground_truth,
            iteration,
        )

        score = evaluation.total_score
        score_history.append(score)

        logger.info("[OK] Iteration %s score: %.2f", iteration + 1, score)

        save_iteration_result(iteration, question_set, evaluation, current_prompt)

        if score > best_score:
            best_score = score
            best_prompt = current_prompt
            save_best_prompt(best_prompt, best_score)
            no_improvement_count = 0
        else:
            no_improvement_count += 1

        if score >= SCORE_THRESHOLD:
            logger.info(
                "[OK] Converged at iteration %s score %.2f",
                iteration + 1,
                score,
            )
            return OptimizationResult(
                best_score=best_score,
                best_prompt=best_prompt,
                iterations_run=iteration + 1,
                converged=True,
                score_history=score_history,
                final_questions=question_set.questions,
                errors=[],
            )

        if no_improvement_count >= NO_IMPROVEMENT_LIMIT:
            logger.warning("[WARNING] No improvement for 3 iterations — stopping early")
            break

        current_prompt = await improve_prompt(current_prompt, evaluation, iteration)

    iterations_run = len(score_history)
    return OptimizationResult(
        best_score=best_score,
        best_prompt=best_prompt,
        iterations_run=iterations_run,
        converged=False,
        score_history=score_history,
        final_questions=[],
        errors=["Did not converge — best score: " + str(best_score)],
    )


async def main() -> None:
    """Run optimization on LastMile fixtures (transcript, crawl, judge loop)."""
    from services.crawler_service import scrape_linkedin
    from services.input_parser import extract_urls, load_ground_truth, parse_input_folder
    from services.website_reader import read_website

    print("[OK] Loading LastMile training data...")
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

    print("[OK] Starting optimization loop...")
    result = await run_optimization(
        transcript=parsed.transcript,
        context=parsed.context,
        linkedin_raw=linkedin.raw_text,
        website_raw=website.raw_text,
        ground_truth=ground_truth,
    )

    print(f"\n{'=' * 50}")
    print("OPTIMIZATION COMPLETE")
    print(f"{'=' * 50}")
    print(f"Converged: {result.converged}")
    print(f"Best score: {result.best_score:.2f}")
    print(f"Iterations run: {result.iterations_run}")
    print(f"Score history: {[f'{s:.2f}' for s in result.score_history]}")
    if result.errors:
        print(f"[WARNING] {result.errors}")
    print("\nBest prompt saved to: tests/optimization_results/best_prompt.txt")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
