"""Stages 3-8: deterministic checks. No LLM in this file.

Every check produces a Finding. The decision is simply the worst severity found:
  reject > hold > warn > info > pass   ->   REJECT / HOLD / APPROVE
"""
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

DATA = Path(__file__).parent / "data"
BUYER = json.loads((DATA / "buyer.json").read_text())
VENDORS = json.loads((DATA / "vendors.json").read_text())
POS = json.loads((DATA / "purchase_orders.json").read_text())
VENDOR_BY_ID = {v["vendor_id"]: v for v in VENDORS}
PO_BY_NUMBER = {p["po_number"]: p for p in POS}

SEVERITY_RANK = {"pass": 0, "info": 1, "warn": 2, "hold": 3, "reject": 4}
GST_SLABS = {0, 0.25, 3, 5, 18, 40}   # post Sept-2025 rationalisation
CRITICAL_FIELDS = ["invoice_number", "invoice_date", "total", "vendor_gstin", "po_reference", "bank_account"]


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    detail: str = ""
    action: str = ""
    data: dict = field(default_factory=dict)


def inr(x) -> str:
    if x is None:
        return "-"
    neg = x < 0
    whole, frac = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return ("-₹" if neg else "₹") + whole + ("" if frac == "00" else "." + frac)


def val(ex: dict, key: str):
    f = ex.get(key)
    return f.get("value") if isinstance(f, dict) else f


def conf(ex: dict, key: str) -> float:
    f = ex.get(key)
    return f.get("confidence", 1.0) if isinstance(f, dict) else 1.0


