"""DSPy-based PR research agent: generates interview questions from transcript + sources."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

import dspy
from dotenv import load_dotenv
from dspy.teleprompt import MIPROv2, BootstrapFewShot
from pydantic import BaseModel

from services.crawler_service import scrape_linkedin
from services.input_parser import extract_urls, load_ground_truth, parse_input_folder
from services.website_reader import read_website

load_dotenv()

logger = logging.getLogger(__name__)

lm = dspy.LM(
    "anthropic/claude-sonnet-4-5",
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    max_tokens=1500,
)
dspy.configure(lm=lm)


class NewsAngleInterviewer(dspy.Signature):
    """
    You are a senior tech journalist generating interview questions
    to find newsworthy angles in a startup's story.
    Cover ALL 5 signal types: funding, product, traction,
    founder background, and industry insight.
    Ask for specific numbers, quotable stats, and shocking facts.
    """

    transcript: str = dspy.InputField(desc="Sales call transcript")
    context: str = dspy.InputField(desc="Company background and URLs")
    linkedin_data: str = dspy.InputField(desc="Founder LinkedIn profile data")
    website_data: str = dspy.InputField(desc="Company website content")

    questions: list[str] = dspy.OutputField(
        desc="12 interview questions covering all 5 signal types. "
        "Each question must ask for ONE specific data point. "
        "Push for exact numbers, not generalities."
    )
    signal_targets: list[str] = dspy.OutputField(
        desc="Signal type for each question: funding/product/traction/founder/insight"
    )
    interview_strategy: str = dspy.OutputField(
        desc="One paragraph: which 2-3 signals look strongest and why"
    )


class PRResearchAgent(dspy.Module):
    def __init__(self):
        self.generate = dspy.ChainOfThought(NewsAngleInterviewer)

    def forward(self, transcript, context, linkedin_data, website_data):
        return self.generate(
            transcript=transcript[:6000],
            context=context[:1000],
            linkedin_data=linkedin_data[:2000] if linkedin_data else "Not available",
            website_data=website_data[:2000] if website_data else "Not available",
        )


class BriefingDoc(BaseModel):
    session_id: str
    company_folder: str
    questions: list[str]
    signal_targets: list[str]
    interview_strategy: str
    known_facts: list[str]
    errors: list[str]


def _folder_has_first_and_second_meeting(folder: Path) -> bool:
    names = {p.name.lower() for p in folder.iterdir() if p.is_file()}
    return "1st meeting.txt" in names and "2nd meeting.txt" in names


def load_training_data() -> list[dspy.Example]:
    examples = []
    fixtures = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    if not fixtures.is_dir():
        return examples

    for folder in sorted(p for p in fixtures.iterdir() if p.is_dir()):
        if folder.name.lower() == "aisa":
            continue
        if not _folder_has_first_and_second_meeting(folder):
            continue

        parsed = parse_input_folder(str(folder))
        ground_truth = load_ground_truth(str(folder))

        weida_questions = [
            line.replace("PRESSCLUB:", "").strip()
            for line in ground_truth.split("\n")
            if line.strip().startswith("Me:") and "?" in line
        ]

        if len(weida_questions) < 3:
            continue

        example = dspy.Example(
            transcript=parsed.transcript[:6000],
            context=parsed.context[:1000],
            linkedin_data="",
            website_data="",
            questions=weida_questions,
            signal_targets=["unknown"] * len(weida_questions),
            interview_strategy="Focus on strongest signals found",
        ).with_inputs("transcript", "context", "linkedin_data", "website_data")

        examples.append(example)

    return examples


def metric(example, prediction, trace=None) -> float:
    ground_truth_words = set(" ".join(example.questions).lower().split())
    predicted_words = set(" ".join(prediction.questions).lower().split())

    stop_words = {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "what",
        "how",
        "when",
        "where",
        "who",
        "why",
        "can",
        "you",
        "your",
        "have",
        "had",
        "has",
        "will",
        "would",
        "could",
        "tell",
        "me",
        "us",
        "about",
        "and",
        "or",
        "in",
        "of",
        "to",
        "for",
    }

    ground_truth_words -= stop_words
    predicted_words -= stop_words

    if not ground_truth_words:
        return 0.0

    overlap = len(ground_truth_words & predicted_words)
    recall = overlap / len(ground_truth_words)
    precision = overlap / len(predicted_words) if predicted_words else 0

    if recall + precision == 0:
        return 0.0

    f1 = 2 * (precision * recall) / (precision + recall)

    signals = ["funding", "product", "traction", "founder", "insight"]
    targets_text = " ".join(prediction.signal_targets).lower()
    signal_coverage = sum(1 for s in signals if s in targets_text) / 5

    return (f1 * 0.7) + (signal_coverage * 0.3)


async def optimize_agent() -> PRResearchAgent:
    saved_path = Path("tests/optimization_results/optimized_agent.json")
    if saved_path.exists():
        agent = PRResearchAgent()
        agent.load(str(saved_path))
        return agent

    trainset = load_training_data()
    if len(trainset) < 2:
        logger.warning("[WARNING] Not enough training data — using unoptimized agent")
        return PRResearchAgent()

    logger.info("[OK] Starting DSPy optimization on %s training examples", len(trainset))
    optimizer = BootstrapFewShot(metric=metric, max_bootstrapped_demos=2)
    agent = PRResearchAgent()
    optimized = await asyncio.to_thread(
        optimizer.compile,
        agent,
        trainset=trainset,
    )

    saved_path.parent.mkdir(parents=True, exist_ok=True)
    optimized.save(str(saved_path))
    logger.info("[OK] Optimized agent saved to %s", saved_path)

    return optimized


async def run_research(
    folder_path: str,
    session_id: str,
    use_optimized: bool = True,
) -> BriefingDoc:
    parsed = parse_input_folder(folder_path)
    urls = extract_urls(parsed.context)

    linkedin_task = scrape_linkedin(
        urls["linkedin_urls"][0] if urls["linkedin_urls"] else "",
        context_fallback=parsed.context,
    )
    website_task = read_website(
        urls["website_urls"][0] if urls["website_urls"] else "",
    )
    linkedin, website = await asyncio.gather(linkedin_task, website_task)

    if use_optimized:
        agent = await optimize_agent()
    else:
        agent = PRResearchAgent()

    logger.info("[LLM-CALL] PRResearchAgent generating questions for %s", session_id)
    prediction = await asyncio.to_thread(
        agent,
        transcript=parsed.transcript,
        context=parsed.context,
        linkedin_data=linkedin.raw_text,
        website_data=website.raw_text,
    )

    known_facts: list[str] = []
    if parsed.context:
        known_facts.append(f"Company context: {parsed.context[:200]}")

    return BriefingDoc(
        session_id=session_id,
        company_folder=parsed.company_folder,
        questions=list(prediction.questions),
        signal_targets=list(prediction.signal_targets),
        interview_strategy=str(prediction.interview_strategy),
        known_facts=known_facts,
        errors=[],
    )


async def main() -> None:
    folder = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/LastMile"
    session_id = str(uuid.uuid4())[:8]

    print(f"[OK] Running research agent on: {folder}")
    print(f"[OK] Session ID: {session_id}")

    result = await run_research(folder, session_id, use_optimized=True)

    print(f"\n{'=' * 50}")
    print(f"BRIEFING DOC — {result.company_folder}")
    print(f"{'=' * 50}")
    print(f"\nINTERVIEW STRATEGY:\n{result.interview_strategy}")
    print(f"\nQUESTIONS ({len(result.questions)} total):")
    for i, q in enumerate(result.questions, 1):
        target = result.signal_targets[i - 1] if i <= len(result.signal_targets) else "?"
        print(f"  {i}. [{target.upper()}] {q}")
    print(f"\nKNOWN FACTS (won't ask about):")
    for f in result.known_facts:
        print(f"  - {f}")
    if result.errors:
        print(f"\n[WARNING] Errors: {result.errors}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main())
