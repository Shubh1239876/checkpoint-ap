"""TEST-ONLY stand-in for the Gemini call: returns the ground-truth reading of each
sample PDF (written by tools/generate_data.py), in the same shape Gemini returns.
Used to test the rules + UI offline. The app itself never imports this."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_BY_HASH = {}
for pdf in (ROOT / "samples").glob("*.pdf"):
    _BY_HASH[hashlib.sha256(pdf.read_bytes()).hexdigest()] = ROOT / "tests" / "fixtures" / (pdf.stem + ".json")

SCALAR = ["vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin", "invoice_number", "invoice_date",
          "due_date", "po_reference", "currency", "tax_treatment", "bank_name", "bank_account", "ifsc"]
NUM = ["subtotal", "tax_rate_percent", "total"]


def fixture_extractor(pdf: bytes, text: str = ""):
    path = _BY_HASH.get(hashlib.sha256(pdf).hexdigest())
    if not path:
        from app.extract import ExtractionError
        raise ExtractionError("No fixture for this file (offline test mode)")
    gt = json.loads(path.read_text())
    c = 0.97 if text.strip() else 0.9
    out = {k: {"value": gt.get(k), "confidence": c if gt.get(k) is not None else 0.0,
               "source_text": None} for k in SCALAR + NUM}
    out["line_items"] = gt["line_items"]
    out["tax_lines"] = [{"label": t["label"], "rate_percent": None, "amount": t["amount"]} for t in gt["tax_lines"]]
    out["notes"] = gt.get("notes")
    return out, {"model": "fixture (offline test)", "attempts": 1}
