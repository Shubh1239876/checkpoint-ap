"""Checks your AI keys and how accurately the invoices are read, end to end.

    $env:GROQ_API_KEY="gsk_..."          (and/or $env:GEMINI_API_KEY="...")
    python tools/check_ai.py              # all 9 samples
    python tools/check_ai.py 02           # just one
"""
import json
import os
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["CHECKPOINT_DB"] = f"{tmp}/check.db"
os.environ["CHECKPOINT_UPLOADS"] = f"{tmp}/uploads"

import httpx  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import llm, rules, store  # noqa: E402
from app.pipeline import file_hash, run_pipeline  # noqa: E402

print("Providers that will be used, in order:", llm.providers() or "NONE - set GROQ_API_KEY or GEMINI_API_KEY")
if not llm.providers():
    sys.exit(1)


class Ping(BaseModel):
    reply: str


if llm.groq_key():
    try:
        r = httpx.get("https://api.groq.com/openai/v1/models", timeout=20,
                      headers={"Authorization": f"Bearer {llm.groq_key()}"})
        r.raise_for_status()
        ids = sorted(m["id"] for m in r.json()["data"])
        print("\nGroq models on this key:", ", ".join(ids))
        for label, models in (("text", llm.GROQ_TEXT_MODELS), ("vision", llm.GROQ_VISION_MODELS),
                              ("explain", llm.GROQ_EXPLAIN_MODELS)):
            print(f"  {label:7s}: " + ", ".join(f"{m} {'OK' if m in ids else '(not available)'}" for m in models))
    except Exception as e:
        print("Could not list Groq models:", str(e)[:200])
    for m in (llm.GROQ_TEXT_MODELS[0], llm.GROQ_VISION_MODELS[0]):
        t = time.time()
        try:
            llm.groq_json([m], [{"role": "user", "content": 'Reply with JSON {"reply": "OK"}'}], Ping, timeout_s=30)
            print(f"  ping {m}: OK in {time.time() - t:.1f}s")
        except Exception as e:
            print(f"  ping {m}: FAILED - {str(e)[:250]}")

EXPECT = {"01": "APPROVE", "02": "APPROVE", "03": "HOLD", "04": "HOLD", "05": "HOLD",
          "06": "HOLD", "07": "REJECT", "08": "APPROVE", "09": "HOLD"}
CHECK = ["vendor_gstin", "buyer_gstin", "invoice_number", "invoice_date", "po_reference", "total", "bank_account", "ifsc"]
only = sys.argv[1] if len(sys.argv) > 1 else None
store.init(reset=True)
ok_all = True
print("\nReading the invoices (a short pause between each keeps us under free-tier limits)...", flush=True)
for i, pdf_path in enumerate(sorted((ROOT / "samples").glob("*.pdf"))):
    if only and not pdf_path.name.startswith(only):
        continue
    if i and not only:
        time.sleep(8)
    gt = json.loads((ROOT / "tests" / "fixtures" / f"{pdf_path.stem}.json").read_text())
    pdf = pdf_path.read_bytes()
    rid = store.create_run(pdf_path.name, file_hash(pdf), str(pdf_path))
    run_pipeline(rid, pdf, lambda e: None, today=date(2026, 11, 12))
    r = store.get_run(rid)
    ex = r.get("extraction") or {}
    st = next((s for s in r.get("stages") or [] if s["stage"] == "extract"), {})
    misses = []
    for k in CHECK:
        got, want = (ex.get(k) or {}).get("value"), gt.get(k)
        same = (got == want) or (isinstance(want, (int, float)) and got is not None and abs(float(got) - want) < 0.01) \
            or (k == "bank_account" and str(got or "").replace(" ", "") == str(want or "")) \
            or (k == "po_reference" and rules.norm_po(got) == rules.norm_po(want))
        if not same:
            misses.append(f"{k}: got {got!r}, expected {want!r}")
    failed_read = any(f["code"] == "EXTRACTION_FAILED" for f in (r.get("findings") or []))
    want = EXPECT[pdf_path.name[:2]]
    good = r["decision"] == want and not failed_read
    ok_all &= good
    d = st.get("data") or {}
    print(f"{'PASS' if good else 'FAIL'}  {pdf_path.name}  [{d.get('provider', '-')}/{d.get('model', '-')} via {d.get('mode', '-')}, {st.get('ms', 0) / 1000:.1f}s]")
    print(f"      decision {r['decision']} (expected {want}) - {r['headline']}")
    for m in misses:
        print("      read mismatch ->", m)
    for f in r.get("findings") or []:
        if f["severity"] in ("hold", "reject", "warn"):
            print(f"      [{f['severity']}] {f['title']}")
    for f in r.get("findings") or []:
        if f["code"] == "EXTRACTION_FAILED":
            print("      AI read failed ->", f["detail"][:500])
    if d.get("fallbacks"):
        print("      (first choice failed, backup used):", " | ".join(d["fallbacks"])[:300])
print("\nAll decisions as expected." if ok_all else "\nSome decisions differ - send this output to Claude.")
