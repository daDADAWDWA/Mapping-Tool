"""
Regression test against a HUMAN-VERIFIED Final Output sheet (Gurukrupa Vyom).

Run from the app folder:      python tests/test_gurukrupa_final_output.py

The CRE / RERA / agreement figures below are the ones that actually produced
the verified sheet, so this pins down four things that were previously wrong:

  1. An agreement's carpet area must have its balcony ADDED, exactly as CRE
     figures do. Agreements state RERA carpet excluding balcony, and because
     agreements outrank CRE, forgetting this dragged whole rows ~10 m² low.
  2. A cluster's representative must be chosen by SOURCE PRIORITY, weighing
     support per priority level -- not by raw floor count, which let a RERA
     value on many floors override a CRE value on few.
  3. Duplex agreements (e.g. "1503-1603") collapse to one row, floors joined
     with "/" and the two floors' areas summed.
  4. Formatting: Series "01", Tower "Standalone", "115.21 m²", "1,240 ft²",
     and series with no data omitted rather than emitted blank.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook

from core.agreement import detect_combined_unit
from core.final_output import build_final_output_sheet
from core.template_model import TemplateModel

FT = 10.7639104167

CRE = {  # (position, floor): total m², balcony already folded in
    (1, 1): 86.33, (1, 2): 115.21, (1, 11): 115.21, (1, 12): 115.21,
    (1, 14): 115.21, (1, 15): 115.21, (1, 16): 138.43,
    (1, 13): 2183 / FT, (1, 9): 1240 / FT, (1, 8): 1990 / FT,
    (2, 1): 115.33, (2, 4): 115.33, (2, 8): 115.33, (2, 9): 115.33,
    (2, 11): 115.33, (2, 13): 115.33, (2, 14): 115.33, (2, 15): 115.33,
    (2, 16): 91.5,
    (3, 2): 115.39, (3, 6): 115.39, (3, 9): 115.39, (3, 10): 115.39,
    (3, 11): 115.39, (3, 14): 115.39, (3, 15): 115.39,
    (4, 2): 115.36, (4, 3): 115.36, (4, 10): 115.36, (4, 12): 115.36,
    (4, 14): 115.36, (4, 15): 115.36, (4, 9): 1240 / FT,
}

AGREEMENTS = {  # (position, floor): (carpet EXCLUDING balcony, balcony, file)
    (1, 1): (77.82, 8.51, "101(2).png"),
    (1, 6): (101.22, 13.99, "601(1).png"),
    (1, 8): (184.88, None, "801(2).png"),
    (1, 13): (2485.62 / FT, None, "1301(1).png"),
    (1, 16): (124.85, 13.58, "1601.png"),
    (2, 4): (104.78, 10.55, "402(2).png"),
    (2, 12): (104.78, 10.55, "1302.png"),
    (2, 13): (104.78, 10.55, "1302-1303.png"),
    (2, 16): (81.29, 10.21, "1602.png"),
    (3, 10): (105.25, 10.14, "1003.png"),
    (3, 13): (105.25, 10.14, "1302-1303.png"),
    (3, 15): (105.25, 10.14, "1503-1603.png"),   # duplex 15/16
    (4, 11): (105.36, 10.0, "1104(1).png"),
    (4, 16): (105.39, 10.0, "1604(1).png"),
}

# (Series, Final Carpet m²) pairs from the verified sheet.
EXPECTED = {
    ("01", "86.33 m²"), ("01", "115.21 m²"), ("01", "184.88 m²"),
    ("01", "230.92 m²"), ("01", "138.43 m²"),
    ("02", "115.33 m²"), ("02", "91.5 m²"),
    ("03", "115.39 m²"), ("03", "230.78 m²"),
    ("04", "115.36 m²"),
}
EXPECTED_DUPLEX_FLOORS = "15/16"


def build_rera():
    rera = {}
    for floor in range(1, 17):
        rera[(1, floor)] = 86.35 if floor == 1 else (184.88 if floor == 8 else 115.20)
        rera[(2, floor)] = 115.10
        if floor not in (1, 8):
            rera[(3, floor)] = 115.28
            rera[(4, floor)] = 115.39
    rera[(3, 1)] = 90.03
    return rera


def run():
    tm = TemplateModel.from_csv(
        Path(__file__).resolve().parent.parent / "projects" / "Gurukrupa_Vyom" / "template.csv"
    )
    by_col, rera_by_col, agreements = {}, {}, {}

    for (pos, floor), value in CRE.items():
        col = tm.section1.series_cols[pos - 1]
        by_col.setdefault(col, []).append({
            "value": value, "unit": "sq.m", "area_type": "reracarpet",
            "floor": floor, "confirmed_area_type": "RERA Carpet",
        })
    for (pos, floor), value in build_rera().items():
        col = tm.section2.series_cols[pos - 1]
        rera_by_col.setdefault(col, []).append(
            {"floor": floor, "value": value, "verified": True}
        )
    for (pos, floor), (carpet, balcony, filename) in AGREEMENTS.items():
        combined = detect_combined_unit(filename, None)
        record = {
            "area_value": carpet, "area_unit": "sq.m", "area_type": "RERA Carpet",
            "balcony_area": balcony, "filename": filename, "verified": True,
            "combined": combined, "page": None,
        }
        agreements[(floor, pos)] = record
        if combined and combined["kind"] == "duplex":
            for other in combined["floors"]:
                agreements[(other, pos)] = record

    wb = Workbook()
    wb.remove(wb.active)
    build_final_output_sheet(wb, [{
        "letter": "", "tm": tm, "by_col_values": by_col,
        "rera_by_col_values": rera_by_col,
        "agreements_by_pos": agreements, "brochure_by_col": {},
    }], "Gurukrupa Vyom", tolerance_ft=5.0)

    rows = [r for r in wb["Final Output"].iter_rows(min_row=4, values_only=True) if r[2]]
    failures = []

    got = {(r[2], str(r[5])) for r in rows}
    for expected in EXPECTED:
        if expected not in got:
            failures.append(f"missing verified row: Series {expected[0]} = {expected[1]}")

    if not any(str(r[4]) == EXPECTED_DUPLEX_FLOORS for r in rows):
        failures.append(f"no duplex row with floors '{EXPECTED_DUPLEX_FLOORS}'")

    for r in rows:
        if r[1] != "Standalone":
            failures.append(f"Tower should be 'Standalone', got {r[1]!r}")
            break
    for r in rows:
        if r[2] and not str(r[2]).isdigit():
            failures.append(f"Series should be zero-padded digits, got {r[2]!r}")
            break
    if any(str(r[2]) in ("05", "06") for r in rows):
        failures.append("empty series 05/06 should be omitted")

    print(f"{len(rows)} rows produced; {len(got & EXPECTED)}/{len(EXPECTED)} verified values matched")
    for r in rows:
        mark = "ok " if (r[2], str(r[5])) in EXPECTED else "?? "
        print(f"  {mark}{r[1]:11} {r[2]:3} {str(r[4]):20} {str(r[5]):12} {r[7]}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nAll verified values reproduced.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
