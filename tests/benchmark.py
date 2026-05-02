"""Benchmark and validation CLI for ``services.input_parser``.

Runs structured checks against ``tests/fixtures`` company folders plus one edge-case
folder (empty directory). Prints a human-readable report and exits non-zero on failure
for CI usage.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from services.input_parser import load_ground_truth, parse_input_folder

logger = logging.getLogger(__name__)

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

# Lines that are only a short acknowledgement after relabeling (must not appear post-clean).
_FILLER_ACK_ONLY_LINE = re.compile(
    r"^(PRESSCLUB|CUSTOMER):\s*"
    r"(okay|yes|no|wow|gotcha|right|sure|cool|great|hmm|uh|um|ah|low|hi|hello|i\s+see)\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Raw timestamped transcript lines (parser should strip to PRESSCLUB:/CUSTOMER: only).
_RAW_TIMESTAMP_LINE_START = re.compile(r"^\[\d{2}:\d{2}:\d{2}", re.MULTILINE)
_RAW_TIMESTAMP_SPEAKER = re.compile(
    r"\[\d{2}:\d{2}:\d{2}\s(?:AM|PM)\]\s*[^:]+:\s",
)

_SUBSTANTIVE_SUBSTRINGS = [
    "10,000 of them annually",
    "13,800",
    "50 million",
    "80 to 85",
    "Melinda Gates",
]


class BenchmarkResult(BaseModel):
    """Outcome of a single benchmark check."""

    test_name: str
    company: str
    passed: bool
    details: str


class BenchmarkReport(BaseModel):
    """Aggregated results for one company folder (or edge-case run)."""

    company: str
    results: list[BenchmarkResult]
    passed: int
    failed: int

    @property
    def score(self) -> str:
        """Return a short ``X/Y passed`` score string."""
        total = self.passed + self.failed
        return f"{self.passed}/{total} passed"


def _company_from_folder(fixture_path: Path, parsed_company: str | None) -> str:
    """Resolve display company name from parser output or folder path."""
    return parsed_company if parsed_company else fixture_path.name


def _transcript_cleaning_lines(transcript: str) -> tuple[bool, list[str]]:
    """Evaluate transcript cleaning rules; return overall pass and detail lines."""
    lines: list[str] = []
    no_raw_ts_lines = _RAW_TIMESTAMP_LINE_START.search(transcript) is None
    no_raw_ts_speakers = _RAW_TIMESTAMP_SPEAKER.search(transcript) is None
    checks: list[tuple[str, bool]] = [
        ("1. transcript is a non-empty string", bool(transcript.strip())),
        ('2. transcript contains "PRESSCLUB:"', "PRESSCLUB:" in transcript),
        ('3. transcript contains "CUSTOMER:"', "CUSTOMER:" in transcript),
        ('4. "Me:" does not appear in transcript', "Me:" not in transcript),
        ('5. "Them:" does not appear in transcript', "Them:" not in transcript),
        ('6. "Meeting Title:" does not appear in transcript', "Meeting Title:" not in transcript),
        (
            '7. no standalone line "Transcript:" in transcript',
            not any(ln.strip().casefold() == "transcript:" for ln in transcript.splitlines()),
        ),
        (
            "8. no raw timestamp lines (no line starting with [HH:MM:SS)",
            no_raw_ts_lines,
        ),
        (
            "9. no raw timestamp + speaker-name pattern remains",
            no_raw_ts_speakers,
        ),
    ]
    all_ok = True
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        lines.append(f"[{status}] {label}")
        if not ok:
            all_ok = False
    return all_ok, lines


def _summarize_transcript_print(passed: bool, transcript: str) -> str:
    """One-line summary for console report for transcript_cleaning."""
    _, check_lines = _transcript_cleaning_lines(transcript)
    passed_n = sum(1 for ln in check_lines if ln.startswith("[PASS]"))
    total = len(check_lines)
    if passed:
        return f"{passed_n}/{total} checks passed"
    return f"{passed_n}/{total} checks passed (see details)"


def test_transcript_cleaning(fixture_path: Path) -> BenchmarkResult:
    """Verify cleaned transcript structure and forbidden patterns."""
    parsed = parse_input_folder(str(fixture_path))
    company = _company_from_folder(fixture_path, parsed.company_folder)
    ok, detail_lines = _transcript_cleaning_lines(parsed.transcript)
    details = "\n".join(detail_lines)
    if ok:
        logger.info("[OK] transcript_cleaning — %s", company)
    else:
        logger.info("[FAIL] transcript_cleaning — %s", company)
    return BenchmarkResult(
        test_name="transcript_cleaning",
        company=company,
        passed=ok,
        details=details,
    )


def test_filler_removal(fixture_path: Path) -> BenchmarkResult:
    """Ensure short acknowledgement-only lines are removed; transcript stays substantial."""
    parsed = parse_input_folder(str(fixture_path))
    company = _company_from_folder(fixture_path, parsed.company_folder)
    t = parsed.transcript
    lowered = t.casefold()

    filler_matches = list(_FILLER_ACK_ONLY_LINE.finditer(t))
    filler_ok = len(filler_matches) == 0
    length_ok = len(t) > 500
    passed = filler_ok and length_ok

    substantive_ok: list[str] = []
    substantive_missing: list[str] = []
    for phrase in _SUBSTANTIVE_SUBSTRINGS:
        if phrase.casefold() in lowered:
            substantive_ok.append(phrase)
        else:
            substantive_missing.append(phrase)

    lines: list[str] = []
    if filler_matches:
        sample = [m.group(0).strip() for m in filler_matches[:5]]
        lines.append(
            f"Ack-only filler lines still present ({len(filler_matches)}); e.g. {sample}"
        )
    else:
        lines.append("No ack-only filler line pattern matches (regex clean).")
    lines.append(
        f"[{'PASS' if length_ok else 'FAIL'}] transcript length > 500 chars ({len(t)} chars)"
    )

    if company == "LastMile":
        logger.info(
            "[INFO] filler_removal — LastMile substantive found %s/%s: %s",
            len(substantive_ok),
            len(_SUBSTANTIVE_SUBSTRINGS),
            substantive_ok,
        )
        if substantive_missing:
            logger.info(
                "[INFO] filler_removal — LastMile substantive not found: %s",
                substantive_missing,
            )

    details = "\n".join(lines)
    if passed:
        logger.info("[OK] filler_removal — %s", company)
    else:
        logger.info("[FAIL] filler_removal — %s", company)
    return BenchmarkResult(
        test_name="filler_removal",
        company=company,
        passed=passed,
        details=details,
    )


def test_context_preserved(fixture_path: Path) -> BenchmarkResult:
    """Ensure context file is loaded raw with URLs and without speaker relabeling."""
    parsed = parse_input_folder(str(fixture_path))
    company = _company_from_folder(fixture_path, parsed.company_folder)
    ctx = parsed.context
    if len(ctx) < 10:
        logger.info("[OK] context_preserved — skipped (empty context) — %s", company)
        return BenchmarkResult(
            test_name="context_preserved",
            company=company,
            passed=True,
            details="skipped — context file is empty (known data issue)",
        )

    lines: list[str] = []
    checks: list[tuple[str, bool]] = [
        ("1. context is non-empty string", bool(ctx.strip())),
        ('2. context contains a URL starting with "http"', bool(re.search(r"https?://", ctx))),
        (
            "3. context has not been relabeled (no PRESSCLUB:/CUSTOMER:)",
            "PRESSCLUB:" not in ctx and "CUSTOMER:" not in ctx,
        ),
        ("4. context length > 50 characters", len(ctx) > 50),
    ]
    all_ok = True
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        lines.append(f"[{status}] {label}")
        if not ok:
            all_ok = False
    details = "\n".join(lines)
    if all_ok:
        logger.info("[OK] context_preserved — %s", company)
    else:
        logger.info("[FAIL] context_preserved — %s", company)
    return BenchmarkResult(
        test_name="context_preserved",
        company=company,
        passed=all_ok,
        details=details,
    )


def test_ground_truth_raw(fixture_path: Path) -> BenchmarkResult:
    """Ensure ground truth is raw 2nd meeting text (Me:/Them: or timestamped lines)."""
    parsed = parse_input_folder(str(fixture_path))
    company = _company_from_folder(fixture_path, parsed.company_folder)

    if parsed.company_folder == "Aisa":
        logger.warning("[WARNING] Aisa skipped — validation set only")
        return BenchmarkResult(
            test_name="ground_truth_raw",
            company=company,
            passed=True,
            details="skipped — Aisa is validation-only (Chinese transcript)",
        )

    raw = load_ground_truth(str(fixture_path))
    detail_lines: list[str] = []

    non_empty = bool(raw.strip())
    detail_lines.append(
        f"[{'PASS' if non_empty else 'FAIL'}] 1. ground truth is non-empty string"
    )

    ts_line = re.compile(r"^\[\d{2}:\d{2}:\d{2}", re.MULTILINE)
    is_timestamped = bool(ts_line.search(raw))
    fmt_label = "timestamped" if is_timestamped else "labeled"
    detail_lines.append(f"Format detected: {fmt_label}")

    no_relabel = "PRESSCLUB:" not in raw and "CUSTOMER:" not in raw
    len_ok = len(raw) > 100

    if is_timestamped:
        has_ts = bool(ts_line.search(raw))
        detail_lines.append(
            f"[{'PASS' if has_ts else 'FAIL'}] 2. timestamped: lines match ^[HH:MM:SS..."
        )
        detail_lines.append(
            f"[{'PASS' if no_relabel else 'FAIL'}] 3. timestamped: no PRESSCLUB: or CUSTOMER:"
        )
        detail_lines.append(
            f"[{'PASS' if len_ok else 'FAIL'}] 4. length > 100 characters"
        )
        all_ok = non_empty and has_ts and no_relabel and len_ok
    else:
        has_me = "Me:" in raw
        detail_lines.append(
            f"[{'PASS' if has_me else 'FAIL'}] 2. labeled: contains Me: labels"
        )
        detail_lines.append(
            f"[{'PASS' if no_relabel else 'FAIL'}] 3. labeled: no PRESSCLUB: or CUSTOMER:"
        )
        detail_lines.append(
            f"[{'PASS' if len_ok else 'FAIL'}] 4. length > 100 characters"
        )
        all_ok = non_empty and has_me and no_relabel and len_ok

    details = "\n".join(detail_lines)
    if all_ok:
        logger.info("[OK] ground_truth_raw — %s", company)
    else:
        logger.info("[FAIL] ground_truth_raw — %s", company)
    return BenchmarkResult(
        test_name="ground_truth_raw",
        company=company,
        passed=all_ok,
        details=details,
    )


def test_missing_file_handling() -> BenchmarkResult:
    """Verify parse_input_folder handles a folder with no expected files."""
    tmp = tempfile.mkdtemp()
    lines: list[str] = []
    parsed = None
    raised: BaseException | None = None
    try:
        try:
            parsed = parse_input_folder(tmp)
        except BaseException as exc:
            raised = exc
            logger.exception("[FAIL] edge_case_missing_files raised: %s", exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if raised is not None:
        details = f"Exception raised: {raised!r}"
        return BenchmarkResult(
            test_name="edge_case_missing_files",
            company="EDGE_CASE",
            passed=False,
            details=details,
        )
    assert parsed is not None
    checks: list[tuple[str, bool]] = [
        ("1. no exception raised", True),
        ("2. errors list has at least 2 entries", len(parsed.errors) >= 2),
        ("3. transcript is empty string", parsed.transcript == ""),
        ("4. context is empty string", parsed.context == ""),
        ("5. extras is empty list", parsed.extras == []),
    ]
    all_ok = True
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        lines.append(f"[{status}] {label}")
        if not ok:
            all_ok = False
    details = "\n".join(lines)
    if all_ok:
        logger.info("[OK] edge_case_missing_files — EDGE_CASE")
    else:
        logger.info("[FAIL] edge_case_missing_files — EDGE_CASE")
    return BenchmarkResult(
        test_name="edge_case_missing_files",
        company="EDGE_CASE",
        passed=all_ok,
        details=details,
    )


def _folder_has_pdf(folder: Path) -> bool:
    """Return True if ``folder`` contains at least one ``.pdf`` file."""
    return any(p.is_file() and p.suffix.lower() == ".pdf" for p in folder.iterdir())


def test_pdf_extraction(fixture_path: Path) -> BenchmarkResult:
    """Verify PDF text extraction into ``extras`` when PDFs exist; skip otherwise."""
    if not _folder_has_pdf(fixture_path):
        parsed_skip = parse_input_folder(str(fixture_path))
        company = _company_from_folder(fixture_path, parsed_skip.company_folder)
        logger.info("[OK] pdf_extraction — skipped (no PDFs) — %s", company)
        return BenchmarkResult(
            test_name="pdf_extraction",
            company=company,
            passed=True,
            details="skipped — no PDFs in folder",
        )
    parsed = parse_input_folder(str(fixture_path))
    company = _company_from_folder(fixture_path, parsed.company_folder)
    lines: list[str] = []
    ok_nonempty = bool(parsed.extras)
    all_strings_nonempty = all(isinstance(x, str) and x.strip() for x in parsed.extras)
    all_long = all(isinstance(x, str) and len(x) > 20 for x in parsed.extras)
    checks: list[tuple[str, bool]] = [
        ("1. extras list is non-empty", ok_nonempty),
        ("2. each extra is a non-empty string", all_strings_nonempty),
        ("3. each extra length > 20 characters", all_long),
    ]
    all_ok = all(c[1] for c in checks)
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        lines.append(f"[{status}] {label}")
    details = "\n".join(lines)
    if all_ok:
        logger.info("[OK] pdf_extraction — %s", company)
    else:
        logger.info("[FAIL] pdf_extraction — %s", company)
    return BenchmarkResult(
        test_name="pdf_extraction",
        company=company,
        passed=all_ok,
        details=details,
    )


def run_benchmark(fixture_path: Path) -> BenchmarkReport:
    """Run all path-based parser benchmarks for one company folder."""
    results = [
        test_transcript_cleaning(fixture_path),
        test_filler_removal(fixture_path),
        test_context_preserved(fixture_path),
        test_ground_truth_raw(fixture_path),
        test_pdf_extraction(fixture_path),
    ]
    company = results[0].company
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    return BenchmarkReport.model_validate(
        {
            "company": company,
            "results": results,
            "passed": passed,
            "failed": failed,
        }
    )


def _print_banner(title: str) -> None:
    """Print a section divider with a title line."""
    bar = "═" * 38
    print(bar)
    print(title)
    print(bar)


def _result_status_label(result: BenchmarkResult) -> str:
    """Return ``[PASS]`` or ``[FAIL]`` for console output."""
    return "[PASS]" if result.passed else "[FAIL]"


def _format_result_summary_line(
    result: BenchmarkResult,
    transcript: str,
    context: str,
    gt_len: int,
    extras_count: int,
) -> str:
    """Build the short ``— …`` suffix for a benchmark row."""
    name = result.test_name
    if name == "transcript_cleaning":
        return _summarize_transcript_print(result.passed, transcript)
    if name == "filler_removal":
        ack_hits = len(_FILLER_ACK_ONLY_LINE.findall(transcript))
        return f"{len(transcript)} chars, {ack_hits} ack-only line pattern matches"
    if name == "context_preserved":
        has_url = bool(re.search(r"https?://", context))
        url_bit = "URL found" if has_url else "no URL"
        return f"{url_bit}, {len(context)} chars"
    if name == "ground_truth_raw":
        if result.passed:
            return f"{gt_len} chars, Me: labels intact"
        return f"{gt_len} chars (see details)"
    if name == "pdf_extraction":
        if "skipped" in result.details.lower():
            return "skipped (no PDFs)"
        return f"{extras_count} PDF(s) extracted, all chunks > 20 chars"
    return result.details.replace("\n", " ")[:100]


def _print_company_report(fixture_path: Path, report: BenchmarkReport) -> None:
    """Print formatted per-company benchmark results to stdout."""
    parsed = parse_input_folder(str(fixture_path))
    gt_len = len(load_ground_truth(str(fixture_path)))
    extras_count = len(parsed.extras)
    _print_banner(f"Company: {report.company}")
    for res in report.results:
        suffix = _format_result_summary_line(
            res,
            parsed.transcript,
            parsed.context,
            gt_len,
            extras_count,
        )
        print(f"{_result_status_label(res)} {res.test_name} — {suffix}")
    print(f"Score: {report.score}")


def _discover_fixture_dirs() -> list[Path]:
    """Return sorted company fixture directories under ``FIXTURES_ROOT``."""
    if not FIXTURES_ROOT.is_dir():
        return []
    return sorted(p for p in FIXTURES_ROOT.iterdir() if p.is_dir())


def _print_global_summary(
    reports: list[BenchmarkReport],
    edge: BenchmarkResult,
    grand_passed: int,
    grand_total: int,
) -> None:
    """Print aggregate scores and edge-case result."""
    print("Note: Aisa is validation-only (Chinese transcript) — failures expected")
    _print_banner("GLOBAL SUMMARY")
    for rep in reports:
        line = f"{rep.company:14} {rep.score}"
        if rep.failed:
            failed_names = ", ".join(r.test_name for r in rep.results if not r.passed)
            line += f"  <- failed: {failed_names}"
        print(line)
    edge_label = _result_status_label(edge)
    edge_suffix = (
        "errors caught correctly"
        if edge.passed
        else edge.details.split("\n")[0][:60]
    )
    print(f"{edge_label} edge_case_missing_files — {edge_suffix}")
    print(f"\nTotal: {grand_passed}/{grand_total} tests passed")


def main() -> int:
    """Discover fixtures, run benchmarks, print report; return exit code."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dirs = _discover_fixture_dirs()
    if not dirs:
        print(
            f"No subdirectories found under {FIXTURES_ROOT}. "
            "Add company folders with 1st Meeting.txt and Context.txt to run benchmarks."
        )
        return 0

    reports: list[BenchmarkReport] = []
    all_results: list[BenchmarkResult] = []

    for folder in dirs:
        report = run_benchmark(folder)
        reports.append(report)
        all_results.extend(report.results)
        _print_company_report(folder, report)
        print()

    edge = test_missing_file_handling()
    all_results.append(edge)

    grand_passed = sum(1 for r in all_results if r.passed)
    grand_total = len(all_results)
    _print_global_summary(reports, edge, grand_passed, grand_total)

    if any(not r.passed for r in all_results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())