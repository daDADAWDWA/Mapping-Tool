"""
Finding the flat's area in a long agreement, cheaply.

A 200-page agreement contains the area in one or two places, and everything
else is boilerplate. Reading it front to back -- or even sending a handful of
pages picked by page number -- is both slow and expensive, and on a real file
usually misses: measured on a 100-page scanned agreement, the densest pages
were 83-89, so the schedules sit at the END, nowhere near the first pages a
naive reader would try.

So pages are searched in PRIORITY ORDER and the search STOPS as soon as one
page yields a confident answer:

    Priority 1  SECOND SCHEDULE / SCHEDULE OF THE SAID FLAT
                (and ... AND SAID PARKING)  -- the definitive statement
    Priority 2  any page naming an area type: RERA CARPET, MOFA, BUILT-UP,
                SALEABLE AREA, or a FLAT NO / UNIT NO line
    Priority 3  floor plans -- the boxed RERA CARPET / BALCONY / TOTAL values
    Priority 4  a general property description

For a text-based PDF the whole 200 pages are scanned for these markers in
about half a second and no tokens at all, so the normal cost of an agreement
is ONE model call on ONE page.

For a scan (including a multi-page TIFF, which is how agreements often
arrive) there is no text to search, so pages go out as cheap low-resolution
thumbnails in batches to locate the schedule, and only the winners are read
properly.
"""

import base64
import io
import re
from pathlib import Path

from .ai_assist import _ask, _client, _parse_json_block, _values_present_in_text
from .config import DEFAULT_MODEL

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif",
                    ".tif", ".tiff", ".bmp"}
_MAX_IMAGE_EDGE = 1568

# ---------------------------------------------------------------------------
# Page priority
# ---------------------------------------------------------------------------
PRIORITY_TIERS = [
    (1, [  # the official description of the flat
        "second schedule", "the second schedule", "schedule above referred",
        "second schedule hereinabove referred", "schedule of the said flat",
        "schedule of the said flat and said parking",
        "description of the said premises", "description of property",
        "description of the flat", "description of the said flat",
    ]),
    (2, [  # the operative sale clause
        "agreement for sale", "agreement for sale of premises",
        "agreement for sale of flat", "sale agreement", "agreement to sell",
        "sale deed", "a residential premise being", "residential flat bearing",
        "residential premises", "premises bearing", "admeasuring",
    ]),
    (3, [  # plans, which carry the cleanest area breakdown
        "floor plan", "unit plan", "typical floor plan", "layout plan",
        "architectural plan",
    ]),
    (4, [  # annexures
        "annexure", "property details", "property description",
        "specification of flat", "schedule",
    ]),
    (5, [  # last resort: anything naming a flat or an area basis
        "flat no", "unit no", "apartment no", "carpet", "built-up", "built up",
        "mofa", "rera", "saleable", "super built", "कार्पेट", "क्षेत्रफळ",
    ]),
]

# Pages whose area figures are NEVER the flat's area. A registered deed set is
# mostly these: a valuation sheet quotes a built-up area for stamp duty, an
# Index II quotes land and OLD-property areas, and a property card quotes plot
# area. Reading any of them produces a confident, plausible, wrong number --
# so these pages are excluded before extraction, not filtered afterwards.
PAGE_SKIP_MARKERS = [
    "मूल्यांकन पत्रक", "valuation id", "मूल्यांकनाचे वर्ष", "वार्षिक मूल्य दर",
    "सूची क्र", "index-ii", "index ii", "दुय्यम निबंधक",
    "challan", "mtr form", "grn", "document handling charges",
    "मालमत्ता पत्रक", "property card", "brihanmumbai mahanagar palika",
    "assessee", "electricity", "adani", "bill unpaid",
    "manager's cheque", "share certificate", "memorandum of the transfer",
    "permanent account number", "income tax department", "आयकर विभाग",
    "unique identification authority", "आधार",
    "occupation certificate", "occupancy certificate", "b.c.c",
    "दस्त गोषवारा", "know your rights as registrants",
]

# Area wordings we want, mapped to a normalized label.
AREA_LABELS = [
    ("rera carpet", "RERA Carpet"),
    ("mofa carpet", "MOFA Carpet"),
    ("mofa", "MOFA Carpet"),
    ("super built", "Super Built-up"),
    ("built-up", "Built-up"),
    ("built up", "Built-up"),
    ("builtup", "Built-up"),
    ("saleable", "Saleable"),
    ("dry balcony", "Dry Balcony"),
    ("balcony", "Balcony"),
    ("terrace", "Terrace"),
    ("deck", "Deck"),
    ("total area", "Total"),
    ("total", "Total"),
    ("carpet", "Carpet"),
]

