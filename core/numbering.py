"""
Working out what a unit number MEANS, per project, from the data itself.

Two conventions turn up, and they disagree completely:

    FLOOR_POSITION      floor = n // 100, position = n % 100
                        601 -> floor 6, position 1          (Maharashtra)

    TOWER_FLOOR_UNIT    tower = first digit, unit = last digit,
                        floor = the digits between
                        451 -> tower 4, floor 5, unit 1     (Bengaluru)

Applying the wrong one puts every value in the wrong cell while the output
still looks plausible, so it must not be guessed or hardcoded. It is inferred
instead: the descriptions usually state the floor in words ("on the Seventh
Floor", "Block No. 5TH FLOOR"), which gives a ground truth to test each rule
against. On a real Embassy Pristine export, TOWER_FLOOR_UNIT fitted 22 of 22
rows and FLOOR_POSITION fitted 2 -- a decisive answer that no amount of
guessing would have produced.

A project can override the choice with a `numbering.json` in its folder:

    {"rule": "tower_floor_unit"}
"""

import json
import re
from pathlib import Path

FLOOR_POSITION = "floor_position"
TOWER_FLOOR_UNIT = "tower_floor_unit"
RULES = (FLOOR_POSITION, TOWER_FLOOR_UNIT)

RULE_LABELS = {
    FLOOR_POSITION: "floor x 100 + position (e.g. 601 = floor 6, position 1)",
    TOWER_FLOOR_UNIT: "tower + floor + unit (e.g. 451 = tower 4, floor 5, unit 1)",
}


def _digits(raw):
    if raw is None:
        return None
    text = re.sub(r'^[A-Za-z]+[\s/\-]*', '', str(raw).strip())
    digits = re.sub(r'\D', '', text)
    return digits or None


def decode(raw, rule=FLOOR_POSITION):
    """
    (tower, floor, position) -- tower is None for rules that don't encode one.
    Returns None when the value can't be decoded under this rule.
    """
    digits = _digits(raw)
    if not digits:
        return None

    if rule == TOWER_FLOOR_UNIT:
        # Needs at least tower + floor + unit.
        if len(digits) < 3:
            return None
        tower, floor_text, position = digits[0], digits[1:-1], digits[-1]
        if not floor_text:
            return None
        try:
            return tower, int(floor_text), int(position)
        except ValueError:
            return None

    if len(digits) < 3:
        return None
    try:
        number = int(digits)
    except ValueError:
        return None
    floor, position = number // 100, number % 100
    if position == 0:
        return None
    return None, floor, position


def score_rules(samples):
    """
    samples: [(unit_no, stated_floor, stated_tower)] -- floor required, tower
    optional. Returns {rule: (hits, tested)}.
    """
    scores = {rule: [0, 0] for rule in RULES}
    for unit, floor, tower in samples:
        if unit is None or floor is None:
            continue
        for rule in RULES:
            decoded = decode(unit, rule)
            if decoded is None:
                scores[rule][1] += 1
                continue
            got_tower, got_floor, _ = decoded
            ok = got_floor == floor
            if ok and tower and got_tower:
                ok = str(got_tower) == str(tower).strip()
            scores[rule][0] += int(ok)
            scores[rule][1] += 1
    return {rule: tuple(v) for rule, v in scores.items()}


def infer_rule(samples, project_dir=None, min_samples=3):
    """
    Returns (rule, note). An explicit numbering.json always wins; otherwise the
    rule that fits the stated floors best. With too little evidence the
    conservative default (FLOOR_POSITION) is kept and the note says so, rather
    than switching every cell on the strength of one row.
    """
    if project_dir:
        path = Path(project_dir) / "numbering.json"
        if path.exists():
            try:
                configured = str(json.loads(path.read_text(encoding="utf-8")).get("rule") or "")
                if configured in RULES:
                    return configured, f"numbering rule set explicitly in {path.name}"
            except (json.JSONDecodeError, OSError):
                pass

    scores = score_rules(samples)
    tested = max(v[1] for v in scores.values()) if scores else 0
    if tested < min_samples:
        return FLOOR_POSITION, (
            f"only {tested} row(s) stated a floor to check against, so the default "
            f"rule was kept — {RULE_LABELS[FLOOR_POSITION]}"
        )

    best = max(RULES, key=lambda r: scores[r][0])
    hits, total = scores[best]
    other = [r for r in RULES if r != best][0]
    other_hits = scores[other][0]

    if hits == 0:
        return FLOOR_POSITION, (
            f"neither rule matched the floors stated in {total} row(s); the default "
            f"was kept, so unit numbers here may follow a convention this app "
            f"doesn't know — check a few cells"
        )
    if hits == other_hits:
        return FLOOR_POSITION, (
            f"both rules fit equally ({hits}/{total}), so the default was kept"
        )
    return best, (
        f"numbering detected from the data: {RULE_LABELS[best]} fits {hits}/{total} "
        f"rows where the text states a floor, against {other_hits}/{total} for the "
        f"alternative"
    )


def collect_samples(descriptions, unit_numbers, towers=None):
    """
    Build inference samples by reading the floor out of each description.
    """
    towers = towers or [None] * len(descriptions)
    samples = []
    for description, unit, tower in zip(descriptions, unit_numbers, towers):
        floor = floor_from_text(description)
        if unit and floor is not None:
            samples.append((unit, floor, tower))
    return samples


_ORDINALS = ["ground", "first", "second", "third", "fourth", "fifth", "sixth",
             "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
             "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
             "eighteenth", "nineteenth", "twentieth"]


def floor_from_text(description):
    """
    The floor a description states, or None.

    Where two are given -- "Seventh Floor (Eighth Floor as referred in the BBMP
    sanctioned Plan)" -- the FIRST is taken: that is the flat's own numbering,
    which is what the stack view is built on.
    """
    text = str(description or "")
    if not text:
        return None
    m = re.search(r'Block No\.?\s*(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*FLOOR', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'on the (\w+)\s+[Ff]loor', text)
    if m and m.group(1).lower() in _ORDINALS:
        return _ORDINALS.index(m.group(1).lower())
    m = re.search(r'\b(\d{1,2})\s*(?:st|nd|rd|th)\s+floor', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r'ground\s+floor', text, re.IGNORECASE):
        return 0
    return None
