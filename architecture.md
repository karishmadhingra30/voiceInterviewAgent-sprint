# PressClub AI Voice Interviewer — Architecture

**Sprint Build — May 2026 | Author: K. Dhingra**

---

## 1. Project Overview

PressClub is a PR automation service that achieves a 10x higher journalist response rate than industry average. The current workflow requires a human to conduct a 30-minute interview with each startup founder to extract newsworthy angles before launching an outreach campaign.

This system replaces that human interviewer with an AI voice agent. The agent ingests all available context about a startup, conducts a structured interview via a browser-based call, and produces structured output that feeds directly into PressClub's existing pitch generation pipeline.

| Attribute | Value |
|---|---|
| Sprint Duration | 24 hours |
| Evaluation Criteria | Completeness, Engineering Excellence, Collaboration |
| Deployment Target | Standalone browser interface (no auth required) |
| Output Format | .txt / .markdown for pipeline ingestion |
| Voice Platform | Vapi (managed orchestration) |
| Primary LLM | Gemini (via Vapi) + Claude (research & synthesis) |

---

## 2. Architecture Decision — Why Vapi

Three approaches were evaluated:

| Approach | Latency | Control | Sprint Risk | Decision |
|---|---|---|---|---|
| Native Voice-to-Voice (Gemini Live, OpenAI Realtime) | 300ms | Black box | High — no visibility into reasoning, stretch goals very hard | Rejected |
| Raw Orchestration (Whisper + Claude + ElevenLabs) | 800-1500ms | Full | High — WebRTC alone is a multi-day problem | Rejected |
| Vapi (managed orchestration) | <600ms | High — Custom LLM URL for stretch goals | Low — one SDK, audio fully abstracted | **Selected** |

**Key rationale:** Vapi abstracts WebRTC, STT, TTS, turn-taking, recording, and transcript generation under one SDK. This frees all engineering time for the intelligence layer — signal detection, briefing doc synthesis, and question strategy — which is the actual differentiated value.

**Future-proofing:** Stretch goals are additive, not rewrites.
- Stretch 1 (dynamic follow-ups) = point Vapi at a Custom LLM URL endpoint
- Stretch 2 (rambling interruption) = add `stopSpeakingPlan` config and hooks
- Zero audio code changes required for either

---

## 3. System Architecture — End to End

| Phase | Input | Process | Output |
|---|---|---|---|
| **0 — Ingestion** | .txt transcript OR audio file + Company URL + LinkedIn URL | Transcriber converts audio to text. Files normalized to clean strings. | `raw_transcript` (str), `company_url` (str), `linkedin_url` (str) |
| **1 — Research Agent** | raw_transcript, company_url, linkedin_url | Claude reads website. Oxylabs scrapes LinkedIn. Claude synthesizes all sources against newsworthy angle framework. | `briefing_doc.json` — known facts, hypothesized angles, ranked questions, interview strategy |
| **2 — Vapi Interview** | briefing_doc.json + founder in browser | Vapi browser SDK handles audio. Gemini LLM uses briefing doc as system prompt context. Interview runs 20-30 min. | Audio file (stored by Vapi) + raw transcript (Vapi webhook) |
| **3 — Synthesis Agent** | Vapi transcript + briefing_doc.json | Claude reads full interview transcript. Scores each newsworthy signal. Produces structured notes. | `output.md` — scorecard + structured notes → Weida's pipeline |

---

## 4. File Structure

```
pressclub-sprint/
├── main.py                    # FastAPI: all routes + CORS + Vapi webhooks
├── agents/
│   ├── research_agent.py      # Phase 1: orchestrates all research → Briefing Doc
│   └── synthesis_agent.py     # Phase 3: transcript → scorecard + .txt output
├── services/
│   ├── transcriber.py         # Audio/txt → clean text (Whisper)
│   ├── crawler_service.py     # Oxylabs LinkedIn API wrapper
│   ├── website_reader.py      # URL → structured content via Claude
│   ├── vapi_service.py        # Vapi assistant configuration only
│   └── storage.py             # Save .txt, .mp3, .md locally
├── prompts/
│   ├── research.txt           # Signal detection prompt
│   ├── interviewer.txt        # Tenacious journalist persona
│   └── synthesizer.txt        # Newsworthy pitch generator
├── static/
│   ├── index.html             # Upload form: transcript + URLs
│   ├── interview.html         # Active call page (Vapi browser SDK)
│   └── results.html           # Scorecard + transcript + download
├── tests/
│   ├── benchmark.py           # All step benchmarks with pass/fail output
│   └── fixtures/
│       ├── sample_transcript.txt
│       ├── expected_briefing.json
│       └── expected_scorecard.txt
├── .env                       # All API keys (never committed)
├── .env.example               # Key template for collaborators
├── requirements.txt
└── README.md
```