def parse_date(s):
    try:
        return date.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def norm_invoice_no(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def digit_core(s: str | None) -> str:
    """The trailing numeric run of an invoice number: 'INV1042-A' -> '1042', 'PL/26-27/0457' -> '457'."""
    runs = re.findall(r"\d+", s or "")
    return runs[-1].lstrip("0") if runs else ""


def norm_po(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(20\d{2})\D{0,4}(\d{3,5})", s)
    return f"PO-{m.group(1)}-{int(m.group(2)):04d}" if m else s.strip().upper()


def gstin_valid(g: str | None) -> bool:
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    g = (g or "").strip().upper()
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        p = chars.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return chars[(36 - total % 36) % 36] == g[14]


def gstin_misread_of(read: str, name: str) -> dict | None:
    """A GSTIN that fails its checksum is not a real registration, so it can't identify another
    company. If it is within 2 characters of exactly one vendor's GSTIN and the vendor name also
    matches, it is a misreading of that vendor's GSTIN (typical on scans)."""
    read = (read or "").upper().replace(" ", "")
    if not read or gstin_valid(read):
        return None
    close = [v for v in VENDORS if Levenshtein.distance(read, v["gstin"]) <= 2]
    if len(close) == 1 and fuzz.token_set_ratio((name or "").lower(), close[0]["legal_name"].lower()) >= 85:
        return close[0]
    return None


def digits(s) -> str:
    return re.sub(r"\D", "", s or "")


# ================================================================ stage 3: validate
def validate(ex: dict, settings: dict, today: date) -> tuple[list[Finding], dict]:
    F: list[Finding] = []
    ctx: dict = {}
    min_conf = settings["min_field_confidence"]

    # required fields
    if not val(ex, "invoice_number"):
        F.append(Finding("MISSING_INVOICE_NUMBER", "hold", "No invoice number on the document",
                         "Without a number the invoice can't be reliably checked for duplicates or referenced in payment advice.",
                         "Ask the vendor to reissue the invoice with an invoice number."))
    if val(ex, "total") is None:
        F.append(Finding("MISSING_TOTAL", "hold", "No total amount could be found",
                         action="Check the document manually; request a corrected invoice if the total is missing."))
    if not val(ex, "invoice_date"):
        F.append(Finding("MISSING_DATE", "hold", "No invoice date", action="Request a dated invoice from the vendor."))

    # confidence gate: never act on a value the reader wasn't sure about
    low = [(k, conf(ex, k)) for k in CRITICAL_FIELDS if val(ex, k) is not None and conf(ex, k) < min_conf]
    if low:
        F.append(Finding("LOW_CONFIDENCE", "hold", "Some key fields were hard to read",
                         ", ".join(f"{k.replace('_', ' ')} ({c:.0%})" for k, c in low),
                         "Compare these fields against the PDF before continuing.", {"fields": dict(low)}))

    # arithmetic
    lines = ex.get("line_items") or []
    for i, l in enumerate(lines, 1):
        q, p, a = l.get("quantity"), l.get("unit_price"), l.get("amount")
        if None not in (q, p, a) and abs(q * p - a) > 1:
            F.append(Finding("LINE_MATH", "hold", f"Line {i}: quantity × rate ≠ amount",
                             f"{q:g} × {inr(p)} = {inr(q * p)}, invoice shows {inr(a)}",
                             "Ask the vendor to correct the line.", {"line": i}))
    line_sum = round(sum(l.get("amount") or 0 for l in lines), 2) if lines else None
    treatment = (val(ex, "tax_treatment") or "separate").lower()
    total = val(ex, "total")
    rate = val(ex, "tax_rate_percent")
    subtotal = val(ex, "subtotal")
    tax_lines = ex.get("tax_lines") or []
    tax_sum = round(sum(t.get("amount") or 0 for t in tax_lines), 2)

    if treatment == "inclusive":
        if rate is None or total is None:
            F.append(Finding("INCLUSIVE_NO_RATE", "hold", "Prices include tax but the tax rate isn't stated",
                             action="Confirm the GST rate with the vendor so the pre-tax amount can be matched to the PO."))
        else:
            if line_sum is not None and abs(line_sum - total) > 1:
                F.append(Finding("SUM_MISMATCH", "hold", "Line items don't add up to the total",
                                 f"Lines sum to {inr(line_sum)}, total shows {inr(total)}",
                                 "Ask the vendor for a corrected invoice."))
            subtotal = round(total / (1 + rate / 100), 2)
            F.append(Finding("TAX_INCLUSIVE", "info", "Tax is included in the prices",
                             f"Derived pre-tax value {inr(subtotal)} and GST {inr(total - subtotal)} at {rate:g}% — "
                             "pre-tax value is what gets compared to the PO.", data={"derived_subtotal": subtotal}))
    else:
        if subtotal is None:
            subtotal = line_sum
        if line_sum is not None and val(ex, "subtotal") is not None and abs(line_sum - val(ex, "subtotal")) > 1:
            F.append(Finding("SUM_MISMATCH", "hold", "Line items don't add up to the stated subtotal",
                             f"Lines sum to {inr(line_sum)}, subtotal printed as {inr(val(ex, 'subtotal'))} "
                             f"(difference {inr(val(ex, 'subtotal') - line_sum)}).",
                             "Ask the vendor to reissue with correct totals. Do not pay the printed figure.",
                             {"line_sum": line_sum, "stated": val(ex, "subtotal")}))
        if subtotal is not None and total is not None:
            if tax_lines and abs(subtotal + tax_sum - total) > 1:
                F.append(Finding("TOTAL_MISMATCH", "hold", "Subtotal + tax ≠ total",
                                 f"{inr(subtotal)} + {inr(tax_sum)} = {inr(subtotal + tax_sum)}, total shows {inr(total)}",
                                 "Ask the vendor for a corrected invoice."))
            if rate is not None and tax_lines and subtotal and abs(tax_sum - subtotal * rate / 100) > max(1, subtotal * 0.001):
                F.append(Finding("TAX_RATE_MISMATCH", "warn", "Tax amount doesn't match the stated rate",
                                 f"{rate:g}% of {inr(subtotal)} is {inr(subtotal * rate / 100)}, tax shown is {inr(tax_sum)}"))
    if rate is not None and rate not in GST_SLABS:
        F.append(Finding("UNUSUAL_GST_RATE", "warn", f"GST rate {rate:g}% is not a standard slab",
                         "Standard slabs are 0, 5, 18 and 40% (plus 0.25/3% for specific goods)."))

    # place of supply -> tax type
    vg = (val(ex, "vendor_gstin") or "").upper()
    labels = " ".join(t.get("label", "") for t in tax_lines).upper()
    if vg[:2].isdigit() and tax_lines:
        intra = vg[:2] == BUYER["state_code"]
        has_igst, has_split = "IGST" in labels, ("CGST" in labels or "SGST" in labels)
        if intra and has_igst or (not intra and has_split):
            F.append(Finding("WRONG_TAX_TYPE", "hold", "Wrong type of GST for this supply",
                             f"Vendor state {vg[:2]} vs buyer state {BUYER['state_code']} needs "
                             f"{'CGST + SGST' if intra else 'IGST'}, invoice charges {'IGST' if has_igst else 'CGST + SGST'}.",
                             "Ask the vendor to reissue — input tax credit can't be claimed on the wrong tax type."))

    # dates
    d = parse_date(val(ex, "invoice_date"))
    if d:
        if d > today:
            F.append(Finding("FUTURE_DATE", "hold", "Invoice is dated in the future", f"Dated {d}, today is {today}",
                             "Confirm the date with the vendor."))
        elif (today - d).days > settings["max_invoice_age_days"]:
            F.append(Finding("STALE_INVOICE", "hold", "Invoice is unusually old",
                             f"{(today - d).days} days old (limit {settings['max_invoice_age_days']})",
                             "Check whether this was already settled outside the system."))
    ctx["invoice_date"] = d

    # identities
    if vg and not gstin_valid(vg) and not gstin_misread_of(vg, val(ex, "vendor_name")):
        F.append(Finding("BAD_GSTIN", "hold", "Vendor GSTIN is not valid",
                         f"'{vg}' fails the GSTIN format/checksum.",
                         "Verify the vendor's GSTIN on the GST portal before paying."))
    bg = (val(ex, "buyer_gstin") or "").upper().replace(" ", "")
    if bg and bg != BUYER["gstin"]:
        name_ok = fuzz.token_set_ratio((val(ex, "buyer_name") or "").lower(), BUYER["name"].lower()) >= 85
        near_ours = Levenshtein.distance(bg, BUYER["gstin"]) <= 2
        if not gstin_valid(bg) and (name_ok or near_ours):
            # an invalid checksum means the characters were misread (e.g. a blurry scan), not that the
            # invoice belongs to another registered company - and the billed-to name is ours
            F.append(Finding("BUYER_GSTIN_MISREAD", "warn", "Billed-to GSTIN looks misread",
                             f"Read as {bg}, which fails the GSTIN checksum, so it isn't a real registration"
                             + (f"; it is {Levenshtein.distance(bg, BUYER['gstin'])} character(s) from ours ({BUYER['gstin']})" if near_ours else "")
                             + (f"; the billed-to name matches {BUYER['name']}." if name_ok else ".")))
        else:
            F.append(Finding("WRONG_BUYER", "hold", "Invoice is addressed to a different company",
                             f"Billed-to GSTIN {bg} is a valid registration that isn't ours ({BUYER['gstin']}).",
                             "Return to vendor; it may belong to another entity."))

    ctx.update(subtotal=subtotal, total=total, rate=rate, treatment=treatment, line_sum=line_sum)
    if not F:
        F.append(Finding("VALID", "pass", "All required fields present and the arithmetic checks out"))
    return F, ctx


# ================================================================ stage 4: vendor
def check_vendor(ex: dict) -> tuple[list[Finding], dict | None]:
    F: list[Finding] = []
    g = (val(ex, "vendor_gstin") or "").upper().strip()
    name = val(ex, "vendor_name") or ""
    vendor = next((v for v in VENDORS if v["gstin"] == g), None) if g else None
    how = "GSTIN"
    if not vendor and g:
        vendor = gstin_misread_of(g, name)
        if vendor:
            how = "GSTIN (corrected misread)"
            F.append(Finding("VENDOR_GSTIN_MISREAD", "warn", "Vendor GSTIN looks misread — matched to the vendor master",
                             f"Read as {g}, which fails the GSTIN checksum. It is "
                             f"{Levenshtein.distance(g, vendor['gstin'])} character(s) from {vendor['legal_name']}'s "
                             f"registered {vendor['gstin']}, and the name matches.",
                             data={"read": g, "master": vendor["gstin"]}))
    if not vendor and name:
        best = max(VENDORS, key=lambda v: fuzz.token_set_ratio(name.lower(), v["legal_name"].lower()))
        score = fuzz.token_set_ratio(name.lower(), best["legal_name"].lower())
        if score >= 90:
            vendor, how = best, "name"
            F.append(Finding("VENDOR_BY_NAME", "warn" if not g else "hold",
                             "Vendor matched by name, not GSTIN",
                             f"'{name}' ≈ '{best['legal_name']}' ({score:.0f}%). "
                             + (f"GSTIN on invoice {g} differs from master {best['gstin']}." if g else "No GSTIN on invoice."),
                             "Confirm the vendor's GSTIN." if g else ""))
    if not vendor:
        F.append(Finding("UNKNOWN_VENDOR", "hold", "Vendor is not in the vendor master",
                         f"No approved vendor matches '{name}' / {g or 'no GSTIN'}.",
                         "Route to vendor onboarding. Do not pay until the vendor is verified."))
        return F, None

    if vendor["status"] != "Active":
        F.append(Finding("VENDOR_BLOCKED", "reject", f"Vendor is {vendor['status'].lower()}",
                         vendor.get("blocked_reason") or "", "Do not pay. Escalate to procurement if this is unexpected."))

    acct, ifsc = digits(val(ex, "bank_account")), (val(ex, "ifsc") or "").upper().strip()
    if acct or ifsc:
        mismatched = []
        if acct and acct != vendor["bank_account"]:
            mismatched.append(("account", acct, vendor["bank_account"]))
        if ifsc and ifsc != vendor["ifsc"]:
            mismatched.append(("IFSC", ifsc, vendor["ifsc"]))
        if mismatched:
            F.append(Finding(
                "BANK_CHANGED", "hold", "Bank details on the invoice differ from the vendor master",
                "; ".join(f"{k}: invoice {a} vs master {b}" for k, a, b in mismatched)
                + ". Changed bank details on an otherwise normal invoice is the most common payment-fraud pattern.",
                f"Call the vendor on their REGISTERED number ({vendor['registered_phone']}) — not any number on the "
                "invoice — to confirm. Update the master only after verification.",
                {"invoice": {"account": acct, "ifsc": ifsc},
                 "master": {"account": vendor["bank_account"], "ifsc": vendor["ifsc"]}}))
    else:
        F.append(Finding("NO_BANK_ON_INVOICE", "info", "No bank details on invoice — payment will go to the account on file"))

    if not [f for f in F if SEVERITY_RANK[f.severity] >= 2]:
        F.insert(0, Finding("VENDOR_OK", "pass", f"Approved vendor {vendor['vendor_id']} · matched by {how}",
                            f"{vendor['legal_name']} · active · bank details match master"
                            if acct else f"{vendor['legal_name']} · active"))
    return F, vendor


# ================================================================ stage 5: duplicates
def check_duplicates(ex: dict, vendor: dict | None, ctx: dict, ledger: list[dict],
                     same_file_runs: list[dict], settings: dict) -> list[Finding]:
    F: list[Finding] = []
    num = val(ex, "invoice_number")
    n_norm, n_core = norm_invoice_no(num), digit_core(num)
    total, d = ctx.get("total"), ctx.get("invoice_date")
    po_ref = norm_po(val(ex, "po_reference"))
    vid = vendor["vendor_id"] if vendor else None

    candidates = []
    for row in ledger:
        if vid and row["vendor_id"] != vid:
            continue
        if n_norm and norm_invoice_no(row["invoice_number"]) == n_norm:
            F.append(Finding("EXACT_DUPLICATE", "reject", "This invoice has already been processed",
                             f"{row['invoice_number']} from this vendor is already in the ledger "
                             f"({row['status']}{', paid ' + row['paid_on'] if row.get('paid_on') else ''}).",
                             "Do not pay again. Reply to the vendor with the original payment reference.",
                             {"match": row}))
            return F
        signals, score = [], 0
        if n_core and digit_core(row["invoice_number"]) == n_core:
            score += 50
            signals.append(f"same invoice number core '{n_core}'")
        if total is not None and abs(row["total"] - total) <= 1:
            score += 30
            signals.append(f"same amount {inr(total)}")
        rd = parse_date(row["invoice_date"])
        if d and rd:
            gap = abs((d - rd).days)
            if gap == 0:
                score += 20
                signals.append("same invoice date")
            elif gap <= 7:
                score += 15
                signals.append(f"dated {gap} days apart")
        if po_ref and row.get("po_number") == po_ref:
            score += 10
            signals.append("same PO")
        if score >= 40:
            candidates.append((score, row, signals))

    candidates.sort(key=lambda c: -c[0])
    if candidates and candidates[0][0] >= settings["duplicate_hold_score"]:
        score, row, signals = candidates[0]
        F.append(Finding("PROBABLE_DUPLICATE", "hold", f"Probable duplicate of {row['invoice_number']}",
                         f"Duplicate score {score}/110: " + ", ".join(signals) + ". "
                         + (f"{row['invoice_number']} was paid on {row['paid_on']}." if row.get('paid_on')
                          else f"{row['invoice_number']} is {row['status'].lower()}."),
                         f"Compare with {row['invoice_number']}. If it's a re-send, reject it and tell the vendor it was already paid.",
                         {"match": row, "score": score, "signals": signals}))
    elif candidates:
        score, row, signals = candidates[0]
        F.append(Finding("SIMILAR_NOT_DUPLICATE", "info", f"Resembles {row['invoice_number']} but isn't a duplicate",
                         f"Score {score}/110 ({', '.join(signals)}), below the hold threshold "
                         f"{settings['duplicate_hold_score']} — the invoice number is different, consistent with a recurring or instalment charge.",
                         data={"match": row, "score": score}))
    if same_file_runs:
        F.append(Finding("SAME_FILE_SEEN", "info", "This exact file was uploaded before",
                         ", ".join(f"run #{r['id']} → {r['decision']}" for r in same_file_runs)))
    if not any(f.code in ("PROBABLE_DUPLICATE", "SIMILAR_NOT_DUPLICATE") for f in F):
        F.insert(0, Finding("NO_DUPLICATE", "pass", "No matching invoice in the ledger"))
    return F


# ================================================================ stage 6: PO match
def _desc_sim(inv_lines, po_lines) -> float:
    if not inv_lines:
        return 0
    return sum(max(fuzz.token_set_ratio((il.get("description") or "").lower(), pl["description"].lower())
                   for pl in po_lines) for il in inv_lines) / len(inv_lines)


def remaining_on_po(po: dict, ledger: list[dict]) -> tuple[float, float, list[dict]]:
    prior = [r for r in ledger if r.get("po_number") == po["po_number"]]
    invoiced = round(sum(r["subtotal"] for r in prior), 2)
    return invoiced, round(po["subtotal"] - invoiced, 2), prior


def match_po(ex: dict, vendor: dict | None, ctx: dict, ledger: list[dict], settings: dict) -> tuple[list[Finding], dict | None]:
    F: list[Finding] = []
    raw = val(ex, "po_reference")
    ref = norm_po(raw)
    if ref:
        po = PO_BY_NUMBER.get(ref)
        if not po:
            F.append(Finding("PO_NOT_FOUND", "hold", f"PO '{raw}' does not exist",
                             "No purchase order with that number.", "Ask the vendor for the correct PO number."))
            return F, None
        if vendor and po["vendor_id"] != vendor["vendor_id"]:
            F.append(Finding("PO_OTHER_VENDOR", "hold", f"{ref} belongs to a different vendor",
                             f"{ref} was raised on {VENDOR_BY_ID[po['vendor_id']]['legal_name']}.",
                             "Ask the vendor for the correct PO number."))
            return F, None
        note = f" (printed as '{raw}')" if raw.strip().upper() != ref else ""
        F.append(Finding("PO_EXPLICIT", "pass", f"Matched to {ref}{note}",
                         f"PO dated {po['po_date']} · {inr(po['subtotal'])} pre-tax · {po['status']}"))
        return F, po

    if not vendor:
        F.append(Finding("PO_SKIPPED", "hold", "Can't look for a PO without a known vendor"))
        return F, None

    # no PO reference -> score the vendor's open POs; only accept a clear winner
    inv_lines = ex.get("line_items") or []
    sub, d = ctx.get("subtotal"), ctx.get("invoice_date")
    scored = []
    for po in POS:
        if po["vendor_id"] != vendor["vendor_id"] or po["status"] != "Open":
            continue
        _, remaining, _ = remaining_on_po(po, ledger)
        desc = _desc_sim(inv_lines, po["lines"])
        amount = 100.0 if sub is not None and sub <= remaining + 1 else 0.0
        prices = [l.get("unit_price") for l in inv_lines if l.get("unit_price") is not None]
        price = (100.0 * sum(any(abs(p - pl["unit_price"]) <= pl["unit_price"] * 0.01 for pl in po["lines"]) for p in prices)
                 / len(prices)) if prices else 0.0
        dt = 100.0 if d and d >= date.fromisoformat(po["po_date"]) else 0.0
        score = round(0.45 * desc + 0.20 * amount + 0.25 * price + 0.10 * dt)
        scored.append({"po_number": po["po_number"], "score": score, "description": po["lines"][0]["description"],
                       "remaining": remaining,
                       "breakdown": {"description": round(desc), "amount_fits": amount, "unit_price": round(price), "date": dt}})
    scored.sort(key=lambda s: -s["score"])
    if not scored:
        F.append(Finding("NO_OPEN_PO", "hold", "No PO reference, and the vendor has no open POs",
                         action="Ask the requester whether this purchase was authorised; a PO may need to be raised."))
        return F, None
    top = scored[0]
    gap = top["score"] - (scored[1]["score"] if len(scored) > 1 else 0)
    if top["score"] >= settings["po_inference_min_score"] and gap >= settings["po_inference_min_gap"]:
        F.append(Finding("PO_INFERRED", "info", f"No PO on invoice — inferred {top['po_number']}",
                         f"Score {top['score']}/100, {gap} points ahead of the next option.", data={"candidates": scored}))
        return F, PO_BY_NUMBER[top["po_number"]]
    F.append(Finding("PO_AMBIGUOUS", "hold", "No PO on the invoice, and more than one PO fits",
                     " vs ".join(f"{s['po_number']} ({s['score']})" for s in scored[:3])
                     + f". The best match is not clearly ahead (gap {gap} < {settings['po_inference_min_gap']}), "
                     "so the process won't guess.",
                     "Pick the correct PO from the candidates, or ask the vendor which PO this bills against.",
                     {"candidates": scored}))
    return F, None


# ================================================================ stage 7: amounts vs PO
def check_amounts(ex: dict, po: dict | None, ctx: dict, ledger: list[dict], settings: dict) -> list[Finding]:
    F: list[Finding] = []
    if not po:
        return [Finding("AMOUNT_SKIPPED", "info", "Skipped — amounts can only be checked once a PO is known",
                        "If a reviewer picks the PO, this check runs against it.")]
    sub = ctx.get("subtotal")
    invoiced, remaining, prior = remaining_on_po(po, ledger)
    tol = min(po["subtotal"] * settings["amount_tolerance_pct"] / 100, settings["amount_tolerance_abs"])
    summary = {"po_subtotal": po["subtotal"], "invoiced_before": invoiced, "remaining": remaining,
               "this_invoice": sub, "tolerance": round(tol, 2),
               "prior_invoices": [{"invoice_number": r["invoice_number"], "subtotal": r["subtotal"],
                                   "invoice_date": r["invoice_date"]} for r in prior]}

    if sub is not None:
        if sub > remaining + tol:
            excess = round(sub - max(remaining, 0), 2)
            if remaining <= 0:
                title, act = f"{po['po_number']} is already fully billed", "Do not pay against this PO. Ask the vendor what this invoice is for."
            else:
                title = f"Invoice exceeds what's left on {po['po_number']}"
                act = (f"Either pay only {inr(remaining)} (+GST) and ask the vendor to credit {inr(excess)}, "
                       "or get the PO amended by procurement before paying in full.")
            F.append(Finding("PO_OVERRUN", "hold", title,
                             f"PO {inr(po['subtotal'])} − already invoiced {inr(invoiced)} "
                             f"({len(prior)} invoice{'s' if len(prior) != 1 else ''}) = {inr(remaining)} left. "
                             f"This invoice is {inr(sub)} → over by {inr(excess)}.",
                             act, dict(summary, excess=excess)))
        elif sub > remaining:
            F.append(Finding("WITHIN_TOLERANCE", "info", "Slightly above the PO balance, within tolerance",
                             f"Over by {inr(sub - remaining)}, tolerance {inr(tol)}.", data=summary))

    # line-level: price and quantity against the PO lines
    rate = ctx.get("rate") or po["tax_rate"]
    for i, l in enumerate(ex.get("line_items") or [], 1):
        desc = (l.get("description") or "").lower()
        pl = max(po["lines"], key=lambda p: fuzz.token_set_ratio(desc, p["description"].lower()))
        price = l.get("unit_price")
        if price is not None and ctx.get("treatment") == "inclusive":
            price = round(price / (1 + rate / 100), 4)
        if price is not None and price > pl["unit_price"] * (1 + settings["price_tolerance_pct"] / 100):
            F.append(Finding("PRICE_ABOVE_PO", "hold", f"Line {i}: unit price higher than agreed",
                             f"Invoice {inr(price)} vs PO {inr(pl['unit_price'])} for '{pl['description']}'.",
                             "Pay only at the PO price, or get the PO amended.", {"line": i}))
        q = l.get("quantity")
        if q is not None and q > pl["qty"]:
            F.append(Finding("QTY_ABOVE_PO", "hold", f"Line {i}: more units billed than ordered",
                             f"Billed {q:g}, ordered {pl['qty']:g}.", "Confirm delivery quantities with the receiving team."))
    if ctx.get("rate") is not None and abs(ctx["rate"] - po["tax_rate"]) > 0.01:
        F.append(Finding("TAX_RATE_VS_PO", "warn", "GST rate differs from the PO",
                         f"Invoice {ctx['rate']:g}% vs PO {po['tax_rate']:g}%."))
    total = ctx.get("total")
    if total is not None and total > settings["auto_approve_limit"]:
        F.append(Finding("ABOVE_AUTO_LIMIT", "hold", "Above the auto-approval limit",
                         f"{inr(total)} > {inr(settings['auto_approve_limit'])}.",
                         "Needs a finance manager's approval."))
    if not [f for f in F if SEVERITY_RANK[f.severity] >= 2]:
        F.insert(0, Finding("AMOUNT_OK", "pass", "Amount and prices are within the PO",
                            f"{inr(sub)} against {inr(remaining)} remaining on {po['po_number']}.", data=summary))
    return F


# ================================================================ stage 8: decide
def decide(all_findings: list[Finding]) -> str:
    worst = max((SEVERITY_RANK[f.severity] for f in all_findings), default=0)
    return "REJECT" if worst == 4 else "HOLD" if worst == 3 else "APPROVE"


def to_dicts(F: list[Finding]) -> list[dict]:
    return [asdict(f) for f in F]
