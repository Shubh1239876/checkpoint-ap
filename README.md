# Checkpoint — AP invoice review

Takes a vendor invoice PDF and decides **Approve / Hold / Reject**, with every step visible.
Built for the Zamp AI Solutions Analyst case study (PS-1). Design and assumptions: [PROCESS.md](PROCESS.md).

**AI reads, rules decide.** An AI model reads the PDF (digital or scanned) and words the result.
Groq is the main provider (free tier) and Gemini the automatic backup: if one is busy or out of
quota, the other takes over.
Every decision — vendor, duplicates, PO match, amounts — is deterministic code in `app/rules.py`.

## Run it on your computer

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export GROQ_API_KEY=gsk_...          # Windows PowerShell: $env:GROQ_API_KEY="gsk_..."
export GEMINI_API_KEY=...            # optional backup:     $env:GEMINI_API_KEY="..."
uvicorn app.main:app --reload
```
Open http://localhost:8000 and click **Run** on any sample invoice.

Check the keys and extraction accuracy on all 9 samples:
```bash
python tools/check_ai.py
```

## Deploy (Render, free)
1. Push this folder to a GitHub repository.
2. On render.com: **New → Blueprint**, pick the repo (it reads `render.yaml`).
3. Enter `GROQ_API_KEY` (and optionally `GEMINI_API_KEY`) when asked. Deploy. Your link is `https://checkpoint-ap-xxxx.onrender.com`.

The free plan sleeps after 15 minutes idle (first load then takes ~1 minute) and resets data on
redeploy — open the link a few minutes before any demo, and use **Rules → Reset demo data** to start clean.

## Project layout
```
app/
  llm.py        AI providers (Groq, Gemini) with time limits, retries and automatic fallback
  extract.py    stage 2: AI reads the invoice (value + confidence + source text per field);
                digital PDFs via their text layer, scanned PDFs via a page image
  rules.py      stages 3-8: validation, vendor, duplicates, PO match, amounts, decision (no AI)
  explain.py    stage 9: AI words the result; template fallback if AI is unavailable
  pipeline.py   runs the stages, streams each one to the UI, saves the audit trail
  main.py       web server (upload, live events, dashboard, review queue, settings)
  static/       the UI
  data/         vendor master, purchase orders, paid-invoice history (sample data)
samples/        9 invoice PDFs: 2 happy paths, 4 edge cases, 3 more handled cases
tools/          generate_data.py (rebuilds all sample data), check_ai.py (live accuracy check)
tests/          offline tests of every scenario (no API key needed): python tests/test_scenarios.py
```
