"""
Color scheme for anomaly highlighting in the output .xlsx.

These are the ACTUAL legend colors (confirmed from the project's real
legend reference), not placeholders. Currently computed and applied:
No-txn, Area-type-missing, Area-mismatch. The rest (Duplex, Refuge floor,
Fake Jodi, Agreement-to-be-downloaded) are defined here now so they're
ready the moment those features get built -- not yet applied anywhere.
"""

# openpyxl PatternFill expects ARGB hex strings, no "#".
COLOR_NO_TXN = "FFD9D9D9"              # grey   - "No CRE transaction"
COLOR_AREA_TYPE_MISSING = "FFF4CCCC"   # pink   - "Txn present, area not in Marathi text"
COLOR_AREA_MISMATCH = "FFFFD966"       # gold   - "Area differs within same series"

# Defined for future use -- not yet computed/applied by the pipeline.
COLOR_DUPLEX_UNIT = "FF9FC5E8"         # light blue - "Duplex unit"
COLOR_REFUGE_FLOOR = "FFEA4335"        # red        - "Refuge floor"
COLOR_FAKE_JODI = "FF3C78D8"           # blue       - "Fake Jodi unit"
COLOR_AGREEMENT_TO_DOWNLOAD = "FF00FF00"  # green   - "Agreement to be downloaded"

HEADER_FILL = "FFD9D9D9"   # light grey for header rows
HEADER_FONT_BOLD = True

DEFAULT_FONT_NAME = "Arial"


# Agreement conflicts with the CRE transaction for the same flat. The CRE
# value stays in Section 1 (that section is defined as CRE data); this fill
# marks that a higher-priority agreement disagrees, and Final Output uses the
# agreement's number.
COLOR_AGREEMENT_CONFLICT = "FFB4A7D6"   # purple - "Agreement differs from CRE"

# ---------------------------------------------------------------------------
# Legend wiring
# ---------------------------------------------------------------------------
# The template's LEGEND block names each colour in words but, coming from a
# CSV, carries no actual fills -- so a reader can't tell which colour means
# what. These map the legend's OWN wording to the fills the app applies, so
# the swatches get painted and the note text matches the legend exactly
# rather than paraphrasing it.

LEGEND_COLORS = {
    "no cre transaction": COLOR_NO_TXN,
    "txn present, area not in marathi text": COLOR_AREA_TYPE_MISSING,
    "area differs within same series": COLOR_AREA_MISMATCH,
    "duplex unit": COLOR_DUPLEX_UNIT,
    "refuge floor": COLOR_REFUGE_FLOOR,
    "fake jodi unit": COLOR_FAKE_JODI,
    "agreement to be downloaded": COLOR_AGREEMENT_TO_DOWNLOAD,
}

# Wording used both in the Notes / Anomaly column and in the legend, so the
# two always agree. Change it here and both follow.
NOTE_NO_TXN = "No CRE transaction"
NOTE_AREA_TYPE_MISSING = "Txn present, area not in Marathi text"
NOTE_AREA_MISMATCH = "Area differs within same series"
NOTE_AGREEMENT_CONFLICT = "Agreement differs from CRE"

# Not in the template's printed legend (this app added it), so it gets added
# to the legend block at run time.
COLOR_UNVERIFIED = "FFFCE5CD"           # light orange - "Read by AI, unverified"
NOTE_UNVERIFIED = "Read by AI, unverified"

EXTRA_LEGEND_ENTRIES = [
    (NOTE_AGREEMENT_CONFLICT, COLOR_AGREEMENT_CONFLICT),
    (NOTE_UNVERIFIED, COLOR_UNVERIFIED),
]
