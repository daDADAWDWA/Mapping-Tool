"""
OPTIONAL AI assist for the two places where a builder's inventory PDF most
often defeats plain pattern matching:

  1. COLUMN MAPPING -- deciding which extracted column is the flat number and
     which is the carpet area, when the header wording is unusual, wrapped
     across lines, or when several columns contain the word "area"
     (Balcony Area / Total Area / Carpet Area sitting side by side).
  2. UNIT-NUMBER DECODING -- turning a raw identifier into
     (tower, floor, series position) when it doesn't follow the plain
     floor*100+position convention: "G-04", "PH-2", "1601/1602" (jodi),
     "Shop 5", "B-004", "12th flr, unit 3", and so on.

And, as a last resort only, 3. WHOLE-TABLE EXTRACTION from page text or from
a rasterized page image, for PDFs where pdfplumber finds no table structure
at all (borderless layouts, or a scanned/photographed disclosure).

=======================================================================
THE ONE RULE THIS MODULE IS BUILT AROUND
=======================================================================
The model is NEVER the source of an area number when the PDF's own text can
be. In the normal path it only returns COLUMN INDICES -- the actual values
are then read out of the extracted table by our own code. That way a
hallucinated digit cannot become a carpet area, because the model is never
asked to reproduce one.

In the last-resort path (3) it does have to read values out, so every value
it returns is checked to LITERALLY APPEAR in the page text before being
accepted; anything unverifiable is dropped and reported. For a scanned page
there is no text to check against, so those rows are accepted but marked
`needs_verification` so the UI can tell you they were never cross-checked.

Every function here degrades silently to "no AI result" on any failure --
missing key, missing SDK, network error, malformed JSON, or an answer that
fails validation. The deterministic path always remains the fallback, so
enabling AI can add information but can never break a run.
"""

import base64
import io
import json
import os
import re

from .config import DEFAULT_MODEL

# Fields we ask the model to locate in the inventory table.
MAPPABLE_FIELDS = ["sr_no", "tower", "flat_no", "carpet_area", "status", "date"]


