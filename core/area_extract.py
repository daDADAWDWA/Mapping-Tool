"""
Extracts the carpet-area VALUE, its UNIT (sq.ft / sq.m), and the AREA-TYPE
DESCRIPTOR (e.g. "RERA Carpet Area", "MOFA Carpet Area", "Built-up Area") from
the free-text "Property Description" field of a CRE transaction row.

The `Area` / `Area Type` columns in the transaction CSV are deliberately
IGNORED -- per instruction, only the descriptive text is trusted, since that's
the field that actually carries the RERA/agreement-stated carpet area.

Strategy:
  1. Regex pass over common Marathi + English phrasings for the value+unit.
  2. Regex/dictionary pass over the text immediately following the value+unit
     to classify the area-type descriptor into a compact label (e.g.
     "reracarpet", "mofacarpet", "builtup").
  3. If either step can't confidently resolve, and an Anthropic API key is
     configured, fall back to asking Claude to extract it as structured JSON.
  4. Otherwise, return None for whatever couldn't be determined -- caller
     flags the row for manual review rather than guessing.
"""

import re
import os
import json

from .config import DEFAULT_MODEL

# --- Regex patterns -----------------------------------------------------
# Marathi carpet-area phrasing, e.g.:
#   "क्षेत्रफळ 662 चौ. फुट रेरा कार्पेट एरिया"
#   "एकुण क्षेत्रफळ 756 चौ. फुट रेरा कार्पेट एरिया"
# A number, allowing thousands separators. "2,133.30 sq.ft" previously matched
# only "133.30" -- a silent 10x error, which is far worse than not matching.
# Two digits minimum in the integer part, so a stray "0 square meters" in
# boilerplate isn't picked up as an area.
_NUM = r'(\d{1,3}(?:,\d{3})+(?:\s?\.\s?\d+)?|\d{2,6}(?:\s?\.\s?\d+)?)'

# Marathi: "क्षेत्रफळ 662 चौ. फुट", "756 चौ फुट", "115.20 चौ. मीटर"
_MARATHI_SQFT = re.compile(_NUM + r'\s*चौ\.?\s*फु[टट]', re.UNICODE)
_MARATHI_SQM = re.compile(_NUM + r'\s*चौ\.?\s*मी[टट]र', re.UNICODE)

# English, abbreviated AND spelled out. Maharashtra deeds write "sq. ft.";
# Karnataka deeds write "Square Feet" and "square meters" in full, which the
# abbreviated-only pattern missed entirely.
_ENGLISH_SQFT = re.compile(
    _NUM + r'\s*(?:sq(?:uare)?\.?\s*(?:f(?:ee|oo)?t|ft)\.?|sft\b|sq\.?\s*ft\b)',
    re.IGNORECASE,
)
_ENGLISH_SQM = re.compile(
    _NUM + r'\s*sq(?:uare)?\.?\s*(?:m(?:et(?:er|re)s?)?|mtr?s?)\.?\b',
    re.IGNORECASE,
)

_PATTERNS = [
    (_MARATHI_SQFT, "sq.ft"),
    (_ENGLISH_SQFT, "sq.ft"),
    (_MARATHI_SQM, "sq.m"),
    (_ENGLISH_SQM, "sq.m"),
]

# --- Area-type descriptor dictionary ------------------------------------
# Checked longest/most-specific phrase first, so "super built up" matches
# before the more generic "built up" does. Add new phrasings here as new
# projects surface them -- no other code needs to change.
_AREA_TYPE_DICTIONARY = [
    ("सुपर बिल्ट", "superbuiltup"),
    ("super built", "superbuiltup"),
    ("superbuilt", "superbuiltup"),
    ("मोफा कार्पेट", "mofacarpet"),
    ("mofa carpet", "mofacarpet"),
    ("रेरा कार्पेट", "reracarpet"),
    ("rera carpet", "reracarpet"),
    ("बिल्ट अप", "builtup"),
    ("built up", "builtup"),
    ("builtup", "builtup"),
    ("विक्रीयोग्य", "saleable"),
    ("saleable", "saleable"),
    ("कार्पेट", "carpet"),
    ("carpet", "carpet"),
]


