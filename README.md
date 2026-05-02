# PressClub AI Voice Interviewer

AI voice agent that interviews startup founders to extract newsworthy angles 
for PR pitch generation.

## How It Works
1. Input sales transcripts + company URL + LinkedIn
2. Research agent builds a briefing doc
3. AI voice agent conducts structured interview via browser
4. Output: transcript + audio + newsworthy scorecard → pitch pipeline

## Stack
- Vapi (voice orchestration)
- Claude (LLM)
- FastAPI (backend)
- Plain HTML/JS (frontend)

## Setup
pip install -r requirements.txt
Add API keys to .env (see .env.example)
python main.py

Link to Architecture - 