def _client(api_key=None):
    """An Anthropic client, or None if we can't build one for any reason."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic(api_key=key)
    except Exception:
        return None


def _parse_json_block(text):
    """
    Pull the first JSON object/array out of a model reply. Tolerates code
    fences and any stray prose, since a strict json.loads on the raw reply is
    the most common avoidable failure.
    """
    if not text:
        return None
    text = text.replace("```json", "").replace("```", "").strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _ask(client, prompt, max_tokens=4000, image_b64=None,
         image_media_type="image/png", model=None):
    """One request, returning parsed JSON or None. Never raises."""
    content = []
    if image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type, "data": image_b64},
        })
    content.append({"type": "text", "text": prompt})
    try:
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        return _parse_json_block(text)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. Column mapping
# ---------------------------------------------------------------------------

def verify_column_mapping(header_row, sample_rows, current_map, api_key=None, model=None):
    """
    Ask the model which column index holds each field. Returns
    (mapping, notes) -- or (None, notes) to mean "keep the deterministic
    mapping". `mapping` values are column indices into the extracted rows.

    Only INDICES come back from the model; values are still read from the
    PDF by the caller.
    """
    client = _client(api_key)
    if client is None:
        return None, []

    def describe(row):
        return " | ".join(
            f"[{i}] {str(c).strip()[:40] if c is not None else ''}"
            for i, c in enumerate(row)
        )

    prompt = (
        "You are reading one table out of a MahaRERA 'Sold/Booked Inventory' "
        "disclosure PDF (Circular 29 format). Below is the header row and a "
        "few data rows, already split into columns, each cell prefixed with "
        "its column index.\n\n"
        f"HEADER: {describe(header_row)}\n\n"
        + "\n".join(f"ROW: {describe(r)}" for r in sample_rows[:6])
        + "\n\nOur current guess at the column mapping is:\n"
        + json.dumps(current_map)
        + "\n\nReturn ONLY a JSON object mapping each of these fields to the "
        "correct column index, or null if that field is not present:\n"
        '{"sr_no": n, "tower": n, "flat_no": n, "carpet_area": n, '
        '"status": n, "date": n, "reasoning": "one short sentence"}\n\n'
        "Rules that matter:\n"
        "- carpet_area must be the CARPET area of the flat itself. If there "
        "are several area columns (balcony, terrace, total, built-up, "
        "ancillary, exclusive), pick the carpet one, NOT the total.\n"
        "- flat_no is the flat/shop/unit identifier, not the serial number.\n"
        "- tower is the tower/wing/building letter or name column, if any.\n"
        "- Judge by the DATA in the rows as well as the header text: a "
        "carpet-area column holds numbers in a plausible range, a flat_no "
        "column holds identifiers.\n"
        "- Use only column indices that actually appear above. No other text."
    )

    data = _ask(client, prompt, max_tokens=800, model=model)
    if not isinstance(data, dict):
        return None, []

    width = max([len(header_row)] + [len(r) for r in sample_rows[:6]] or [0])
    mapping = {}
    for field in MAPPABLE_FIELDS:
        idx = data.get(field)
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if 0 <= idx < width:
            mapping[field] = idx

    # A mapping with no flat number or no carpet area is useless -- fall back.
    if "flat_no" not in mapping or "carpet_area" not in mapping:
        return None, ["AI column check returned an incomplete mapping; kept the pattern-matched one."]

    notes = []
    changed = {f: (current_map.get(f), mapping[f]) for f in mapping
               if current_map.get(f) != mapping[f]}
    if changed:
        detail = ", ".join(f"{f}: column {was} -> {now}" for f, (was, now) in changed.items())
        reason = str(data.get("reasoning") or "").strip()[:200]
        notes.append(
            f"AI corrected the inventory PDF column mapping ({detail})."
            + (f" Reason given: {reason}" if reason else "")
        )
    return mapping, notes


# ---------------------------------------------------------------------------
# 2. Unit-number decoding
# ---------------------------------------------------------------------------

def decode_units_ai(raw_values, floors, num_series, api_key=None,
                    series_labels=None, source="inventory", model=None):
    """
    Batch-decode identifiers that decode_unit() could not handle.

    Returns {raw_value: {"tower": str|None, "floor": int, "position": int}}
    containing ONLY entries that survive validation:
      - floor must be a floor that actually exists in this template
      - position must be within 1..num_series

    `position` is the 1-based SLOT counted left to right across the
    template's series columns -- deliberately not the "Series N" label, since
    those are commonly out of numeric order.
    """
    raw_values = [r for r in dict.fromkeys(str(v).strip() for v in raw_values) if r]
    if not raw_values:
        return {}
    client = _client(api_key)
    if client is None:
        return {}

    floor_list = sorted(floors)
    labels = ", ".join(str(l) for l in (series_labels or [])) or "unlabelled"

    prompt = (
        "You are decoding flat identifiers from an Indian residential tower's "
        f"{source} file into (tower, floor, series position).\n\n"
        "The normal convention is: number = floor * 100 + position, so 1201 "
        "is floor 12 position 1, and 602 is floor 6 position 2. Position is "
        "the 1-based slot counted LEFT TO RIGHT across the tower's series "
        "columns.\n\n"
        f"This tower has {num_series} series slots (column labels, left to "
        f"right: {labels}).\n"
        f"Floors that exist in this tower: {floor_list}\n\n"
        "These identifiers could not be decoded by the plain rule. Decode "
        "each one:\n"
        + "\n".join(f"- {v}" for v in raw_values[:200])
        + "\n\nHandle the real-world cases: a leading tower/wing letter "
        "('A-303', 'B/1204') belongs in tower, not the number. Ground floor "
        "may be written 'G-01' or '001'. 'PH' means penthouse (the top "
        "floor). A jodi/combined flat written '1601/1602' or '1601+1602' "
        "should decode to its FIRST component. Zero-padding is meaningless "
        "('0304' is floor 3 position 4).\n\n"
        "Return ONLY a JSON object keyed by the exact identifier string:\n"
        '{"G-04": {"tower": null, "floor": 0, "position": 4}, '
        '"A-1201": {"tower": "A", "floor": 12, "position": 1}}\n\n'
        "If you cannot decode one confidently, OMIT it entirely rather than "
        "guessing -- an omission is reported for manual review, a wrong guess "
        "silently corrupts a unit's area. No other text."
    )

    data = _ask(client, prompt, max_tokens=4000, model=model)
    if not isinstance(data, dict):
        return {}

    floor_set = set(floor_list)
    out = {}
    for raw, decoded in data.items():
        if not isinstance(decoded, dict):
            continue
        floor, position = decoded.get("floor"), decoded.get("position")
        if isinstance(floor, bool) or isinstance(position, bool):
            continue
        if not isinstance(floor, int) or not isinstance(position, int):
            continue
        if floor not in floor_set or not (1 <= position <= num_series):
            continue
        tower = decoded.get("tower")
        tower = str(tower).strip().upper() if tower else None
        out[str(raw).strip()] = {"tower": tower or None, "floor": floor, "position": position}
    return out


# ---------------------------------------------------------------------------
# 3. Last-resort whole-table extraction
# ---------------------------------------------------------------------------

def _values_present_in_text(value, text_norm):
    """Is this area value literally present in the page text?"""
    s = str(value).strip()
    if not s:
        return False
    candidates = {s, s.replace(",", ""), s.rstrip("0").rstrip(".")}
    try:
        f = float(str(value).replace(",", ""))
        candidates.update({f"{f:g}", f"{f:.2f}", f"{f:.1f}", str(int(f)) if f == int(f) else f"{f:g}"})
    except (TypeError, ValueError):
        pass
    return any(c and re.sub(r'\s+', '', c) in text_norm for c in candidates)


def extract_rows_from_text(page_texts, api_key=None, model=None):
    """
    For PDFs where no table structure could be detected. Returns
    (records, warnings) where each record is
    {"tower", "flat_no", "carpet_area"}.

    Every carpet_area returned by the model is checked to literally appear in
    the page text. Rows that fail are DROPPED, because a value that isn't in
    the document didn't come from the document.
    """
    warnings = []
    joined = "\n".join(t for t in page_texts if t)
    if not joined.strip():
        return [], warnings
    client = _client(api_key)
    if client is None:
        return [], warnings

    prompt = (
        "Below is the raw text of a MahaRERA 'Sold/Booked Inventory' "
        "disclosure PDF whose table structure could not be detected "
        "automatically. Reconstruct the inventory rows.\n\n"
        "Return ONLY a JSON array:\n"
        '[{"tower": "A", "flat_no": "1201", "carpet_area": 113.99}, ...]\n\n'
        "Rules:\n"
        "- carpet_area is the flat's CARPET area, never a total that includes "
        "balcony or terrace.\n"
        "- Copy numbers EXACTLY as printed. Do not round, convert units, or "
        "compute anything.\n"
        "- If one flat is disclosed as two area components on two lines "
        "(a main area plus a small ancillary/exclusive area), emit BOTH rows "
        "with the same flat_no -- they get summed downstream.\n"
        "- Omit any row you are not confident about.\n"
        "- tower may be null if the document does not state one.\n\n"
        "TEXT:\n" + joined[:60000]
    )

    data = _ask(client, prompt, max_tokens=8000, model=model)
    if not isinstance(data, list):
        return [], warnings

    text_norm = re.sub(r'\s+', '', joined)
    records, unverified = [], 0
    for row in data:
        if not isinstance(row, dict):
            continue
        flat_no, area = row.get("flat_no"), row.get("carpet_area")
        if flat_no is None or area is None:
            continue
        # A value we can't find in the page text is KEPT but marked unverified,
        # rather than dropped. A flagged figure you can check beats a silently
        # empty cell -- the cell is highlighted and the row is flagged in Final
        # Output, so nothing unverified passes as confirmed.
        ok = _values_present_in_text(area, text_norm)
        if not ok:
            unverified += 1
        records.append({
            "tower": row.get("tower"),
            "flat_no": str(flat_no).strip(),
            "carpet_area": area,
            "verified": ok,
        })

    if records:
        warnings.append(
            f"No table structure was detected in the inventory PDF, so {len(records)} "
            f"row(s) were reconstructed by AI from the page text. Every area was "
            f"cross-checked against the document text. Please spot-check Section 2."
        )
    if unverified:
        warnings.append(
            f"{unverified} AI-reconstructed inventory row(s) had a carpet area that could "
            f"NOT be found in the PDF text. They were kept and highlighted as unverified "
            f"— check those cells before relying on them."
        )
    return records, warnings


def fast_page_texts(pdf_path):
    """
    [(page_number, text), ...] using pypdfium2, which is roughly 60x faster
    than pdfminer/pdfplumber on a long document -- measured at 0.5s vs 32s for
    a 200-page agreement. That difference is the whole reason a 200-page file
    is usable: the old path spent half a minute reading text before it could
    even decide which page to send.

    Falls back to pdfplumber if pypdfium2 isn't available. Returns [] if the
    file can't be opened at all.
    """
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(pdf_path)
        out = []
        for i in range(len(doc)):
            try:
                out.append((i + 1, doc[i].get_textpage().get_text_range() or ""))
            except Exception:
                out.append((i + 1, ""))
        return out
    except Exception:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            out = []
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    out.append((i, page.extract_text() or ""))
                except Exception:
                    out.append((i, ""))
            return out
    except Exception:
        return []


def count_pdf_pages(pdf_path):
    try:
        import pypdfium2
        return len(pypdfium2.PdfDocument(pdf_path))
    except Exception:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def rasterize_page(pdf_path, page_number, resolution=150):
    """One page as a base64 PNG, or None."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_number - 1]
            buf = io.BytesIO()
            page.to_image(resolution=resolution).original.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def rasterize_pages(pdf_path, max_pages=5, resolution=150):
    """
    Page images as base64 PNGs. Returns [] if rasterizing isn't possible in
    this environment.
    """
    images = []
    for n in range(1, min(count_pdf_pages(pdf_path), max_pages) + 1):
        img = rasterize_page(pdf_path, n, resolution=resolution)
        if img is None:
            break
        images.append(img)
    return images