# ---------------------------------------------------------------------------
# Which number is which
# ---------------------------------------------------------------------------
# A description often states THREE areas: the carpet, a separate balcony, and
# an explicit total. e.g.
#
#   "सदनिकेचे क्षेत्र 1562 चौ फुट रेरा कार्पेट व बाल्कनी क्षेत्र 128 चौ फुट
#    अशाप्रकारे सदनिकेचे एकूण क्षेत्र 1690 चौ फुट कार्पेट"
#
#   carpet 1562  +  balcony 128  =  total 1690, all three written out.
#
# Picking "the number nearest the word कार्पेट" grabbed 1690 -- the TOTAL,
# which also has कार्पेट after it -- and then added the balcony on top, giving
# 1818. So each number is classified from the words around it instead.
#
# A label BEFORE the number is the reliable signal ("एकूण क्षेत्र 1690",
# "बाल्कनी क्षेत्र 128"); only if there is none do we look at what FOLLOWS.

_TOTAL_WORDS = ["एकूण", "एकुण", "अशाप्रकारे", "अशा प्रकारे", "एकत्रित",
                "total", "aggregate", "in all"]
_BALCONY_WORDS = ["बाल्कनी", "बालकनी", "गॅलरी", "गॅलरि", "टेरेस", "डेक",
                  "balcony", "terrace", "deck", "verandah"]
_CARPET_WORDS = ["रेरा कार्पेट", "कार्पेट", "चटई", "carpet", "carpet area measuring",
                 "rera carpet", "mofa carpet"]
# These measure something LARGER than carpet. They are captured so they can be
# reported, but never used as the carpet area -- "super built up area of 2666
# Square Feet" followed by "...include the total built up area..." would
# otherwise be read as a carpet figure, or as a stated total.
# Everything here measures something OTHER than the flat's carpet. Captured so
# it can be reported, never used as a carpet area. The abbreviations matter as
# much as the words: Bengaluru deeds write "SBA of 2666 sq ft" and "1082.29
# Square feet UDS", and without those two tokens a land share reads as a
# carpet area -- a confidently wrong number, which is worse than a blank.
# Areas that ARE the flat, but measured on a larger basis. Usable -- they just
# have to be LABELLED, because a super built-up figure is not interchangeable
# with a carpet one.
_BUILTUP_WORDS = [
    ("Super Built-up", ["super built", "superbuilt", "super builtup", "sba", "sbua",
                        "सुपर बिल्ट"]),
    ("Built-up", ["built up", "built-up", "builtup", "bua", "बिल्ट अप"]),
    ("Saleable", ["saleable", "विक्रीयोग्य"]),
]

# NEVER the flat's area, however it is worded. An undivided land share sits in
# the same sentence as the built-up area and reads exactly like an area, which
# is why "1082.29 Square feet UDS" was being written into the stack as carpet.
_EXCLUDE_WORDS = [
    "uds", "udi", "undivided share", "undivided", "proportionate",
    "land share", "owner share", "owner’s land share", "common area",
    "special private", "additional area", "parking", "garden",
    "terrace garden", "plot area", "cts", "survey", "road", "open space",
]

_OTHER_WORDS = [w for _, words in _BUILTUP_WORDS for w in words] + _EXCLUDE_WORDS

_CARPET_WORDS = ["रेरा कार्पेट", "कार्पेट", "चटई", "carpet", "carpet area measuring",
                 "rera carpet", "mofa carpet"]
