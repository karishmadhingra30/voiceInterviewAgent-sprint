# PressClub AI Voice Interviewer — Architecture

**Sprint Build — May 2026 | Author: K. Dhingra**

---

## 1. Project Overview

PressClub is a PR automation service that achieves a 10x higher journalist response rate than industry average. The current workflow requires a human to conduct a 30-minute interview with each startup founder to extract newsworthy angles before launching an outreach campaign.

This system replaces that human interviewer with an AI voice agent. The agent ingests all available context about a startup, conducts a structured interview via a browser-based call, and produces structured output that feeds directly into PressClub's existing pitch generation pipeline.

| Attribute | Value |
|---|---|
| Sprint Duration | 24 hours |
| Deployment Target | Standalone browser interface (no auth required) |
| Output Format | `.md` for pipeline ingestion |
| Voice Platform | Vapi (managed orchestration) |
| LLM — Research & Synthesis | Claude Sonnet (via Anthropic API + DSPy) |
| LLM — Voice Interview | Gemini 2.0 Flash (via Vapi) |
| LinkedIn Scraping | Bright Data (replaces Oxylabs) |

---

## 2. Architecture Decision — Why Vapi

Three approaches were evaluated:

| Approach | Latency | Control | Sprint Risk | Decision |
|---|---|---|---|---|
| Native Voice-to-Voice (Gemini Live, OpenAI Realtime) | 300ms | Black box | High — no visibility into reasoning | Rejected |
| Raw Orchestration (Whisper + Claude + ElevenLabs) | 800-1500ms | Full | High — WebRTC alone is a multi-day problem | Rejected |
| Vapi (managed orchestration) | <600ms | High — Custom LLM URL for stretch goals | Low — one SDK, audio fully abstracted | **Selected** |

**Key rationale:** Vapi abstracts WebRTC, STT, TTS, turn-taking, recording, and transcript generation under one SDK. This frees all engineering time for the intelligence layer — signal detection, question generation, DSPy optimization, and synthesis — which is the actual differentiated value.

---

## 3. System Architecture — End to End

| Phase | Input | Process | Output |
|---|---|---|---|
| **0 — Input Parsing** | Company folder with `1st Meeting.txt` + `Context.txt` (+ optional PDFs) | `input_parser.py` cleans/normalizes transcript, extracts URLs from context | `ParsedInput`: clean transcript, context string, extras list |
| **1 — Research Agent** | ParsedInput | Bright Data scrapes LinkedIn. Claude reads website. DSPy `PRResearchAgent` (with `BootstrapFewShot` optimization) generates 12 interview questions across 5 signal types | `BriefingDoc`: questions, signal targets, interview strategy, known facts |
| **2 — Vapi Interview** | BriefingDoc + founder in browser | Vapi browser SDK handles audio. Gemini 2.0 Flash uses briefing doc as system prompt. Pre-built assistant is PATCHed with session context before call. | Audio (stored by Vapi) + raw transcript (via Vapi webhook) |
| **3 — Synthesis Agent** | Vapi transcript + BriefingDoc | Claude Sonnet reads full interview transcript, scores each signal, produces structured output | `{session_id}_output.md` — scorecard + structured notes |

---

## 4. File Structure