def _compact_ranges(numbers):
    """[1,2,3,7,8] -> '1-3,7-8'."""
    nums = sorted(set(numbers))
    if not nums:
        return ""
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append((start, prev))
        start = prev = n
    out.append((start, prev))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


_IMAGE_TABLE_PROMPT = (
    "This is a scanned page from a MahaRERA disclosure or a builder's "
    "carpet-area statement. Read the table.\n\n"
    "TWO LAYOUTS OCCUR. Decide which this page uses:\n\n"
    "A) per_flat -- ONE ROW PER FLAT, with a flat-number column. Typically "
    "SR NO | Wing | Flat No | Carpet Area | Sold/Booked | Date.\n\n"
    "B) floor_matrix -- rows are FLOORS ('1ST FLOOR', 'GROUND FLOOR', "
    "'16TH FLOOR') and columns are FLAT POSITIONS ('FLAT NO.1', 'FLAT NO.2', "
    "...). Each cell holds that flat's carpet area. There is no flat-number "
    "column at all.\n\n"
    "Return ONLY JSON.\n\n"
    "For per_flat:\n"
    '{"layout":"per_flat","rows":[{"sr_no":1,"tower":"A","flat_no":"401",'
    '"carpet_area":132.94}, ...]}\n\n'
    "For floor_matrix:\n"
    '{"layout":"floor_matrix","rows":[{"floor_label":"1ST FLOOR","position":1,'
    '"carpet_area":86.35}, ...]}\n'
    "  - position is the 1-based index of the flat column counted LEFT TO "
    "RIGHT: the first flat column is 1, the next 2, and so on. Use the column "
    "POSITION, not any number inside its heading.\n"
    "  - emit one entry for every non-empty cell, floor by floor\n"
    "  - SKIP cells shown as '-----', '--', 'NA' or blank -- those flats do "
    "not exist on that floor\n"
    "  - do NOT emit a totals row (e.g. 'TOTAL FLATS = 61 NOs' or a row of "
    "flat counts)\n\n"
    "Rules for both layouts:\n"
    "- sr_no (per_flat only): the row's serial number as an integer, for every "
    "row -- it is used to detect rows that were missed.\n"
    "- Copy every number EXACTLY as printed: no rounding, no unit conversion, "
    "no arithmetic.\n"
    "- carpet_area is the flat's carpet area, not a total including balcony.\n"
    "- Read EVERY data row top to bottom. Do not stop early, do not summarise.\n"
    "- Omit only cells you genuinely cannot read.\n"
    "- If this page has no such table at all, return "
    '{"layout":"per_flat","rows":[]}.'
)

