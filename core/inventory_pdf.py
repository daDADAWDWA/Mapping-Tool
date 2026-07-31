"""
Extracts inventory data (tower, flat number, carpet area) from a MahaRERA-
style "Sold/Booked Inventory" disclosure PDF (the Circular 29 format), using
pdfplumber's table detection.

Handles the structural quirks seen in this document family:
  - The header row appears once, only on the first page; later pages
    continue the same table with no repeated header.
  - A flat disclosed via more than one registered area component (e.g. a
    small ancillary/exclusive area alongside the main flat area) shows up as
    TWO rows under one SR NO / Flat No, with the SR NO / Tower / Flat No
    cells merged (blank/None on the continuation row). These are SUMMED
    into one total carpet area per flat.
  - Any status (SOLD, UNSOLD, REHAB, RESERVED, MORTGAGED, NOT FOR SALE...)
    counts as the flat existing -- status isn't used for anything else yet.

Column headers are matched by KEYWORD CONTAINMENT rather than exact
equality, since these disclosure PDFs wrap column headers across multiple
lines with wording that can vary between builders (e.g. the flat-number
column might be headed "Flat No", or "Number of Flats/Shops/Row House/Plots
etc" -- both contain "flat", which is what's actually keyed on below).
"""

import re

import pandas as pd
import pdfplumber

from .ai_assist import (
    extract_rows_from_images,
    extract_rows_from_text,
    verify_column_mapping,
)

_KEYWORDS = {
    "sr_no": ["srno", "sr."],
    "tower": ["wing", "tower"],
    "flat_no": ["flat", "shop", "unit", "rowhouse", "plot"],
    "carpet_area": ["carpetarea", "area"],
    "status": ["sold", "booked", "status", "unsold"],
    "date": ["registrationdate", "date"],
}


def _normalize(s):
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


def _classify_header(headers):
    """Returns {field: column_index}, matching each field to the FIRST
    column whose normalized header text contains one of its keywords."""
    norm_headers = [_normalize(h) for h in headers]
    mapping = {}
    for field, keywords in _KEYWORDS.items():
        for idx, nh in enumerate(norm_headers):
            if idx in mapping.values():
                continue
            if any(kw in nh for kw in keywords):
                mapping[field] = idx
                break
    return mapping


def _rows_to_summed_df(records, warnings):
    """Shared tail: numeric-coerce, forward-fill merged cells, sum per flat."""
    empty_df = pd.DataFrame(columns=["tower", "flat_no", "carpet_area"])
    df = pd.DataFrame(records)
    if df.empty:
        return empty_df, warnings

    for col in ["tower", "flat_no"]:
        if col in df.columns:
            df[col] = df[col].replace('', None).ffill()

    if "verified" not in df.columns:
        df["verified"] = True
    df["verified"] = df["verified"].fillna(True).astype(bool)

    df["carpet_area"] = pd.to_numeric(
        df["carpet_area"].astype(str).str.replace(',', '').str.strip(), errors="coerce"
    )
    before = len(df)
    df = df.dropna(subset=["carpet_area"])
    dropped = before - len(df)
    if dropped:
        warnings.append(f"{dropped} row(s) in the inventory PDF had a non-numeric carpet area and were skipped.")
    if df.empty:
        return empty_df, warnings

    group_cols = [c for c in ["tower", "flat_no"] if c in df.columns and df[c].notna().any()]
    if not group_cols:
        group_cols = ["flat_no"]
    # A flat is only "verified" if every component row of it was.
    return df.groupby(group_cols, dropna=False, as_index=False).agg(
        carpet_area=("carpet_area", "sum"), verified=("verified", "all")
    ), warnings


def extract_inventory_from_pdf(pdf_path: str, api_key: str = None, use_ai: bool = False,
                               model: str = None):
    """
    Returns (DataFrame with columns [tower, flat_no, carpet_area], warnings).
    One row per physical flat -- carpet area already summed across any
    split area-component rows for that flat.

    With `use_ai` and an API key, three assists switch on:
      - the detected column mapping is checked by AI before any value is read
        (the model returns column INDICES only -- never area values);
      - if no table structure is found at all, rows are reconstructed by AI
        from the page text, with every area cross-checked against that text;
      - if there is no text either (a scanned disclosure), pages are
        rasterized and read as images, and the result is flagged unverified.
    """
    warnings = []
    all_rows = []
    page_texts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for t in page.extract_tables():
                    all_rows.extend(t)
                try:
                    page_texts.append(page.extract_text() or "")
                except Exception:
                    page_texts.append("")
    except Exception as e:
        warnings.append(
            f"Could not open/read this file as a PDF ({type(e).__name__}: {e}). "
            f"Section 2 will be left empty for this tower."
        )
        return pd.DataFrame(columns=["tower", "flat_no", "carpet_area"]), warnings

    empty_df = pd.DataFrame(columns=["tower", "flat_no", "carpet_area"])

    if not all_rows:
        warnings.append("No tables could be detected in this PDF at all.")
        if use_ai:
            records, ai_warnings = extract_rows_from_text(page_texts, api_key=api_key, model=model)
            warnings.extend(ai_warnings)
            if not records:
                records, img_warnings = extract_rows_from_images(pdf_path, api_key=api_key, model=model)
                warnings.extend(img_warnings)
            if records:
                return _rows_to_summed_df(records, warnings)
        else:
            warnings.append(
                "Tick 'Use AI to parse the inventory PDF' in the sidebar to try "
                "reconstructing this table with AI instead."
            )
        return empty_df, warnings

    header_idx = None
    col_map = None
    for i, row in enumerate(all_rows):
        candidate_map = _classify_header(row)
        if "flat_no" in candidate_map and "carpet_area" in candidate_map:
            header_idx = i
            col_map = candidate_map
            break

    if header_idx is None:
        warnings.append(
            "Could not find a header row with recognizable Flat No / Carpet "
            "Area columns in this PDF."
        )
        if use_ai:
            records, ai_warnings = extract_rows_from_text(page_texts, api_key=api_key, model=model)
            warnings.extend(ai_warnings)
            if records:
                return _rows_to_summed_df(records, warnings)
        warnings.append("Section 2 will be left empty for this tower.")
        return empty_df, warnings

    # AI check on the column mapping, BEFORE any value is read out of a row.
    if use_ai:
        data_sample = [r for r in all_rows[header_idx + 1:header_idx + 12]
                       if r and any(c not in (None, '') for c in r)]
        ai_map, map_notes = verify_column_mapping(
            all_rows[header_idx], data_sample, col_map, api_key=api_key, model=model
        )
        warnings.extend(map_notes)
        if ai_map:
            col_map = ai_map

    def get(row, field):
        idx = col_map.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    records = []
    for row in all_rows[header_idx + 1:]:
        if row is None or all(c is None or str(c).strip() == '' for c in row):
            continue
        records.append({
            "tower": get(row, "tower"),
            "flat_no": get(row, "flat_no"),
            "carpet_area": get(row, "carpet_area"),
        })

    if not records:
        warnings.append("A header row was found in the PDF but no data rows followed it.")
        return empty_df, warnings

    return _rows_to_summed_df(records, warnings)