---

## 5. Services — Detailed Spec

### 5.1 `transcriber.py`
Converts any input format to clean transcript text.

| Input | Process | Output |
|---|---|---|
| `.txt` file | Read file directly | String |
| Audio file (`.mp3`/`.wav`) | Whisper API transcription | String |

- **Tools:** `openai.Audio.transcribe` (Whisper), Python file I/O
- **Pass criteria:** `.txt` = 100% accuracy. Audio = >90% word accuracy, <30s runtime.

---

### 5.2 `crawler_service.py`
Wraps Oxylabs LinkedIn scraper API. Returns normalized founder/company profile.

| Field Extracted | Used For |
|---|---|
| Name + current role | Personalization in interview |
| Company name + stage | Signal context |
| Work history highlights | Founder uniqueness signal (1-in-10,000 rarity check) |
| Education | Secondary signal |

- **Pass criteria:** Required fields present, manual accuracy check vs actual LinkedIn page, <10s runtime.

---

### 5.3 `website_reader.py`
Passes company URL to Claude with web access. Extracts structured content.

| Field Extracted | Newsworthy Signal |
|---|---|
| Product description | World-first or world-best positioning |
| Metrics mentioned | Traction signals, benchmark improvements |
| Funding mentioned | Funding announcement signals |
| Customer names | Quality traction (Fortune 500 vs general) |

- **Pass criteria:** Zero hallucinated facts (verified against actual site), key fields present, <15s runtime.

---

### 5.4 `vapi_service.py`
Configures and creates a Vapi assistant. Injects briefing doc into system prompt. Returns `assistant_id` for browser SDK.

- **MVP:** Uses Vapi built-in Gemini LLM
- **Stretch 1:** Replace model config with Custom LLM URL pointing to FastAPI `/chat/completions` endpoint

---

### 5.5 `storage.py`
Saves all outputs locally. Structured for easy S3 migration as stretch goal.

| File | When Saved |
|---|---|
| `briefing_doc.json` | After research agent completes |
| `interview_audio.mp3` | After Vapi call ends (Vapi provides URL) |
| `interview_transcript.txt` | After Vapi webhook fires |
| `output.md` | After synthesis agent completes |

---

## 6. Agents — Detailed Spec

### 6.1 `research_agent.py`
Orchestrates Phase 1 end-to-end. The quality of the briefing doc determines the quality of the entire interview.

#### Tools defined:

| Tool Name | Description | Input | Output |
|---|---|---|---|
| `parse_transcript` | Extract key facts from sales call text | raw transcript string | facts dict |
| `read_website` | Calls website_reader.py | company URL | structured content dict |
| `scrape_linkedin` | Calls crawler_service.py | LinkedIn URL | profile dict |
| `synthesize_briefing` | Claude synthesizes all sources into briefing doc | facts + website + linkedin | briefing_doc.json |