_FLOOR_NUM_RE = re.compile(r'(\d+)')


def parse_floor_label(text):
    """
    '1ST FLOOR' -> 1, '16TH FLOOR' -> 16, 'GROUND FLOOR' -> 0.
    Returns None for anything that isn't a floor (totals rows, stilt, podium),
    so those never get mistaken for a floor number.
    """
    s = str(text or "").strip().upper()
    if not s or "TOTAL" in s or "STILT" in s or "PODIUM" in s or "PARKING" in s:
        return None
    m = _FLOOR_NUM_RE.search(s)
    if m:
        return int(m.group(1))
    if "GROUND" in s or s in ("G", "GF", "G FLOOR"):
        return 0
    return None


def _read_table_page(client, pdf_path, page_no, model, resolution):
    """
    (records, sr_numbers, skipped) for one rasterized page.

    Handles both layouts. A floor_matrix cell is converted to this app's own
    numbering -- flat number = floor * 100 + position -- so everything
    downstream is identical regardless of which shape the document used.
    """
    image_b64 = rasterize_page(pdf_path, page_no, resolution=resolution)
    if image_b64 is None:
        return [], set(), []
    data = _ask(client, _IMAGE_TABLE_PROMPT, max_tokens=8000,
                image_b64=image_b64, model=model)

    # Tolerate a bare list as well as the documented object form.
    if isinstance(data, list):
        data = {"layout": "per_flat", "rows": data}
    if not isinstance(data, dict):
        return [], set(), []
    rows = data.get("rows")
    if not isinstance(rows, list):
        return [], set(), []

    layout = str(data.get("layout") or "per_flat").strip().lower()
    records, srs, skipped = [], set(), []

    if layout == "floor_matrix":
        for row in rows:
            if not isinstance(row, dict) or row.get("carpet_area") is None:
                continue
            floor = parse_floor_label(row.get("floor_label"))
            pos = row.get("position")
            if floor is None or not isinstance(pos, int) or isinstance(pos, bool) or pos < 1:
                skipped.append(str(row.get("floor_label"))[:30])
                continue
            records.append({
                "tower": row.get("tower"),
                # 3-digit minimum so a ground-floor flat still decodes
                # (floor 0, position 1 -> "001").
                "flat_no": f"{floor * 100 + pos:03d}",
                "carpet_area": row["carpet_area"],
            })
        return records, srs, skipped

    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("flat_no") is None or row.get("carpet_area") is None:
            continue
        records.append({
            "tower": row.get("tower"),
            "flat_no": str(row["flat_no"]).strip(),
            "carpet_area": row["carpet_area"],
        })
        sr = row.get("sr_no")
        if isinstance(sr, int) and not isinstance(sr, bool):
            srs.add(sr)
    return records, srs, skipped


