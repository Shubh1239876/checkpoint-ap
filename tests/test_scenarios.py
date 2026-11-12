"""Runs every sample invoice through the real pipeline (offline extractor) and checks
the decision and the key finding. Run:  python -m pytest -q tests/  (or python tests/test_scenarios.py)"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
tmp = tempfile.mkdtemp()
os.environ["CHECKPOINT_DB"] = f"{tmp}/test.db"
os.environ["CHECKPOINT_UPLOADS"] = f"{tmp}/uploads"
os.environ.pop("GEMINI_API_KEY", None)

from app import store  # noqa: E402
from app.pipeline import run_pipeline, file_hash  # noqa: E402
from tests.fixture_extractor import fixture_extractor  # noqa: E402

TODAY = date(2026, 11, 12)
EXPECT = {
    "01_happy_path_blueoak": ("APPROVE", None),
    "02_happy_tax_inclusive_scanned_greenfield": ("APPROVE", "TAX_INCLUSIVE"),
    "03_edge_split_po_overrun_precision": ("HOLD", "PO_OVERRUN"),
    "04_edge_disguised_duplicate_scanned_blueoak": ("HOLD", "PROBABLE_DUPLICATE"),
    "05_edge_no_po_two_candidates_sunrise": ("HOLD", "PO_AMBIGUOUS"),
    "06_edge_bank_details_changed_acme": ("HOLD", "BANK_CHANGED"),
    "07_extra_blocked_vendor_coastal": ("REJECT", "VENDOR_BLOCKED"),
    "08_extra_recurring_retainer_nimbus": ("APPROVE", "SIMILAR_NOT_DUPLICATE"),
    "09_extra_math_error_no_number_metro": ("HOLD", "SUM_MISMATCH"),
}


def run(path: Path):
    pdf = path.read_bytes()
    rid = store.create_run(path.name, file_hash(pdf), str(path))
    events = []
    run_pipeline(rid, pdf, events.append, extractor=fixture_extractor, today=TODAY)
    return store.get_run(rid), events


def test_all_scenarios(verbose=False):
    store.init(reset=True)
    failures = []
    for stem, (want, code) in EXPECT.items():
        r, _ = run(ROOT / "samples" / f"{stem}.pdf")
        codes = [f["code"] for f in r["findings"]]
        ok = r["decision"] == want and (code is None or code in codes)
        if verbose:
            print(f"{'OK ' if ok else 'FAIL'} {stem:48s} {r['decision']:8s} {r['headline']}")
            for f in r["findings"]:
                if f["severity"] not in ("pass",):
                    print(f"       [{f['severity']}] {f['code']}: {f['title']} — {f['detail'][:140]}")
        if not ok:
            failures.append((stem, r["decision"], codes))
    # re-upload of the approved happy path must now be rejected as an exact duplicate
    r, _ = run(ROOT / "samples" / "01_happy_path_blueoak.pdf")
    if verbose:
        print(f"{'OK ' if r['decision'] == 'REJECT' else 'FAIL'} re-upload of 01                                  {r['decision']:8s} {r['headline']}")
    if r["decision"] != "REJECT":
        failures.append(("reupload", r["decision"], []))
    assert not failures, failures


if __name__ == "__main__":
    test_all_scenarios(verbose=True)
    print("all scenarios pass")