# Never a flat's area, however it is worded. Enforced in the prompt AND here,
# because confusing a land area with a flat area is the single most damaging
# mistake this extraction can make.
FORBIDDEN_AREA_WORDS = re.compile(
    r'land|cts|c\.t\.s|survey|plot area|parking|open space|garden|amenity|'
    r'common area|recreation|layout area|stamp|registration charge|consideration',
    re.IGNORECASE,
)

CARPET_PREFERENCE = ["RERA Carpet", "MOFA Carpet", "Carpet"]
BALCONY_LABELS = {"Balcony", "Dry Balcony", "Terrace", "Deck"}
NON_CARPET_LABELS = {"Built-up", "Super Built-up", "Saleable"}


def normalise_label(text):
    if not text:
        return None
    low = str(text).lower()
    for phrase, label in AREA_LABELS:
        if phrase in low:
            return label
    return str(text).strip().title() or None


# ---------------------------------------------------------------------------
# Page access -- PDF, multi-page TIFF, or a single photo
# ---------------------------------------------------------------------------

def is_multipage_tiff(path):
    if Path(path).suffix.lower() not in (".tif", ".tiff"):
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1) > 1
    except Exception:
        return False


def page_count(path):
    suffix = Path(path).suffix.lower()
    if suffix in (".tif", ".tiff"):
        try:
            from PIL import Image
            with Image.open(path) as im:
                return getattr(im, "n_frames", 1)
        except Exception:
            return 0
    if suffix in IMAGE_EXTENSIONS:
        return 1
    try:
        import pypdfium2
        return len(pypdfium2.PdfDocument(path))
    except Exception:
        return 0