_TRIAGE_PROMPT = (
    "Each image below is one page of an Indian flat-purchase agreement, in "
    "order. They are low-resolution thumbnails -- you are NOT being asked to "
    "read the numbers, only to spot WHICH pages state the flat's carpet area.\n\n"
    "A page qualifies if it contains wording like 'carpet area', 'RERA carpet', "
    "'admeasuring', 'क्षेत्रफळ', 'कार्पेट', or a schedule of the flat with its "
    "area.\n\n"
    'Return ONLY JSON: {"pages": [2, 5]} listing the 1-based positions of '
    "qualifying images within THIS batch, most likely first. Return "
    '{"pages": []} if none qualify. No other text.'
)


def triage_scanned_pages(pdf_path, api_key=None, model=None, max_pages=400,
                         batch_size=8, thumb_resolution=70):
    """
    Which pages of a SCANNED agreement are likely to state the carpet area.

    A scan has no text to grep, so the only way to find the area clause in a
    200-page document is to look. Reading every page at full resolution would
    be 200 expensive calls; instead pages go out as cheap low-resolution
    thumbnails, `batch_size` at a time, and only the pages that come back are
    read properly afterwards. For 200 pages that is ~25 cheap calls instead of
    200 expensive ones.

    Returns (page_numbers, warnings).
    """
    warnings = []
    client = _client(api_key)
    if client is None:
        return [], warnings

    total = count_pdf_pages(pdf_path)
    if not total:
        return [], warnings
    limit = min(total, max_pages)
    if total > max_pages:
        warnings.append(
            f"This scanned agreement has {total} pages; only the first {max_pages} were "
            f"searched for the area clause. If the area sits later than that, upload a "
            f"photo of just that page instead -- it is far faster and more reliable."
        )

    hits = []
    for start in range(1, limit + 1, batch_size):
        pages = list(range(start, min(start + batch_size, limit + 1)))
        images = [(n, rasterize_page(pdf_path, n, resolution=thumb_resolution))
                  for n in pages]
        images = [(n, b) for n, b in images if b]
        if not images:
            continue
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b}}
                   for _, b in images]
        content.append({"type": "text", "text": _TRIAGE_PROMPT})
        try:
            resp = client.messages.create(
                model=model or DEFAULT_MODEL, max_tokens=300,
                messages=[{"role": "user", "content": content}],
            )
            data = _parse_json_block("".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ))
        except Exception:
            data = None
        if isinstance(data, dict):
            for idx in (data.get("pages") or []):
                if isinstance(idx, int) and 1 <= idx <= len(images):
                    hits.append(images[idx - 1][0])
        if len(hits) >= 4:
            # Enough candidates to try; no need to keep scanning a long file.
            break

    return list(dict.fromkeys(hits)), warnings


