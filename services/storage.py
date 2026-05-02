"""Local file storage helpers for session artifacts."""

from pathlib import Path
import logging

logger = logging.getLogger(__name__)

OUTPUTS_DIR = Path("data/outputs")


def ensure_outputs_dir() -> None:
    """Create outputs directory if it doesn't exist."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def get_output_path(session_id: str, suffix: str) -> Path:
    """
    Returns full path for a session artifact.
    suffix examples: 'briefing.json', 'transcript.txt', 'output.md'
    """
    ensure_outputs_dir()
    return OUTPUTS_DIR / f"{session_id}_{suffix}"


def session_exists(session_id: str) -> bool:
    """Check if any artifacts exist for this session."""
    return any(OUTPUTS_DIR.glob(f"{session_id}_*"))