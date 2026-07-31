"""
Reads the two evidence documents that fill Sections 3 and 4 of the stack view:

  - THE BUILDER BROCHURE -> Section 3 (series-level areas as printed)
  - AN AGREEMENT PDF     -> Section 4 (one row per agreement, exact area)

Both are AI-assisted, because neither is a table: a brochure states areas
inside floor-plan artwork, and an agreement buries the area in a paragraph of
Marathi/English legalese. Neither yields to regex reliably.

COST CONTROL
============
Agreements run to 40+ pages and brochures to 60+. Sending everything would be
slow and expensive for no gain, so each document is first scanned CHEAPLY for
pages whose text mentions an area at all (keyword shortlist below). Only those
pages go to the model. A 60-page brochure typically shortlists to 3-6 pages.

THE SAME SAFETY RULE AS EVERYWHERE ELSE
=======================================
Every area value the model returns is checked to LITERALLY APPEAR in that
page's text before it is accepted. A value that isn't in the document didn't
come from the document, so it is discarded and reported. For a scanned page
there is no text layer to check against, so those results are accepted but
marked `verified=False` and surfaced as a warning.

The model reads documents. It never decides which source wins, never converts
units, and never computes anything -- all of that stays in final_output.py.
"""

import base64
import io
import re
from pathlib import Path

from .ai_assist import _ask, _client, _values_present_in_text, rasterize_pages

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp"}

# Anthropic resizes anything larger anyway, so downscaling first cuts upload
# time and token cost without losing any legibility.
_MAX_IMAGE_EDGE = 1568

# Pages worth sending. Deliberately broad -- a false positive costs one page
# of tokens, a false negative loses the area entirely.
_BROCHURE_KEYWORDS = [
    "floor plan", "floorplan", "unit plan", "typical floor", "individual floor",
    "carpet area", "rera carpet", "saleable", "built up", "built-up",
    "sq. ft", "sq.ft", "sqft", "चौ", "कार्पेट",
]
_AGREEMENT_KEYWORDS = [
    "carpet area", "rera carpet", "mofa", "built up", "built-up", "admeasuring",
    "area of the said", "कार्पेट", "क्षेत्रफळ", "चौ. फुट", "चौ.फुट", "चौ. मी", "चौ.मी",
]

_MAX_PAGES_TO_SEND = 8


def prepare_image(path):
    """
    A photo -> (base64 JPEG, media_type), downscaled and EXIF-rotated.

    Phone photos arrive 3-12 MB and frequently carry an EXIF rotation flag
    that viewers honour but raw pixel data does not -- so a portrait photo of
    an agreement can reach the model on its side and read badly. Both are
    handled here.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None, None
    if Path(path).suffix.lower() in (".heic", ".heif"):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            return None, None
    try:
        with Image.open(path) as im:
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
    except Exception:
        return None, None


def _page_texts(pdf_path):
    """[(page_number, text), ...]. Empty list if the file can't be opened."""
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


def _shortlist(pages, keywords, limit=_MAX_PAGES_TO_SEND):
    """Pages whose text mentions any keyword, most-mentions first."""
    scored = []
    for page_no, text in pages:
        low = text.lower()
        hits = sum(low.count(k) for k in keywords)
        if hits:
            scored.append((hits, page_no, text))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(p, t) for _, p, t in scored[:limit]]


# ---------------------------------------------------------------------------
# Brochure -> Section 3
# ---------------------------------------------------------------------------

