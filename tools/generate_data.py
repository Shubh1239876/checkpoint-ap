"""Generates Checkpoint's sample data: vendor master, purchase orders, paid-invoice
ledger, and invoice PDFs (digital and scanned) for the happy paths and edge cases.

Run:  python tools/generate_data.py
Writes to app/data/ (reference data) and samples/ (invoice PDFs) and
tests/fixtures/ (ground-truth extraction for offline tests).
"""
import io
import json
import random
from pathlib import Path

from PIL import Image, ImageFilter
import numpy as np
import pypdfium2 as pdfium
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "app" / "data"
SAMPLES = ROOT / "samples"
FIXTURES = ROOT / "tests" / "fixtures"
for d in (DATA, SAMPLES, FIXTURES):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- helpers
GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin(state: str, pan: str, entity: str = "1") -> str:
    body = f"{state}{pan}{entity}Z"
    total = 0
    for i, ch in enumerate(body):
        p = GST_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return body + GST_CHARS[(36 - total % 36) % 36]


def inr(x: float) -> str:
    """Indian digit grouping: 157500 -> 1,57,500.00"""
    neg = x < 0
    s = f"{abs(x):.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
        "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
        "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return (TENS[n // 10] + " " + ONES[n % 10]).strip()
    if n < 1000:
        return (ONES[n // 100] + " Hundred " + _words(n % 100)).strip()
    for div, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if n >= div:
            return (_words(n // div) + f" {name} " + _words(n % div)).strip()
    return ""


def words_inr(x: float) -> str:
    return f"Indian Rupees {_words(int(round(x)))} Only"


# ---------------------------------------------------------------- reference data
BUYER = {
    "name": "Kestrel Foods Pvt Ltd",
    "gstin": gstin("29", "AAKCK4521M"),
    "address": "No. 14, 2nd Main, Peenya Industrial Area, Bengaluru 560058, Karnataka",
    "state_code": "29",
}

V = {
    "V001": dict(name="Acme Packaging Pvt Ltd", state="29", pan="AAECA1234F",
                 city="Bengaluru, Karnataka", addr="Plot 22, KIADB Industrial Area, Bommasandra, Bengaluru 560099",
                 bank="HDFC Bank", acct="50100234567890", ifsc="HDFC0001234",
                 phone="+91 80 4123 5500", email="accounts@acmepack.in", status="Active"),
    "V002": dict(name="Sunrise Logistics LLP", state="33", pan="AAFFS5678K",
                 city="Chennai, Tamil Nadu", addr="41 GST Road, Guindy, Chennai 600032",
                 bank="ICICI Bank", acct="000405012345", ifsc="ICIC0000004",
                 phone="+91 44 2250 7788", email="billing@sunriselogistics.in", status="Active"),
    "V003": dict(name="BlueOak IT Services Pvt Ltd", state="27", pan="AABCB2468H",
                 city="Pune, Maharashtra", addr="5th Floor, Panchshil Tech Park, Yerwada, Pune 411006",
                 bank="Axis Bank", acct="917020045678123", ifsc="UTIB0000917",
                 phone="+91 20 6612 3400", email="ar@blueoakit.com", status="Active"),
    "V004": dict(name="Greenfield Agro Supplies", state="29", pan="AAJFG1357D",
                 city="Mysuru, Karnataka", addr="88 Hunsur Road, Hinkal, Mysuru 570017",
                 bank="State Bank of India", acct="38912345670", ifsc="SBIN0040123",
                 phone="+91 821 240 1188", email="greenfieldagro@gmail.com", status="Active"),
    "V005": dict(name="Precision Labels & Print Co", state="33", pan="AAKFP8642L",
                 city="Hosur, Tamil Nadu", addr="SIPCOT Phase II, Hosur 635109",
                 bank="Kotak Mahindra Bank", acct="4512789034", ifsc="KKBK0000451",
                 phone="+91 4344 276 900", email="accounts@precisionlabels.co.in", status="Active"),
    "V006": dict(name="Metro Office Essentials", state="29", pan="ABCPM9753Q",
                 city="Bengaluru, Karnataka", addr="12 Avenue Road, Chickpet, Bengaluru 560002",
                 bank="Canara Bank", acct="2716101004567", ifsc="CNRB0002716",
                 phone="+91 80 2287 3311", email="metro.office@outlook.com", status="Active"),
    "V007": dict(name="Coastal Cold Chain Pvt Ltd", state="29", pan="AAHCC3141R",
                 city="Mangaluru, Karnataka", addr="Baikampady Industrial Estate, Mangaluru 575011",
                 bank="Karnataka Bank", acct="2492000100078901", ifsc="KARB0000249",
                 phone="+91 824 240 7766", email="finance@coastalcold.in", status="Blocked",
                 blocked_reason="Failed compliance re-verification (Aug 2026)"),
    "V008": dict(name="Nimbus Cloud Solutions Pvt Ltd", state="36", pan="AAGCN2718S",
                 city="Hyderabad, Telangana", addr="Level 9, Skyview Tower, HITEC City, Hyderabad 500081",
                 bank="HDFC Bank", acct="50200098765432", ifsc="HDFC0000789",
                 phone="+91 40 4455 9000", email="billing@nimbuscloud.io", status="Active"),
}
for vid, v in V.items():
    v["vendor_id"] = vid
    v["gstin"] = gstin(v["state"], v["pan"])

vendors_out = [
    {
        "vendor_id": vid, "legal_name": v["name"], "gstin": v["gstin"], "pan": v["pan"],
        "state_code": v["state"], "address": f'{v["addr"]}', "bank_name": v["bank"],
        "bank_account": v["acct"], "ifsc": v["ifsc"], "registered_phone": v["phone"],
        "registered_email": v["email"], "payment_terms_days": 30, "status": v["status"],
        "blocked_reason": v.get("blocked_reason"),
    }
    for vid, v in V.items()
]


def po(po_number, vendor_id, date, lines, tax_rate, status="Open", note=""):
    subtotal = round(sum(l["qty"] * l["unit_price"] for l in lines), 2)
    return {
        "po_number": po_number, "vendor_id": vendor_id, "po_date": date,
        "lines": [dict(l, amount=round(l["qty"] * l["unit_price"], 2)) for l in lines],
        "subtotal": subtotal, "tax_rate": tax_rate,
        "total": round(subtotal * (1 + tax_rate / 100), 2), "status": status, "note": note,
    }


L = lambda d, q, p, u="": {"description": d, "qty": q, "unit_price": p, "uom": u}

pos = [
    po("PO-2026-0129", "V003", "2026-09-08",
       [L("Laptop repair & annual maintenance contract (40 units)", 40, 2400, "unit")], 18, "Closed"),
    po("PO-2026-0133", "V008", "2026-01-05",
       [L("Managed cloud hosting - monthly retainer (Jan-Dec 2026)", 12, 50000, "month")], 18),
    po("PO-2026-0139", "V004", "2026-09-15",
       [L("Organic compost fertiliser, 50 kg bags", 1000, 150, "bag")], 5),
    po("PO-2026-0144", "V003", "2026-09-22",
       [L("Microsoft 365 Business Standard licence - annual", 120, 1500, "licence"),
        L("IT helpdesk support - Q4 2026", 1, 60000, "quarter")], 18),
    po("PO-2026-0150", "V005", "2026-08-05",
       [L("Printed product labels 50 x 80 mm, 4-colour", 400000, 1.00, "label")], 18,
       note="Billed in instalments as labels are delivered"),
    po("PO-2026-0155", "V007", "2026-09-01",
       [L("Reefer cold storage - September 2026 (pallet-days)", 900, 95, "pallet-day")], 18),
    po("PO-2026-0158", "V001", "2026-10-01",
       [L("Corrugated shipping cartons, 5-ply, 18 x 12 x 12 in", 8000, 42.50, "carton"),
        L("BOPP packing tape 48 mm x 65 m", 1200, 55, "roll")], 18),
    po("PO-2026-0160", "V006", "2026-10-06",
       [L("A4 copier paper 75 gsm (ream of 500)", 400, 260, "ream"),
        L("Whiteboard markers, box of 10", 50, 350, "box")], 18),
    po("PO-2026-0162", "V002", "2026-10-01",
       [L("Road freight - Bengaluru to Chennai DC, October 2026", 12, 7000, "trip")], 18),
    po("PO-2026-0171", "V002", "2026-10-01",
       [L("Road freight - Bengaluru to Hyderabad DC, October 2026", 12, 7000, "trip")], 18),
]


def led(inv, vid, po_no, date, subtotal, rate, paid):
    return {"invoice_number": inv, "vendor_id": vid, "po_number": po_no, "invoice_date": date,
            "subtotal": subtotal, "total": round(subtotal * (1 + rate / 100), 2),
            "status": "Paid", "paid_on": paid, "source": "seed"}


ledger = [
    led("INV-1042", "V003", "PO-2026-0129", "2026-10-14", 96000, 18, "2026-10-20"),
    led("PL/26-27/0311", "V005", "PO-2026-0150", "2026-09-02", 150000, 18, "2026-09-30"),
    led("PL/26-27/0398", "V005", "PO-2026-0150", "2026-10-03", 150000, 18, "2026-10-31"),
]
for m in range(1, 11):
    ledger.append(led(f"NC-2026-{m:03d}", "V008", "PO-2026-0133", f"2026-{m:02d}-01",
                      50000, 18, f"2026-{m:02d}-28"))

(DATA / "buyer.json").write_text(json.dumps(BUYER, indent=2))
(DATA / "vendors.json").write_text(json.dumps(vendors_out, indent=2))
(DATA / "purchase_orders.json").write_text(json.dumps(pos, indent=2))
(DATA / "ledger_seed.json").write_text(json.dumps(ledger, indent=2))

# ---------------------------------------------------------------- invoice specs
# Each spec is both the thing we render and the ground truth an ideal extractor returns.


def inv_spec(file, template, vendor_id, number, date, due, po_ref, lines, tax_kind, rate,
             treatment="separate", bank=None, note="", scanned=False, stated_subtotal=None,
             show_number=True, scenario="", expected=""):
    v = V[vendor_id]
    lines = [dict(l, amount=round(l["qty"] * l["unit_price"], 2)) for l in lines]
    line_sum = round(sum(l["amount"] for l in lines), 2)
    if treatment == "inclusive":
        total = line_sum
        subtotal = round(total / (1 + rate / 100), 2)
        tax = round(total - subtotal, 2)
    else:
        subtotal = stated_subtotal if stated_subtotal is not None else line_sum
        tax = round(subtotal * rate / 100, 2)
        total = round(subtotal + tax, 2)
    return dict(file=file, template=template, vendor=v, vendor_id=vendor_id,
                number=number if show_number else None, date=date, due=due, po_ref=po_ref,
                lines=lines, tax_kind=tax_kind, rate=rate, treatment=treatment,
                subtotal=subtotal, tax=tax, total=total,
                bank=bank or {"name": v["bank"], "acct": v["acct"], "ifsc": v["ifsc"]},
                note=note, scanned=scanned, scenario=scenario, expected=expected)


specs = [
    inv_spec("01_happy_path_blueoak.pdf", "A", "V003", "INV-1057", "2026-11-03", "2026-12-03",
             "PO-2026-0144",
             [L("Microsoft 365 Business Standard licence - annual", 120, 1500, "licence"),
              L("IT helpdesk support - Q4 2026", 1, 60000, "quarter")], "igst", 18,
             scenario="Clean digital invoice, explicit PO, exact match", expected="APPROVE"),
    inv_spec("02_happy_tax_inclusive_scanned_greenfield.pdf", "C", "V004", "GAS/1123",
             "2026-11-02", "2026-12-02", "P.O. No. 2026/0139",
             [L("Organic compost fertiliser 50kg bag", 1000, 157.50, "bag")], "cgst_sgst", 5,
             treatment="inclusive", scanned=True,
             note="All prices are inclusive of GST @ 5% (CGST 2.5% + SGST 2.5%).",
             scenario="Scanned invoice, tax embedded in prices, PO ref written differently",
             expected="APPROVE"),
    inv_spec("03_edge_split_po_overrun_precision.pdf", "B", "V005", "PL/26-27/0457",
             "2026-11-04", "2026-12-04", "PO-2026-0150",
             [L("Printed product labels 50 x 80 mm, 4-colour", 150000, 1.00, "label")], "igst", 18,
             scenario="3rd instalment on a PO; fine alone, overruns PO cumulatively",
             expected="HOLD"),
    inv_spec("04_edge_disguised_duplicate_scanned_blueoak.pdf", "A", "V003", "INV1042-A",
             "2026-10-14", "2026-11-13", "PO-2026-0129",
             [L("Laptop repair & annual maintenance contract (40 units)", 40, 2400, "unit")],
             "igst", 18, scanned=True,
             scenario="Scanned re-send of paid INV-1042 with a suffixed number", expected="HOLD"),
    inv_spec("05_edge_no_po_two_candidates_sunrise.pdf", "C", "V002", "SLL/2026/1189",
             "2026-11-05", "2026-12-05", None,
             [L("Road freight charges - October 2026", 12, 7000, "trip")], "igst", 18,
             scenario="No PO reference; vendor has two equally plausible open POs",
             expected="HOLD"),
    inv_spec("06_edge_bank_details_changed_acme.pdf", "B", "V001", "ACME/2026/0871",
             "2026-11-06", "2026-12-06", "PO-2026-0158",
             [L("Corrugated shipping cartons, 5-ply, 18 x 12 x 12 in", 8000, 42.50, "carton"),
              L("BOPP packing tape 48 mm x 65 m", 1200, 55, "roll")], "cgst_sgst", 18,
             bank={"name": "HDFC Bank", "acct": "50100998877665", "ifsc": "HDFC0009911"},
             note="Kindly note our UPDATED bank details below for this and all future payments.",
             scenario="Everything matches except payee bank account", expected="HOLD"),
    inv_spec("07_extra_blocked_vendor_coastal.pdf", "A", "V007", "CCC-24518", "2026-10-30",
             "2026-11-29", "PO-2026-0155",
             [L("Reefer cold storage - September 2026 (pallet-days)", 900, 95, "pallet-day")],
             "cgst_sgst", 18, scenario="Vendor is blocked in the vendor master", expected="REJECT"),
    inv_spec("08_extra_recurring_retainer_nimbus.pdf", "A", "V008", "NC-2026-011",
             "2026-11-01", "2026-12-01", "PO-2026-0133",
             [L("Managed cloud hosting - monthly retainer, November 2026", 1, 50000, "month")],
             "igst", 18,
             scenario="Same amount as last month's paid invoice - must NOT be flagged duplicate",
             expected="APPROVE"),
    inv_spec("09_extra_math_error_no_number_metro.pdf", "C", "V006", None, "2026-11-07",
             "2026-12-07", "PO-2026-0160",
             [L("A4 copier paper 75 gsm (ream of 500)", 400, 260, "ream"),
              L("Whiteboard markers, box of 10", 50, 350, "box")], "cgst_sgst", 18,
             stated_subtotal=125100, show_number=False,
             scenario="Lines don't add up to stated subtotal; no invoice number",
             expected="HOLD"),
]

# ---------------------------------------------------------------- rendering
W, H = A4
INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#616e7c")


def tax_rows(s):
    if s["treatment"] == "inclusive":
        return []
    if s["tax_kind"] == "igst":
        return [(f"IGST @ {s['rate']:g}%", s["tax"])]
    half = round(s["tax"] / 2, 2)
    return [(f"CGST @ {s['rate']/2:g}%", half), (f"SGST @ {s['rate']/2:g}%", round(s["tax"] - half, 2))]


def fmt_date(d):
    y, m, dd = d.split("-")
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{int(dd):02d}-{months[int(m)-1]}-{y}"


def render_A(c, s):
    """Modern SaaS-style invoice."""
    v = s["vendor"]
    c.setFillColor(colors.HexColor("#0b3d91"))
    c.rect(0, H - 38 * mm, W, 38 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(18 * mm, H - 18 * mm, v["name"])
    c.setFont("Helvetica", 8.5)
    c.drawString(18 * mm, H - 24 * mm, v["addr"])
    c.drawString(18 * mm, H - 29 * mm, f"GSTIN: {v['gstin']}   |   {v['email']}   |   {v['phone']}")
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - 18 * mm, H - 20 * mm, "INVOICE")
    y = H - 52 * mm
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, y, "BILL TO")
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, y - 5 * mm, BUYER["name"])
    c.drawString(18 * mm, y - 10 * mm, BUYER["address"][:60])
    c.drawString(18 * mm, y - 15 * mm, BUYER["address"][60:])
    c.drawString(18 * mm, y - 20 * mm, f"GSTIN: {BUYER['gstin']}")
    meta = [("Invoice #", s["number"] or ""), ("Invoice date", fmt_date(s["date"])),
            ("Due date", fmt_date(s["due"])), ("PO number", s["po_ref"] or "-")]
    for i, (k, val) in enumerate(meta):
        c.setFont("Helvetica", 9)
        c.setFillColor(MUTED)
        c.drawString(125 * mm, y - i * 5.5 * mm, k)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(W - 18 * mm, y - i * 5.5 * mm, val)
    y -= 34 * mm
    c.setFillColor(colors.HexColor("#eef2f7"))
    c.rect(18 * mm, y - 2 * mm, W - 36 * mm, 8 * mm, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(20 * mm, y + 1 * mm, "Description")
    c.drawRightString(125 * mm, y + 1 * mm, "Qty")
    c.drawRightString(155 * mm, y + 1 * mm, "Rate (INR)")
    c.drawRightString(W - 20 * mm, y + 1 * mm, "Amount (INR)")
    c.setFont("Helvetica", 9)
    for l in s["lines"]:
        y -= 9 * mm
        c.drawString(20 * mm, y, l["description"])
        c.drawRightString(125 * mm, y, f"{l['qty']:,g}")
        c.drawRightString(155 * mm, y, inr(l["unit_price"]))
        c.drawRightString(W - 20 * mm, y, inr(l["amount"]))
    y -= 6 * mm
    c.setStrokeColor(colors.HexColor("#cbd2d9"))
    c.line(18 * mm, y, W - 18 * mm, y)
    rows = [("Subtotal", s["subtotal"])] + tax_rows(s)
    for k, val in rows:
        y -= 7 * mm
        c.drawString(125 * mm, y, k)
        c.drawRightString(W - 20 * mm, y, inr(val))
    y -= 10 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawString(125 * mm, y, "Total due")
    c.drawRightString(W - 20 * mm, y, "INR " + inr(s["total"]))
    y -= 22 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(18 * mm, y, "Payment details")
    c.setFont("Helvetica", 9)
    b = s["bank"]
    c.drawString(18 * mm, y - 5 * mm, f"Bank: {b['name']}   A/c No: {b['acct']}   IFSC: {b['ifsc']}")
    c.drawString(18 * mm, y - 10 * mm, f"Beneficiary: {v['name']}")
    if s["note"]:
        c.drawString(18 * mm, y - 17 * mm, s["note"])
    c.setFillColor(MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(18 * mm, 15 * mm, "Payment terms: Net 30. This is a computer generated invoice.")


def render_B(c, s):
    """Classic boxed Indian GST tax invoice."""
    v = s["vendor"]
    c.setStrokeColor(colors.black)
    c.setFillColor(colors.black)
    x0, x1 = 12 * mm, W - 12 * mm
    top = H - 12 * mm
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(W / 2, top - 6 * mm, "TAX INVOICE")
    c.rect(x0, 40 * mm, x1 - x0, top - 10 * mm - 40 * mm)
    y = top - 12 * mm
    mid = W / 2 + 10 * mm
    c.line(mid, y + 2 * mm, mid, y - 44 * mm)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(x0 + 3 * mm, y - 4 * mm, v["name"].upper())
    c.setFont("Helvetica", 8)
    c.drawString(x0 + 3 * mm, y - 9 * mm, v["addr"])
    c.drawString(x0 + 3 * mm, y - 13 * mm, f"GSTIN/UIN: {v['gstin']}    PAN: {v['pan']}")
    c.drawString(x0 + 3 * mm, y - 17 * mm, f"State Code: {v['state']}   Ph: {v['phone']}")
    c.line(x0, y - 21 * mm, mid, y - 21 * mm)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x0 + 3 * mm, y - 25 * mm, "Buyer (Bill to)")
    c.setFont("Helvetica", 8)
    c.drawString(x0 + 3 * mm, y - 29 * mm, BUYER["name"])
    c.drawString(x0 + 3 * mm, y - 33 * mm, BUYER["address"][:70])
    c.drawString(x0 + 3 * mm, y - 37 * mm, f"GSTIN/UIN: {BUYER['gstin']}   State Code: 29")
    cells = [("Invoice No.", s["number"] or ""), ("Dated", fmt_date(s["date"])),
             ("Buyer's Order No.", s["po_ref"] or ""), ("Mode/Terms of Payment", "30 days"),
             ("Due Date", fmt_date(s["due"]))]
    for i, (k, val) in enumerate(cells):
        yy = y - i * 8.5 * mm
        c.setFont("Helvetica", 7)
        c.drawString(mid + 3 * mm, yy - 2 * mm, k)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(mid + 3 * mm, yy - 6 * mm, val)
        c.line(mid, yy - 7.5 * mm, x1, yy - 7.5 * mm)
    y -= 44 * mm
    c.line(x0, y, x1, y)
    cols = [x0 + 3 * mm, x0 + 12 * mm, 110 * mm, 128 * mm, 150 * mm, x1 - 3 * mm]
    c.setFont("Helvetica-Bold", 8)
    for txt, xx, right in (("Sl", cols[0], False), ("Description of Goods/Services", cols[1], False),
                           ("HSN/SAC", cols[2], False), ("Quantity", cols[3] + 18 * mm, True),
                           ("Rate", cols[4] + 18 * mm, True), ("Amount", cols[5], True)):
        (c.drawRightString if right else c.drawString)(xx, y - 5 * mm, txt)
    c.line(x0, y - 7 * mm, x1, y - 7 * mm)
    c.setFont("Helvetica", 8.5)
    yy = y - 13 * mm
    hsn = {"V001": "4819", "V005": "4821"}.get(s["vendor_id"], "9983")
    for i, l in enumerate(s["lines"], 1):
        c.drawString(cols[0], yy, str(i))
        c.drawString(cols[1], yy, l["description"][:52])
        c.drawString(cols[2], yy, hsn)
        c.drawRightString(cols[3] + 18 * mm, yy, f"{l['qty']:,g} {l['uom']}")
        c.drawRightString(cols[4] + 18 * mm, yy, inr(l["unit_price"]))
        c.drawRightString(cols[5], yy, inr(l["amount"]))
        yy -= 7 * mm
    yy -= 4 * mm
    c.line(110 * mm, yy + 3 * mm, x1, yy + 3 * mm)
    for k, val in [("Taxable Value", s["subtotal"])] + tax_rows(s):
        c.drawString(128 * mm, yy - 2 * mm, k)
        c.drawRightString(cols[5], yy - 2 * mm, inr(val))
        yy -= 6 * mm
    c.setFont("Helvetica-Bold", 10)
    c.line(x0, yy, x1, yy)
    c.drawString(128 * mm, yy - 6 * mm, "Total")
    c.drawRightString(cols[5], yy - 6 * mm, "Rs. " + inr(s["total"]))
    c.line(x0, yy - 9 * mm, x1, yy - 9 * mm)
    c.setFont("Helvetica", 8)
    c.drawString(x0 + 3 * mm, yy - 14 * mm, "Amount Chargeable (in words):")
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x0 + 3 * mm, yy - 19 * mm, words_inr(s["total"]))
    yb = 70 * mm
    if s["note"]:
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(x0 + 3 * mm, yb + 8 * mm, s["note"])
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x0 + 3 * mm, yb, "Company's Bank Details")
    c.setFont("Helvetica", 8)
    b = s["bank"]
    c.drawString(x0 + 3 * mm, yb - 5 * mm, f"Bank Name : {b['name']}")
    c.drawString(x0 + 3 * mm, yb - 9 * mm, f"A/c No.   : {b['acct']}")
    c.drawString(x0 + 3 * mm, yb - 13 * mm, f"IFSC Code : {b['ifsc']}")
    c.drawRightString(x1 - 3 * mm, 48 * mm, f"for {v['name']}")
    c.drawRightString(x1 - 3 * mm, 43 * mm, "Authorised Signatory")
    c.setFont("Helvetica", 7)
    c.drawCentredString(W / 2, 35 * mm, "SUBJECT TO LOCAL JURISDICTION. This is a Computer Generated Invoice.")


def render_C(c, s):
    """Plain small-business invoice, letter style."""
    v = s["vendor"]
    c.setFillColor(colors.black)
    c.setFont("Times-Bold", 16)
    c.drawCentredString(W / 2, H - 22 * mm, v["name"])
    c.setFont("Times-Roman", 10)
    c.drawCentredString(W / 2, H - 28 * mm, v["addr"])
    c.drawCentredString(W / 2, H - 33 * mm, f"Ph: {v['phone']}  Email: {v['email']}")
    c.drawCentredString(W / 2, H - 38 * mm, f"GST No: {v['gstin']}")
    c.line(20 * mm, H - 42 * mm, W - 20 * mm, H - 42 * mm)
    c.setFont("Times-Bold", 12)
    c.drawCentredString(W / 2, H - 50 * mm, "BILL")
    c.setFont("Times-Roman", 10.5)
    y = H - 60 * mm
    if s["number"]:
        c.drawString(20 * mm, y, f"Bill No: {s['number']}")
    c.drawRightString(W - 20 * mm, y, f"Date: {fmt_date(s['date'])}")
    y -= 7 * mm
    c.drawString(20 * mm, y, f"To: M/s {BUYER['name']}, Bengaluru")
    y -= 5 * mm
    c.drawString(20 * mm, y, f"     GSTIN {BUYER['gstin']}")
    if s["po_ref"]:
        y -= 7 * mm
        c.drawString(20 * mm, y, f"Ref: {s['po_ref']}")
    y -= 12 * mm
    c.setFont("Times-Bold", 10.5)
    c.drawString(20 * mm, y, "Particulars")
    c.drawRightString(130 * mm, y, "Qty")
    c.drawRightString(158 * mm, y, "Rate")
    c.drawRightString(W - 20 * mm, y, "Amount")
    c.line(20 * mm, y - 2 * mm, W - 20 * mm, y - 2 * mm)
    c.setFont("Times-Roman", 10.5)
    for l in s["lines"]:
        y -= 8 * mm
        c.drawString(20 * mm, y, l["description"])
        c.drawRightString(130 * mm, y, f"{l['qty']:,g}")
        c.drawRightString(158 * mm, y, inr(l["unit_price"]))
        c.drawRightString(W - 20 * mm, y, inr(l["amount"]))
    y -= 5 * mm
    c.line(20 * mm, y, W - 20 * mm, y)
    if s["treatment"] == "inclusive":
        y -= 8 * mm
        c.setFont("Times-Bold", 11)
        c.drawString(110 * mm, y, "Grand Total")
        c.drawRightString(W - 20 * mm, y, "Rs. " + inr(s["total"]))
    else:
        for k, val in [("Sub Total", s["subtotal"])] + tax_rows(s):
            y -= 7 * mm
            c.drawString(110 * mm, y, k)
            c.drawRightString(W - 20 * mm, y, inr(val))
        y -= 8 * mm
        c.setFont("Times-Bold", 11)
        c.drawString(110 * mm, y, "Grand Total")
        c.drawRightString(W - 20 * mm, y, "Rs. " + inr(s["total"]))
    c.setFont("Times-Italic", 10)
    y -= 10 * mm
    c.drawString(20 * mm, y, words_inr(s["total"]))
    if s["note"]:
        y -= 7 * mm
        c.drawString(20 * mm, y, s["note"])
    y -= 14 * mm
    c.setFont("Times-Roman", 10)
    b = s["bank"]
    c.drawString(20 * mm, y, f"Bank: {b['name']}, A/c {b['acct']}, IFSC {b['ifsc']}")
    c.drawRightString(W - 20 * mm, 40 * mm, f"For {v['name']}")
    c.drawRightString(W - 20 * mm, 30 * mm, "Proprietor / Partner")


RENDER = {"A": render_A, "B": render_B, "C": render_C}


def to_scanned(pdf_bytes: bytes, seed: int) -> bytes:
    """Rasterise, then degrade like a phone/office scan. Output has no text layer."""
    rnd = random.Random(seed)
    page = pdfium.PdfDocument(pdf_bytes)[0]
    # ~200 dpi greyscale, slight skew, sensor noise, mild softening: a typical office scanner
    img = page.render(scale=200 / 72).to_pil().convert("L")
    img = img.rotate(rnd.uniform(-0.9, 0.9), resample=Image.BICUBIC, expand=False, fillcolor=245)
    arr = np.asarray(img).astype(np.float32)
    arr = arr * 0.92 + 14  # lift blacks, dull whites
    arr += np.random.default_rng(seed).normal(0, 7, arr.shape)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.4))
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=200, quality=75)
    return out.getvalue()