#### Newsworthy angle framework applied:
1. **Funding signals** — $1M+ general tech, $10M+ B2B SaaS (TechCrunch threshold)
2. **Product signals** — world-first or world-best positioning, benchmark improvements (performance/cost %)
3. **Traction signals** — revenue milestones, growth rate (format: "hits $X in Y timeframe"), Fortune 500 quality
4. **Founder signals** — 1-in-10,000 unique experience or background
5. **Insight signals** — contrarian data (e.g. industry avg 40% return rate vs client's 15%)

#### Output schema — `briefing_doc.json`:
```json
{
  "known_facts": [],
  "hypothesized_angles": [],
  "priority_questions": [],
  "interview_strategy": "",
  "avoid_topics": []
}
```

- **Pass criteria:** Signal recall >80% vs Weida ground truth. False positives <2. Question quality rated >7/10 by Weida on 3 training sets. <60s runtime.

---

### 6.2 `synthesis_agent.py`
Runs after Vapi call ends. Reads full interview transcript against the briefing doc. Produces final output for Weida's pipeline.

#### Output — `output.md`:
- Timestamped interview transcript
- Newsworthy scorecard — each signal marked `confirmed` / `unconfirmed` / `needs follow-up`
- Structured interview notes keyed to signal type
- Recommended pitch angle (top 1-2 signals worth pursuing)

- **Pass criteria:** Scorecard accuracy verified against 2 validation sets. Output format confirmed by Weida as pipeline-compatible. <30s runtime post-call.

---

## 7. Prompts

### 7.1 `research.txt` — Signal Detection
Instructs Claude how to read a sales transcript and map content to the newsworthy angle framework. Specifies output schema. Emphasizes: never fabricate, only extract what is explicitly stated or strongly implied.

### 7.2 `interviewer.txt` — Journalist Persona
Defines the Vapi agent persona:
- **Persona:** Senior tech journalist, 10 years covering B2B SaaS and deep tech
- **Tone:** Curious, warm, but persistent — does not accept vague answers
- **Strategy:** Always asks for specific numbers, not generalizations
- **Briefing doc injected:** Knows what to ask and what to skip
- **Fallback:** If founder deflects, reframes and asks again from a different angle

### 7.3 `synthesizer.txt` — Pitch Generator
Instructs Claude how to map interview transcript to newsworthy signals. Applies PressClub's specific thresholds ($1M/$10M, world-first requirement, Fortune 500 quality bar). Produces scorecard in consistent format for pipeline ingestion.

---

## 8. API Routes — `main.py`

| Method | Route | Description |
|---|---|---|
| `POST` | `/research` | Accepts transcript + URLs, runs research agent, returns `briefing_doc.json` |
| `POST` | `/start-interview` | Creates Vapi assistant with briefing doc, returns `assistant_id` for browser SDK |
| `POST` | `/vapi-webhook` | Receives post-call transcript from Vapi, triggers synthesis agent |
| `GET` | `/results/{session_id}` | Returns scorecard + transcript for results page |
| `GET` | `/static/*` | Serves HTML frontend files |

---

## 9. Stretch Goals

| Goal | What Changes | Files Modified | Estimated Time |
|---|---|---|---|
| **Stretch 1:** Dynamic follow-ups | Add `/chat/completions` FastAPI endpoint. Point Vapi model config to Custom LLM URL. Add signal-checking logic between turns. | `main.py` (+1 route), `vapi_service.py` (model config change only) | 2-3 hours |
| **Stretch 2:** Rambling interruption | Add `stopSpeakingPlan` config to Vapi assistant. Add `user-interrupted` hook that injects redirect message into LLM context. | `vapi_service.py` (config addition only) | 1 hour |

---

## 10. Benchmarks & Testing

| Step | Functional Test | Quality Benchmark | Pass Criteria |
|---|---|---|---|
| Step 1: Input Handler | txt and audio load without error | Audio spot-check vs ground truth | txt 100%, audio >90% word accuracy, <30s |
| Step 2: LinkedIn Scraper | Oxylabs API returns 200 | Manual check vs actual LinkedIn | All required fields present, <10s |
| Step 3: Website Reader | Claude returns non-empty output | Zero hallucinated facts | Key fields present, verified vs site, <15s |
| Step 4: Briefing Doc | Valid JSON, all fields present | Compare signals vs Weida ground truth | >80% signal recall, <2 false positives, >7/10 quality |
| Step 5: Vapi Assistant | Test call connects, agent speaks | On-topic rate, briefing doc usage | >90% on-topic, never asks known facts |
| Step 6: Browser Interface | Page loads, mic permission fires | Works Chrome + Safari | <3s load, <5s to first speech |
| Step 7: Synthesis | Webhook received, .md file created | Scorecard vs validation sets | Weida confirms pipeline compatible |
| Step 8: Results Page | Renders without errors | Data matches output file | Download works, scorecard visible |

---

## 11. Environment Variables

```bash
ANTHROPIC_API_KEY=        # Claude API — research + synthesis agents
VAPI_API_KEY=             # Vapi — voice orchestration
GEMINI_API_KEY=           # Gemini — LLM inside Vapi
OXYLABS_USERNAME=         # LinkedIn scraper
OXYLABS_PASSWORD=         # LinkedIn scraper
OPENAI_API_KEY=           # Whisper — audio transcription
```

---

## 12. Build Order — Hour by Hour

| Hour | Task | Deliverable | Benchmark |
|---|---|---|---|
| Hour 1 | `transcriber.py` + `crawler_service.py` + `website_reader.py` | All three services running independently | Steps 1-3 pass |
| Hour 2 | `research_agent.py` + `prompts/research.txt` | Briefing doc produced from training input | Step 4 passes vs Weida ground truth |
| Hour 3 | `vapi_service.py` + `prompts/interviewer.txt` + `main.py` skeleton | Vapi assistant created, test call connects | Step 5 passes |
| Hour 4 | `static/interview.html` + `static/index.html` + Vapi webhook route | End-to-end: upload → interview → transcript received | Step 6 passes |
| Hour 5 | `synthesis_agent.py` + `prompts/synthesizer.txt` + `static/results.html` | Full pipeline working | Steps 7-8 pass |
| Buffer | `requirements.txt` + README + `.env.example` + cleanup | Repo ready for Weida review | All steps green |
| Stretch | Custom LLM URL + `stopSpeakingPlan` if time permits | Dynamic follow-ups working | Stretch benchmarks pass |

---

*PressClub AI Voice Interviewer — Architecture v1.0 — Sprint May 2026*
