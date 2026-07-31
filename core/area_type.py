"""
Working out WHICH KIND of area a CRE transaction figure actually is.

The transaction's Marathi text gives a number. Whether that number is RERA
carpet, MOFA carpet, built-up or saleable decides whether it can be compared
with anything else -- and the text does not always say.

Three sources of truth, strongest first:

  1. AGREEMENT VALUE MATCH.  The agreement for that flat states both an area
     AND its type. If the transaction's number matches the agreement's number
     within tolerance, then the transaction's number IS that type. Nothing has
     to be inferred from wording at all -- the arithmetic proves it.

  2. THE DESCRIPTION'S OWN WORDING.  The Marathi text often names the type
     ("रेरा कार्पेट एरिया"). On its own that's decent evidence; when it AGREES
     with the agreement it becomes near-certain, and when it CONTRADICTS the
     agreement that is worth flagging loudly -- one of the two documents is
     describing a different measurement.

  3. SERIES INFERENCE.  Flats in one series column share a layout, so once a
     type is confirmed for a value in that series, other flats in the same
     series with the same value (within tolerance) are almost certainly the
     same type. This is how ONE downloaded agreement can label a whole
     column -- which is exactly what the template's checklist means by "every
     unique area/type has agreement evidence".

Every assignment carries a confidence, and nothing here ever changes an area
VALUE -- it only labels one.
"""

from collections import defaultdict

from .final_output import AREA_TYPE_DISPLAY, normalise_area_type, to_m2

# Strongest first. Used for reporting, and to decide which label wins if two
# sources disagree.
CONFIDENCE_AGREEMENT_AND_TEXT = "confirmed by agreement + description text"
CONFIDENCE_AGREEMENT = "confirmed by agreement (value matches)"
CONFIDENCE_TEXT = "from description text only"
CONFIDENCE_SERIES = "inferred from another flat in the same series"
CONFIDENCE_NONE = None


def _cre_display_type(entry):
    """The compact label the description parser produced -> display label."""
    compact = entry.get("area_type")
    if not compact:
        return None
    return AREA_TYPE_DISPLAY.get(compact, str(compact).title())


