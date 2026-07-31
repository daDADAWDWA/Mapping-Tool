"""
Cleans the two input files before anything else touches them. Kept as a
separate, first pipeline step so it's easy to see/extend what "clean" means
without hunting through the matching/extraction logic.
"""

import pandas as pd

from .aliases import find_column


def clean_transaction_df(df: pd.DataFrame, aliases: dict) -> pd.DataFrame:
    df = df.copy()

    # Normalize column names: strip stray whitespace around headers.
    df.columns = [str(c).strip() for c in df.columns]

    # Strip whitespace on every string cell; leave everything else alone.
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"nan": "", "None": ""})

    # Drop fully-blank rows.
    df = df[~(df.astype(str).apply(lambda r: "".join(r).strip() == "", axis=1))]

    # Drop exact duplicate rows (not "same flat, different transaction" --
    # literally identical rows, which would double-count a sale).
    df = df.drop_duplicates()

    # Registration Year should be numeric for "most recent" sorting; coerce
    # bad values to NaN rather than crashing.
    year_col = find_column(df.columns, "registration_year", aliases)
    if year_col:
        df[year_col] = pd.to_numeric(df[year_col], errors="coerce")

    # Parse the registration date, whatever it's actually called in this file.
    # Try DD-MM-YYYY first (the format seen so far), then fall back to a
    # generic day-first parse if that format doesn't fit this file.
    date_col = find_column(df.columns, "registration_date", aliases)
    if date_col:
        parsed = pd.to_datetime(df[date_col], format="%d-%m-%Y", errors="coerce")
        if parsed.isna().mean() > 0.5:
            parsed = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)
        df["_reg_date_parsed"] = parsed
    else:
        df["_reg_date_parsed"] = pd.NaT

    return df.reset_index(drop=True)


def clean_inventory_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace({"nan": "", "None": ""})

    df = df[~(df.astype(str).apply(lambda r: "".join(r).strip() == "", axis=1))]
    df = df.drop_duplicates()

    return df.reset_index(drop=True)
