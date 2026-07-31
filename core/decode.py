"""
Decodes a raw flat/unit identifier (from either the CRE transaction file or the
MahaRERA inventory file) into (floor, position).

Convention (verified against real project data):
    unit_number = floor * 100 + position
    e.g. 1201 -> floor 12, position 1
         602  -> floor 6,  position 2
         303  -> floor 3,  position 3

`position` is a 1-based index into the template's Series column list, counted
LEFT TO RIGHT exactly as the columns appear in the template header row. It is
NOT matched against the "Series N" text label -- towers commonly have their
Series columns out of numeric order (e.g. "Series 1, Series 6, Series 2, ..."),
and position always refers to physical column order, not the label text.

Any leading tower/wing letters (e.g. "A-303", "A/303", "A303") are stripped
before decoding, since a flat number does not need to belong to the same
tower letter as the current project/tower being processed.
"""

import re

_TOWER_PREFIX_RE = re.compile(r'^[A-Za-z]+[\s/\-]*')
_NON_DIGIT_RE = re.compile(r'\D')


def decode_unit(raw):
    """
    Decode a raw flat/unit identifier into (floor: int, position: int).

    Returns None if the value can't be confidently decoded (blank, too short,
    non-numeric, or position == 0).
    """
    if raw is None:
        return None

    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return None

    s = _TOWER_PREFIX_RE.sub('', s)
    digits = _NON_DIGIT_RE.sub('', s)

    if len(digits) < 3:
        # Not enough digits to safely split floor from position.
        return None

    try:
        n = int(digits)
    except ValueError:
        return None

    floor = n // 100
    position = n % 100

    if position == 0:
        return None

    return floor, position


def normalize_tower(raw):
    """
    Normalize a Tower/Wing value down to a bare code for matching against a
    project's tower folders -- "A", "Tower A", "A Wing", "a" all -> "A".

    Returns None for blank/unusable values.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "none"):
        return None

    # Strip the words "tower"/"wing" (as whole words) then any remaining
    # non-alphanumeric characters, and uppercase what's left.
    cleaned = re.sub(r'(?i)\btower\b|\bwing\b', '', s)
    cleaned = re.sub(r'[^A-Za-z0-9]', '', cleaned).strip()

    if not cleaned:
        return None
    return cleaned.upper()