_BROCHURE_PROMPT = (
    "This is a page from an Indian residential developer's brochure. Extract "
    "every unit/flat type whose area is stated on this page.\n\n"
    "Return ONLY a JSON array:\n"
    '[{"unit_type": "3 BHK Palatial", "area_type": "RERA Carpet Area", '
    '"carpet_area": 1096, "carpet_unit": "sq.ft", "balcony_area": 84, '
    '"applicable_floors": "3-16", "series": "Series 2"}, ...]\n\n'
    "Rules:\n"
    "- unit_type is the marketing name as printed (e.g. '3 BHK Palatial', "
    "'4 BHK Signature').\n"
    "- area_type is the wording as printed ('RERA Carpet Area', 'Saleable "
    "Area', 'Built-up Area'). Do NOT normalise it.\n"
    "- carpet_area is the CARPET area only, copied EXACTLY as printed. Never "
    "a saleable/built-up/total figure in that field -- if only a saleable "
    "area is given, put it in carpet_area and say so in area_type.\n"
    "- carpet_unit is 'sq.ft' or 'sq.m'.\n"
    "- balcony_area only if separately stated, else null.\n"
    "- applicable_floors only if the page states which floors this plan "
    "applies to, else null. Do not infer it.\n"
    "- series only if the page names a series/stack, else null.\n"
    "- Do not convert units, round, or compute anything.\n"
    "- If this page states no area, return []. No other text."
)


def extract_brochure(pdf_path, api_key=None, model=None):
    """
    Returns (entries, warnings). Each entry:
        {unit_type, area_type, carpet_area, carpet_unit, balcony_area,
         applicable_floors, series, page, verified}
    """
    warnings = []
    if _client(api_key) is None:
        return [], ["A brochure was uploaded but AI is off (no API key), so it could not be read."]

    pages = _page_texts(pdf_path)
    if not pages:
        return [], ["The brochure PDF could not be opened."]

    shortlist = _shortlist(pages, _BROCHURE_KEYWORDS)
    scanned = not any(t.strip() for _, t in pages)

    if scanned:
        images = rasterize_pages(pdf_path, max_pages=_MAX_PAGES_TO_SEND)
        if not images:
            return [], ["The brochure has no extractable text and could not be rasterized."]
        shortlist = [(i + 1, "") for i in range(len(images))]
    else:
        images = None
        if not shortlist:
            return [], ["No page of the brochure mentioned an area, so Section 3 was left empty."]

    entries, rejected = [], 0
    client = _client(api_key)
    for idx, (page_no, text) in enumerate(shortlist):
        data = _ask(
            client,
            _BROCHURE_PROMPT + ("" if images else "\n\nPAGE TEXT:\n" + text[:12000]),
            max_tokens=3000,
            image_b64=images[idx] if images else None,
            model=model,
        )
        if not isinstance(data, list):
            continue
        text_norm = re.sub(r'\s+', '', text)
        for row in data:
            if not isinstance(row, dict) or row.get("carpet_area") is None:
                continue
            verified = True
            if not images:
                verified = _values_present_in_text(row["carpet_area"], text_norm)
                if not verified:
                    rejected += 1
                    continue
            entries.append({
                "unit_type": row.get("unit_type"),
                "area_type": row.get("area_type"),
                "carpet_area": row.get("carpet_area"),
                "carpet_unit": (row.get("carpet_unit") or "sq.ft"),
                "balcony_area": row.get("balcony_area"),
                "applicable_floors": row.get("applicable_floors"),
                "series": row.get("series"),
                "page": page_no,
                "verified": bool(verified) and not images,
            })

    if rejected:
        warnings.append(
            f"{rejected} brochure area(s) were discarded because the value could not "
            f"be found in that page's text."
        )
    if images and entries:
        warnings.append(
            f"The brochure had no extractable text, so {len(entries)} area(s) were read "
            f"from page images and could NOT be cross-checked. Section 3 is unverified."
        )
    if not entries and not warnings:
        warnings.append("No areas could be extracted from the brochure.")
    return entries, warnings


# ---------------------------------------------------------------------------
# Agreement -> Section 4
# ---------------------------------------------------------------------------

