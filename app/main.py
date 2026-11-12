"""Web server: upload → run pipeline in a background thread → stream stages over SSE."""
import asyncio
import json
import threading
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import pipeline, rules, store

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
STATIC = Path(__file__).parent / "static"
MAX_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Checkpoint — AP invoice review")
store.init()

# extractor is a module attribute so tests/dev_server.py can swap in the offline stand-in
EXTRACTOR = pipeline.extract_invoice

# ---------------------------------------------------------------- live event hub
_events: dict[int, list] = {}
_done: set[int] = set()


def _emitter(run_id: int):
    _events[run_id] = []

    def emit(ev: dict):
        _events[run_id].append(ev)
        if ev["type"] in ("done", "error"):
            _done.add(run_id)
    return emit


def _start(filename: str, pdf: bytes, po_override: str | None = None) -> int:
    if not pdf.startswith(b"%PDF"):
        raise HTTPException(400, "That file isn't a PDF.")
    if len(pdf) > MAX_BYTES:
        raise HTTPException(400, "PDF is larger than 10 MB.")
    h = pipeline.file_hash(pdf)
    rid = store.create_run(filename, h, "")
    path = store.UPLOAD_DIR / f"{rid}.pdf"
    path.write_bytes(pdf)
    store.update_run(rid, file_path=str(path))
    threading.Thread(target=pipeline.run_pipeline, args=(rid, pdf, _emitter(rid)),
                     kwargs={"extractor": EXTRACTOR, "po_override": po_override}, daemon=True).start()
    return rid


@app.post("/api/runs")
async def create_run(file: UploadFile = File(...)):
    return {"id": _start(file.filename or "invoice.pdf", await file.read())}


@app.post("/api/runs/sample/{name}")
def run_sample(name: str):
    path = SAMPLES / Path(name).name
    if not path.exists() or path.suffix != ".pdf":
        raise HTTPException(404, "Unknown sample")
    return {"id": _start(path.name, path.read_bytes())}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: int):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404)

    async def gen():
        # finished runs (or runs from before a restart) are replayed from the database
        if run_id not in _events:
            r = store.get_run(run_id)
            for st in r.get("stages") or []:
                yield {"data": json.dumps({"type": "stage_end", **st})}
            yield {"data": json.dumps({"type": "done" if r["status"] == "done" else "error",
                                       "decision": r["decision"], "explanation": r.get("explanation"),
                                       "message": r.get("error")})}
            return
        i = 0
        while True:
            evs = _events.get(run_id, [])
            while i < len(evs):
                yield {"data": json.dumps(evs[i], default=str)}
                i += 1
            if run_id in _done and i >= len(evs):
                return
            await asyncio.sleep(0.1)

    return EventSourceResponse(gen())


@app.get("/api/runs")
def list_runs():
    out = []
    for r in store.list_runs():
        top = next((f for f in (r.get("findings") or []) if f["severity"] in ("reject", "hold")), None)
        out.append({k: r.get(k) for k in ("id", "created_at", "filename", "status", "decision", "headline",
                                          "vendor_name", "invoice_number", "total", "po_number",
                                          "review_status", "reviewer_note", "duration_ms")}
                   | {"top_reason": top["title"] if top else None, "top_code": top["code"] if top else None})
    return out


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/runs/{run_id}/file")
def get_run_file(run_id: int):
    r = store.get_run(run_id)
    if not r or not r.get("file_path") or not Path(r["file_path"]).exists():
        raise HTTPException(404)
    return FileResponse(r["file_path"], media_type="application/pdf")


class Review(BaseModel):
    action: str  # approve | reject
    note: str = ""
    reviewer: str = "AP reviewer"


