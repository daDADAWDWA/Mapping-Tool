"""
Flexible column-name matching, so a naming difference alone (case, spacing,
underscores vs spaces -- "Unit No" vs "unitNo" vs "UNIT_NO") doesn't break
the pipeline. This does NOT guess between genuinely different, ambiguous
column names (e.g. it will never assume an unfamiliar column like "property"
is the unit number) -- that kind of mapping has to be told to us once, via
an optional per-project/tower `column_aliases.json` override file, which
lets a brand-new data export be supported without touching any code.

Example `column_aliases.json` (place next to that tower's template.csv):
    {
      "unit_no": ["property"],
      "tower": ["towerOrWing"]
    }
"""

import re
import json
from pathlib import Path

# Logical field -> known aliases. Matching is case/space/punctuation-insensitive,
# so listing "Unit No" here also matches "unit_no", "UnitNo", "UNIT NO", etc.
DEFAULT_ALIASES = {
    # Exact-match only, so "unit" cannot collide with "Unit Type"
    # (normalized "unittype") and "flat" cannot collide with "Flat Sale Type".
    "unit_no": ["unit no", "unit number", "unit", "flat no", "flat number", "flat",
                "apartment no", "apartment number", "unit id", "flat id"],
    "description": ["property description", "description"],
    "tower": ["tower/wing", "tower or wing", "tower", "wing"],
    "carpet_area": ["carpet area (sq meter)", "carpet area (sq metre)", "carpet area"],
    "flat_no_inventory": ["flat no", "flat number", "flat", "unit no", "unit",
                          "flat/shop no", "apartment no"],
    "registration_year": ["registration year", "reg year"],
    "registration_date": ["registration date", "reg date", "date"],
}


def _normalize(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(s).lower())


def load_aliases(config_dir) -> dict:
    """
    Merge DEFAULT_ALIASES with an optional column_aliases.json found in
    `config_dir` (a project or tower folder). Project-specific aliases are
    tried BEFORE the defaults.
    """
    merged = {k: list(v) for k, v in DEFAULT_ALIASES.items()}
    override_path = Path(config_dir) / "column_aliases.json"
    if override_path.exists():
        try:
            custom = json.loads(override_path.read_text(encoding="utf-8"))
            for field, names in custom.items():
                existing = merged.get(field, [])
                merged[field] = list(names) + existing
        except (json.JSONDecodeError, OSError):
            pass
    return merged


def find_column(columns, field_key: str, aliases: dict):
    """Returns the actual column name in `columns` matching `field_key`, or None."""
    candidates = aliases.get(field_key, [])
    normalized_cols = {_normalize(c): c for c in columns}
    for cand in candidates:
        norm_cand = _normalize(cand)
        if norm_cand in normalized_cols:
            return normalized_cols[norm_cand]
    return None
