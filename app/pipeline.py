"""Runs the stages in order and emits an event after each one, so the UI can show the
run live. Everything a stage saw and decided is persisted on the run for the audit trail."""
import hashlib
import io
import time
import traceback
from datetime import date
from typing import Callable

from pypdf import PdfReader

from . import rules, store
from .explain import explain
from .extract import ExtractionError, extract_invoice

STAGES = [
    ("intake", "Receive document"),
    ("extract", "Read invoice", ),
    ("validate", "Validate fields & arithmetic"),
    ("vendor", "Verify vendor"),
    ("duplicate", "Check for duplicates"),
    ("po", "Match purchase order"),
    ("amount", "Check amounts against PO"),
    ("decide", "Decide"),
    ("explain", "Explain"),
]
LABEL = dict(STAGES)
AI_STAGES = {"extract", "explain"}


def stage_status(findings: list[dict]) -> str:
    worst = max((rules.SEVERITY_RANK[f["severity"]] for f in findings), default=0)
    return {4: "reject", 3: "hold", 2: "warn"}.get(worst, "pass")


def file_hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def run_pipeline(run_id: int, pdf: bytes, emit: Callable[[dict], None],
                 extractor: Callable = extract_invoice, today: date | None = None,
                 po_override: str | None = None):
    today = today or date.today()
    settings = store.get_settings()
    stages_log: list[dict] = []
    all_findings: list[rules.Finding] = []
    t_run = time.time()

    def stage(key, fn):
        emit({"type": "stage_start", "stage": key, "label": LABEL[key], "ai": key in AI_STAGES})
        t0 = time.time()
        out = fn()
        findings, data, summary = out[:3]
        fd = rules.to_dicts(findings)
        entry = {"stage": key, "label": LABEL[key], "ai": key in AI_STAGES,
                 "status": out[3] if len(out) > 3 else stage_status(fd),
                 "summary": summary, "findings": fd, "data": data, "ms": int((time.time() - t0) * 1000)}
        stages_log.append(entry)
        all_findings.extend(findings)
        store.update_run(run_id, stages_json=stages_log)
        emit({"type": "stage_end", **entry})
        return entry

    try:
        # 1 intake
        meta = {}

        def intake():
            reader = PdfReader(io.BytesIO(pdf))
            text = "\n".join((p.extract_text() or "") for p in reader.pages)
            meta.update(pages=len(reader.pages), text=text,
                        kind="digital" if len(text.strip()) > 40 else "scanned (no text layer)")
            return [], {"pages": meta["pages"], "kind": meta["kind"], "sha256": file_hash(pdf)[:16],
                        "bytes": len(pdf)}, f"{meta['pages']} page PDF · {meta['kind']}"

        stage("intake", intake)

        # 2 extract
        ex_holder = {}

        def extract():
            try:
                ex, info = extractor(pdf, meta.get("text", ""))
            except ExtractionError as e:
                return [rules.Finding("EXTRACTION_FAILED", "hold", "The invoice could not be read automatically",
                                      str(e), "Process this invoice manually.")], {}, "Failed"
            ex_holder["ex"] = ex
            fields = sum(1 for k, v in ex.items() if isinstance(v, dict) and v.get("value") not in (None, ""))
            return [], {"extraction": ex, "model": info.get("model"), "provider": info.get("provider"),
                        "mode": info.get("mode"), "attempts": info.get("attempts"),
                        "fallbacks": info.get("fallbacks") or []}, \
                f"{fields} fields, {len(ex.get('line_items') or [])} line items · {info.get('model')}"

        e = stage("extract", extract)
        ex = ex_holder.get("ex")
        store.update_run(run_id, extraction_json=ex)
        ledger = store.ledger_rows()
        vendor = po = None
        ctx = {}

        if ex:
            # 3 validate
            def validate():
                F, c = rules.validate(ex, settings, today)
                ctx.update(c)
                return F, {"subtotal": c.get("subtotal"), "total": c.get("total"), "tax_rate": c.get("rate"),
                           "tax_treatment": c.get("treatment")}, \
                    f"Total {rules.inr(c.get('total'))} · pre-tax {rules.inr(c.get('subtotal'))}"
            stage("validate", validate)

            # 4 vendor
            def vend():
                nonlocal vendor
                F, vendor = rules.check_vendor(ex)
                return F, {"vendor": vendor}, (vendor["legal_name"] + f" ({vendor['vendor_id']})") if vendor else "Unknown vendor"
            stage("vendor", vend)

            # 5 duplicates
            def dup():
                F = rules.check_duplicates(ex, vendor, ctx, ledger,
                                           store.runs_with_hash(file_hash(pdf), run_id), settings)
                return F, {"ledger_rows_checked": len(ledger)}, f"Compared against {len(ledger)} ledger entries"
            stage("duplicate", dup)

            # 6 PO
            def pom():
                nonlocal po
                if po_override:
                    po = rules.PO_BY_NUMBER[po_override]
                    return [rules.Finding("PO_BY_REVIEWER", "info", f"{po_override} chosen by a reviewer",
                                          "The invoice had no usable PO reference; a person picked the PO and "
                                          "every other check was re-run against it.")], {"po": po}, f"{po_override} · chosen by reviewer"
                F, po = rules.match_po(ex, vendor, ctx, ledger, settings)
                return F, {"po": po}, po["po_number"] if po else "No PO matched"
            stage("po", pom)

            # 7 amounts
            def amt():
                F = rules.check_amounts(ex, po, ctx, ledger, settings)
                if not po:
                    return F, {}, "Skipped — no PO to compare against", "skip"
                return F, {}, "Within PO" if stage_status(rules.to_dicts(F)) in ("pass", "warn") else "Issue found"
            stage("amount", amt)

        # 8 decide
        decision = rules.decide(all_findings)
        fd_all = rules.to_dicts(all_findings)
        invoice_summary = {
            "invoice_number": rules.val(ex, "invoice_number") if ex else None,
            "invoice_date": rules.val(ex, "invoice_date") if ex else None,
            "vendor_name": vendor["legal_name"] if vendor else (rules.val(ex, "vendor_name") if ex else None),
            "vendor_id": vendor["vendor_id"] if vendor else None,
            "po_number": po["po_number"] if po else None,
            "subtotal": ctx.get("subtotal"), "total": ctx.get("total"),
        }

        def dec():
            counts = {s: sum(1 for f in fd_all if f["severity"] == s) for s in ("reject", "hold", "warn")}
            return [], {"decision": decision, "counts": counts}, \
                f"{decision} · {counts['reject']} blocking, {counts['hold']} need review, {counts['warn']} warnings", \
                {"APPROVE": "pass", "HOLD": "hold", "REJECT": "reject"}[decision]
        stage("decide", dec)

        # 9 explain
        expl = {}

        def exp():
            expl.update(explain(decision, fd_all, invoice_summary, vendor))
            return [], {"explanation": expl}, f"Worded by {expl.get('source')}" if expl.get("source") != "template" else "Template (AI unavailable)"
        stage("explain", exp)

        if decision == "APPROVE" and invoice_summary["invoice_number"]:
            store.post_to_ledger(run_id, invoice_summary)
        store.update_run(
            run_id, status="done", decision=decision, headline=expl.get("headline"),
            vendor_id=invoice_summary["vendor_id"], vendor_name=invoice_summary["vendor_name"],
            invoice_number=invoice_summary["invoice_number"], total=invoice_summary["total"],
            po_number=invoice_summary["po_number"], findings_json=fd_all, explanation_json=expl,
            review_status="pending" if decision == "HOLD" else None,
            duration_ms=int((time.time() - t_run) * 1000))
        emit({"type": "done", "decision": decision, "explanation": expl, "invoice": invoice_summary,
              "duration_ms": int((time.time() - t_run) * 1000)})
    except Exception as exc:  # never leave a run stuck in "running"
        traceback.print_exc()
        store.update_run(run_id, status="error", decision="HOLD", error=str(exc), review_status="pending",
                         headline="Hold — the process hit an unexpected error; review manually.")
        emit({"type": "error", "message": str(exc)})