def extract_rows_from_images(pdf_path, api_key=None, max_pages=25, model=None):
    """
    Last resort: a scanned disclosure with no text layer at all. One request
    per page.

    This is the least reliable path in the app, so it is also the loudest. It
    reports the row count for EVERY page, retries a page that comes back
    empty at a higher resolution, and uses the SR NO column to prove whether
    anything went missing. A page silently returning nothing used to leave a
    grid quietly short of a whole page's worth of flats with no indication at
    all -- that is exactly what these checks exist to catch.
    """
    warnings = []
    client = _client(api_key)
    if client is None:
        return [], warnings

    total_pages = count_pdf_pages(pdf_path)
    if not total_pages:
        return [], warnings

    to_read = min(total_pages, max_pages)
    if total_pages > max_pages:
        warnings.append(
            f"The inventory PDF has {total_pages} pages but only the first {max_pages} "
            f"were read. Split the file or raise max_pages, or the remaining pages' "
            f"flats will be missing from Section 2."
        )

    records, sr_seen, per_page, empty_pages = [], set(), [], []
    for page_no in range(1, to_read + 1):
        rows, srs, skipped = _read_table_page(client, pdf_path, page_no, model, resolution=150)
        if not rows:
            # A blank result is far more often a hard-to-read scan than a page
            # with no table, so try once more with a sharper render.
            rows, srs, skipped = _read_table_page(client, pdf_path, page_no, model,
                                                  resolution=220)
            if rows:
                warnings.append(
                    f"Inventory PDF page {page_no} came back empty and needed a second, "
                    f"higher-resolution pass ({len(rows)} rows recovered)."
                )
            else:
                empty_pages.append(page_no)
        per_page.append(f"p{page_no}: {len(rows)}")
        if skipped:
            warnings.append(
                f"Inventory PDF page {page_no}: {len(skipped)} row(s) were skipped because "
                f"their floor label couldn't be read as a floor "
                f"({', '.join(dict.fromkeys(skipped))})."
            )
        sr_seen |= srs
        records.extend(rows)

    if records:
        warnings.append(
            f"The inventory PDF has no text layer (it is a scan), so {len(records)} row(s) "
            f"were read by AI from page images — {', '.join(per_page)}. These could NOT be "
            f"cross-checked against a text layer, so treat Section 2 as unverified until "
            f"reviewed."
        )

    if empty_pages:
        warnings.append(
            f"NOTHING could be read from inventory PDF page(s) "
            f"{_compact_ranges(empty_pages)}, even after a retry. Every flat on "
            f"{'those pages' if len(empty_pages) > 1 else 'that page'} is missing from "
            f"Section 2 — re-upload a clearer scan, or supply those rows as an Excel file."
        )

    # The SR NO column numbers every row, so a gap is proof of data loss.
    if sr_seen:
        lo, hi = min(sr_seen), max(sr_seen)
        missing = sorted(set(range(lo, hi + 1)) - sr_seen)
        if missing:
            warnings.append(
                f"Serial numbers {_compact_ranges(missing)} are missing from what was read "
                f"({len(sr_seen)} of the {hi - lo + 1} rows between SR {lo} and SR {hi} came "
                f"through). Those flats are absent from Section 2."
            )
        if lo > 1:
            warnings.append(
                f"The first serial number read was SR {lo}, so rows 1-{lo - 1} were not "
                f"captured — most likely a page that failed to read."
            )

    return records, warnings

