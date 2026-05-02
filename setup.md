# Setup Guide

**PressClub AI Voice Interviewer — how to run this on your machine**

---

## Prerequisites

- Python 3.10 or higher
- A Vapi account with a pre-built assistant created (dashboard.vapi.ai)
- API keys for: Anthropic, Vapi, and Bright Data

---

## Step 1 — Clone and enter the project

```bash
git clone <repo-url>
cd pressclub-sprint
```

---

## Step 2 — Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate      # Mac/Linux
# venv\Scripts\activate       # Windows
```

---

## Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

---

## Step 4 — Set up environment variables

Copy the example file and fill in your keys:

```bash
cp .env.example .env
```

Open `.env` and add the following:

```
ANTHROPIC_API_KEY=your_anthropic_key_here
VAPI_API_KEY=your_vapi_key_here
VAPI_ASSISTANT_ID=your_vapi_assistant_id_here
BRIGHTDATA_API_KEY=your_brightdata_key_here
BRIGHTDATA_DATASET_ID=your_brightdata_dataset_id_here
```

**Where to find each key:**

| Key | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `VAPI_API_KEY` | dashboard.vapi.ai → Account → API Keys |
| `VAPI_ASSISTANT_ID` | Create an assistant in Vapi dashboard, copy the ID from the URL or settings |
| `BRIGHTDATA_API_KEY` | brightdata.com → Account → API Token |
| `BRIGHTDATA_DATASET_ID` | brightdata.com → Datasets → LinkedIn profile dataset → copy dataset ID |

---

## Step 5 — Install and set up ngrok

Vapi needs to send webhook events (the interview transcript) to your local server after each call ends. For this to work, your server must be publicly reachable — ngrok creates a secure tunnel from a public URL to your localhost.

**Install ngrok:**

```bash
# Mac (Homebrew)
brew install ngrok

# Or download directly from https://ngrok.com/download
```

**Create a free ngrok account** at ngrok.com, then connect your auth token:

```bash
ngrok config add-authtoken your_ngrok_token_here
```

Your auth token is on your ngrok dashboard under **Your Authtoken**.

In your ngrok dashboard, go to Cloud Edge → Domains → click New Domain — it generates a free static domain (e.g. eclair.ngrok-free.app). Then run ngrok with: ngrok http --domain=eclair.ngrok-free.app 8000 and set https://eclair.ngrok-free.app/vapi-webhook as your Vapi webhook URL (under Assistants -> Messaging -> server url) once and never update it again.

---

## Step 6 — Configure the Vapi webhook URL

1. Start ngrok in a terminal (keep this running):

```bash
ngrok http 8000
```

You'll see output like:

```
Forwarding   https://abc123.ngrok-free.app -> http://localhost:8000
```

2. Copy the `https://...ngrok-free.app` URL.

3. Go to your Vapi assistant in the dashboard → **Server URL** (or Webhook settings) → set it to:

```
https://abc123.ngrok-free.app/vapi-webhook
```

4. Save the assistant. **Do this every time you restart ngrok** — the URL changes each session unless you have a paid ngrok plan with a fixed domain.

---

## Step 7 — Run the server

Open a second terminal (keep ngrok running in the first) and start the FastAPI server:

```bash
python main.py
```

The server starts at `http://localhost:8000`. You should see:

```
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:8000
```

Open your browser and go to `http://localhost:8000`.

---

## Step 8 — Run a full interview

1. **Upload page** (`/`) — paste a sales call transcript and optional company context (background text with a website URL and/or LinkedIn URL)
2. Click **Start Research** — the system reads the website, scrapes LinkedIn, and generates 12 interview questions (takes ~20-40 seconds)
3. **Interview page** — review the briefing doc, then click **Start Call** to begin the voice interview in your browser (allow microphone access)
4. Talk to Riley (the AI journalist) — she will work through the question bank and probe for specific numbers
5. End the call — the system automatically transcribes the interview and produces a newsworthy scorecard
6. **Results page** — view the scorecard, structured notes, and recommended pitch angle, then download the output

---

## Optional: Run benchmarks

```bash
python -m tests.benchmark
```

---

## Troubleshooting

**Microphone not working in browser** — Chrome and Safari require HTTPS for microphone access in production. For local development, `localhost` is treated as secure so it should work. Firefox may require additional flags.

**LinkedIn scraping returns nothing** — Bright Data requires a valid dataset ID configured for LinkedIn profiles. Check your Bright Data dashboard to confirm the dataset exists and is active. The system will fall back to using context text if scraping fails.

**Research takes a long time** — The first run triggers DSPy optimization on the training data, which takes 2-5 minutes. Subsequent runs load the saved optimized agent from `tests/optimization_results/optimized_agent.json` and are much faster (~20 seconds).

**Vapi webhook not firing** — Make sure ngrok is running and the webhook URL in your Vapi assistant settings matches the current ngrok URL (see Step 6). The URL changes every time you restart ngrok unless you have a paid plan with a fixed domain.

**`VAPI_ASSISTANT_ID` not set** — You must create an assistant manually in the Vapi dashboard at least once. The system PATCHes this assistant with each session's content rather than creating a new one each time.

---

## Input data format

The system expects a company folder with these files:

```
CompanyName/
├── 1st Meeting.txt    # Sales call transcript
└── Context.txt        # Company background (include website URL and LinkedIn URL here)
```

`Context.txt` example:
```
Company name: Acme Corp
Website: https://acme.com
Founder LinkedIn: https://www.linkedin.com/in/jane-doe/
Brief: B2B SaaS for logistics automation. Raised $3M seed round.
```

Training and validation examples are in `data/Training/` and `data/Validation/`.
