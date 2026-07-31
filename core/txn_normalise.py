"""
Turning a messy transaction export into one clean, checkable CSV.

The `property_description` field carries the real facts -- unit number, wing,
floor and every area -- while the structured columns beside it are often wrong
or empty. In a real export:

    column unit_number = "122"      description = "apartment bearing No.6122"
    column unit_number = "FLAT"     description = "...bearing No.733..."
    column unit_number = "162)"     column carpet_area = ""  (empty for 21 of 47)

So the description is read FIRST, with AI, and the structured columns are used
only to fill what the description didn't state.

WHY THIS RUNS ONCE, NOT EVERY TIME
==================================
The output is a normal CSV with plain columns and a canonical one-line
description per row. It can be read, corrected by hand, kept, and re-run
through the generator with NO AI at all -- the deterministic regex path parses
the canonical line perfectly. So the messy reading happens once and everything
after it is repeatable.

EVERY NUMBER IS CHECKED AGAINST THE SOURCE
==========================================
An area is accepted only if it literally appears in that row's own
description. A value the model produced that isn't in the text is dropped and
the row falls back to regex, then to the structured columns -- each labelled,
so the Source column always says where the number came from.
"""

import json
import re

import pandas as pd

from .ai_assist import _ask, _client, _values_present_in_text
from .area_extract import classify_area_matches, extract_area

OUTPUT_COLUMNS = [
    "Unit No", "Wing", "Tower", "Floor",
    "Carpet Area", "Carpet Unit", "Balcony Area", "Total Area", "Area Type",
    "Property Description", "Registration Date", "Registration Year",
    "Source", "Verified", "Notes", "Original Description",
]

# Column names in the raw export that can fill a gap, per logical field.
FALLBACK_COLUMNS = {
    "unit_no": ["unit_number", "unit no", "unit", "flat_no", "flat number"],
    "wing": ["wing"],
    "tower": ["tower", "tower/wing"],
    "floor": ["floor", "floor no"],
    "carpet": ["carpet_area", "carpet area"],
    "date": ["registration_date", "registration date"],
    "year": ["registration_year", "registration year"],
}

_BATCH_PROMPT = (
    "Each block below is the free-text property description of one registered "
    "Indian property transaction. They are messy, inconsistent, and mix "
    "English with Marathi or Kannada.\\n\\n"
    "For EACH block, extract the flat's details.\\n\\n"
    "Return ONLY JSON:\\n"
    '{"rows": [{"id": 1, "unit_no": "6122", "wing": "A", "tower": "Tower 3", '
    '"floor": "12th Floor", '
    '"areas": [{"area_type": "RERA Carpet", "value": 1227, "unit": "sq.ft"}, '
    '{"area_type": "Balcony Area", "value": 96, "unit": "sq.ft"}], '
    '"source_text": "the exact phrase the areas were read from"}]}\\n\\n'
    "RULES\\n"
    "- id must be the block's number, so rows can be matched back.\\n"
    "- unit_no: the flat/apartment number as the TEXT states it -- e.g. "
    "'apartment bearing No.6122' is 6122, not 122. Strip any building or "
    "khata prefix ('1526/162' is 162 unless the text calls the whole thing "
    "the flat number).\\n"
    "- floor: as stated. Where a document gives two ('Seventh Floor (Eighth "
    "Floor as referred in the sanctioned plan)'), take the FIRST -- that is "
    "the flat's own numbering.\\n"
    "- areas: EVERY area stated for the flat, type worded as printed: RERA "
    "Carpet, Carpet Area, MOFA Carpet, Built-up, Super Built-up, Saleable, "
    "Balcony, Dry Balcony, Terrace, Total Area.\\n"
    "- unit: 'sq.ft' or 'sq.m'. 'Square Feet' and 'चौ. फुट' are sq.ft; "
    "'square meters' and 'चौ. मीटर' are sq.m.\\n"
    "- Copy numbers EXACTLY as printed, including decimals. Do not convert, "
    "round, or add anything up.\\n"
    "- NEVER return these as the flat's area: undivided/proportionate land "
    "share, common area, land share area, plot or CTS area, parking area, "
    "special private area, market value, consideration.\\n"
    "- If a description states only a super built-up or saleable area and no "
    "carpet area, return just that one with its correct type. Do NOT relabel "
    "it as carpet -- it measures considerably more.\\n"
    "- source_text: transcribe the exact phrase the numbers came from.\\n"
    "- Omit a field you cannot read rather than guessing.\\n"
    "No other text."
)

CARPET_PREFERENCE = ["RERA Carpet", "MOFA Carpet", "Carpet"]
BALCONY_LABELS = {"Balcony", "Dry Balcony", "Terrace", "Deck"}


