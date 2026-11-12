"""Stage 9 — turning findings into a message an AP clerk can act on.

The decision is already made by rules. Gemini only rewrites the findings in plain
language (and drafts a vendor email when the vendor has to do something). If Gemini is
unavailable, a deterministic template produces the same structure, so a run never fails here.
"""
import json
import os
from typing import Optional

from pydantic import BaseModel

from . import llm

VENDOR_ACTION_CODES = {"MISSING_INVOICE_NUMBER", "SUM_MISMATCH", "TOTAL_MISMATCH", "LINE_MATH", "PO_OVERRUN",
                       "PO_AMBIGUOUS", "PO_NOT_FOUND", "PO_OTHER_VENDOR", "PRICE_ABOVE_PO", "WRONG_TAX_TYPE",
                       "EXACT_DUPLICATE", "PROBABLE_DUPLICATE"}


class VendorEmail(BaseModel):
    subject: str
    body: str


class Explanation(BaseModel):
    headline: str
    summary: str
    next_steps: list[str]
    vendor_email: Optional[VendorEmail] = None


PROMPT = """You write the result card an accounts-payable clerk reads after an automated invoice check.
The DECISION is final and was made by rules — do not change or question it, and do not add facts
that are not in the findings.

Write:
- headline: one sentence, starts with the decision word (Approve / Hold / Reject), says the main reason.
- summary: 2-3 plain sentences a non-technical person understands. Mention rupee amounts where relevant.
- next_steps: 1-3 short imperative steps, most important first, based on the findings' "action" fields.
- vendor_email: ONLY if the vendor must do something (correct/reissue an invoice, confirm a PO, explain an
  overbilling, acknowledge a duplicate). Otherwise null. Polite, specific, signed "Accounts Payable, Kestrel Foods".
  For bank-detail changes, do NOT email — verification must happen by phone on the registered number, so null.

Decision: {decision}
Invoice: {invoice}
Findings (worst first):
{findings}
"""


def _template(decision: str, findings: list[dict], invoice: dict, vendor: dict | None) -> dict:
    issues = [f for f in findings if f["severity"] in ("reject", "hold")]
    inv = invoice.get("invoice_number") or "the invoice"
    who = invoice.get("vendor_name") or "the vendor"
    if decision == "APPROVE":
        notes = [f["title"] for f in findings if f["severity"] in ("info", "warn")]
        return {"headline": f"Approve — {inv} from {who} matches its PO and passed every check.",
                "summary": "Vendor, duplicate, PO and amount checks all passed. "
                           + (("Notes: " + "; ".join(notes) + ".") if notes else ""),
                "next_steps": ["Include in the next payment run."], "vendor_email": None, "source": "template"}
    word = "Reject" if decision == "REJECT" else "Hold"
    steps = [f["action"] for f in issues if f.get("action")][:3]
    email = None
    if vendor and any(f["code"] in VENDOR_ACTION_CODES for f in issues):
        lines = "\n".join(f"- {f['title']}: {f['detail']}" for f in issues if f["code"] in VENDOR_ACTION_CODES)
        email = {"subject": f"Query on invoice {inv}",
                 "body": f"Dear {vendor['legal_name']} team,\n\nWe are unable to process invoice {inv} yet:\n{lines}\n\n"
                         "Could you please review and respond? \n\nRegards,\nAccounts Payable, Kestrel Foods"}
    return {"headline": f"{word} — {issues[0]['title']}." if issues else word,
            "summary": " ".join(f['detail'] for f in issues[:2] if f.get('detail'))
                       + (f" Also: {'; '.join(f['title'] for f in issues[2:])}." if len(issues) > 2 else ""),
            "next_steps": steps, "vendor_email": email, "source": "template"}


def explain(decision: str, findings: list[dict], invoice: dict, vendor: dict | None) -> dict:
    fallback = _template(decision, findings, invoice, vendor)
    ordered = sorted(findings, key=lambda f: ["reject", "hold", "warn", "info", "pass"].index(f["severity"]))
    prompt = PROMPT.format(decision=decision, invoice=json.dumps(invoice, default=str),
                           findings=json.dumps([{k: f[k] for k in ("severity", "title", "detail", "action")}
                                                for f in ordered if f["severity"] != "pass"], indent=1))
    shape = ('Return ONLY JSON: {"headline": str, "summary": str, "next_steps": [str], '
             '"vendor_email": {"subject": str, "body": str} or null}')
    for provider in llm.providers():
        try:
            if provider == "groq":
                out, meta = llm.groq_json(llm.GROQ_EXPLAIN_MODELS, [{"role": "user", "content": prompt + "\n" + shape}],
                                          Explanation, temperature=0.2, timeout_s=45)
            else:
                out, meta = llm.gemini_json([prompt], Explanation, temperature=0.2, timeout_s=45)
        except Exception:
            continue
        out = out.model_dump()
        # guardrail: the wording must agree with the rule decision, or we don't use it
        if not out["headline"].lower().startswith(decision.lower()[:4]):
            return fallback
        if any(f["code"] == "BANK_CHANGED" for f in findings):
            out["vendor_email"] = None  # verification must be by phone on the registered number
        out["source"] = f"{meta['provider']} · {meta['model']}"
        return out
    return fallback