# These measure something LARGER than carpet. They are captured so they can be
# reported, but never used as the carpet area -- "super built up area of 2666
# Square Feet" followed by "...include the total built up area..." would
# otherwise be read as a carpet figure, or as a stated total.
# Everything here measures something OTHER than the flat's carpet. Captured so
# it can be reported, never used as a carpet area. The abbreviations matter as
# much as the words: Bengaluru deeds write "SBA of 2666 sq ft" and "1082.29
# Square feet UDS", and without those two tokens a land share reads as a
# carpet area -- a confidently wrong number, which is worse than a blank.
_OTHER_WORDS = [
    # super built-up / built-up / saleable
    "super built", "superbuilt", "super builtup", "sba", "sbua",
    "built up", "built-up", "builtup", "bua", "saleable",
    "सुपर बिल्ट", "बिल्ट अप", "विक्रीयोग्य",
    # undivided land share -- the commonest false positive in this family
    "uds", "udi", "undivided share", "undivided", "proportionate",
    "land share", "owner share", "owner’s land share",
    # other non-flat areas
    "common area", "special private", "additional area", "parking",
    "garden", "terrace garden", "plot area", "cts", "survey",
]

_LOOK_BEFORE = 32
_LOOK_AFTER = 45


_WORD_CACHE = {}


def _word_pattern(word):
    """
    A whole-word matcher for ASCII keywords, plain substring for Devanagari.

    Substring matching on short abbreviations is a trap: "udi" matched inside
    "incl-udi-ng", which appears in virtually every legal description, so every
    area in those rows was being filed as an excluded land share. Abbreviations
    like UDS, SBA, BUA and CTS only mean anything as standalone words.
    """
    if word in _WORD_CACHE:
        return _WORD_CACHE[word]
    if word.isascii():
        pattern = re.compile(r'(?<![A-Za-z])' + re.escape(word) + r'(?![A-Za-z])',
                             re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(word))
    _WORD_CACHE[word] = pattern
    return pattern


def _nearest(text, words, from_end=True):
    """Distance from the end (or start) of `text` to the closest of `words`."""
    best = None
    for w in words:
        spans = [m.start() for m in _word_pattern(w).finditer(text)]
        if not spans:
            continue
        idx = max(spans) if from_end else min(spans)
        distance = (len(text) - idx) if from_end else idx
        if best is None or distance < best:
            best = distance
    return best


def classify_area_matches(description):
    """
    Sort every value+unit in the text into carpet / balcony / total.

    Returns {"carpet": [...], "balcony": [...], "total": [...]} where each item
    is (value, unit, start, end), in the order found.
    """
    buckets = {"carpet": [], "balcony": [], "total": [], "builtup": [],
               "excluded": [], "other": []}
    matches = []
    for pattern, unit in _PATTERNS:
        for m in pattern.finditer(description):
            try:
                matches.append((float(re.sub(r"[,\s]", "", m.group(1))), unit,
                                m.start(), m.end()))
            except (ValueError, IndexError):
                continue
    matches.sort(key=lambda x: x[2])

    seen_spans = set()
    previous_end = None
    for value, unit, start, end in matches:
        if any(start < e and end > s for s, e in seen_spans):
            continue
        # "2666 Sq.Ft. (247.68 Sq.Mtr)" restates ONE area in the other unit.
        # Counting the bracketed figure separately invents a second area.
        if previous_end is not None and 0 <= start - previous_end <= 4 \
                and "(" in description[previous_end:start]:
            previous_end = end
            continue
        seen_spans.add((start, end))
        # Clip the look-back so it cannot cross into the PREVIOUS area's phrase.
        # "SBA of 2666 sq ft With 1082.29 sq ft of UDS" -- without clipping, the
        # UDS figure sees the earlier "SBA" and is labelled super built-up. A
        # label belongs to the number that follows it, not the one after that.
        window_start = max(0, start - _LOOK_BEFORE)
        if previous_end is not None:
            window_start = max(window_start, previous_end)
        before = description[window_start:start]
        # And the look-ahead stops at the next number, for the same reason.
        next_start = next((s for _, _, s, _ in matches if s > end), None)
        window_end = end + _LOOK_AFTER
        if next_start is not None:
            window_end = min(window_end, next_start)
        after = description[end:window_end]
        previous_end = end

        # A label before the number wins, checked most-specific first.
        # Order matters: the exclusions are checked first, then the larger
        # bases, then total/balcony/carpet. A label BEFORE the number wins;
        # only when there is none does the wording after it decide.
        groups = [("excluded", _EXCLUDE_WORDS)]
        groups += [(f"builtup:{label}", words) for label, words in _BUILTUP_WORDS]
        groups += [("total", _TOTAL_WORDS), ("balcony", _BALCONY_WORDS),
                   ("carpet", _CARPET_WORDS)]

        category = None
        for name, words in groups:
            if _nearest(before, words) is not None:
                category = name
                break
        if category is None:
            distances = {name: _nearest(after, words, from_end=False)
                         for name, words in groups}
            available = {k: v for k, v in distances.items() if v is not None}
            category = min(available, key=available.get) if available else "carpet"

        if category.startswith("builtup:"):
            label = category.split(":", 1)[1]
            buckets["builtup"].append((value, unit, start, end, label))
            buckets["other"].append((value, unit, start, end))
        elif category == "excluded":
            buckets["excluded"].append((value, unit, start, end))
        else:
            buckets[category].append((value, unit, start, end))
    return buckets