@app.post("/api/runs/{run_id}/review")
def review(run_id: int, body: Review):
    r = store.get_run(run_id)
    if not r or r["decision"] != "HOLD":
        raise HTTPException(400, "Only held invoices can be reviewed.")
    if r.get("review_status") in ("approved", "rejected"):
        raise HTTPException(400, "Already reviewed.")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if not body.note.strip():
        raise HTTPException(400, "A note is required so the audit trail says why.")
    status = "approved" if body.action == "approve" else "rejected"
    store.update_run(run_id, review_status=status, reviewer_note=f"{body.reviewer}: {body.note.strip()}",
                     reviewed_at=store.now_iso())
    if status == "approved" and r.get("invoice_number"):
        store.post_to_ledger(run_id, {"invoice_number": r["invoice_number"], "vendor_id": r["vendor_id"],
                                      "po_number": r["po_number"],
                                      "invoice_date": rules.val(r.get("extraction") or {}, "invoice_date"),
                                      "subtotal": next((s["data"].get("subtotal") for s in r["stages"]
                                                        if s["stage"] == "validate"), r["total"]),
                                      "total": r["total"]})
    return store.get_run(run_id)


class UsePO(BaseModel):
    po_number: str


@app.post("/api/runs/{run_id}/use-po")
def use_po(run_id: int, body: UsePO):
    """Reviewer resolves an ambiguous PO: re-run every check against the chosen PO."""
    r = store.get_run(run_id)
    if not r or r.get("review_status") != "pending":
        raise HTTPException(400, "Only invoices awaiting review can be re-checked.")
    po = rules.PO_BY_NUMBER.get(body.po_number)
    if not po or (r.get("vendor_id") and po["vendor_id"] != r["vendor_id"]):
        raise HTTPException(400, "That PO doesn't belong to this vendor.")
    new_id = _start(r["filename"], Path(r["file_path"]).read_bytes(), po_override=body.po_number)
    store.update_run(run_id, review_status="superseded", reviewed_at=store.now_iso(),
                     reviewer_note=f"AP reviewer: chose {body.po_number}; re-checked in run #{new_id}")
    return {"id": new_id}


@app.get("/api/metrics")
def metrics():
    runs = [r for r in store.list_runs() if r["status"] in ("done", "error")]
    n = len(runs)
    dec = Counter(r["decision"] for r in runs)
    reasons = Counter()
    for r in runs:
        if r["decision"] in ("HOLD", "REJECT"):
            for f in r.get("findings") or []:
                if f["severity"] in ("hold", "reject"):
                    reasons[f["title"] if f["code"] not in ("PO_OVERRUN",) else "Billing exceeds PO"] += 1
    value = lambda d: round(sum(r["total"] or 0 for r in runs if d is None or r["decision"] == d), 2)
    times = [r["duration_ms"] for r in runs if r.get("duration_ms")]
    return {
        "processed": n, "approved": dec["APPROVE"], "held": dec["HOLD"], "rejected": dec["REJECT"],
        "straight_through_rate": round(dec["APPROVE"] / n, 3) if n else None,
        "pending_review": sum(1 for r in runs if r.get("review_status") == "pending"),
        "value_processed": value(None), "value_approved": value("APPROVE"),
        "value_held": value("HOLD"), "value_rejected": value("REJECT"),
        "avg_seconds": round(sum(times) / len(times) / 1000, 1) if times else None,
        "reasons": reasons.most_common(8),
    }


@app.get("/api/reference")
def reference():
    ledger = store.ledger_rows()
    pos = []
    for po in rules.POS:
        invoiced, remaining, _ = rules.remaining_on_po(po, ledger)
        pos.append(po | {"invoiced": invoiced, "remaining": remaining,
                         "vendor_name": rules.VENDOR_BY_ID[po["vendor_id"]]["legal_name"]})
    return {"buyer": rules.BUYER, "vendors": rules.VENDORS, "purchase_orders": pos, "ledger": ledger}


@app.get("/api/settings")
def get_settings():
    return store.get_settings()


@app.put("/api/settings")
def put_settings(values: dict):
    return store.update_settings(values)


@app.post("/api/reset")
def reset():
    store.init(reset=True)
    _events.clear()
    _done.clear()
    return {"ok": True}


@app.get("/api/samples")
def samples():
    return json.loads((SAMPLES / "manifest.json").read_text())


@app.get("/api/samples/{name}")
def sample_file(name: str):
    path = SAMPLES / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/api/health")
def health():
    from . import llm
    return {"ok": True, "providers": llm.providers(),
            "extractor": getattr(EXTRACTOR, "__name__", str(EXTRACTOR))}


@app.exception_handler(HTTPException)
async def http_err(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
