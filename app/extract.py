"""Stage 2 - reading the invoice. The only place (with explain.py) that uses an LLM.

The model returns every field as {value, confidence, source_text}, so later stages can
refuse to act on a field the model was unsure about, and the UI can show where each
value came from.

Routing:
  digital PDF (has a text layer) -> text model reads the extracted text
  scanned PDF (image only)       -> vision model reads a rendered image of the page
Providers are tried in order (Groq, then Gemini); see llm.py.
"""
import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from . import llm


class Str(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)  # e.g. invoice number 1057 -> "1057"
    value: Optional[str] = Field(None, description="Value exactly as needed; null if not present on the document")
    confidence: float = Field(..., description="0-1. How sure you are this value is correct and legible")
    source_text: Optional[str] = Field(None, description="The exact text on the document this came from")


class Num(BaseModel):
    value: Optional[float] = Field(None, description="Plain number, no currency symbols or commas; null if absent")
    confidence: float
    source_text: Optional[str] = None


class LineItem(BaseModel):
    description: str = ""
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class TaxLine(BaseModel):
    label: str = Field(..., description="As printed, e.g. 'IGST @ 18%', 'CGST @ 9%'")
    rate_percent: Optional[float] = None
    amount: Optional[float] = None


class InvoiceExtraction(BaseModel):
    vendor_name: Str
    vendor_gstin: Str
    buyer_name: Str
    buyer_gstin: Str
    invoice_number: Str
    invoice_date: Str = Field(..., description="ISO format YYYY-MM-DD")
    due_date: Str = Field(..., description="ISO format YYYY-MM-DD")
    po_reference: Str = Field(..., description="Purchase order / buyer's order number exactly as printed")
    currency: Str
    line_items: list[LineItem]
    subtotal: Num = Field(..., description="Pre-tax subtotal / taxable value as PRINTED on the invoice. Null if not printed")
    tax_treatment: Str = Field(..., description="'separate' if tax is shown as its own line(s), 'inclusive' if prices include tax, 'none' if no tax")
    tax_rate_percent: Num = Field(..., description="Total GST rate, e.g. 18 for CGST 9% + SGST 9%")
    tax_lines: list[TaxLine]
    total: Num = Field(..., description="Final amount payable as printed")
    bank_name: Str
    bank_account: Str = Field(..., description="Digits only")
    ifsc: Str
    notes: Optional[str] = Field(None, description="Any remark relevant to payment, e.g. a notice that bank details changed")


PROMPT = """You are reading a vendor invoice received by an accounts-payable team in India.
Extract the fields in the schema. Rules:
- Report what is PRINTED. Never compute or infer a value that is not on the page
  (e.g. if no subtotal is printed, subtotal is null; if no invoice number, it is null).
- Dates as YYYY-MM-DD. Amounts as plain numbers (1,57,500.00 -> 157500.00).
- confidence: 0.95+ only when the text is clearly legible and unambiguous; lower it for
  blurry, partially visible or ambiguous values.
- source_text: copy the exact characters from the document for each value.
- The buyer is the party being billed ("Bill to", "Buyer", "To"). The vendor is the issuer.
"""


# A compact template for providers without native schema support (Groq JSON mode).
_F = '{"value": ..., "confidence": 0.0-1.0, "source_text": "..."}'
TEMPLATE = "{\n" + ",\n".join(f'  "{k}": {_F}' for k in [
    "vendor_name", "vendor_gstin", "buyer_name", "buyer_gstin", "invoice_number", "invoice_date", "due_date",
    "po_reference", "currency", "subtotal", "tax_treatment", "tax_rate_percent", "total",
    "bank_name", "bank_account", "ifsc"]) + """,
  "line_items": [{"description": "...", "quantity": number|null, "unit_price": number|null, "amount": number|null}],
  "tax_lines": [{"label": "e.g. IGST @ 18%", "rate_percent": number|null, "amount": number|null}],
  "notes": "string or null"
}"""
FIELD_NOTES = """Field notes: value is null when not printed. Numbers are plain numbers (subtotal, total,
tax_rate_percent). invoice_date/due_date as YYYY-MM-DD. bank_account digits only.
subtotal = pre-tax subtotal/taxable value AS PRINTED (null if not printed).
tax_treatment = "separate" | "inclusive" | "none". tax_rate_percent = total GST rate (CGST 9% + SGST 9% -> 18)."""


class ExtractionError(Exception):
    pass


def _groq_messages(text_layer: str, images: list[str]) -> list[dict]:
    instr = PROMPT + "\n" + FIELD_NOTES + "\nReturn ONLY a JSON object of exactly this shape:\n" + TEMPLATE
    if images:
        content = [{"type": "text", "text": instr + "\n\nThe invoice image follows."}]
        content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images]
        return [{"role": "user", "content": content}]
    return [{"role": "system", "content": instr},
            {"role": "user", "content": "Invoice text (extracted from the PDF, layout approximate):\n\n" + text_layer[:15000]}]


def extract_invoice(pdf_bytes: bytes, text_layer: str = "") -> tuple[dict, dict]:
    """Returns (extraction dict, meta). Tries each configured provider in turn."""
    digital = len(text_layer.strip()) > 40
    order = llm.providers(scanned=not digital)
    if not order:
        raise ExtractionError("No AI key set on the server (GROQ_API_KEY or GEMINI_API_KEY)")
    failures = []
    for provider in order:
        try:
            if provider == "groq":
                if digital:
                    out, meta = llm.groq_json(llm.GROQ_TEXT_MODELS, _groq_messages(text_layer, []), InvoiceExtraction)
                    meta["mode"] = "text layer"
                else:
                    imgs = llm.render_pages(pdf_bytes)
                    out, meta = llm.groq_json(llm.GROQ_VISION_MODELS, _groq_messages("", imgs), InvoiceExtraction)
                    meta["mode"] = "page image"
            else:
                from google.genai import types
                parts = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), PROMPT]
                if digital:
                    parts.append("Embedded text layer of the PDF (may help with exact characters):\n" + text_layer[:12000])
                # tighter time limit on the first-choice reader so a busy Gemini hands over to Groq quickly
                out, meta = llm.gemini_json(parts, InvoiceExtraction, timeout_s=30)
                meta["mode"] = "PDF"
            meta["fallbacks"] = failures
            return out.model_dump(), meta
        except llm.AIError as e:
            failures.append(str(e)[:600])
        except Exception as e:  # never let one provider's surprise stop the others
            failures.append(f"{provider}: {type(e).__name__}: {str(e)[:300]}")
    raise ExtractionError("Could not read the invoice. " + " || ".join(failures))


# kept for backwards compatibility with older imports
extract_with_gemini = extract_invoice