def extract_balcony_regex(description: str, exclude_span=None):
    """(value, unit) of a separately-stated balcony area, or (None, None)."""
    if not description or not isinstance(description, str):
        return None, None
    balconies = classify_area_matches(description)["balcony"]
    if exclude_span:
        balconies = [b for b in balconies
                     if b[3] <= exclude_span[0] or b[2] >= exclude_span[1]]
    if not balconies:
        return None, None
    return balconies[0][0], balconies[0][1]


def _classify_area_type(window: str):
    """Returns a compact label like 'reracarpet', or None if nothing in
    the dictionary matches this window of text."""
    if not window:
        return None
    lower = window.lower()
    for phrase, label in _AREA_TYPE_DICTIONARY:
        if phrase in window or phrase.lower() in lower:
            return label
    return None


def extract_area_regex(description: str):
    """
    Returns (carpet, unit, area_type, balcony, balcony_unit, stated_total,
    total_unit) -- each None if absent.

    Nothing is summed here. Whether the flat's area is the stated total, or
    carpet + balcony, is decided by the caller, so all three figures stay
    visible and checkable.
    """
    if not description or not isinstance(description, str):
        return None, None, None, None, None, None, None

    buckets = classify_area_matches(description)
    carpets, balconies, totals = buckets["carpet"], buckets["balcony"], buckets["total"]

    carpet = unit = None
    carpet_span = None
    if carpets:
        carpet, unit, cs, ce = carpets[0]
        carpet_span = (cs, ce)
    elif totals:
        # Only a total was labelled (e.g. "Total area ... 756 sq ft RERA
        # carpet area") -- then that IS the flat's carpet figure.
        carpet, unit, cs, ce = totals[0]
        carpet_span = (cs, ce)
        totals = totals[1:]

    balcony = balcony_unit = None
    if balconies:
        balcony, balcony_unit = balconies[0][0], balconies[0][1]

    stated_total = total_unit = None
    if totals:
        stated_total, total_unit = totals[0][0], totals[0][1]

    area_type = None
    if carpet_span:
        area_type = _classify_area_type(description[carpet_span[1]:carpet_span[1] + 40])
    if area_type is None and stated_total is None and carpets:
        area_type = _classify_area_type(description[carpets[0][3]:carpets[0][3] + 40])

    return carpet, unit, area_type, balcony, balcony_unit, stated_total, total_unit