def confirm_area_types(transaction_grid, agreements_by_pos, series_cols,
                       tolerance_m, series_of_position=None):
    """
    transaction_grid:   {(floor, position): {value, unit, area_type, ...}}
    agreements_by_pos:  {(floor, position): agreement record}
    series_cols:        the template's series column indices, left to right
    series_of_position: optional {position: series label} for readable notes

    Returns (resolved, warnings) where resolved is
        {(floor, position): {"area_type": str|None, "confidence": str|None,
                             "evidence": str|None}}
    """
    resolved = {}
    warnings = []
    labels = series_of_position or {}

    # ---- 1 & 2: agreement value match, cross-checked against the wording ----
    confirmed_by_series = defaultdict(list)   # position -> [(value_m, type)]
    for cell, txn in transaction_grid.items():
        if txn.get("value") is None:
            continue
        floor, position = cell
        text_type = _cre_display_type(txn)
        agreement = agreements_by_pos.get(cell)

        agreement_type = None
        if agreement is not None and agreement.get("area_value") is not None:
            agreement_type = normalise_area_type(agreement.get("area_type"))
            txn_m = to_m2(txn.get("carpet_only", txn["value"]), txn.get("unit"))
            agr_m = to_m2(agreement["area_value"], agreement.get("area_unit"))
            values_match = (
                txn_m is not None and agr_m is not None
                and abs(txn_m - agr_m) < tolerance_m
            )
            series_name = labels.get(position, f"position {position}")

            if values_match and agreement_type:
                if text_type and text_type != agreement_type:
                    # Both documents name a type and they disagree. The
                    # agreement wins (it is the primary document) but this is
                    # exactly the case a human must see.
                    warnings.append(
                        f"Floor {floor} {series_name}: the agreement calls this "
                        f"{agreement_type} but the registration text calls it {text_type}, "
                        f"even though the areas match. Using {agreement_type} — please "
                        f"confirm which basis is correct."
                    )
                    confidence = CONFIDENCE_AGREEMENT
                elif text_type:
                    confidence = CONFIDENCE_AGREEMENT_AND_TEXT
                else:
                    confidence = CONFIDENCE_AGREEMENT
                resolved[cell] = {
                    "area_type": agreement_type, "confidence": confidence,
                    "evidence": agreement.get("filename"),
                }
                confirmed_by_series[position].append((txn_m, agreement_type))
                continue

            if not values_match and agreement_type and txn_m is not None and agr_m is not None:
                # Different numbers -- so they are measuring different things,
                # or one of them is wrong. Either way the agreement's TYPE
                # cannot be transferred onto the transaction's number.
                warnings.append(
                    f"Floor {floor} {series_name}: the agreement says "
                    f"{agreement['area_value']} {agreement.get('area_unit')} "
                    f"({agreement_type}) but the registration text says "
                    f"{txn.get('carpet_only', txn['value'])} {txn.get('unit')}. The areas "
                    f"differ, so the registration figure's type could not be confirmed "
                    f"from the agreement."
                )

        # No usable agreement for this cell: fall back to the wording.
        if text_type:
            resolved[cell] = {"area_type": text_type, "confidence": CONFIDENCE_TEXT,
                              "evidence": None}
        else:
            resolved[cell] = {"area_type": None, "confidence": CONFIDENCE_NONE,
                              "evidence": None}

    # ---- 3: spread a confirmed type across its own series ----
    inferred = 0
    for cell, current in resolved.items():
        if current["confidence"] in (CONFIDENCE_AGREEMENT,
                                     CONFIDENCE_AGREEMENT_AND_TEXT):
            continue
        floor, position = cell
        txn = transaction_grid.get(cell) or {}
        value_m = to_m2(txn.get("carpet_only", txn.get("value")), txn.get("unit"))
        if value_m is None:
            continue
        for confirmed_value, confirmed_type in confirmed_by_series.get(position, []):
            if abs(value_m - confirmed_value) < tolerance_m:
                if current["area_type"] and current["area_type"] != confirmed_type:
                    warnings.append(
                        f"Floor {floor} {labels.get(position, f'position {position}')}: "
                        f"description says {current['area_type']} but an agreement-confirmed "
                        f"flat with the same area in this series is {confirmed_type}. Using "
                        f"{confirmed_type}."
                    )
                resolved[cell] = {"area_type": confirmed_type,
                                  "confidence": CONFIDENCE_SERIES, "evidence": None}
                inferred += 1
                break

    if confirmed_by_series:
        n_confirmed = sum(
            1 for r in resolved.values()
            if r["confidence"] in (CONFIDENCE_AGREEMENT, CONFIDENCE_AGREEMENT_AND_TEXT)
        )
        warnings.append(
            f"Area type confirmed from agreements for {n_confirmed} flat(s) by matching "
            f"areas, and inferred for {inferred} more sharing the same area within their "
            f"series."
        )

    return resolved, warnings


def summarise_series_type(resolved, position, floors=None):
    """
    The best-supported area-type label for one series column, plus its
    confidence -- used for the "Area Type ->" row and Final Output.
    """
    order = [CONFIDENCE_AGREEMENT_AND_TEXT, CONFIDENCE_AGREEMENT,
             CONFIDENCE_SERIES, CONFIDENCE_TEXT]
    best = None
    for (floor, pos), info in resolved.items():
        if pos != position or not info.get("area_type"):
            continue
        if floors is not None and floor not in floors:
            continue
        rank = order.index(info["confidence"]) if info["confidence"] in order else len(order)
        if best is None or rank < best[0]:
            best = (rank, info["area_type"], info["confidence"])
    return (best[1], best[2]) if best else (None, None)