def ground_truth(s):
    v, b = s["vendor"], s["bank"]
    return {
        "vendor_name": v["name"], "vendor_gstin": v["gstin"], "buyer_name": BUYER["name"],
        "buyer_gstin": BUYER["gstin"], "invoice_number": s["number"], "invoice_date": s["date"],
        "due_date": s["due"], "po_reference": s["po_ref"], "currency": "INR",
        "line_items": [{"description": l["description"], "quantity": l["qty"],
                        "unit_price": l["unit_price"], "amount": l["amount"]} for l in s["lines"]],
        "subtotal": None if s["treatment"] == "inclusive" else s["subtotal"],
        "tax_treatment": s["treatment"],
        "tax_lines": [{"label": k, "amount": a} for k, a in tax_rows(s)],
        "tax_rate_percent": s["rate"], "total": s["total"],
        "bank_account": b["acct"], "ifsc": b["ifsc"], "bank_name": b["name"],
        "notes": s["note"] or None,
    }


manifest = []
for i, s in enumerate(specs):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {s['number'] or ''}")
    RENDER[s["template"]](c, s)
    c.showPage()
    c.save()
    data = buf.getvalue()
    if s["scanned"]:
        data = to_scanned(data, seed=100 + i)
    (SAMPLES / s["file"]).write_bytes(data)
    (FIXTURES / (Path(s["file"]).stem + ".json")).write_text(json.dumps(ground_truth(s), indent=2))
    manifest.append({"file": s["file"], "scenario": s["scenario"], "expected": s["expected"],
                     "scanned": s["scanned"]})

(SAMPLES / "manifest.json").write_text(json.dumps(manifest, indent=2))
print(f"buyer GSTIN {BUYER['gstin']}")
print(f"{len(vendors_out)} vendors, {len(pos)} POs, {len(ledger)} ledger rows, {len(specs)} invoices")