_AGREEMENT_PROMPT = (
    "This is a page from an Indian flat-purchase agreement (Marathi and/or "
    "English). Extract the flat's identity and its stated carpet area.\n\n"
    "Return ONLY a JSON object:\n"
    '{"unit_no": "1201", "tower": "A", "area_value": 1227, '
    '"area_unit": "sq.ft", "area_type": "RERA Carpet Area", '
    '"agreement_date": "2023-06-12"}\n\n'
    "Rules:\n"
    "- area_value is the flat's CARPET area, copied EXACTLY as printed. Not a "
    "built-up, saleable or total figure, and not the balcony or terrace area.\n"
    "- area_unit is 'sq.ft' or 'sq.m' exactly as the document states it "
    "('चौ. फुट' is sq.ft, 'चौ. मीटर' is sq.m).\n"
    "- area_type is the wording as printed -- 'RERA Carpet Area', 'MOFA "
    "Carpet Area', 'बिल्ट अप एरिया'. Do NOT normalise or translate it.\n"
    "- unit_no is the flat/unit number. tower is the wing/tower letter, or "
    "null.\n"
    "- agreement_date in YYYY-MM-DD if stated, else null.\n"
    "- Do not convert units, round, or compute anything.\n"
    "- Use null for anything this page does not state. If the page states no "
    "carpet area at all, return {}. No other text."
)


def extract_agreement(pdf_path, filename, api_key=None, model=None):
    """
    Returns (record, warnings) -- record is None if nothing usable was found.
    Record: {unit_no, tower, area_value, area_unit, area_type,
             agreement_date, page, filename, verified}

    The unit number is read from inside the document (per the chosen
    behaviour), with the filename used only as a cross-check.
    """
    warnings = []
    if _client(api_key) is None:
        return None, [f"'{filename}': AI is off (no API key), so the agreement could not be read."]

    pages = _page_texts(pdf_path)
    if not pages:
        return None, [f"'{filename}': could not be opened as a PDF."]

    scanned = not any(t.strip() for _, t in pages)
    client = _client(api_key)

    if scanned:
        images = rasterize_pages(pdf_path, max_pages=_MAX_PAGES_TO_SEND)
        if not images:
            return None, [f"'{filename}': no extractable text and could not be rasterized."]
        candidates = [(i + 1, "", images[i]) for i in range(len(images))]
    else:
        shortlist = _shortlist(pages, _AGREEMENT_KEYWORDS)
        if not shortlist:
            return None, [f"'{filename}': no page mentioned a carpet area."]
        candidates = [(p, t, None) for p, t in shortlist]

    for page_no, text, image_b64 in candidates:
        data = _ask(
            client,
            _AGREEMENT_PROMPT + ("" if image_b64 else "\n\nPAGE TEXT:\n" + text[:12000]),
            max_tokens=1200,
            image_b64=image_b64,
            model=model,
        )
        if not isinstance(data, dict) or data.get("area_value") is None:
            continue

        verified = True
        if not image_b64:
            verified = _values_present_in_text(data["area_value"], re.sub(r'\s+', '', text))
            if not verified:
                warnings.append(
                    f"'{filename}' page {page_no}: an area was proposed but not found in "
                    f"the page text, so it was discarded."
                )
                continue

        record = {
            "unit_no": str(data.get("unit_no")).strip() if data.get("unit_no") else None,
            "tower": str(data.get("tower")).strip().upper() if data.get("tower") else None,
            "area_value": data["area_value"],
            "area_unit": (data.get("area_unit") or "sq.ft"),
            "area_type": data.get("area_type"),
            "agreement_date": data.get("agreement_date"),
            "page": page_no,
            "filename": filename,
            "verified": bool(verified) and not image_b64,
        }

        if record["unit_no"] is None:
            warnings.append(
                f"'{filename}': an area was found but no unit number, so it could not "
                f"be placed. Rename the file as Tower_UnitNo or check the document."
            )
            return None, warnings

        # Filename cross-check only -- the document is authoritative.
        digits_in_name = re.findall(r'\d{3,4}', filename)
        doc_digits = re.sub(r'\D', '', record["unit_no"])
        if digits_in_name and doc_digits and doc_digits not in digits_in_name:
            warnings.append(
                f"'{filename}': the document says unit {record['unit_no']} but the "
                f"filename suggests {digits_in_name[0]}. Used the document."
            )
        if image_b64:
            warnings.append(
                f"'{filename}': read from a page image (no text layer) -- area is "
                f"unverified, please spot-check."
            )
        return record, warnings

    return None, warnings + [f"'{filename}': no carpet area could be extracted."]