def _encode(im):
    """PIL image -> (base64 JPEG, media_type), EXIF-rotated and downscaled."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    longest = max(im.size)
    if longest > _MAX_IMAGE_EDGE:
        scale = _MAX_IMAGE_EDGE / longest
        im = im.resize((max(1, int(im.width * scale)),
                        max(1, int(im.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def page_image(path, page_no, max_edge=_MAX_IMAGE_EDGE):
    """(base64 JPEG, media_type) for one page of a PDF, TIFF or photo."""
    suffix = Path(path).suffix.lower()
    global _MAX_IMAGE_EDGE
    previous, _MAX_IMAGE_EDGE = _MAX_IMAGE_EDGE, max_edge
    try:
        from PIL import Image
        if suffix in (".heic", ".heif"):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                return None, None
        if suffix in (".tif", ".tiff"):
            with Image.open(path) as im:
                im.seek(page_no - 1)
                return _encode(im)
        if suffix in IMAGE_EXTENSIONS:
            with Image.open(path) as im:
                return _encode(im)
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return _encode(pdf.pages[page_no - 1].to_image(resolution=150).original)
    except Exception:
        return None, None
    finally:
        _MAX_IMAGE_EDGE = previous


def page_texts(path):
    """[(page_no, text)] -- empty text for scans and images."""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return [(n, "") for n in range(1, page_count(path) + 1)]
    from .ai_assist import fast_page_texts
    return fast_page_texts(path)


def is_skip_page(text):
    """True for valuation sheets, Index II, challans, tax receipts, ID cards --
    pages whose areas are land/built-up/old-property figures."""
    low = str(text or "").lower()
    return any(marker in low for marker in PAGE_SKIP_MARKERS)


def shortlist_pages(texts, per_tier=3):
    """
    [(page_no, tier)] in search order: all Priority 1 pages first, then 2..5.
    Pages on the skip list are removed first, whatever they contain.
    """
    usable = [(n, txt) for n, txt in texts if txt and not is_skip_page(txt)]
    ordered = []
    for tier, markers in PRIORITY_TIERS:
        scored = []
        for page_no, text in usable:
            low = text.lower()
            hits = sum(low.count(m) for m in markers)
            if hits:
                scored.append((hits, page_no))
        scored.sort(key=lambda s: (-s[0], s[1]))
        for _, page_no in scored[:per_tier]:
            if page_no not in [p for p, _ in ordered]:
                ordered.append((page_no, tier))
    return ordered


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
EXTRACTION_PROMPT = (
    "This is one page of a scanned Maharashtra property document -- most likely "
    "a SCHEDULE describing the flat, a clause of a sale deed, or a floor plan. "
    "It may be skewed, noisy, low-resolution or partly handwritten.\n\n"
    "Extract the flat's identity and EVERY area stated FOR THAT FLAT.\n\n"
    "Return ONLY JSON:\n"
    '{"flats": [{"flat_no": "601", "wing": null, "building": "13", '
    '"floor": "6th Floor", '
    '"areas": [{"area_type": "RERA Carpet", "value": 1240, "unit": "sq.ft"}, '
    '{"area_type": "MOFA Carpet", "value": 1180, "unit": "sq.ft"}], '
    '"source_text": "the exact phrase the areas were read from"}]}\n\n'
    "IDENTITY\n"
    "- flat_no: 'Flat No.', 'Unit No.', 'Apartment No.', 'Premises bearing' -- "
    "e.g. 402, 801, 1302, A-1402, 601.\n"
    "- wing / building: 'Wing', 'Tower', 'Building', 'Block' -- e.g. A Wing, "
    "Tower B, Building No. 13. null if absent.\n"
    "- floor: e.g. '6th Floor', '13th Floor', 'Ground Floor'. null if absent.\n\n"
    "AREAS TO EXTRACT -- every one stated, with the type worded as printed:\n"
    "RERA Carpet, Carpet Area, MOFA Carpet, Built-up Area, Built Up Area, "
    "Saleable Area, Super Built-up Area, Balcony Area, Dry Balcony Area, "
    "Terrace Area, Deck Area, Total Area.\n"
    "- Where the text gives one figure 'equivalent to' another on a different "
    "basis (e.g. '1180 sq.ft as per MOFA Carpet equivalent to 1240 sq.ft as per "
    "RERA Carpet'), return BOTH as separate entries with their own types.\n"
    "- On a FLOOR PLAN read the boxed figures carefully: plans normally print "
    "RERA CARPET, BALCONY AREA and TOTAL AREA as separate boxes, and all three "
    "are wanted.\n"
    "- unit: 'sq.ft' or 'sq.m' as printed (Sq.Mt / चौ.मी. is sq.m, Sq.Ft / "
    "चौ.फुट is sq.ft).\n\n"
    "NEVER EXTRACT -- these are not the flat's area:\n"
    "CTS Area, Survey Area, Land Area, Plot Area, Road Area, Open Space, "
    "Parking Area, parking dimensions, Garden Area, FSI, Stamp Duty, "
    "Registration Charges, Market Value, Consideration Amount, and anything on "
    "a valuation sheet, government calculation sheet, Index II or tax page.\n\n"
    "THE OLD-FLAT TRAP\n"
    "A redevelopment deed describes TWO flats: the OLD flat surrendered (often "
    "a small area like '467.73 sq.ft', with an old number like '13/253') and "
    "the NEW flat allotted in its place. Return ONLY the NEW flat -- the one "
    "in the new project, with the higher floor/number. If the page describes an "
    "old flat being given up, do not return its area as the flat's area.\n\n"
    "If several DIFFERENT flats are genuinely covered, return one entry each.\n"
    "Copy numbers EXACTLY as printed: no rounding, no unit conversion, no "
    "arithmetic of your own.\n"
    "source_text: transcribe the exact phrase the numbers came from, character "
    "for character -- this is how a human checks your reading.\n"
    'If this page states no flat area, return {"flats": []}. No other text.'
)


def resolve_final_area(carpet, balcony, stated_total, tolerance_ft=5.0):
    """
    The flat's area, and whether a balcony still needs adding.

    Three shapes occur, and telling them apart is what stops double-counting:

      1. A total is written out  -> use it; the balcony is already inside it.
      2. Carpet and balcony only -> add them.
      3. Carpet alone            -> use it.

    Plus the case this document family actually produced: a deed states
    "1240 sq.ft RERA Carpet" while the floor plan for the same flat breaks that
    down as RERA CARPET 101.22 + BALCONY 13.99 = TOTAL 115.21 sq.m -- and
    1240 sq.ft IS 115.20 sq.m. So the deed's "carpet" is really the total. Any
    carpet figure that already equals carpet+balcony, or equals a stated total,
    is treated as a total and the balcony is NOT added again.
    """
    from .final_output import FT_PER_M2, to_m2
    tolerance_m = tolerance_ft / FT_PER_M2

    carpet_m = to_m2(carpet["value"], carpet["unit"]) if carpet else None
    balcony_m = to_m2(balcony["value"], balcony["unit"]) if balcony else None
    total_m = to_m2(stated_total["value"], stated_total["unit"]) if stated_total else None

    if total_m is not None:
        return total_m, True, "stated total"
    if carpet_m is not None and balcony_m is not None:
        # Does the carpet figure already include the balcony?
        if abs(carpet_m - balcony_m) > tolerance_m and carpet_m > balcony_m * 3:
            return carpet_m + balcony_m, True, "carpet + balcony"
        return carpet_m, bool(balcony_m), "carpet (balcony looks already included)"
    if carpet_m is not None:
        return carpet_m, False, "carpet only"
    if balcony_m is not None:
        return balcony_m, False, "balcony only"
    return None, False, None


def pick_areas(areas):
    """
    Sort the extracted areas into what the app needs:
        (carpet, balcony, stated_total, others)
    each of carpet/balcony/total being {"label","value","unit"} or None.

    Carpet preference is RERA Carpet, then MOFA Carpet, then a plain Carpet
    Area. Built-up / Super Built-up / Saleable are recorded but never used as
    the carpet area -- they measure something larger, and silently treating
    one as carpet would inflate a flat.
    """
    clean = []
    for a in areas or []:
        if not isinstance(a, dict) or a.get("value") is None:
            continue
        raw_label = str(a.get("area_type") or "")
        if FORBIDDEN_AREA_WORDS.search(raw_label):
            continue
        try:
            value = float(str(a["value"]).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        unit = str(a.get("unit") or "").lower()
        unit = "sq.m" if ("m" in unit and "mm" not in unit) else "sq.ft"
        clean.append({"label": normalise_label(raw_label) or raw_label,
                      "value": value, "unit": unit, "as_printed": raw_label})

    carpet = None
    for preferred in CARPET_PREFERENCE:
        matches = [c for c in clean if c["label"] == preferred]
        if matches:
            carpet = matches[0]
            break

    balconies = [c for c in clean if c["label"] in BALCONY_LABELS]
    balcony = None
    if balconies:
        # Several balcony-ish areas (balcony + dry balcony) add up.
        total = sum(b["value"] for b in balconies if b["unit"] == balconies[0]["unit"])
        balcony = {"label": ", ".join(b["label"] for b in balconies),
                   "value": round(total, 2), "unit": balconies[0]["unit"]}

    stated_total = next((c for c in clean if c["label"] == "Total"), None)
    others = [c for c in clean
              if c["label"] in NON_CARPET_LABELS
              or (c is not carpet and c["label"] not in BALCONY_LABELS
                  and c["label"] != "Total")]

    # Nothing labelled carpet: fall back to a built-up/saleable figure rather
    # than returning nothing, but say which it is so it can't pass as carpet.
    if carpet is None and others:
        carpet = others[0]

    return carpet, balcony, stated_total, others


def _floor_from_text(text):
    if not text:
        return None
    s = str(text).upper()
    m = re.search(r'(\d+)', s)
    if m:
        return int(m.group(1))
    return 0 if "GROUND" in s else None


# Blank pages are skipped for free and thumbnails are cheap, so there's no
# reason to cap this tightly. A cap of 80 would have missed a real 100-page
# agreement whose schedules sat on pages 83-89 -- measured, not hypothetical.
DEFAULT_TRIAGE_CAP = 400


def read_agreement(path, filename, api_key=None, model=None,
                   max_triage_pages=DEFAULT_TRIAGE_CAP, verbose_calls=None):
    """
    Returns (records, warnings). One record per flat found.

    Searches pages in priority order and stops at the first page that yields a
    confident answer -- a flat number plus at least one usable area.
    """
    warnings = []
    calls = 0
    client = _client(api_key)
    if client is None:
        return [], [f"'{filename}': AI is off (no API key), so the agreement could not be read."]

    total_pages = page_count(path)
    if not total_pages:
        return [], [f"'{filename}': could not be opened."]

    texts = page_texts(path)
    has_text = any(t.strip() for _, t in texts)

    if has_text:
        candidates = shortlist_pages(texts)
        if not candidates:
            warnings.append(
                f"'{filename}': no page matched any schedule/area marker, so the first "
                f"few pages were tried as a fallback."
            )
            candidates = [(n, 9) for n, _ in texts[:3]]
    else:
        # A scan: locate the schedule visually with cheap thumbnails.
        from .ai_assist import triage_scanned_pages
        if Path(path).suffix.lower() in IMAGE_EXTENSIONS and total_pages == 1:
            candidates = [(1, 0)]
        else:
            pages, triage_warnings = _triage_scan(path, api_key, model,
                                                  max_triage_pages, total_pages)
            warnings.extend(triage_warnings)
            candidates = [(p, 0) for p in pages]
            if not candidates:
                # Triage found nothing -- fall back to the densest pages, which
                # is where schedules actually live, rather than giving up.
                dense = densest_pages(path, top_n=4, max_pages=max_triage_pages)
                if dense:
                    warnings.append(
                        f"'{filename}': visual page triage found no schedule, so the "
                        f"{len(dense)} most text-dense page(s) were read instead "
                        f"(pages {', '.join(str(d) for d in dense)})."
                    )
                    candidates = [(p, 0) for p in dense]
                else:
                    warnings.append(
                        f"'{filename}': it's a scan and no page could be identified as "
                        f"stating the area. Upload a photo of the schedule page instead."
                    )
                    return [], warnings

    for page_no, tier in candidates:
        image_b64, media_type = page_image(path, page_no)
        if image_b64 is None:
            continue
        data = _ask(client, EXTRACTION_PROMPT, max_tokens=2000,
                    image_b64=image_b64, image_media_type=media_type, model=model)
        calls += 1
        if not isinstance(data, dict):
            continue

        records = []
        for flat in (data.get("flats") or []):
            if not isinstance(flat, dict):
                continue
            carpet, balcony, stated_total, others = pick_areas(flat.get("areas"))
            if carpet is None and balcony is None:
                continue
            unit_no = str(flat.get("flat_no")).strip() if flat.get("flat_no") else None
            if not unit_no:
                from_name = re.findall(r'\d{3,4}', filename)
                if not from_name:
                    continue
                unit_no = from_name[0]
                warnings.append(
                    f"'{filename}' page {page_no}: no flat number on the page, so "
                    f"'{unit_no}' was taken from the filename. Check it's the right flat."
                )

            source_text = str(flat.get("source_text") or "")
            primary = carpet or balcony
            verified = _values_present_in_text(primary["value"],
                                               re.sub(r'\s+', '', source_text))
            if not verified:
                warnings.append(
                    f"'{filename}' page {page_no}: the area {primary['value']} is not in "
                    f"the phrase the model quoted, so it's kept but flagged unverified."
                )

            # A stated Total should equal carpet + balcony; if it doesn't, say so
            # rather than quietly preferring one.
            if stated_total and carpet and balcony and \
                    carpet["unit"] == balcony["unit"] == stated_total["unit"]:
                if abs((carpet["value"] + balcony["value"]) - stated_total["value"]) > 0.5:
                    warnings.append(
                        f"'{filename}': stated Total {stated_total['value']} doesn't equal "
                        f"{carpet['value']} carpet + {balcony['value']} balcony "
                        f"({carpet['value'] + balcony['value']:.2f}). Please check."
                    )

            combined = detect_combined_unit(filename, unit_no)
            final_m, includes_balcony, basis = resolve_final_area(
                carpet, balcony, stated_total
            )
            records.append({
                "unit_no": unit_no,
                "tower": _wing_code(flat.get("wing")) or _wing_code(flat.get("building")),
                "combined": combined,
                "final_area_m2": final_m,
                "area_includes_balcony": includes_balcony,
                "area_basis": basis,
                "stated_floor": _floor_from_text(flat.get("floor")),
                "area_value": carpet["value"] if carpet else None,
                "area_unit": (carpet or balcony)["unit"],
                "area_type": carpet["as_printed"] if carpet else (balcony["label"] if balcony else None),
                "balcony_area": balcony["value"] if balcony else None,
                "is_balcony_only": carpet is None,
                "stated_total": stated_total["value"] if stated_total else None,
                "other_areas": others,
                "agreement_date": None,
                "page": page_no,
                "filename": filename,
                "verified": bool(verified),
                "is_photo": not has_text,
                "source_text": source_text or None,
            })

        if records:
            tier_note = {1: "a schedule page", 2: "an area-type page",
                         3: "a floor plan", 4: "a description page",
                         0: "the page image", 9: "a fallback page"}.get(tier, "a page")
            warnings.append(
                f"'{filename}': found on page {page_no} ({tier_note}) after {calls} "
                f"model call(s) — searched {total_pages} pages."
            )
            if verbose_calls is not None:
                verbose_calls.append(calls)
            return records, warnings

    warnings.append(
        f"'{filename}': no flat area could be extracted after trying "
        f"{calls} page(s)."
    )
    return [], warnings


def densest_pages(path, top_n=4, max_pages=DEFAULT_TRIAGE_CAP):
    """
    The most ink-dense pages of a scan, densest first.

    A schedule page is a dense block of text and figures, while the bulk of an
    agreement is ordinary prose and a good fraction is blank. Measured on a
    real 100-page agreement the densest pages were 83-89 -- exactly where the
    schedules were. So when visual triage comes back empty this is a far better
    guess than "the first few pages".
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return []
    suffix = Path(path).suffix.lower()
    scores = []
    try:
        if suffix in (".tif", ".tiff"):
            with Image.open(path) as im:
                for n in range(1, min(getattr(im, "n_frames", 1), max_pages) + 1):
                    try:
                        im.seek(n - 1)
                        arr = np.asarray(im.convert("L").resize((120, 180)))
                        scores.append((float((arr < 128).mean()), n))
                    except Exception:
                        continue
        else:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                for n, page in enumerate(pdf.pages[:max_pages], start=1):
                    try:
                        arr = np.asarray(
                            page.to_image(resolution=40).original.convert("L").resize((120, 180))
                        )
                        scores.append((float((arr < 128).mean()), n))
                    except Exception:
                        continue
    except Exception:
        return []
    scores.sort(key=lambda s: -s[0])
    return [n for _, n in scores[:top_n]]