def extract_area_ai(description: str, api_key: str = None, model: str = None):
    """
    Fallback extraction using the Claude API. Only called when regex fails
    to resolve the value/unit and an API key is available. Returns
    (value, unit, area_type) or (None, None, None) on any failure -- this
    must NEVER raise, since a flaky/missing key should just degrade to
    "flag for manual review", not crash the whole batch.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not description:
        return None, None, None, None, None, None, None

    try:
        import anthropic
    except ImportError:
        return None, None, None, None, None, None, None

    try:
        client = anthropic.Anthropic(api_key=key)
        prompt = (
            "Extract the RERA/agreement carpet area from this Indian property "
            "registration description (Marathi and/or English). Respond with "
            'ONLY a JSON object like {"value": 662, "unit": "sq.ft", '
            '"area_type": "reracarpet", "balcony": 84, "balcony_unit": "sq.ft", '
            '"stated_total": 1690, "total_unit": "sq.ft"} '
            'where unit is "sq.ft" or "sq.m", and '
            "area_type is a compact lowercase no-space label for whatever area "
            "descriptor is stated (e.g. RERA carpet area -> \"reracarpet\", "
            "MOFA carpet area -> \"mofacarpet\", built-up area -> \"builtup\"). "
            "Set balcony and balcony_unit ONLY if the text states a SEPARATE "
            "balcony, gallery, terrace or deck area in addition to the carpet "
            "area (RERA carpet excludes balcony) -- often further along in the "
            "text than the carpet figure. Use null for both if it does not. "
            "Never put the carpet figure in balcony. "
            "Set stated_total ONLY if the text itself writes out a combined "
            "figure (e.g. 'अशाप्रकारे सदनिकेचे एकूण क्षेत्र 1690 चौ फुट' -- "
            "'thus the total area of the flat is 1690'). Do NOT calculate it "
            "yourself, and do NOT put the total in value: value must be the "
            "carpet figure alone (1562 in that example), balcony the balcony "
            "figure alone (128), stated_total the written total (1690). "
            'If no carpet area can be found, respond with {"value": null, '
            '"unit": null, "area_type": null, "balcony": null, '
            '"balcony_unit": null}. No other text.\n\n'
            f"Description:\n{description}"
        )
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = json.loads(text)
        value = data.get("value")
        unit = data.get("unit")
        area_type = data.get("area_type")
        if value is None or unit not in ("sq.ft", "sq.m"):
            return None, None, None, None, None, None, None
        balcony = data.get("balcony")
        try:
            balcony = float(balcony) if balcony is not None else None
        except (TypeError, ValueError):
            balcony = None
        balcony_unit = data.get("balcony_unit")
        if balcony_unit not in ("sq.ft", "sq.m"):
            balcony_unit = unit
        stated_total = data.get("stated_total")
        try:
            stated_total = float(stated_total) if stated_total is not None else None
        except (TypeError, ValueError):
            stated_total = None
        total_unit = data.get("total_unit") if data.get("total_unit") in ("sq.ft", "sq.m") else unit
        return float(value), unit, area_type, balcony, balcony_unit, stated_total, total_unit
    except Exception:
        return None, None, None, None, None, None, None


AREA_PREFERENCE = ["RERA Carpet", "MOFA Carpet", "Carpet",
                   "Built-up", "Super Built-up", "Saleable"]


def all_areas(description):
    """
    EVERY area stated for the flat, labelled:
        [{"label", "value", "unit", "usable"}]

    `usable` is False for figures that are never the flat's area -- undivided
    land share, parking, common or garden area. They are returned rather than
    dropped so a reviewer can see what was rejected and why, instead of
    wondering where a number went.
    """
    if not description:
        return []
    buckets = classify_area_matches(description)
    found = []

    for value, unit, _, end in buckets["carpet"]:
        compact = _classify_area_type(description[end:end + 40]) or ""
        label = {"reracarpet": "RERA Carpet", "mofacarpet": "MOFA Carpet"}.get(compact, "Carpet")
        found.append({"label": label, "value": value, "unit": unit, "usable": True})
    for entry in buckets["builtup"]:
        found.append({"label": entry[4], "value": entry[0], "unit": entry[1], "usable": True})
    for value, unit, _, end in buckets["balcony"]:
        window = description[max(0, end - 60):end + 30].lower()
        label = ("Dry Balcony" if "dry balcony" in window else
                 "Terrace" if "terrace" in window else
                 "Deck" if "deck" in window else "Balcony")
        found.append({"label": label, "value": value, "unit": unit, "usable": True})
    for value, unit, _, _ in buckets["total"]:
        found.append({"label": "Total", "value": value, "unit": unit, "usable": True})
    for value, unit, _, end in buckets["excluded"]:
        window = description[max(0, end - 70):end + 40].lower()
        label = ("UDS / undivided share" if ("uds" in window or "undivided" in window
                                             or "proportionate" in window) else
                 "Parking" if "parking" in window else
                 "Land / owner share" if "land share" in window or "owner share" in window else
                 "Garden" if "garden" in window else
                 "Common / other")
        found.append({"label": label, "value": value, "unit": unit, "usable": False})

    seen, unique = set(), []
    for area in found:
        key = (area["label"], area["value"], area["unit"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(area)
    return unique


def primary_area(description):
    """
    (value, unit, label) -- the best area stated for the flat, whatever its
    basis, always labelled.

    Carpet is preferred where it exists, then built-up, super built-up and
    saleable. Many deeds state no carpet area at all: Bengaluru documents are
    written on super built-up, and refusing anything but carpet leaves 46 of 47
    rows empty. Excluded figures (land share, parking, common area) are never
    eligible, whatever is missing.
    """
    if not description:
        return None, None, None
    buckets = classify_area_matches(description)

    if buckets["carpet"]:
        value, unit, _, end = buckets["carpet"][0]
        label = _classify_area_type(description[end:end + 40]) or ""
        display = {"reracarpet": "RERA Carpet", "mofacarpet": "MOFA Carpet"}.get(label, "Carpet")
        return value, unit, display

    for wanted in ("Built-up", "Super Built-up", "Saleable"):
        for entry in buckets["builtup"]:
            if entry[4] == wanted:
                return entry[0], entry[1], wanted
    if buckets["builtup"]:
        entry = buckets["builtup"][0]
        return entry[0], entry[1], entry[4]
    if buckets["total"]:
        value, unit, _, _ = buckets["total"][0]
        return value, unit, "Total"
    return None, None, None


def extract_area(description: str, api_key: str = None, use_ai_fallback: bool = True,
                 model: str = None):
    """
    Main entry point. Returns a dict:
        {"value", "unit", "area_type", "source",
         "balcony", "balcony_unit", "stated_total", "total_unit"}

    `value` is the CARPET area alone, as stated. `balcony` is a separately
    stated balcony. `stated_total` is a combined figure the text itself writes
    out. Nothing is added together here -- the caller decides, so all three
    stated numbers stay inspectable and a written total can never be
    double-counted.
    """
    carpet, unit, area_type, balcony, balcony_unit, total, total_unit = \
        extract_area_regex(description)
    # Figures that are NOT the carpet area (super built-up, saleable, land
    # share). Reported so a row with no carpet area can say what it does have,
    # instead of looking like a blank failure.
    non_carpet = [
        {"value": v, "unit": u}
        for v, u, _, _ in classify_area_matches(description)["other"]
    ] if description else []

    def _needs_ai():
        if not use_ai_fallback:
            return False
        if carpet is None:
            return True
        if area_type is None:
            return True
        # The text gave a total that doesn't equal carpet + balcony, so one of
        # the three was probably mis-attributed. Worth a second opinion.
        if total is not None and balcony is not None:
            expected = carpet + balcony
            if abs(expected - total) > max(1.0, 0.01 * total):
                return True
        return False

    if carpet is not None and not _needs_ai():
        return {"value": carpet, "unit": unit, "area_type": area_type,
                "source": "regex", "balcony": balcony,
                "balcony_unit": balcony_unit or unit,
                "stated_total": total, "total_unit": total_unit or unit,
                "non_carpet_areas": non_carpet,
                "basis": basis_display(area_type) or "Carpet"}

    if use_ai_fallback:
        ai = extract_area_ai(description, api_key=api_key, model=model)
        ai_carpet, ai_unit, ai_type, ai_balcony, ai_balcony_unit, ai_total, ai_total_unit = ai
        if ai_carpet is not None:
            # Keep the regex's carpet figure when it found one -- it reads the
            # literal text. AI is here to resolve what the numbers MEAN.
            if carpet is not None and abs(ai_carpet - carpet) < 0.01:
                pass
            else:
                carpet, unit = ai_carpet, ai_unit
            return {"value": carpet, "unit": unit or ai_unit,
                    "area_type": area_type or ai_type, "source": "ai",
                    "balcony": balcony if balcony is not None else ai_balcony,
                    "balcony_unit": (balcony_unit or ai_balcony_unit or unit),
                    "stated_total": total if total is not None else ai_total,
                    "total_unit": (total_unit or ai_total_unit or unit),
                    "non_carpet_areas": non_carpet,
                    "basis": basis_display(area_type or ai_type) or "Carpet"}

    if carpet is not None:
        return {"value": carpet, "unit": unit, "area_type": area_type,
                "source": "regex", "balcony": balcony,
                "balcony_unit": balcony_unit or unit,
                "stated_total": total, "total_unit": total_unit or unit,
                "non_carpet_areas": non_carpet, "basis": basis_display(area_type) or "Carpet"}

    # No carpet figure -- but a built-up, super built-up or saleable area is
    # still the flat's area. Use the most precise one stated and RECORD which
    # basis it is, so nothing downstream can mistake it for carpet.
    best, all_areas = best_available_area(description)
    if best is not None:
        return {"value": best["value"], "unit": best["unit"],
                "area_type": best["basis"], "source": "regex",
                "balcony": balcony, "balcony_unit": balcony_unit or best["unit"],
                "stated_total": total, "total_unit": total_unit or best["unit"],
                "non_carpet_areas": [a for a in all_areas if a is not best],
                "basis": best["basis"]}

    return {"value": None, "unit": None, "area_type": None, "source": None,
            "balcony": None, "balcony_unit": None,
            "stated_total": None, "total_unit": None,
            "non_carpet_areas": non_carpet, "basis": None}

# ---------------------------------------------------------------------------
# Any stated area is usable -- as long as its BASIS is recorded
# ---------------------------------------------------------------------------
# A stack view does not have to be RERA carpet. Built-up, super built-up and
# saleable are all valid, provided the sheet says which one it is: they measure
# different things, and 2666 sq.ft of super built-up is the same flat as
# 1866 sq.ft of carpet. What must never happen is a basis being recorded as
# though it were another.
#
# Preference runs most-precise first, so where a description gives several the
# carpet figure still wins.
BASIS_PREFERENCE = [
    "RERA Carpet", "MOFA Carpet", "Carpet",
    "Built-up", "Super Built-up", "Saleable",
]

# Phrase -> basis. Longest/most specific first, since "super built up" contains
# "built up" and "rera carpet" contains "carpet".
_BASIS_PHRASES = [
    ("rera carpet", "RERA Carpet"), ("रेरा कार्पेट", "RERA Carpet"),
    ("mofa carpet", "MOFA Carpet"), ("mofa", "MOFA Carpet"), ("मोफा", "MOFA Carpet"),
    ("super built up", "Super Built-up"), ("super built-up", "Super Built-up"),
    ("super builtup", "Super Built-up"), ("sba", "Super Built-up"),
    ("sbua", "Super Built-up"), ("सुपर बिल्ट", "Super Built-up"),
    ("saleable", "Saleable"), ("विक्रीयोग्य", "Saleable"),
    ("built up", "Built-up"), ("built-up", "Built-up"), ("builtup", "Built-up"),
    ("bua", "Built-up"), ("बिल्ट अप", "Built-up"),
    ("carpet", "Carpet"), ("कार्पेट", "Carpet"), ("चटई", "Carpet"),
]

# Never the flat's own area, whatever the number looks like.
_IGNORED_BASES = [
    "uds", "udi", "undivided", "proportionate", "land share", "owner share",
    "common area", "special private", "additional area", "parking",
    "terrace garden", "garden", "plot area", "cts", "survey", "road",
]


def _window_candidates(window, reverse):
    """
    Labels found in one window, nearest to the number first.

    Two subtleties, both learned from real text:
      - "super built up" CONTAINS "built up", and the shorter phrase sits
        closer to the number, so distance alone picks the wrong basis. A phrase
        whose span is inside a longer one is dropped.
      - distance has to be measured to the NUMBER, so a label 20 characters
        before it loses to one 4 characters after it. "SBA of 2666 sq ft With
        1082.29 sq ft of UDS" is the case: the 1082.29 is a land share, even
        though SBA appears earlier in the sentence.
    """
    low = window.lower()
    found = []
    for phrase, basis in ([(p, None) for p in _IGNORED_BASES] + _BASIS_PHRASES):
        idx = low.rfind(phrase) if reverse else low.find(phrase)
        if idx == -1:
            continue
        found.append({"start": idx, "end": idx + len(phrase),
                      "basis": basis, "phrase": phrase})
    keep = [
        c for c in found
        if not any(o is not c and o["start"] <= c["start"] and o["end"] >= c["end"]
                   and len(o["phrase"]) > len(c["phrase"]) for o in found)
    ]
    for c in keep:
        c["distance"] = (len(window) - c["end"]) if reverse else c["start"]
    keep.sort(key=lambda c: c["distance"])
    return keep


# A label after a SEPARATOR describes the next area, not this one. In
# "super built-up area of 334.08 square meters, carpet area of - square meters"
# the word "carpet" is two characters past the comma, so without this the
# super built-up figure gets labelled Carpet -- the exact error this whole
# basis system exists to prevent.
_AFTER_STOP = re.compile(r'[,;(]|\band\b|\bwith\b|\btogether\b', re.IGNORECASE)


def _basis_near(before, after):
    """
    The area basis named nearest the number, across both windows, or None when
    the nearest label says this number is not the flat's area at all.
    """
    stop = _AFTER_STOP.search(after)
    if stop:
        after = after[:stop.start()]
    candidates = _window_candidates(before, True) + _window_candidates(after, False)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["distance"])
    return candidates[0]["basis"]


_COMPACT_TO_BASIS = {
    "reracarpet": "RERA Carpet", "mofacarpet": "MOFA Carpet",
    "superbuiltup": "Super Built-up", "builtup": "Built-up",
    "saleable": "Saleable", "carpet": "Carpet",
}


def basis_display(label):
    """'reracarpet' -> 'RERA Carpet'. One spelling everywhere downstream."""
    if not label:
        return None
    return _COMPACT_TO_BASIS.get(str(label), str(label))


def extract_all_areas(description):
    """
    Every area in the text as {value, unit, basis}, ignoring land share,
    parking and the rest. `basis` is the measurement it is stated on.
    """
    if not description or not isinstance(description, str):
        return []
    out, seen = [], set()
    buckets = classify_area_matches(description)
    for bucket in ("carpet", "total", "other"):
        for value, unit, start, end in buckets.get(bucket, []):
            before = description[max(0, start - _LOOK_BEFORE):start]
            after = description[end:end + _LOOK_AFTER]
            basis = _basis_near(before, after)
            if basis is None:
                continue
            key = (value, unit, basis)
            if key in seen:
                continue
            seen.add(key)
            out.append({"value": value, "unit": unit, "basis": basis})
    out.sort(key=lambda a: BASIS_PREFERENCE.index(a["basis"])
             if a["basis"] in BASIS_PREFERENCE else len(BASIS_PREFERENCE))
    return out


def best_available_area(description):
    """
    ({value, unit, basis} | None, [all areas]) -- the most precise basis stated.
    A super built-up figure is a perfectly good answer; it just has to be
    labelled as one.
    """
    areas = extract_all_areas(description)
    return (areas[0] if areas else None), areas