# ---------------------------------------------------------------------------
# 1b. Column-NAME resolution for CSV / Excel inputs
# ---------------------------------------------------------------------------
# Aliases only match names we already know. A real export can call the unit
# number "Unit", "property", "Flat ID" or anything else, and the app refuses to
# guess between ambiguous names -- correctly, because guessing wrong silently
# attaches one flat's area to another. Asking the model instead keeps that
# safety while removing the dead end: it sees the actual column names AND
# sample values, and its answer is validated against the real column list.

FIELD_DESCRIPTIONS = {
    "unit_no": (
        "the flat/unit NUMBER identifying which flat each row is about, e.g. "
        "'1201', 'A-303', '402'. NOT the unit TYPE (like '2 BHK'), not a "
        "building or project name, and not a database/serial id"
    ),
    "description": (
        "the long free-text legal description of the property, usually Marathi "
        "and/or English, in which the carpet area is written out in words. It "
        "is a long sentence or paragraph, NOT a short numeric area column and "
        "NOT an area-type label"
    ),
    "tower": "the tower / wing / building identifier for the flat",
    "carpet_area": "the NUMERIC carpet area of the flat",
    "flat_no_inventory": (
        "the flat/unit number identifying which flat each row is about"
    ),
    "registration_year": "the year the sale was registered",
    "registration_date": "the full date the sale was registered",
}


def resolve_columns_ai(columns, sample_rows, needed_fields, api_key=None,
                       model=None, file_kind="data file"):
    """
    Ask which real column holds each still-unresolved field.

    columns:       the file's actual column names
    sample_rows:   a few rows as {column: value} dicts, for judging by content
    needed_fields: field keys that alias matching could not resolve

    Returns {field: column_name} containing only names that genuinely exist in
    `columns` -- a hallucinated column name is dropped rather than used.
    """
    client = _client(api_key)
    if client is None or not needed_fields:
        return {}, []

    # Internal working columns are not part of the user's file.
    visible = [c for c in columns if not str(c).startswith("_")]

    lines = []
    for i, row in enumerate(sample_rows[:3], start=1):
        parts = []
        for c in visible:
            val = str(row.get(c, ""))[:60].replace("\n", " ")
            if val:
                parts.append(f"{c}={val!r}")
        lines.append(f"ROW {i}: " + " | ".join(parts))

    wanted = "\n".join(
        f'- "{f}": {FIELD_DESCRIPTIONS.get(f, f)}' for f in needed_fields
    )

    prompt = (
        f"You are mapping the columns of an Indian real-estate {file_kind} onto "
        f"the fields an application needs.\n\n"
        f"COLUMN NAMES: {visible}\n\n"
        + "\n".join(lines)
        + "\n\nFind the column for each of these fields:\n" + wanted
        + "\n\nReturn ONLY a JSON object mapping each field to the EXACT column "
        "name from the list above, or null if no column holds that field:\n"
        '{"' + needed_fields[0] + '": "Exact Column Name", "reasoning": '
        '"one short sentence"}\n\n'
        "Rules that matter:\n"
        "- Use column names copied exactly from the list. Do not invent one.\n"
        "- Judge by the SAMPLE VALUES as much as the name: the unit-number "
        "column holds short flat identifiers, the description column holds long "
        "free text.\n"
        "- Return null rather than a doubtful guess. A wrong column silently "
        "attaches the wrong area to a flat, which is worse than the field being "
        "reported as missing.\n"
        "- No other text."
    )

    data = _ask(client, prompt, max_tokens=700, model=model)
    if not isinstance(data, dict):
        return {}, []

    resolved, notes = {}, []
    lookup = {str(c).strip().lower(): c for c in visible}
    for field in needed_fields:
        name = data.get(field)
        if not name or not isinstance(name, str):
            continue
        actual = lookup.get(name.strip().lower())
        if actual is None:
            notes.append(
                f"AI proposed a column '{name}' for '{field}' that isn't in the file, "
                f"so it was ignored."
            )
            continue
        resolved[field] = actual

    if resolved:
        detail = ", ".join(f"{f} -> '{c}'" for f, c in resolved.items())
        reason = str(data.get("reasoning") or "").strip()[:200]
        notes.append(
            f"AI matched {file_kind} columns that alias matching couldn't: {detail}."
            + (f" Reason given: {reason}" if reason else "")
            + " To make this permanent and skip the AI call next time, add it to a "
            "column_aliases.json next to the template, e.g. "
            + json.dumps({f: [c] for f, c in resolved.items()})
        )
    return resolved, notes