def _triage_scan(path, api_key, model, max_triage_pages, total_pages):
    """
    Locate the useful pages of a scan by CLASSIFYING every page, rather than
    guessing from ink density -- see classify_scanned_pages for why.
    """
    pages, warnings = classify_scanned_pages(
        path, api_key=api_key, model=model, max_pages=max_triage_pages
    )
    return [page for page, _ in pages], warnings


CLASSIFY_PROMPT = (
    "Each image is one page of a scanned Maharashtra property document set, in "
    "order. They are low-resolution thumbnails -- do NOT try to read numbers. "
    "Classify each page by WHAT KIND OF PAGE it is.\n\n"
    'Return ONLY JSON: {"pages": [{"i": 1, "type": "skip"}, '
    '{"i": 2, "type": "schedule"}]} -- one entry per image, "i" being its '
    "1-based position in THIS batch.\n\n"
    "TYPES:\n"
    "  schedule   SECOND SCHEDULE / THE SCHEDULE ABOVE REFERRED TO / SCHEDULE "
    "OF THE SAID FLAT / DESCRIPTION OF THE SAID PREMISES -- a block of prose "
    "describing the flat.\n"
    "  agreement  the body of a SALE DEED or AGREEMENT FOR SALE: numbered or "
    "lettered clauses of legal prose.\n"
    "  floorplan  an architectural drawing of a flat, with room names and "
    "dimensions and usually boxed area figures.\n"
    "  annexure   ANNEXURE / PROPERTY DETAILS / SPECIFICATION OF FLAT.\n"
    "  skip       anything else.\n\n"
    "Mark as 'skip' -- even though they show area figures, because those are "
    "land, built-up-for-valuation or OLD-property areas:\n"
    "  - valuation sheet (मूल्यांकन पत्रक, 'Valuation ID', rate tables)\n"
    "  - Index II (सूची क्र.2), any e-search / registration extract\n"
    "  - challan, MTR Form, document handling charges receipt\n"
    "  - property card (मालमत्ता पत्रक), municipal or tax receipt\n"
    "  - electricity bill, cheque, share certificate, transfer memorandum\n"
    "  - PAN card, Aadhaar card, photograph/thumbprint pages\n"
    "  - occupation certificate, possession letter, society NOC\n"
    "  - registration receipt / दस्त गोषवारा pages\n\n"
    "A page that is mostly photographs, stamps or signatures is 'skip'. "
    "No other text."
)

