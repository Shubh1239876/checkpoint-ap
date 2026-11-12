# Checkpoint — AP invoice review: process map & assumptions

## The job being automated
An AP clerk at **Kestrel Foods Pvt Ltd** (Bengaluru) receives vendor invoices as PDFs.
For each one they must decide: **pay it, hold it for someone to look at, or reject it** —
and be able to say why. Checkpoint does that work and shows every step.

Guiding principle: **AI reads, rules decide.**
- AI is used only where judgment over messy input is needed: reading the PDF,
  and writing the plain-English explanation at the end.
- Every decision (match, tolerance, duplicate, approve/hold/reject) is made by deterministic
  code, so the same invoice always gets the same decision and every decision traces to a rule.

## Pipeline

| # | Stage | What happens | Done by |
|---|---|---|---|
| 1 | Intake | Hash the file, count pages, detect text layer → "digital" or "scanned" | Code |
| 2 | Extract | Pull header fields, line items, tax, bank details. Each field returns value + confidence + source snippet. Digital PDF → text model; scanned → vision model | **AI** (Groq, Gemini backup) |
| 3 | Validate | Required fields present, low-confidence fields flagged, lines sum to subtotal, subtotal + tax = total, tax rate sane, dates sane, GSTIN checksum, billed-to = Kestrel | Code |
| 4 | Vendor check | Find vendor in vendor master (GSTIN, then fuzzy name). Status active? Bank account & IFSC match master? | Code |
| 5 | Duplicate check | Compare against invoices already posted: exact match → reject; near match scored on number core, amount, date, PO | Code |
| 6 | PO match | Explicit PO → look up and confirm vendor. No PO → score vendor's open POs; accept only if clearly best | Code |
| 7 | Amount check | Unit prices vs PO; invoice vs **remaining** PO balance (PO minus already invoiced), with tolerance | Code |
| 8 | Decide | Worst finding wins: any REJECT → Reject; any HOLD → Hold; else Approve | Code |
| 9 | Explain | One-line verdict + next action for the AP clerk, grounded only in the findings | **AI** (template fallback) |

Approved invoices are posted to the ledger (so a re-send is caught next time).
Held invoices go to a **review queue**; a person approves or rejects with a note.

## Decisions
- **Approve** — everything checks out; ready for payment run.
- **Hold** — something needs a human; the output says exactly what and what to do.
- **Reject** — must not be paid (exact duplicate, blocked vendor).

When unsure, hold. A late payment is an inconvenience; a wrong payment is hard to recover.

## Edge cases (featured)
1. **Split PO overrun** — vendor bills one PO across several invoices. The new invoice looks
   fine alone, but cumulative billing exceeds the PO. → Hold, with the amount that *can* be paid.
2. **Disguised duplicate** — a scanned re-send of an already-paid invoice, renumbered
   `INV1042-A`. Exact-number check misses it; near-match scoring catches it. → Hold, linked to original.
3. **No PO reference, two plausible POs** — the process must not guess. → Hold with ranked candidates.
4. **Perfect invoice, changed bank account** — all numbers match, but the payee account differs
   from the vendor master (classic payment-diversion fraud). → Hold; verify via registered contact.

Also handled (not featured): blocked vendor (reject), recurring retainer with identical amount
(correctly *not* a duplicate), arithmetic errors / missing invoice number (hold), exact re-upload (reject),
tax-inclusive pricing (correctly approved, no false alarm).

## Assumptions
- Single buyer entity: Kestrel Foods Pvt Ltd, GSTIN 29AAKCK4521M1Z3, Karnataka.
- Currency INR. GST: intra-state (Karnataka vendors) = CGST + SGST; inter-state = IGST.
- PO amounts compared **pre-tax**; tax is checked separately for arithmetic.
- Amount tolerance: invoice may exceed remaining PO balance by up to **1% or ₹500, whichever is lower**.
- Unit price tolerance: **1%** above PO price.
- Auto-approve limit: **₹10,00,000** per invoice; above that → hold for finance manager.
- Invoice older than **180 days** or dated in the future → hold. Invoice dated before its PO → hold.
- Fields extracted with confidence below **0.80** are not trusted for decisions → hold if the field matters.
- Inferred PO accepted only if score ≥ 75 and ≥ 15 points ahead of the next candidate.
- 2-way match (invoice ↔ PO). Goods-receipt (3-way) matching is the natural next step.
- Invoices arrive as uploaded PDFs (email intake would sit in front of stage 1 in production).