```
pressclub-sprint/
├── main.py                        # FastAPI: all routes, CORS, Vapi webhooks, background tasks
├── agents/
│   ├── research_agent.py          # DSPy PRResearchAgent — generates questions from all sources
│   ├── synthesis_agent.py         # Phase 3: transcript → scorecard + output.md
│   ├── question_generator.py      # DEPRECATED — standalone Claude question generator
│   ├── question_evaluator.py      # DEPRECATED — Claude-as-judge scorer
│   └── prompt_optimizer.py        # DEPRECATED — manual generate→evaluate→rewrite loop
├── services/
│   ├── input_parser.py            # Folder → ParsedInput (transcript clean, URL extraction)
│   ├── crawler_service.py         # Bright Data LinkedIn scraper + Claude analysis
│   ├── website_reader.py          # URL → structured content via Claude
│   ├── vapi_service.py            # PATCH pre-built Vapi assistant with session context
│   └── storage.py                 # Save outputs locally
├── prompts/
│   ├── question_generator.txt     # Journalist persona prompt (auto-updated by optimizer)
│   ├── evaluator.txt              # Claude-as-judge rubric for question scoring
│   ├── prompt_improver.txt        # Meta-prompt: rewrites question_generator.txt
│   ├── linkedin_analyzer.txt      # Structures raw LinkedIn data for PR signals
│   ├── synthesizer.txt            # Newsworthy pitch generator prompt
│   └── website_reader.txt         # Website content extraction prompt
├── static/
│   ├── index.html                 # Upload form: transcript paste + optional URLs
│   ├── interview.html             # Active call page (Vapi browser SDK)
│   └── results.html               # Scorecard + transcript + download
├── tests/
│   ├── benchmark.py               # Step benchmarks with pass/fail output
│   ├── fixtures/                  # Training data (Clarasight, Knowidea, LastMile, PineTree, Aisa)
│   │   └── {Company}/
│   │       ├── 1st Meeting.txt    # First sales call (input transcript)
│   │       ├── 2nd Meeting.txt    # Second meeting (ground truth for optimizer)
│   │       └── Context.txt        # Company background + URLs
│   └── optimization_results/      # Persisted optimizer outputs
│       ├── best_prompt.txt        # Best question_generator.txt found so far
│       ├── best_score.txt         # Score of best prompt
│       ├── optimized_agent.json   # Serialized DSPy agent weights
│       └── iteration_{n}.json     # Per-iteration scores, questions, feedback
├── data/
│   ├── Training/                  # Original training sets (5 companies)
│   ├── Validation/                # Validation sets (Ascend, Autositu)
│   └── outputs/                   # Runtime outputs per session
│       ├── {session_id}_status.txt
│       ├── {session_id}_briefing.json
│       ├── {session_id}_transcript.txt
│       └── {session_id}_output.md
├── .env                           # All API keys (never committed)
├── .env.example                   # Key template for collaborators
└── requirements.txt
```

---

## 5. Services — Detailed Spec

### 5.1 `input_parser.py`
Converts raw company folder into clean `ParsedInput`. Handles two transcript formats:

| Format | Description |
|---|---|
| Format A (labeled_sameline) | `Me: ...` / `Them: ...` speaker lines → relabeled to `PRESSCLUB` / `CUSTOMER` |
| Format B (timestamped_nextline) | `[HH:MM:SS AM/PM] Name:` headers with content on next line |

- Drops short acknowledgement-only filler lines (`okay`, `yes`, `wow`, etc.)
- Extracts PDFs via PyMuPDF for extra context
- `extract_urls()` splits context into LinkedIn URLs and website URLs
- `load_ground_truth()` returns raw `2nd Meeting.txt` (used by optimizer only)

---

### 5.2 `crawler_service.py`
Wraps **Bright Data** LinkedIn dataset API. Triggers a snapshot job, polls until ready, returns up to 6000 chars of raw profile data. Claude then structures it into a `LinkedInProfile`.

Fallback chain:
1. Bright Data scrape → Claude analysis
2. Context text fallback (partial — research agent uses transcript as primary)
3. Empty profile with error (pipeline continues, transcript is sole source)

---

### 5.3 `website_reader.py`
Passes company URL to Claude with `prompts/website_reader.txt`. Extracts product description, metrics, funding, and customer signals.

---

### 5.4 `vapi_service.py`
**PATCHes a pre-built Vapi assistant** (does not create a new one per session). Injects the session's system prompt and metadata before each call. Returns `VAPI_ASSISTANT_ID` for the browser SDK.

The system prompt is built by `build_system_prompt()`:
- Persona: Riley, senior tech journalist
- Behavioral rules (push for exact numbers, handle rambling, no vague quantifiers)
- Interview strategy from briefing doc
- Known facts block (do not re-ask)
- Question bank (12 questions, ordered by signal priority)

`stopSpeakingPlan` is configured: `numWords=5`, `voiceSeconds=0.3`, `backoffSeconds=1.0`.