# Best first. A floor plan is ranked above the agreement body because its boxed
# figures give carpet, balcony AND total separately, which the prose rarely does.
_TYPE_ORDER = {"schedule": 1, "floorplan": 2, "agreement": 3, "annexure": 4}


def classify_scanned_pages(path, api_key=None, model=None,
                           max_pages=DEFAULT_TRIAGE_CAP, batch_size=8,
                           thumb_edge=520):
    """
    Classify every page of a scan and return the useful ones in priority order.

    This replaces guessing by ink density, which on a real 49-page deed picked
    pages 19, 20, 23 and 44-46 -- share certificates, cheques, ID cards and an
    electricity bill, every one on the ignore list -- and missed both the
    Schedule (p14) and the floor plan (p22). Dark is not the same as
    informative, so the pages are classified instead of measured.
    """
    warnings = []
    client = _client(api_key)
    if client is None:
        return [], warnings

    total = page_count(path)
    if not total:
        return [], warnings
    limit = min(total, max_pages)
    if total > max_pages:
        warnings.append(
            f"This scan has {total} pages; only the first {max_pages} were classified."
        )

    # Blank pages cost nothing to drop and are common in scanned sets.
    candidates = []
    if Path(path).suffix.lower() in (".tif", ".tiff"):
        try:
            from PIL import Image
            import numpy as np
            with Image.open(path) as im:
                for n in range(1, limit + 1):
                    im.seek(n - 1)
                    arr = np.asarray(im.convert("L").resize((120, 180)))
                    if float((arr < 128).mean()) > 0.008:
                        candidates.append(n)
        except Exception:
            candidates = list(range(1, limit + 1))
    else:
        candidates = list(range(1, limit + 1))
    if len(candidates) < limit:
        warnings.append(f"Skipped {limit - len(candidates)} blank page(s) before classifying.")

    found = []
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        images = []
        for n in batch:
            b64, media = page_image(path, n, max_edge=thumb_edge)
            if b64:
                images.append((n, b64, media))
        if not images:
            continue
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": media, "data": b64}}
                   for _, b64, media in images]
        content.append({"type": "text", "text": CLASSIFY_PROMPT})
        try:
            resp = client.messages.create(
                model=model or DEFAULT_MODEL, max_tokens=800,
                messages=[{"role": "user", "content": content}],
            )
            data = _parse_json_block("".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"))
        except Exception:
            data = None
        for item in (data or {}).get("pages", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            idx, kind = item.get("i"), str(item.get("type") or "").strip().lower()
            if not isinstance(idx, int) or not (1 <= idx <= len(images)):
                continue
            if kind in _TYPE_ORDER:
                found.append((_TYPE_ORDER[kind], images[idx - 1][0], kind))

    found.sort(key=lambda f: (f[0], f[1]))
    if found:
        summary = ", ".join(f"p{page} {kind}" for _, page, kind in found[:6])
        warnings.append(f"Page classification found: {summary}.")
    return [(page, tier) for tier, page, _ in found], warnings


def detect_combined_unit(filename, unit_no, known_positions=None):
    """
    Spot an agreement covering TWO flats, from the flat numbers in its filename
    -- e.g. '1503-1603.png' or '1302-1303.png'.

      - SAME position, adjacent floors  -> a DUPLEX (one home over two floors)
      - SAME floor, adjacent positions  -> a JODI (two flats knocked together)

    Returns {"kind", "floors", "positions"} or None. Read from the naming
    convention already in use, so nothing new has to be typed.
    """
    numbers = list(dict.fromkeys(re.findall(r'\d{3,4}', str(filename or ""))))
    if len(numbers) < 2:
        return None

    decoded = []
    for n in numbers[:2]:
        value = int(n)
        floor, position = value // 100, value % 100
        if position == 0:
            return None
        decoded.append((floor, position))

    (f1, p1), (f2, p2) = decoded
    if p1 == p2 and abs(f1 - f2) == 1:
        return {"kind": "duplex", "floors": sorted([f1, f2]), "positions": [p1]}
    if f1 == f2 and abs(p1 - p2) == 1:
        return {"kind": "jodi", "floors": [f1], "positions": sorted([p1, p2])}
    return None


def _wing_code(raw):
    """'A Wing' / 'Tower B' / 'Building No. 13' -> 'A' / 'B' / '13'."""
    if not raw:
        return None
    s = re.sub(r'(?i)\b(wing|tower|building|block|no\.?)\b', ' ', str(raw))
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    return s or None