_PHOTO_PROMPT = (
    "This is a photo or screenshot of one page of an Indian flat-purchase "
    "agreement (Marathi and/or English), taken because this page states the "
    "flat's carpet area.\n\n"
    "Return ONLY a JSON object:\n"
    '{"unit_no": "1201", "tower": "A", "area_value": 1227, '
    '"area_unit": "sq.ft", "area_type": "RERA Carpet Area", '
    '"balcony_area": 84, "agreement_date": "2023-06-12", '
    '"source_text": "admeasuring 1227 sq. ft. RERA Carpet Area"}\n\n'
    "Rules:\n"
    "- area_value is the flat's CARPET area, copied EXACTLY as printed. Not a "
    "built-up, saleable or total figure, and not the balcony or terrace area.\n"
    "- balcony_area is the balcony/terrace/deck area if the page states one, "
    "else null. Never put a balcony figure in area_value.\n"
    "- If this page shows ONLY a balcony/terrace area and no carpet area, set "
    "area_value to null and fill balcony_area -- do not substitute one for the "
    "other.\n"
    "- area_unit is 'sq.ft' or 'sq.m' as stated ('चौ. फुट' is sq.ft, "
    "'चौ. मीटर' is sq.m).\n"
    "- area_type is the wording as printed. Do NOT normalise or translate it.\n"
    "- source_text MUST be the exact phrase from the image that contains the "
    "area, transcribed character for character, including the number. This is "
    "how a human checks your reading against the photo, so do not paraphrase "
    "it or tidy it up.\n"
    "- Do not convert units, round, or compute anything.\n"
    "- Use null for anything the image does not show. If you cannot read the "
    "area clearly, return {} rather than guessing. No other text."
)


def extract_agreement_from_image(image_path, filename, api_key=None, model=None):
    """
    Read an agreement area from a PHOTO of the relevant page.

    A photo has no text layer, so the usual verification -- checking the value
    literally appears in the page text -- is impossible. Two things stand in
    for it:

      1. The model must transcribe the exact phrase it read the area from
         (`source_text`). That phrase goes into Section 4's Notes, so a
         reviewer can compare one short line against the photo instead of
         re-reading the whole page.
      2. The value must appear inside that transcribed phrase. If the model
         reports 1227 but its own quoted phrase doesn't contain 1227, the two
         halves of its answer disagree and the result is rejected.

    Records from this path are always marked verified=False, so Final Output
    flags them as unverified.
    """
    warnings = []
    client = _client(api_key)
    if client is None:
        return None, [f"'{filename}': AI is off (no API key), so the photo could not be read."]

    image_b64, media_type = prepare_image(image_path)
    if image_b64 is None:
        return None, [
            f"'{filename}': could not be opened as an image. HEIC photos need "
            f"'pillow-heif' installed (it's in requirements.txt) -- or re-save as JPEG."
        ]

    if not isinstance(data := _ask(client, _PHOTO_PROMPT, max_tokens=1200,
                                   image_b64=image_b64, image_media_type=media_type,
                                   model=model), dict):
        return None, [f"'{filename}': nothing could be read from this photo."]

    # A photo of a balcony page has no carpet area on it, and that is a valid
    # reading -- it still belongs in Section 4, and its figure is still the
    # best balcony source. Rejecting it for having no carpet area threw away
    # evidence the user deliberately supplied.
    carpet, balcony = data.get("area_value"), data.get("balcony_area")
    if carpet is None and balcony is None:
        return None, [f"'{filename}': no carpet or balcony area could be read from this photo."]

    primary = carpet if carpet is not None else balcony
    source_text = str(data.get("source_text") or "")
    if not _values_present_in_text(primary, re.sub(r'\s+', '', source_text)):
        return None, [
            f"'{filename}': discarded -- the area reported ({primary}) does not appear in "
            f"the text the model quoted from the photo (\"{source_text[:80]}\"), so the "
            f"reading is not self-consistent."
        ]

    unit_no = str(data.get("unit_no")).strip() if data.get("unit_no") else None
    if not unit_no:
        # The photo may crop out the unit number even though it shows the area.
        from_name = re.findall(r'\d{3,4}', filename)
        if from_name:
            unit_no = from_name[0]
            warnings.append(
                f"'{filename}': the photo doesn't show a unit number, so '{unit_no}' was "
                f"taken from the filename instead. Check it is the right flat."
            )
        else:
            return None, warnings + [
                f"'{filename}': an area was read but no unit number, in the photo or the "
                f"filename, so it could not be placed. Rename the file as Tower_UnitNo."
            ]

    warnings.append(
        f"'{filename}': read from a photo, so the area could not be cross-checked "
        f"against a text layer. The quoted phrase is in Section 4's Notes."
    )
    area_type = data.get("area_type")
    if carpet is None:
        # Balcony-only page: label it honestly so nothing downstream can
        # mistake this figure for a carpet area.
        area_type = area_type or "Balcony Area"

    return {
        "unit_no": unit_no,
        "tower": str(data.get("tower")).strip().upper() if data.get("tower") else None,
        "area_value": carpet,
        "area_unit": (data.get("area_unit") or "sq.ft"),
        "area_type": area_type,
        "balcony_area": balcony,
        "is_balcony_only": carpet is None,
        "agreement_date": data.get("agreement_date"),
        "page": None,
        "filename": filename,
        "verified": False,
        "is_photo": True,
        "source_text": source_text or None,
    }, warnings