---

### 5.5 `storage.py`
Saves outputs locally. Structured for easy S3 migration.

---

## 6. Agents — Detailed Spec

### 6.1 `research_agent.py` — DSPy `PRResearchAgent`
The core intelligence layer. Uses **DSPy** with `ChainOfThought` over the `NewsAngleInterviewer` signature.

#### DSPy Signature inputs/outputs:

| Field | Type | Description |
|---|---|---|
| `transcript` | Input | Cleaned 1st meeting transcript (truncated to 6000 chars) |
| `context` | Input | Company background text (truncated to 1000 chars) |
| `linkedin_data` | Input | Structured LinkedIn profile (truncated to 2000 chars) |
| `website_data` | Input | Company website content (truncated to 2000 chars) |
| `questions` | Output | 12 interview questions covering all 5 signal types |
| `signal_targets` | Output | Signal type per question: funding/product/traction/founder/insight |
| `interview_strategy` | Output | Paragraph identifying 2-3 strongest signals |

#### DSPy Optimization:
On first run, `optimize_agent()` uses `BootstrapFewShot` with `max_bootstrapped_demos=2` against 4 training companies (Clarasight, Knowidea, LastMile, PineTree — Aisa excluded). Optimized weights saved to `tests/optimization_results/optimized_agent.json` and reloaded on subsequent runs.

Metric: F1 score on keyword overlap between generated and ground truth questions (70% weight) + signal type coverage across all 5 types (30% weight).

#### Output — `BriefingDoc`:
```json
{
  "session_id": "",
  "company_folder": "",
  "questions": [],
  "signal_targets": [],
  "interview_strategy": "",
  "known_facts": [],
  "errors": []
}
```

---

### ~~6.2 `question_generator.py`~~ — DEPRECATED
> **Not used by the active pipeline.** Superseded by `research_agent.py`. Was a standalone Claude-based question generator that loaded `prompts/question_generator.txt` and returned a structured `QuestionSet`. DSPy `PRResearchAgent` replaced this approach entirely.

### ~~6.3 `question_evaluator.py`~~ — DEPRECATED
> **Not used by the active pipeline.** Superseded by `research_agent.py`. Was a Claude-as-judge scorer that evaluated questions against `2nd Meeting.txt` ground truth. DSPy's built-in `BootstrapFewShot` metric handles optimization natively.

### ~~6.4 `prompt_optimizer.py`~~ — DEPRECATED
> **Not used by the active pipeline.** Superseded by `research_agent.py`. Was a manual generate → evaluate → rewrite loop (up to 10 iterations, score threshold 0.80). Replaced by DSPy which handles all of this internally.

---

### 6.2 `synthesis_agent.py`
Runs after Vapi call ends. Triggered by Vapi webhook. Reads full interview transcript against briefing doc. Uses `prompts/synthesizer.txt`.

#### Output — `{session_id}_output.md` sections:
- **Interview Summary** — one paragraph overview
- **Newsworthy Scorecard** — each signal marked `confirmed` / `unconfirmed` / `needs follow-up` with evidence quote
- **Structured Interview Notes** — key findings by signal type
- **Recommended Pitch Angle** — top 1-2 signals with specific data points
- **Transcript (Timestamped)** — full interview transcript

Signal types scored: `funding`, `product_world_first`, `product_world_best`, `traction_revenue`, `traction_growth`, `traction_quality`, `founder_uniqueness`, `insight_contrarian`

---

## 7. API Routes — `main.py`

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Serves `static/index.html` |
| `POST` | `/research` | Accepts transcript + optional URLs/context. Starts research agent as `BackgroundTasks`. Returns `session_id` immediately. Writes `status=processing` before returning to prevent race condition on first poll. |
| `GET` | `/research/status/{session_id}` | Poll for research completion. Returns `processing` / `ready` / `error` plus briefing doc when ready. |
| `POST` | `/start-interview` | PATCHes pre-built Vapi assistant with briefing doc system prompt. Returns `vapi_assistant_id` for browser SDK. |
| `POST` | `/vapi-webhook` | Receives Vapi `end-of-call-report`. Extracts transcript, resolves `session_id` from call metadata (with fallbacks to assistant name and assistant metadata). Saves transcript, fires synthesis agent as `asyncio.create_task`. |
| `GET` | `/results/{session_id}` | Returns synthesis output, briefing doc, and transcript for results page. Returns `status=processing` if output not yet written. |
| `GET` | `/static/*` | Served via `StaticFiles` mount |

