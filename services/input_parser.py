"""Parse company input folders: sales transcript, context, optional PDFs, and ground truth.

Reads ``1st Meeting.txt`` (cleaned + relabeled), ``Context.txt`` (raw), PDFs via PyMuPDF,
and exposes ``load_ground_truth`` for raw ``2nd Meeting.txt`` used only in benchmarks.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

from pydantic import BaseModel

class ParsedInput(BaseModel):
    transcript: str
    context: str
    extras: list[str]
    company_folder: str
    errors: list[str]

# Acknowledgement-only tokens (after normalizing "i see" → isee). Used to drop short filler.
_ACK_TOKENS = frozenset(
    {
        "isee",  # normalized from "i see"
        "okay",
        "yes",
        "no",
        "wow",
        "gotcha",
        "right",
        "sure",
        "cool",
        "great",
        "hmm",
        "uh",
        "um",
        "ah",
        "low",
        "hi",
        "hello",
    }
)

_SPEAKER_LINE = re.compile(r"^\s*(Me|Them)\s*:\s*(.*)$", re.IGNORECASE)
_TIMESTAMP_LINE_START = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\s(?:AM|PM)\]",
    re.MULTILINE,
)


def _detect_format(raw: str) -> str:
    """Detect transcript format and log the result.

    Returns ``"labeled_sameline"`` (Format A) when the file contains ``Me:`` /
    ``Them:`` speaker labels, otherwise ``"timestamped_nextline"`` (Format B)
    when at least one ``[HH:MM:SS AM/PM]`` line exists. Defaults to
    ``"labeled_sameline"`` for legacy or unrecognised transcripts.
    """
    if re.search(r"^\s*(?:Me|Them)\s*:", raw, re.MULTILINE | re.IGNORECASE):
        fmt = "labeled_sameline"
    elif _TIMESTAMP_LINE_START.search(raw):
        fmt = "timestamped_nextline"
    else:
        fmt = "labeled_sameline"
    logger.info("[OK] Detected transcript format: %s", fmt)
    return fmt


def _find_file_case_insensitive(folder: Path, filename_lower: str) -> Path | None:
    """Return the path to a file in ``folder`` whose name matches ``filename_lower`` case-insensitively."""
    for child in folder.iterdir():
        if child.is_file() and child.name.lower() == filename_lower:
            return child
    return None


def _strip_transcript_header(raw: str, fmt: str = "labeled_sameline") -> str:
    """Drop the header block and return the transcript body only.

    For Format A (``labeled_sameline``): drop everything up to and including the
    ``Transcript:`` line; if absent, start at the first ``Me:`` / ``Them:`` line.
    For Format B (``timestamped_nextline``): drop everything up to and including
    the first separator line composed entirely of ``=`` characters.
    """
    lines = raw.splitlines()
    if fmt == "timestamped_nextline":
        for idx, line in enumerate(lines):
            if re.fullmatch(r"=+", line.strip()):
                return "\n".join(lines[idx + 1 :]).lstrip("\n")
        logger.warning("[WARNING] No '====' separator found; using full file as body.")
        return raw
    for idx, line in enumerate(lines):
        if line.strip().lower().startswith("transcript:"):
            return "\n".join(lines[idx + 1 :]).lstrip("\n")
    for idx, line in enumerate(lines):
        if _SPEAKER_LINE.match(line):
            return "\n".join(lines[idx:]).lstrip("\n")
    logger.warning("[WARNING] No 'Transcript:' marker found; using full file as body.")
    return raw


def _normalize_for_ack_tokens(text: str) -> list[str]:
    """Lowercase, collapse ``i see`` to ``isee``, then return alphabetic tokens."""
    lowered = text.strip().lower()
    lowered = re.sub(r"\bi\s+see\b", "isee", lowered)
    return re.findall(r"[a-z]+", lowered)


def _is_short_acknowledgement_only(speaker_body: str) -> bool:
    """True if content is 3 or fewer words and every token is a known acknowledgement."""
    tokens = _normalize_for_ack_tokens(speaker_body)
    if not tokens or len(tokens) > 3:
        return False
    return all(token in _ACK_TOKENS for token in tokens)


def _relabel_and_filter_line(line: str) -> str | None:
    """Relabel Me/Them, drop filler acknowledgement lines; return new line or None to skip."""
    stripped = line.strip()
    if not stripped:
        return None
    match = _SPEAKER_LINE.match(line)
    if not match:
        return stripped
    label, body = match.group(1), match.group(2)
    body_stripped = body.strip()
    if not body_stripped:
        return None
    if _is_short_acknowledgement_only(body_stripped):
        return None
    speaker = "PRESSCLUB" if label.lower() == "me" else "CUSTOMER"
    return f"{speaker}: {body_stripped}"


_TIMESTAMP_NAME_LINE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\s(?:AM|PM)\]\s*(.+?)\s*:\s*$"
)


def _clean_timestamped_nextline_first_meeting(raw: str) -> str:
    """Clean Format B (``timestamped_nextline``) transcripts.

    Strips the header up to the ``====`` separator, then walks the body line by
    line. ``[HH:MM:SS AM/PM] Name:`` lines set the current speaker; subsequent
    non-empty lines are the utterance content for that speaker. The first
    speaker observed in the file is relabeled PRESSCLUB and all others CUSTOMER.
    Short acknowledgement-only lines are dropped using the same filter as
    Format A; empty lines and stray content before the first speaker are
    skipped.
    """
    body = _strip_transcript_header(raw, "timestamped_nextline")
    first_speaker: str | None = None
    current_speaker: str | None = None
    out_lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        ts_match = _TIMESTAMP_NAME_LINE.match(stripped)
        if ts_match:
            current_speaker = ts_match.group(1).strip()
            if first_speaker is None:
                first_speaker = current_speaker
            continue
        if current_speaker is None:
            continue
        if _is_short_acknowledgement_only(stripped):
            continue
        speaker = "PRESSCLUB" if current_speaker == first_speaker else "CUSTOMER"
        out_lines.append(f"{speaker}: {stripped}")
    return "\n".join(out_lines).strip()


def _clean_labeled_first_meeting(raw: str) -> str:
    """Clean Format A (``labeled_sameline``) transcripts.

    Drops the header up to ``Transcript:``, removes filler acknowledgement-only
    lines, and relabels ``Me`` / ``Them`` speakers to ``PRESSCLUB`` /
    ``CUSTOMER`` while keeping each utterance on its original same-line form.
    """
    body = _strip_transcript_header(raw, "labeled_sameline")
    out_lines: list[str] = []
    for line in body.splitlines():
        processed = _relabel_and_filter_line(line)
        if processed is not None:
            out_lines.append(processed)
    return "\n".join(out_lines).strip()


def _clean_first_meeting_text(raw: str) -> str:
    """Dispatch to the cleaner that matches the detected transcript format."""
    fmt = _detect_format(raw)
    if fmt == "timestamped_nextline":
        return _clean_timestamped_nextline_first_meeting(raw)
    return _clean_labeled_first_meeting(raw)


def _extract_pdf_plain_text(pdf_path: Path) -> str:
    """Extract plain text from a PDF using PyMuPDF (fitz)."""
    import fitz  # type: ignore[import-untyped]  # PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        chunks: list[str] = []
        for page in doc:
            chunks.append(page.get_text("text"))
        return "\n".join(chunks).strip()
    finally:
        doc.close()


def parse_input_folder(folder_path: str) -> ParsedInput:
    """Read transcript, context, and PDFs from a company folder.
      NOTE: Synchronous. Call via asyncio.to_thread() from async contexts.
    Returns a dict with keys: ``transcript``, ``context``, ``extras`` (list of PDF texts),
    ``company_folder`` (basename), and ``errors`` (human-readable issues).
    """
    folder = Path(folder_path).expanduser().resolve()
    errors: list[str] = []
    company_folder = folder.name

    transcript = ""
    first_path = _find_file_case_insensitive(folder, "1st meeting.txt")
    if first_path is None:
        msg = f"Missing 1st Meeting.txt in {folder}"
        logger.error("[ERROR] %s", msg)
        errors.append(msg)
    else:
        try:
            transcript = _clean_first_meeting_text(first_path.read_text(encoding="utf-8"))
            logger.info("[OK] Loaded transcript: %s (%d chars)", first_path.name, len(transcript))
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"Failed to read {first_path}: {exc}"
            logger.error("[ERROR] %s", msg)
            errors.append(msg)

    context = ""
    ctx_path = _find_file_case_insensitive(folder, "context.txt")
    if ctx_path is None:
        msg = f"Missing Context.txt in {folder}"
        logger.error("[ERROR] %s", msg)
        errors.append(msg)
    else:
        try:
            context = ctx_path.read_text(encoding="utf-8")
            if len(context.strip()) < 10:
                context = ""
                logger.warning(
                    "[WARNING] context appears empty for %s",
                    company_folder,
                )
                errors.append("context_empty")
            else:
                logger.info("[OK] Loaded context: %s (%d chars)", ctx_path.name, len(context))
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"Failed to read {ctx_path}: {exc}"
            logger.error("[ERROR] %s", msg)
            errors.append(msg)

    extras: list[str] = []
    if folder.is_dir():
        for pdf_path in sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf"):
            try:
                extras.append(_extract_pdf_plain_text(pdf_path))
                logger.info("[OK] Extracted PDF: %s", pdf_path.name)
            except Exception as exc:
                msg = f"Failed to extract PDF {pdf_path.name}: {exc}"
                logger.exception("[ERROR] %s", msg)
                errors.append(msg)

    return ParsedInput(
        transcript=transcript,
        context=context,
        extras=extras,
        company_folder=company_folder,
        errors=errors
    )


def load_ground_truth(folder_path: str) -> str:
    """Return the raw contents of ``2nd Meeting.txt`` with no cleaning (validation / benchmark only)."""
    folder = Path(folder_path).expanduser().resolve()
    path = _find_file_case_insensitive(folder, "2nd meeting.txt")
    if path is None:
        msg = f"Missing 2nd Meeting.txt in {folder}"
        logger.error("[ERROR] %s", msg)
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.error("[ERROR] Failed to read ground truth %s: %s", path, exc)
        return ""

def extract_urls(text: str) -> dict:
    """
    Extract LinkedIn and website URLs from context text.
    
    Scans raw context string for all http/https URLs.
    Splits them into LinkedIn profile URLs and general website URLs.
    
    Returns:
        {
            "linkedin_urls": [...],   # URLs containing linkedin.com/in/
            "website_urls": [...]     # all other http/https URLs
        }
    """
    all_urls = re.findall(r'https?://[^\s\)\"\']+', text)
    
    linkedin_urls = []
    website_urls = []
    
    for url in all_urls:
        # Strip trailing punctuation that got caught in regex
        url = url.rstrip('.,;:)')
        if 'linkedin.com/in/' in url:
            linkedin_urls.append(url)
        else:
            website_urls.append(url)
    
    return {
        "linkedin_urls": linkedin_urls,
        "website_urls": website_urls
    }



if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    
    root = Path(__file__).resolve().parent.parent
    folder = sys.argv[1] if len(sys.argv) > 1 else str(root / "tests" / "fixtures" / "LastMile")
    
    result = parse_input_folder(folder)
    print(f"[OK] company: {result.company_folder}")
    print(f"[OK] transcript: {len(result.transcript)} chars")
    print(f"[OK] context: {len(result.context)} chars")
    print(f"[OK] extras: {len(result.extras)} PDFs")
    if result.errors:
        print(f"[WARNING] errors: {result.errors}")
    
    gt = load_ground_truth(folder)
    print(f"[OK] ground truth: {len(gt)} chars")

    # Test URL extraction
    urls = extract_urls(result.context)
    print(f"[OK] LinkedIn URLs:{urls['linkedin_urls']}")
    print(f"[OK] Website URLs: {urls['website_urls']}")
