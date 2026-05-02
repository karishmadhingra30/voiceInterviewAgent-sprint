"""FastAPI application: routes, CORS, Vapi webhooks,
background research tasks for PressClub AI Voice Interviewer."""

from __future__ import annotations

# stdlib
import asyncio
import json
import logging
import uuid
from pathlib import Path

# third party
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
logger = logging.getLogger(__name__)

## App Setup
app = FastAPI(title="PressClub AI Voice Interviewer")

# CORS — sprint only. Remove credentials=True and restrict origins in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # cannot be True with wildcard origins
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

OUTPUTS_DIR = Path("data/outputs")
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


## Pydantic Models — use Field(default_factory=list/dict) for mutable defaults
class ResearchRequest(BaseModel):
    folder_path: str
    session_id: str = ""


class ResearchResponse(BaseModel):
    status: str
    session_id: str
    message: str = ""


class ResearchStatusResponse(BaseModel):
    status: str
    session_id: str
    briefing_doc: dict = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class StartInterviewRequest(BaseModel):
    session_id: str


class StartInterviewResponse(BaseModel):
    status: str
    session_id: str
    vapi_assistant_id: str = ""
    errors: list[str] = Field(default_factory=list)


## Async File I/O Helpers
# RULE: Never use Path.read_text() or Path.write_text() in async functions.
# Always use asyncio.to_thread() to avoid blocking the FastAPI event loop.


async def async_write(path: Path, content: str) -> None:
    """Write file content without blocking the event loop."""
    await asyncio.to_thread(path.write_text, content, encoding="utf-8")


async def async_read(path: Path) -> str:
    """Read file content without blocking the event loop."""
    return await asyncio.to_thread(path.read_text, encoding="utf-8")


async def async_exists(path: Path) -> bool:
    """Check file existence without blocking the event loop."""
    return await asyncio.to_thread(path.exists)


## Background Task
async def run_research_background(folder_path: str, session_id: str) -> None:
    """
    Runs research agent in background. Called by BackgroundTasks.
    Status file is set to processing in start_research before this runs.

    Saves:
      data/outputs/{session_id}_status.txt   → ready or error (processing set earlier)
      data/outputs/{session_id}_briefing.json → BriefingDoc output
    """
    status_path = OUTPUTS_DIR / f"{session_id}_status.txt"
    briefing_path = OUTPUTS_DIR / f"{session_id}_briefing.json"

    try:
        logger.info("[OK] Background research started session=%s", session_id)

        from agents.research_agent import run_research

        briefing_doc = await run_research(folder_path, session_id)

        await async_write(
            briefing_path,
            json.dumps(briefing_doc.model_dump(), indent=2),
        )
        await async_write(status_path, "ready")
        logger.info("[OK] Research complete session=%s", session_id)

    except Exception as exc:
        logger.error("[ERROR] Research failed session=%s: %s", session_id, exc)
        await async_write(status_path, "error")
        await async_write(
            briefing_path,
            json.dumps({"errors": [str(exc)]}),
        )


## Routes


@app.get("/")
async def root():
    """Serve the main upload page."""
    return FileResponse("static/index.html")


@app.post("/research", response_model=ResearchResponse)
async def start_research(
    request: ResearchRequest,
    background_tasks: BackgroundTasks,
):
    """
    Start research agent in background. Returns session_id immediately.

    RACE FIX: Writes status=processing before returning
    so the first poll never gets a 404.
    Poll /research/status/{session_id} every 3 seconds until ready.
    """
    session_id = request.session_id or str(uuid.uuid4())[:8]
    status_path = OUTPUTS_DIR / f"{session_id}_status.txt"
    folder = Path(request.folder_path)

    if not await async_exists(folder):
        raise HTTPException(
            status_code=400,
            detail=f"Folder not found: {request.folder_path}",
        )
    if not await asyncio.to_thread(folder.is_dir):
        raise HTTPException(
            status_code=400,
            detail=f"Not a directory: {request.folder_path}",
        )

    # Write status file BEFORE adding background task — prevents race condition
    await async_write(status_path, "processing")

    background_tasks.add_task(
        run_research_background,
        request.folder_path,
        session_id,
    )

    logger.info(
        "[OK] Research queued session=%s folder=%s",
        session_id,
        request.folder_path,
    )

    return ResearchResponse(
        status="processing",
        session_id=session_id,
        message=f"Research started. Poll /research/status/{session_id}",
    )