def _find_column(columns, candidates):
    normalised = {re.sub(r'[^a-z0-9]', '', str(c).lower()): c for c in columns}
    for cand in candidates:
        key = re.sub(r'[^a-z0-9]', '', cand.lower())
        if key in normalised:
            return normalised[key]
    return None


_ORDINALS = {
    "ground": 0, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
}


def _clean_floor(raw):
    """'Seventh Floor' / '7th' / '7' -> 7; 'Ground Floor' -> 0; junk -> None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    for word, number in _ORDINALS.items():
        if word in s:
            return number
    m = re.search(r'(\d{1,3})', s)
    if m:
        value = int(m.group(1))
        return value if 0 <= value <= 200 else None
    return None


def _clean_wing(raw):
    """
    A wing is a short letter code -- 'A', 'B Wing', 'Tower 3'. A value that is
    really a floor ('5TH') or a long phrase is rejected, because copying it
    through corrupts every row that used it.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or len(s) > 20:
        return None
    if re.search(r'(?i)\d+\s*(st|nd|rd|th)\b', s):     # "5TH" is a floor
        return None
    cleaned = re.sub(r'(?i)\b(wing|tower|block|building|no\.?)\b', ' ', s)
    cleaned = re.sub(r'[^A-Za-z0-9]', '', cleaned).upper()
    if not cleaned or len(cleaned) > 6:
        return None
    if cleaned.isdigit() and len(cleaned) > 2:            # a flat number, not a wing
        return None
    return cleaned


def _clean_unit(raw):
    """Keep the digits of a flat number; reject junk like 'FLAT' or '962,Embassy'."""
    if raw is None:
        return None
    s = str(raw).strip()
    numbers = re.findall(r'\d{3,4}', s)
    if numbers:
        return numbers[-1]                                # '1526/162' -> '162'
    digits = re.sub(r'\D', '', s)
    return digits or None


def _label(text):
    from .agreement import normalise_label
    return normalise_label(text)