def extract_agreements(files, api_key=None, model=None):
    """
    files: [(path, display_filename), ...]. PDFs, multi-page TIFFs and single
    photos are all accepted.

    Each file is searched in priority order (schedule pages first) and stops at
    the first page that answers -- see core/agreement.py. One file can yield
    several records if the agreement covers several flats.

    Records are then merged by unit number: a carpet photo and a balcony photo
    of the same flat combine, while two genuinely different areas for one flat
    are both reported rather than silently resolved.
    """
    from .agreement import read_agreement

    records, warnings = [], []
    for path, name in files:
        found, warns = read_agreement(path, name, api_key=api_key, model=model)
        warnings.extend(warns)
        records.extend(found)

    by_unit, merged = {}, []
    for rec in records:
        key = re.sub(r'\D', '', str(rec.get("unit_no") or "")) or rec.get("unit_no")
        if key not in by_unit:
            by_unit[key] = rec
            merged.append(rec)
            continue
        kept = by_unit[key]

        # A carpet reading and a balcony-only reading of the same flat are
        # complementary, not conflicting.
        # Don't fold a balcony reading into a figure that is already a total --
        # a deed's "1240 sq.ft RERA Carpet" IS carpet+balcony, so adding a
        # floor plan's 13.99 sq.m balcony to it would double-count.
        if kept.get("area_includes_balcony") or rec.get("area_includes_balcony"):
            if kept.get("final_area_m2") and not rec.get("final_area_m2"):
                continue
        if kept.get("is_balcony_only") != rec.get("is_balcony_only"):
            carpet_rec = rec if kept.get("is_balcony_only") else kept
            balcony_rec = kept if kept.get("is_balcony_only") else rec
            carpet_rec["balcony_area"] = carpet_rec.get("balcony_area") \
                or balcony_rec.get("balcony_area") or balcony_rec.get("area_value")
            carpet_rec["balcony_evidence"] = balcony_rec.get("filename")
            if carpet_rec is not kept:
                merged[merged.index(kept)] = carpet_rec
                by_unit[key] = carpet_rec
            continue

        same = str(kept.get("area_value")) == str(rec.get("area_value")) and \
            str(kept.get("area_unit")) == str(rec.get("area_unit"))
        if same:
            continue
        warnings.append(
            f"Unit {rec.get('unit_no')}: two sources give different areas -- "
            f"'{kept.get('filename')}' says {kept.get('area_value')} {kept.get('area_unit')}, "
            f"'{rec.get('filename')}' says {rec.get('area_value')} {rec.get('area_unit')}. "
            f"Please check which is correct."
        )
        if (rec.get("agreement_date") or "") > (kept.get("agreement_date") or ""):
            merged[merged.index(kept)] = rec
            by_unit[key] = rec
    return merged, warnings