@app.get("/research/status/{session_id}", response_model=ResearchStatusResponse)
async def research_status(session_id: str):
    """
    Poll to check research progress.
    Returns status: processing / ready / error
    All file I/O uses async_read/async_exists — never blocks event loop.
    """
    status_path = OUTPUTS_DIR / f"{session_id}_status.txt"
    briefing_path = OUTPUTS_DIR / f"{session_id}_briefing.json"

    if not await async_exists(status_path):
        raise HTTPException(
            status_code=404,
            detail=f"Session not found: {session_id}",
        )

    status = (await async_read(status_path)).strip()

    if status == "ready" and await async_exists(briefing_path):
        briefing_doc = json.loads(await async_read(briefing_path))
        return ResearchStatusResponse(
            status="ready",
            session_id=session_id,
            briefing_doc=briefing_doc,
        )

    if status == "error":
        errors: list[str] = []
        if await async_exists(briefing_path):
            data = json.loads(await async_read(briefing_path))
            errors = data.get("errors", [])
        return ResearchStatusResponse(
            status="error",
            session_id=session_id,
            errors=errors,
        )

    return ResearchStatusResponse(
        status="processing",
        session_id=session_id,
    )


@app.post("/start-interview", response_model=StartInterviewResponse)
async def start_interview(request: StartInterviewRequest):
    """
    Creates Vapi assistant loaded with briefing doc.
    Returns vapi_assistant_id for the browser SDK.

    IMPORTANT: vapi_service.create_vapi_assistant must attach
    session_id to the Vapi call metadata so the webhook can
    retrieve it later:
      metadata={"session_id": session_id}
    Without this the webhook always logs session_id=unknown.
    """
    briefing_path = OUTPUTS_DIR / f"{request.session_id}_briefing.json"

    if not await async_exists(briefing_path):
        raise HTTPException(
            status_code=404,
            detail=f"Briefing doc not found: {request.session_id}",
        )

    status_path = OUTPUTS_DIR / f"{request.session_id}_status.txt"
    if await async_exists(status_path):
        status = (await async_read(status_path)).strip()
        if status == "error":
            raise HTTPException(
                status_code=400,
                detail=f"Research failed for session: {request.session_id}",
            )

    briefing_doc = json.loads(await async_read(briefing_path))

    from services.vapi_service import create_vapi_assistant

    try:
        assistant_id = await create_vapi_assistant(
            briefing_doc=briefing_doc,
            session_id=request.session_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return StartInterviewResponse(
        status="ok",
        session_id=request.session_id,
        vapi_assistant_id=assistant_id,
    )


@app.post("/vapi-webhook")
async def vapi_webhook(request: Request):
    """
    Receives post-call events from Vapi.
    Only triggers synthesis on end-of-call-report.

    Vapi payload structure:
      {
        "message": {
          "type": "end-of-call-report",
          "call": {
            "metadata": {"session_id": "..."}  ← set by vapi_service
          },
          "artifact": {
            "transcript": "...",
            "messages": [...]
          }
        }
      }

    NOTE: session_id is inside message.call.metadata NOT at root.
    This only works if vapi_service attaches metadata when creating
    the assistant. If session_id = unknown, check vapi_service.
    """
    request_body = await request.json()
    message = request_body.get("message", {})
    event_type = message.get("type", "")

    logger.info("[VAPI] Webhook received: type=%s", event_type)

    if event_type != "end-of-call-report":
        return {"status": "ignored"}

    artifact = message.get("artifact", {})
    transcript = artifact.get("transcript", "")

    if not transcript:
        messages = artifact.get("messages", [])
        transcript = "\n".join(
            [
                f"{m.get('role', '')}: {m.get('content', '')}"
                for m in messages
            ],
        )

    if not transcript:
        logger.warning("[WARNING] Vapi webhook: no transcript in payload")
        return {"status": "no_transcript"}

    call = message.get("call", {})
    session_id = call.get("metadata", {}).get("session_id", "unknown")

    if session_id == "unknown":
        logger.warning(
            "[WARNING] session_id not found in call metadata — "
            "check vapi_service attaches metadata on assistant creation",
        )

    logger.info(
        "[VAPI] Call ended session=%s transcript_len=%d",
        session_id,
        len(transcript),
    )

    transcript_path = OUTPUTS_DIR / f"{session_id}_transcript.txt"
    await async_write(transcript_path, transcript)
    logger.info("[OK] Transcript saved session=%s", session_id)

    async def run_synthesis_with_logging() -> None:
        try:
            from agents.synthesis_agent import synthesize

            await synthesize(session_id, transcript)
        except Exception as exc:
            logger.error(
                "[ERROR] Synthesis failed session=%s: %s",
                session_id,
                exc,
            )

    asyncio.create_task(run_synthesis_with_logging())

    return {"status": "ok", "session_id": session_id}


@app.get("/results/{session_id}")
async def get_results(session_id: str):
    """Return synthesis output for results page."""
    output_path = OUTPUTS_DIR / f"{session_id}_output.md"
    briefing_path = OUTPUTS_DIR / f"{session_id}_briefing.json"

    if not await async_exists(output_path):
        return {"status": "processing", "session_id": session_id}

    briefing_doc: dict = {}
    if await async_exists(briefing_path):
        briefing_doc = json.loads(await async_read(briefing_path))

    return {
        "status": "ready",
        "session_id": session_id,
        "output": await async_read(output_path),
        "briefing_doc": briefing_doc,
    }


## Dev server
if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