def _sort_areas(areas, description):
    """
    (carpet, balcony, total, others) from the model's list, keeping only values
    that genuinely appear in this row's own description.
    """
    clean = []
    text_norm = re.sub(r'\s+', '', description or "")
    for a in areas or []:
        if not isinstance(a, dict) or a.get("value") is None:
            continue
        try:
            value = float(str(a["value"]).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        label = _label(a.get("area_type")) or str(a.get("area_type") or "").title()
        if re.search(r'land|cts|plot|parking|common|undivided|special private',
                     str(a.get("area_type") or ""), re.IGNORECASE):
            continue
        unit = str(a.get("unit") or "").lower()
        unit = "sq.m" if ("m" in unit and "mm" not in unit) else "sq.ft"
        verified = _values_present_in_text(value, text_norm)
        clean.append({"label": label, "value": value, "unit": unit,
                      "verified": verified})

    carpet = next((c for pref in CARPET_PREFERENCE for c in clean if c["label"] == pref), None)
    balcony = next((c for c in clean if c["label"] in BALCONY_LABELS), None)
    total = next((c for c in clean if c["label"] == "Total"), None)
    others = [c for c in clean if c not in (carpet, balcony, total)]
    return carpet, balcony, total, others


def _ask_batch(client, descriptions, model, max_chars=4000):
    """descriptions: [(id, text)] -> {id: parsed row}"""
    blocks = "\n\n".join(
        f"--- BLOCK {i} ---\n{(text or '')[:max_chars]}" for i, text in descriptions
    )
    data = _ask(client, _BATCH_PROMPT + "\n\n" + blocks, max_tokens=4000, model=model)
    if not isinstance(data, dict):
        return {}
    out = {}
    for row in data.get("rows") or []:
        if isinstance(row, dict) and isinstance(row.get("id"), int):
            out[row["id"]] = row
    return out


def normalise_transactions(df, api_key=None, model=None, use_ai=True,
                           batch_size=6, progress=None):
    """
    Returns (clean_df, report) where clean_df has OUTPUT_COLUMNS and report is
    a dict of counts plus a list of per-row notes.

    Never raises on a bad row: anything unreadable becomes a row with a blank
    area and a Notes entry saying why, so nothing disappears silently.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    desc_col = _find_column(df.columns, ["property_description", "property description",
                                         "description"])
    if desc_col is None:
        return pd.DataFrame(columns=OUTPUT_COLUMNS), {
            "error": "No property description column found in this file.",
            "rows": 0, "notes": [],
        }
    cols = {k: _find_column(df.columns, v) for k, v in FALLBACK_COLUMNS.items()}

    ai_rows = {}
    client = _client(api_key) if use_ai else None
    if client is not None:
        pending = [(i, str(df.at[i, desc_col] or "")) for i in df.index
                   if str(df.at[i, desc_col] or "").strip()]
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            ai_rows.update(_ask_batch(client, chunk, model))
            if progress:
                progress(min(start + batch_size, len(pending)), len(pending))

    records, notes = [], []
    counts = {"ai": 0, "regex": 0, "csv_column": 0, "none": 0, "unverified": 0}

    for i in df.index:
        description = str(df.at[i, desc_col] or "")
        row_note, source, verified = [], None, True
        unit = wing = tower = floor = None
        carpet = balcony = total = None
        others = []

        got = ai_rows.get(i)
        if got:
            unit = str(got.get("unit_no")).strip() if got.get("unit_no") else None
            wing = str(got.get("wing")).strip() if got.get("wing") else None
            tower = str(got.get("tower")).strip() if got.get("tower") else None
            floor = str(got.get("floor")).strip() if got.get("floor") else None
            carpet, balcony, total, others = _sort_areas(got.get("areas"), description)
            if carpet is not None:
                source = "description (AI)"
                counts["ai"] += 1
                if not carpet["verified"]:
                    verified = False
                    counts["unverified"] += 1
                    row_note.append(
                        f"carpet {carpet['value']:g} not found verbatim in the "
                        f"description — check it"
                    )

        # Regex second: it reads the literal text, so it is the better authority
        # when it and the model disagree about what is written.
        if carpet is None:
            fallback = extract_area(description, use_ai_fallback=False)
            if fallback["value"] is not None:
                carpet = {"label": None, "value": fallback["value"],
                          "unit": fallback["unit"], "verified": True}
                if fallback["balcony"] is not None:
                    balcony = {"label": "Balcony", "value": fallback["balcony"],
                               "unit": fallback["balcony_unit"], "verified": True}
                if fallback["stated_total"] is not None:
                    total = {"label": "Total", "value": fallback["stated_total"],
                             "unit": fallback["total_unit"], "verified": True}
                source = "description (pattern)"
                counts["regex"] += 1
            else:
                seen = set()
                for entry in fallback.get("non_carpet_areas") or []:
                    key = (entry["value"], entry["unit"])
                    if key in seen:
                        continue
                    seen.add(key)
                    others.append({"label": "Super Built-up / other",
                                   "value": entry["value"], "unit": entry["unit"],
                                   "verified": True})

        # Structured columns last, only to fill a genuine gap.
        if carpet is None and cols.get("carpet"):
            raw = str(df.at[i, cols["carpet"]] or "").replace(",", "").strip()
            try:
                value = float(raw)
                if value > 0:
                    carpet = {"label": None, "value": value, "unit": "sq.ft",
                              "verified": False}
                    source = "carpet_area column"
                    counts["csv_column"] += 1
                    row_note.append(
                        "no carpet area in the description; taken from the "
                        "carpet_area column and assumed sq.ft"
                    )
                    verified = False
            except ValueError:
                pass

        if carpet is None:
            counts["none"] += 1
            if others:
                listed = ", ".join(f"{o['value']:g} {o['unit']}" for o in others[:3])
                row_note.append(
                    f"no carpet area anywhere — only {listed} (super built-up / "
                    f"saleable), which cannot substitute for carpet"
                )
            else:
                row_note.append("no area of any kind could be read")

        # Fill identity gaps from the structured columns -- but VALIDATE first.
        # In a real export the `wing` column contained values like "5TH", which
        # is a floor, and copying it through produced "Flat No 174, 2, 5TH Wing".
        if unit is None and cols.get("unit_no"):
            value = _clean_unit(str(df.at[i, cols["unit_no"]] or ""))
            if value:
                unit = value
                row_note.append("unit number taken from the column, not the text")
        if wing is None and cols.get("wing"):
            wing = _clean_wing(str(df.at[i, cols["wing"]] or ""))
        if tower is None and cols.get("tower"):
            tower = _clean_wing(str(df.at[i, cols["tower"]] or "")) or None
        if floor is None and cols.get("floor"):
            floor = _clean_floor(str(df.at[i, cols["floor"]] or ""))
        floor = _clean_floor(floor) if floor is not None else None

        canonical = _canonical_description(unit, wing, tower, floor,
                                           carpet, balcony, total)
        records.append({
            "Unit No": unit,
            "Wing": wing,
            "Tower": tower,
            "Floor": floor,
            "Carpet Area": carpet["value"] if carpet else None,
            "Carpet Unit": carpet["unit"] if carpet else None,
            "Balcony Area": balcony["value"] if balcony else None,
            "Total Area": total["value"] if total else None,
            "Area Type": (carpet or {}).get("label"),
            "Property Description": canonical,
            "Registration Date": str(df.at[i, cols["date"]]) if cols.get("date") else None,
            "Registration Year": str(df.at[i, cols["year"]]) if cols.get("year") else None,
            "Source": source or "none",
            "Verified": "Yes" if verified and carpet else "No",
            "Notes": "; ".join(row_note) or None,
            "Original Description": description[:1500],
        })
        if row_note:
            notes.append(f"Row {i + 1} (unit {unit or '?'}): {'; '.join(row_note)}")

    clean = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    report = {"rows": len(clean), "notes": notes, **counts}
    return clean, report


def _canonical_description(unit, wing, tower, floor, carpet, balcony, total):
    """
    One machine-readable line per row, so the cleaned CSV can be re-run through
    the generator with no AI at all -- the ordinary pattern matching reads this
    exactly. It also makes the file reviewable by eye.
    """
    parts = []
    if unit:
        parts.append(f"Flat No {unit}")
    if floor is not None:
        parts.append(f"floor {floor}")
    if wing:
        parts.append(f"{wing} Wing")
    if tower:
        parts.append(str(tower))
    head = ", ".join(parts)

    areas = []
    if carpet:
        label = carpet.get("label") or "Carpet"
        areas.append(f"{label} area {carpet['value']:g} {carpet['unit']}")
    if balcony:
        areas.append(f"balcony area {balcony['value']:g} {balcony['unit']}")
    if total:
        areas.append(f"total area {total['value']:g} {total['unit']}")
    if not areas:
        return head or None
    return f"{head} — " + "; ".join(areas) if head else "; ".join(areas)

# ---------------------------------------------------------------------------
# Unit number and tower, by search priority
# ---------------------------------------------------------------------------
# The unit number is the APARTMENT IDENTIFIER, and finding it matters more than
# matching any particular phrasing. "Flat No. FLAT NO 451" must yield 451, not
# the word FLAT. Numbers that are NOT unit numbers -- survey, property, PID,
# parking, assessment, floor, road, document -- must never be returned.

UNIT_TOWER_PROMPT = (
    "You are an expert Indian real-estate document parser. From the property "
    "description below, identify the UNIT NUMBER and the TOWER.\n\n"
    "UNIT NUMBER is the apartment identifier only -- 451, 733, 1802, 12B. "
    "Never output the words FLAT, Apartment or Unit. 'Flat No. FLAT NO 451' is "
    "451, not FLAT.\n\n"
    "SEARCH IN THIS ORDER:\n"
    "1. Flat No. / Flat Number / Apartment Bearing No. / Apartment No. / "
    "Unit No. / Unit Number / Residential Apartment Bearing No.\n"
    "2. Municipal address ('bearing municipal address FLAT 451, ...')\n"
    "3. Assessment number -- in '1641/451/TOWER-4' the unit is 451\n"
    "4. PID patterns -- in '1783/733/TOWER-7' the unit is 733\n"
    "5. If several numbers appear, take the one immediately following Flat, "
    "Apartment, Unit or Bearing No.\n\n"
    "NEVER return a survey number, property number, PID, parking number, "
    "assessment number as a whole, floor number, road number or document "
    "number.\n\n"
    "TOWER: give the number and, where a flower/tree/building name is present, "
    "the name too. 'TOWER-4(DAFFODIL)' is number 4, name DAFFODIL. "
    "'Tower 9 IRIS' is 9, IRIS. 'Elm Tower 5' is 5, ELM. "
    "'Tower-2(BLUE BELL)' is 2, BLUE BELL.\n\n"
    "Return ONLY JSON:\n"
    '{"unit_number": "", "tower_number": "", "tower_name": "", '
    '"confidence": "high|medium|low", "reason": ""}'
)

# Priority 1 labels, most explicit first.
_UNIT_PATTERNS = [
    (1, re.compile(r'(?:Residential\s+)?Apartment\s+[Bb]earing\s+No\.?[^0-9]{0,20}(\d{2,4}[A-Za-z]?)\b')),
    (1, re.compile(r'Flat\s*(?:No\.?|Number)[^0-9]{0,25}(\d{2,4}[A-Za-z]?)\b', re.I)),
    (1, re.compile(r'Apartment\s*(?:No\.?|Number)[^0-9]{0,25}(\d{2,4}[A-Za-z]?)\b', re.I)),
    (1, re.compile(r'Unit\s*(?:No\.?|Number)[^0-9]{0,25}(\d{2,4}[A-Za-z]?)\b', re.I)),
    (2, re.compile(r'municipal address[^0-9]{0,40}(\d{2,4}[A-Za-z]?)\b', re.I)),
    (3, re.compile(r'\d{3,5}\s*/\s*(\d{2,4}[A-Za-z]?)\s*/\s*TOWER', re.I)),
    (5, re.compile(r'(?:Flat|Apartment|Unit)\s+(\d{2,4}[A-Za-z]?)\b', re.I)),
]

# Numbers that are never a unit number, matched so they can be excluded.
_NOT_UNIT = re.compile(
    r'(?:survey|property|PID|parking|assessment|khata|katha|ward|floor|road|'
    r'document|registration|pin\s*code|PIN)\s*(?:no\.?|number)?[^0-9]{0,6}\d+',
    re.IGNORECASE,
)

_TOWER_PATTERNS = [
    # TOWER-4(DAFFODIL) / Tower-2(BLUE BELL)
    re.compile(r'TOWER[\s\-]*(\d{1,2})\s*\(\s*([A-Za-z][A-Za-z\s]{1,22}?)\s*\)', re.I),
    # TOWER-3/Carnation  or  TOWER-3 - Carnation
    re.compile(r'TOWER[\s\-]*(\d{1,2})\s*[/\-–]\s*([A-Za-z][A-Za-z]{1,20})', re.I),
    # Tower 9 IRIS -- the name is in capitals, so stop at the first lowercase word
    # The word "Tower" may be any case, but the NAME is in capitals -- that is
    # what separates "Tower 9 IRIS" from "Tower 9 of Embassy". A blanket
    # re.IGNORECASE would let "of" through as a name.
    re.compile(r'(?i:TOWER)[\s\-]*(\d{1,2})\s+([A-Z]{2,}(?:\s+[A-Z]{2,})?)\b'),
    # Elm Tower 5 / Gardenia Tower 7
    re.compile(r'\b([A-Za-z]{3,20})\s+TOWER[\s\-]*(\d{1,2})\b', re.I),
    # bare TOWER-6
    re.compile(r'TOWER[\s\-]*(\d{1,2})\b', re.I),
]

_TOWER_NAME_NOISE = re.compile(
    r'(?i)\b(of|the|in|at|and|no|bearing|block|flat|apartment|situated|known|as|'
    r'development|project|property|schedule|residential|premises)\b')


def extract_unit_tower(description):
    """
    {"unit_number", "tower_number", "tower_name", "confidence", "reason"}

    A deterministic implementation of the same priority order the AI prompt
    uses, so it works with no key and doubles as a cross-check on the model.
    """
    text = str(description or "")
    if not text.strip():
        return {"unit_number": None, "tower_number": None, "tower_name": None,
                "confidence": "low", "reason": "empty description"}

    # Spans that belong to survey/PID/parking/assessment numbers, so a digit
    # inside one of them can't be mistaken for the flat.
    banned = [(m.start(), m.end()) for m in _NOT_UNIT.finditer(text)]

    unit, priority, why = None, None, None
    for level, pattern in _UNIT_PATTERNS:
        for m in pattern.finditer(text):
            start = m.start(1)
            # Priority 3 IS an assessment number, and its middle field is the
            # flat -- so it is exempt from the ban.
            if level != 3 and any(s <= start < e for s, e in banned):
                continue
            unit, priority = m.group(1).upper(), level
            why = f"priority {level}: {re.sub(r'[ ]+', ' ', m.group(0))[:60]!r}"
            break
        if unit:
            break

    # Keep looking for a NAME even after a number is found: the bare "TOWER-6"
    # pattern matches almost everything, so stopping at the first hit would
    # throw away the name a later pattern would have supplied.
    tower_number = tower_name = None
    for pattern in _TOWER_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groups()
        number, name = (groups[0], None) if len(groups) == 1 else (
            (groups[0], groups[1]) if str(groups[0]).strip().isdigit()
            else (groups[1], groups[0])
        )
        if name:
            name = _TOWER_NAME_NOISE.sub(" ", name)
            name = re.sub(r'\s+', " ", name).strip().upper() or None
            if name and len(name) < 2:
                name = None
        if tower_number is None:
            tower_number = number
        if name and tower_name is None:
            tower_name = name
        if tower_number and tower_name:
            break

    if unit and tower_number:
        confidence = "high" if priority == 1 else "medium"
    elif unit:
        confidence = "medium" if priority == 1 else "low"
    else:
        confidence = "low"

    return {"unit_number": unit, "tower_number": tower_number,
            "tower_name": tower_name, "confidence": confidence,
            "reason": why or "no unit-number pattern matched"}