### Session ID Resolution in Webhook
`session_id` lookup priority:
1. `message.call.metadata.session_id` (set by `vapi_service` during PATCH)
2. Assistant name: `PressClub-{session_id}` (fallback)
3. `message.assistant.metadata.session_id` (second fallback)

---

## 8. Data Flow — Session Lifecycle

```
User pastes transcript + context into index.html
        ↓
POST /research → session_id returned immediately
        ↓
BackgroundTasks: parse_input_folder → scrape_linkedin + read_website (parallel)
        → PRResearchAgent (DSPy) → BriefingDoc saved as {session_id}_briefing.json
        ↓
Frontend polls GET /research/status/{session_id} every 3s
        ↓ (status=ready)
User sees briefing doc on interview.html
        ↓
POST /start-interview → PATCH Vapi assistant with session system prompt
        → returns vapi_assistant_id
        ↓
Vapi browser SDK starts call with vapi_assistant_id
        ↓
Interview runs (Gemini 2.0 Flash via Vapi, guided by question bank)
        ↓
Call ends → Vapi fires POST /vapi-webhook (end-of-call-report)
        → transcript extracted, session_id resolved
        → asyncio.create_task: synthesis_agent runs
        ↓
User redirected to results.html
        ↓
Frontend polls GET /results/{session_id} until output.md ready
```

---

## 9. Newsworthy Signal Framework

The five signal types applied throughout the pipeline:

1. **Funding** — $1M+ general tech, $10M+ B2B SaaS (TechCrunch threshold)
2. **Product** — world-first or world-best positioning, benchmark improvements (performance/cost %)
3. **Traction** — revenue milestones, growth rate ("hits $X in Y timeframe"), Fortune 500 quality
4. **Founder** — 1-in-10,000 unique experience or background
5. **Insight** — contrarian data (e.g. industry avg 40% return rate vs client's 15%)

---

## 10. Environment Variables

```bash
ANTHROPIC_API_KEY=        # Claude API — research agent (DSPy), synthesis, optimizer
VAPI_API_KEY=             # Vapi — voice orchestration REST API
VAPI_ASSISTANT_ID=        # Pre-built Vapi assistant ID (PATCHed per session)
BRIGHTDATA_API_KEY=       # Bright Data — LinkedIn scraper
BRIGHTDATA_DATASET_ID=    # Bright Data dataset ID for LinkedIn profiles
```

Note: `GEMINI_API_KEY` and `OPENAI_API_KEY` are **not required** — Gemini runs inside Vapi (no direct API call), and audio transcription via Whisper was removed in favor of Vapi's built-in transcript.

---

## 11. Key Design Decisions Made During Sprint

| Decision | Why |
|---|---|
| DSPy `BootstrapFewShot` for research agent | Automatically improves question quality using training examples without manual prompt iteration |
| PATCH pre-built assistant instead of POST new one | Avoids Vapi rate limits from creating many assistants per demo; simpler session management |
| `BackgroundTasks` for research, `asyncio.create_task` for synthesis | Research must not block the HTTP response. Synthesis is fire-and-forget after webhook ACK. |
| Status file written before `add_task` | Prevents 404 on first poll before background task starts |
| Three-level session_id fallback in webhook | Vapi metadata propagation is unreliable — assistant name and metadata provide safety net |
| `Bright Data` replacing Oxylabs | Switched LinkedIn scraper provider; same fallback chain preserved |
| `input_parser.py` handles two transcript formats | Training data came in two formats (labeled same-line vs timestamped next-line); both normalized to PRESSCLUB/CUSTOMER |

---

*PressClub AI Voice Interviewer — Architecture v2.0 — Sprint May 2026*
