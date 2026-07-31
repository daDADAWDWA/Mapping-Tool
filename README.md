# Stack View Generator

Turns a **Transaction CSV** + **Inventory Excel** into a filled-in Stack View
`.xlsx`, for any project, using only the project's template file — no code
changes needed to add a new tower.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

This opens in your browser at `http://localhost:8501`, running entirely on
your own machine. (When you're ready to host it for a team, the same app
runs unchanged on a Streamlit server / Docker container — nothing here is
tied to being local.)

## Adding a new project

**Single-tower project:** In the app sidebar → "Add a new project or tower" →
give it a name, leave "Tower letter" blank, upload that project's blank
template CSV. That's it — the app reads the floor list and Series-column
layout directly out of the template file every time, so a 40-floor/6-series
tower and a 12-floor/3-series tower both just work.

**Multi-tower project (Tower A, Tower B, ...):** Same panel, but fill in the
tower letter (A, B, C...) and upload *that tower's* template CSV. Repeat with
the same project name for each additional tower. Towers within one project
can have completely different floor counts / Series layouts — each has its
own template file:
```
/projects/<ProjectName>/
    template.csv        (optional -- shared default shape)
    A/template.csv       (optional -- only needed if Tower A's layout differs)
    B/template.csv
```
When you generate, the app reads the Tower/Wing column from your Transaction
CSV and Inventory Excel and **splits automatically the moment it sees more
than one tower value in the data** — you don't need to set up subfolders at
all if every tower shares the same layout. Each tower found gets its own
sheet: if it has its own subfolder template, that's used; otherwise it falls
back to the shared `template.csv` at the project root. You only need to add
a tower-specific subfolder for a tower whose layout genuinely differs from
the default. A tower value with neither a dedicated template nor a root
fallback is skipped with a clear warning — not an error.

(You can also just create these folders/files directly on disk instead of
through the sidebar, if you prefer.)

## Handling a new file format (different column names)

Column matching is case/spacing-insensitive already (`"Unit No"`, `"unitNo"`,
`"UNIT_NO"` all match the same thing). But if a data export uses a genuinely
different, ambiguous name for something (e.g. the unit number is in a column
called `property` instead of anything resembling "Unit No"), the app will
**not** guess — it'll flag it in a warning rather than risk silently
mismatching data. To fix that, drop a `column_aliases.json` next to the
relevant `template.csv` (project-level or tower-level):

```json
{
  "unit_no": ["property"],
  "tower": ["towerOrWing"]
}
```

Valid keys: `unit_no`, `description`, `tower`, `carpet_area`,
`flat_no_inventory`, `registration_year`, `registration_date`. No code
changes needed — this is exactly the "add a mapping file, not code" system
from the original brief.

## What it currently does

- **Section 1 (CRE stack)** — filled from the Transaction CSV.
  - Ignores the `Area` / `Area Type` columns entirely; extracts the carpet
    area value + unit (sq.ft / sq.m) from the free-text description field
    (regex first, optional Claude API fallback for anything regex can't
    parse — only used if you provide an API key).
  - **Each number is classified from the words around it** — carpet, balcony
    or an explicit total. A real description states all three:
    `सदनिकेचे क्षेत्र 1562 चौ फुट रेरा कार्पेट व बाल्कनी क्षेत्र 128 चौ फुट
    अशाप्रकारे सदनिकेचे एकूण क्षेत्र 1690 चौ फुट`. Picking "the number
    nearest the word कार्पेट" grabbed **1690** (the total also has कार्पेट
    after it) and then added the balcony again, giving 1818. A label *before*
    the number is the reliable signal (`एकूण क्षेत्र 1690`,
    `बाल्कनी क्षेत्र 128`); only when there is none does the wording after it
    decide.
  - **A balcony is added only if the text hasn't already totalled it.** Where
    a total is written out, that total is used as-is; otherwise the cell holds
    `carpet + balcony`. Either way the Notes column shows the arithmetic
    (`1562 carpet + 128 balcony = stated total 1690 sq.ft`), and if a stated
    total doesn't equal carpet + balcony that mismatch is reported rather than
    silently preferred. Balcony wording is matched in Marathi and English (बाल्कनी,
    गॅलरी, टेरेस, balcony, terrace, deck), and the figure also populates
    Balcony Area in Final Output. The same number can never be counted
    twice: the span already taken as the carpet area is excluded.
  - If a flat has more than one transaction, the most recent one wins.
- **Section 2 (MahaRERA stack)** — filled from the Inventory file, which can
  be **either an Excel file (Flat No + Carpet Area) or a MahaRERA-style
  "Sold/Booked Inventory" disclosure PDF** (Circular 29 format). For a PDF:
  - Tables are detected and extracted automatically. This works on a genuine
    digital PDF; for a borderless layout or a scanned one, the optional AI
    assist below can reconstruct the table instead
  - **Two table shapes are supported.** The usual one-row-per-flat
    disclosure, and a **floor × flat matrix** — rows labelled `1ST FLOOR`,
    `2ND FLOOR`... and columns `FLAT NO.1`, `FLAT NO.2`... with the area in
    each cell. A matrix carries no flat numbers at all, so each cell is
    converted to this app's own numbering (`floor * 100 + position`), which
    means a ground-floor flat still decodes correctly (`001` → floor 0,
    position 1). Cells shown as `-----` are treated as "no flat there", and
    totals rows (`TOTAL FLATS = 61 NOs`) are skipped with a warning rather
    than mistaken for a floor
  - Header row only needs to appear once; later pages continuing the same
    table are handled automatically
  - If a flat has two area rows (a common pattern — a small ancillary/
    exclusive area disclosed alongside the main flat area), **both are
    summed into one total carpet area** for that flat
  - Any status (SOLD, UNSOLD, REHAB, RESERVED, MORTGAGED, NOT FOR SALE...)
    counts as the flat existing, same as SOLD
  - This grid is the "ground truth" for **which floor/series cells are real
    units at all** — but *not* for their area. See the reconciliation rules
    below: a registered transaction always outranks the builder's disclosure
    on the area value itself.
- **Floor/Series decoding**: strips any tower-letter prefix (e.g. "A-303"),
  then `floor = number // 100`, `position = number % 100`. Position is a
  1-based index into the template's Series columns **left to right**, not
  matched against the "Series N" label text (towers commonly have their
  columns out of numeric order).
- **The legend is painted and the notes quote it.** A template exported as
  CSV lists each colour in words but carries no actual fills, so a reader is
  told a colour exists without being shown it. Every legend swatch is now
  filled with the colour it names, and the Notes / Anomaly column uses the
  legend's **exact wording** rather than a paraphrase — so a cell reads
  `Series 1: Txn present, area not in Marathi text`, matching the legend
  line above it word for word. Both come from one place (`core/colors.py`),
  so changing the wording there changes it in both. The one colour this app
  added that isn't in the printed legend, `Agreement differs from CRE`, is
  appended to the legend block at run time using a free slot.
- **Automatic anomaly highlighting + tally**, computed fresh every run:
  - **Grey — no CRE transaction.** There is no transaction row for this flat
    at all, but the builder inventory shows the unit exists.
  - **Pink — transaction exists, area not in the text.** A transaction *was*
    found, but no carpet area could be read out of its Marathi/English
    description (neither pattern matching nor the AI fallback could find
    one). Someone has to open the agreement for this one. These two cases
    are counted separately in the Tally row.
  - **Gold — area differs** from the majority value within its Series column
  - The Tally row's "No-txn" and "Area missing" counts are written
    automatically. **Refuge floors / Duplex / Fake Jodi are intentionally
    left untouched for now** — not computed yet.
- Everything else in the template (society header, checklist, legend, the
  Unit Type row, Sections 3–6) passes through completely untouched.
- **Leftover values in the stack grids are cleared, not inherited.** Real
  templates are usually saved from a part-finished working file, so they
  arrive with old areas still sitting in Section 1/Section 2. Those cells
  are blanked before this run's data is written, so last project's numbers
  can never survive into a new output wherever the new data happens to be
  empty. Only the two stack grids and the Area Type row are cleared —
  nothing else in the file is touched.
- **Never crashes on missing/bad data** — unmatched or malformed rows are
  skipped and reported in the UI, not thrown as errors.

### Third sheet: "Final Output" (NocoDB hand-off)

Every run also produces a **"Final Output"** sheet — matching the reference
hand-off schema, with one row per **distinct area found within each Series
per Tower** (not just one row per series):

- A Series can legitimately have more than one area across its floors (e.g.
  floor 1 is a smaller unit, floor 2 a mid-size unit, floors 3–15 the
  standard size) — these become **separate rows**, each scoped to exactly
  the floors sharing that area. Floor ranges can be non-contiguous (e.g.
  `4-6,8-15` when a floor has no data for that series).
- **Near-duplicate collapsing**: values within **5 sq ft** of each other
  (e.g. 525 and 528) are treated as the same area — measurement noise, not
  a real difference. The exact reading appearing on **more floors** wins;
  ties go to the larger value.
- **CRE (the registered transaction) is the authoritative source for the
  area.** The builder's RERA inventory is the weaker source — it decides
  which units exist and acts as a cross-check, but it never overwrites a
  transaction area. Where a transaction area exists, that is the number
  written to Final Carpet, full stop.
- RERA is checked **per row, scoped to that row's own floors only** — not
  the whole series, and **floor by floor**, so one contradicting floor in an
  otherwise-agreeing row still gets caught rather than outvoted. The CRE
  value is still what's written; the row is flagged
  `"Needs Review — CRE/RERA differ (CRE used) on floor(s) 6,15"`, naming
  exactly which floors to check.
- Floors where RERA shows a unit but **no usable transaction exists** get
  their own RERA-sourced rows — otherwise unsold or unregistered units
  would disappear from the hand-off entirely. If such a gap matches an
  existing CRE row's area within tolerance it folds into that row instead
  of duplicating it: this sheet is one row per *distinct area*, not one row
  per source.
- CRE descriptions may state sq.ft **or** sq.m; both are normalised to m²
  before anything is compared or clustered. Final Carpet (ft²) is always
  just that row's m² figure converted, never an independently-reported
  number.
- **Area Type** comes from the CRE-extracted descriptor for CRE-sourced
  rows, falling back to `"RERA Carpet"` where no descriptor could be
  classified or where the row is RERA-sourced.
- Columns with no available data source yet (Unit Type, Bathroom count,
  Floor Offset, Exit Direction, OC Status, Map Link, Evidence Ref, Carpet
  Area, Finalized By/Date) are left blank — Balcony Area is always `—`,
  Mapping Status defaults to `In Progress` unless flagged above.

## Every description is read, automatically

There is no button. On generate, each row's `property_description` is read for:

- **unit number** — by search priority: `Flat No.` / `Apartment Bearing No.` /
  `Unit No.`, then the municipal address, then an assessment number
  (`1641/451/TOWER-4` → `451`). Survey, PID, parking, floor and document
  numbers are excluded. `Flat No. FLAT NO 451` yields `451`, not `FLAT`.
- **tower number and name** — `TOWER-4(DAFFODIL)` → 4, DAFFODIL;
  `Tower 9 IRIS` → 9, IRIS; `Elm Tower 5` → 5, ELM.
- **floor** — as stated. Where two are given ("Seventh Floor (Eighth Floor as
  referred in the sanctioned plan)"), the first is used.
- **every area, with its type.**

The structured columns fill only what the description didn't state, because in
a real export `unit_number` held `122` where the text said `No.6122`, and the
`wing` column held `5TH` — a floor.

### Any area basis is accepted, and always labelled

Carpet is preferred where it exists, then Built-up, Super Built-up, Saleable.
Bengaluru deeds are written on **super built-up**, so insisting on carpet left
43 of 47 rows empty. On that file the result is 47/47 — Super Built-up 42,
Carpet 4, Built-up 1 — and the `Area Type →` row shows the real basis per
series.

**Never accepted as the flat's area:** UDS / undivided share, land or owner
share, parking, garden, common and special-private areas, CTS, survey, plot,
road. `1082.29 Square feet UDS` was previously written into the stack as a
carpet area — a confidently wrong number, which is worse than a blank.

Three bugs found while getting this right, each of which produced wrong values
rather than missing ones:

- **`udi` matched inside "incl-udi-ng".** Short abbreviations were matching as
  substrings of ordinary words, and "including" appears in nearly every legal
  description, so those rows' areas were all filed as land shares. Keywords
  now match whole words only.
- **Labels leaked between phrases.** In `SBA of 2666 sq ft With 1082.29 sq ft
  of UDS`, the UDS figure saw the earlier `SBA` and was labelled super
  built-up. The look-back now stops at the previous number.
- **`2666 Sq.Ft. (247.68 Sq.Mtr)` counted twice.** One area restated in
  another unit. Fixing it also surfaced a real carpet figure nested inside an
  SBA phrase: `which includes Carpet Area measuring 1866.137 Sq. Ft.`

### The numbering convention is inferred, never hardcoded

Two conventions occur and they disagree completely:

| Rule | Example |
|---|---|
| `floor × 100 + position` | `601` → floor 6, position 1 (Maharashtra) |
| `tower + floor + unit` | `451` → tower 4, floor 5, unit 1 (Bengaluru) |

Each candidate is tested against the floors the descriptions state. On the
Embassy export tower+floor+unit fitted **35/38** and the alternative **2/38**;
on a Maharashtra export the reverse. Add `numbering.json` to a project folder
(`{"rule": "tower_floor_unit"}`) to override the choice. Applying the wrong
rule puts every value in the wrong cell while the output still looks
plausible, which is exactly why it isn't guessed.

Towers found in the descriptions also drive the **sheet split** — the Embassy
file produces 9 tower sheets with no tower column present at all.

## Cleaning the transaction CSV first (optional)

A transaction export's structured columns are frequently wrong while the
free-text `property_description` beside them is right. From a real file:

| column says | description says |
|---|---|
| `unit_number` = `122` | `apartment bearing No.6122` |
| `unit_number` = `FLAT` | `...Apartment bearing No.733...` |
| `wing` = `5TH` | (that's a floor, not a wing) |
| `carpet_area` = empty on 21 of 47 rows | — |

So the **description is read first**, with AI, and the columns fill only what
it didn't state. Press *Clean this CSV with AI* and you get a tidy file with
`Unit No`, `Wing`, `Tower`, `Floor`, `Carpet Area`, `Carpet Unit`,
`Balcony Area`, `Total Area`, `Area Type`, plus a `Source` column saying where
each number came from, `Verified`, per-row `Notes`, and the original text kept
for audit.

**It runs once.** The cleaned file carries a canonical one-line description per
row, so it can be reviewed, hand-corrected, kept, and re-run through the
generator with **no AI at all** — ordinary pattern matching reads it exactly.
The messy reading happens once; everything after is repeatable.

**Every number is checked against its own row's text.** A value the model
produced that isn't literally in the description is dropped, and the row falls
back to pattern matching, then to the structured columns — each labelled in
`Source`, so an unverified number is never mistaken for a confirmed one.

**Column values are validated, not copied.** `5TH` is rejected as a wing,
`FLAT` as a unit number, `1526/162` becomes `162`. Blind copying produced rows
reading `Flat No 174, 2, 5TH Wing`.

**Super built-up is never relabelled as carpet.** Where a description states
only a super built-up or saleable area — 21 of those 47 rows — the row is left
without a carpet area and says what it does have. Super built-up includes
walls, balconies and a share of lobbies, so substituting it would inflate a
flat by 40–50%.

## Deciding WHICH area type a transaction figure is

The registration text gives a number; whether it's RERA carpet, MOFA carpet,
built-up or saleable decides whether it can be compared with anything else —
and the text doesn't always say. Three sources, strongest first
(`core/area_type.py`):

1. **Agreement value match.** The agreement states both an area and its type.
   If the transaction's number matches the agreement's number within
   tolerance, the transaction's number **is** that type — proved by the
   arithmetic, not inferred from wording.
2. **The description's own wording.** Decent evidence alone; near-certain when
   it agrees with the agreement. When it *contradicts* the agreement the
   agreement wins and the disagreement is reported, because one of the two
   documents is describing a different measurement.
3. **Series inference.** Once a type is confirmed for a value in a series,
   other flats in that series with the same value inherit it. This is how one
   downloaded agreement labels a whole column — exactly what the template
   checklist means by "every unique area/type has agreement evidence".

Each assignment carries a confidence, the `Area Type →` row shows the
best-supported label per series, and any series resting only on wording gets
a warning telling you to download an agreement for it. Nothing here ever
changes an area **value** — it only labels one.

## AI assist (optional)

### The API key lives in a file, not the UI

There is no key field on screen. The key is read from disk so it never
appears in a browser form and never has to be re-typed. Resolution order,
first hit wins:

1. `ANTHROPIC_API_KEY` environment variable
2. **Streamlit secrets** — for hosted apps (see below)
3. `config.json` next to `app.py` — `{"api_key": "sk-ant-...", "model": "..."}`
4. `.env` next to `app.py` — `ANTHROPIC_API_KEY=sk-ant-...`

The sidebar lists all four and which one was found, so a deployment that can't
see its key can be diagnosed without guessing.

### Hosting it

**Streamlit Community Cloud** (`share.streamlit.io`) — point it at the repo,
branch `main`, main file `app.py`. Then **Manage app → Settings → Secrets**:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```

Secrets live in `st.secrets`, which `os.environ` never sees — that is why a
hosted app reported "no API key found" however correctly the secret was set.

**Render / Railway** work too, with start command:
`streamlit run app.py --server.port $PORT --server.address 0.0.0.0`

**Vercel cannot host this.** It expects a serverless function exporting an
`app`/`handler` object; Streamlit is a long-running server holding a WebSocket
per browser tab, so there is nothing to export and no edit to `app.py` fixes it.

**The filesystem is ephemeral when hosted.** A project added through the
sidebar is written to `projects/` and disappears on the next restart. Commit
each project's `template.csv` to the repo instead — that is why
`Gurukrupa_Vyom` and `Shelton_Elite` survive restarts.

The sidebar reports which source was used and the key's **last four
characters only**, so you can tell which key is loaded without exposing it.
Restart the app after editing the file.

A key pasted with stray quotes, spaces or a trailing newline is cleaned
automatically — that copy-paste slip otherwise surfaces as a confusing 401.

**Both files hold a live credential in plain text.** They're in
`.gitignore` already; don't commit them and don't hand the folder to someone
else with the key still in it. If it leaks, revoke it at
console.anthropic.com rather than editing it out and hoping.

### Choosing the model

The sidebar has a model picker whose options are **fetched live from your
account** rather than hardcoded — a hardcoded list goes stale, offering
models that no longer exist while hiding ones that do. Pick a different model
for a single run, or press "Make this the default" to write it into
`config.json`. If the list can't be fetched the field falls back to free-text
entry.

The choice applies to every AI call in that run, and there is no model name
hardcoded anywhere else in the codebase — `core/config.py` holds the single
fallback.

### The three switches

All three require a key. With no key they do nothing and the app runs
entirely on pattern matching.

| Switch | Covers | Fallback without AI |
|---|---|---|
| Read the area out of hard transaction descriptions | Section 1 | regex handles most rows |
| Parse the inventory PDF + decode unit numbers | Section 2 | keyword column matching |
| Read the brochure and agreements | Sections 3 & 4 | **none — these need AI** |

The third one has no deterministic fallback: a brochure states its areas
inside floor-plan artwork and an agreement buries them in legalese, so
neither is a table. Untick it and an uploaded brochure/agreement is ignored,
with a warning saying so rather than silently empty sections.

**1. "Read the area out of hard transaction descriptions"**
Only touches rows where the regex found no carpet area in the
Marathi/English description, or found the number but couldn't classify the
descriptor. Counted in the "Areas read by AI" metric.

**2. "Parse the inventory PDF + decode unit numbers"**
Three separate assists:

- **Column-NAME matching for CSV/Excel.** Aliases only match names already
  listed, so an export calling the unit number `Unit`, `property` or `Flat ID`
  used to be a dead end — the app refuses to guess between ambiguous names,
  correctly, since a wrong guess silently attaches one flat's area to another.
  AI now resolves the name instead: it sees the real column names *and* sample
  values, and its answer is validated against the actual column list, so a
  hallucinated column is dropped. The warning names what it matched and gives
  you the `column_aliases.json` snippet to make it permanent and skip the call
  next time.
- **Column mapping in PDFs.** Before any value is read, the detected header mapping
  is checked. This exists because keyword matching picks the *first* column
  containing "area" — so a disclosure laid out as
  `Flat No | Balcony Area | Carpet Area` silently returns every balcony
  area as though it were the carpet area. That failure is invisible in the
  output: the numbers look plausible, they're just the wrong column. Any
  correction is reported as a warning naming the old and new column.
- **Unit-number decoding.** Identifiers the `floor*100+position` rule can't
  handle are batched to AI with this tower's real floor list and series
  count as context: `G-04`, `PH-2`, `1601/1602` (jodi), `Shop 5`, `B-004`.
  Counted in the "Unit nos decoded by AI" metric. A decoded floor that
  isn't in the template, or a position beyond the tower's series count, is
  **rejected** and reported as undecodable rather than written somewhere
  wrong.
- **Table rescue.** If no table structure is found at all, rows are
  reconstructed from the page text; if there's no text either (a scanned
  disclosure), pages are rasterized and read as images, one call per page.

  A fully scanned disclosure is the **least reliable** input this app takes,
  so that path is also the loudest:
  - the row count for **every page** is reported (`p1: 0, p2: 36, p3: 30`),
    so a page that read nothing is visible immediately
  - a page returning nothing is **retried at higher resolution** before being
    given up on
  - the **SR NO column is used as an integrity check** — it numbers every row,
    so a gap proves data was lost and the warning names the missing serial
    numbers exactly
  - if the PDF has more pages than the limit, that's a warning rather than
    silent truncation

  For a scan, an Excel file of `Flat No` + `Carpet Area` is still far more
  reliable than any amount of image reading.

### What keeps this trustworthy

- **The model is never the source of an area number when the PDF's own text
  can be.** In the normal path it returns *column indices only* — the values
  are then read out of the extracted table by our own code. A hallucinated
  digit cannot become a carpet area because the model is never asked to
  produce one.
- In the text-rescue path it does have to read values, so **every area it
  returns is checked to literally appear in the page text**. A value that
  fails is **kept, not discarded** — it's written to the cell, highlighted
  in light orange (`Read by AI, unverified` in the legend), noted in the
  Notes column, counted in a warning, and flagged in that row's Mapping
  Status. A figure you can see and check beats a silently empty cell; what
  matters is that nothing unverified is ever presented as confirmed.
- The image path has no text layer to check against, so rows from it are
  accepted but the warning says explicitly that Section 2 is unverified.
- Anything AI touched shows up in the metrics or the warnings, so a run is
  always auditable.
- Every AI call is wrapped to degrade to "no result" on any failure.

### What it still won't do

- Decide areas, anomalies, tallies or the Final Output reconciliation —
  those stay fully deterministic.
- Guess when it isn't confident: the decoder is instructed to omit rather
  than guess, because an omission gets reported for manual review while a
  wrong guess silently corrupts a unit.

## Sections 3 and 4: brochure and agreements

Two optional uploads. Both need an API key, since neither document is a
table — a brochure states areas inside floor-plan artwork and an agreement
buries them in legalese.

These are the third switch, "Read the brochure and agreements".

**Brochure → Section 3**, one row per series. Pages are shortlisted cheaply
by keyword (*floor plan*, *individual floor plan*, *carpet area*, *चौ*) and
only those go to the model, so a 60-page brochure typically costs 3–6 pages
of tokens. Plans are matched to series through the Unit Type labels already
in your template's `Unit Type →` row, and one plan can legitimately map to
several series — Series 1 and Series 6 are both "4 BHK Signature".

**Agreements → Section 4**, one row per file, uploaded many at a time. Two
kinds of file are accepted:

#### How a long agreement is searched

A 200-page agreement states the area in one or two places; the rest is
boilerplate. Pages are searched in PRIORITY ORDER and the search stops at the
first page that yields a confident answer (flat number + at least one usable
area):

| Priority | Pages |
|---|---|
| 1 | `SECOND SCHEDULE`, `THE SCHEDULE ABOVE REFERRED TO`, `SCHEDULE OF THE SAID FLAT`, `DESCRIPTION OF THE SAID PREMISES` |
| 2 | `AGREEMENT FOR SALE` / `SALE DEED` clause prose — "admeasuring...", "Flat No...", "Residential premises bearing..." |
| 3 | floor plans (`FLOOR PLAN`, `UNIT PLAN`, `TYPICAL FLOOR PLAN`, `LAYOUT PLAN`) |
| 4 | annexures — `ANNEXURE`, `PROPERTY DETAILS`, `SPECIFICATION OF FLAT` |
| 5 | last resort: any page naming a flat or an area basis |

**Pages that are never read**, because their area figures are land, built-up
for valuation, or the OLD property being surrendered: valuation sheet
(मूल्यांकन पत्रक), Index II (सूची क्र.2), challan / MTR Form, document
handling receipt, property card (मालमत्ता पत्रक), municipal or tax receipt,
electricity bill, cheque, share certificate, transfer memorandum, PAN/Aadhaar
card, photograph or thumbprint pages, occupation certificate, possession
letter, society NOC, registration receipt. On a real 49-page deed, 30 of the
pages were one of these.

**For a text-based PDF this costs nothing.** All 200 pages are scanned for
those markers with pypdfium2 in about 0.4 seconds and zero tokens — measured
at 0.41s for 200 pages, against 32s for the same file with pdfplumber. Only
the winning page is sent, so a normal agreement is **one model call**.

**For a scan** (including a multi-page TIFF, which is how agreements often
arrive — one real file was a 100-frame TIFF) there is no text to search, so:
- blank pages are skipped first, for free — 45 of 100 in that real file
- the rest go out as cheap low-resolution thumbnails, 8 per call, to locate
  the schedule; the search stops after a few hits
- only the winning page is then read at full resolution
- **pages are classified, not measured.** Each thumbnail batch comes back
  labelled `schedule` / `floorplan` / `agreement` / `annexure` / `skip`, and
  only the useful ones are read at full resolution, schedule first.

  Ink density was the previous fallback and it was actively wrong: on a real
  49-page deed the darkest pages were 19, 20, 23 and 44–46 — share
  certificates, cheques, ID cards and an electricity bill, every one on the
  ignore list — while it missed both the Schedule (p14) and the floor plan
  (p22). Dark is not the same as informative. With classification the same
  file goes straight to p14 in 7 cheap calls plus one extraction.

#### What gets extracted

Flat/unit number, wing/tower/building, floor, and **every** area stated, each
with its type as printed: RERA Carpet, Carpet Area, MOFA Carpet, Built-up,
Saleable, Super Built-up, Balcony, Dry Balcony, Terrace, Total. Several flats
in one agreement produce several records.

**The old-flat trap.** A redevelopment deed describes two flats: the old one
surrendered (e.g. `13/253`, 467.73 sq.ft) and the new one allotted. Only the
new flat is returned — taking the old area would silently shrink the unit by
more than half.

**One figure "equivalent to" another.** Deeds routinely write
`1180.00 Sq. Ft. (as per MOFA Carpet) equivalent to 1240 sq. ft. (as per RERA
Carpet)`. Both are captured; RERA wins as the carpet and MOFA is recorded
alongside.

**A "carpet" figure that is really a total.** In that same deed the floor plan
breaks the flat down as `RERA CARPET 101.22` + `BALCONY 13.99` =
`TOTAL 115.21 sq.m` — and 1240 sq.ft *is* 115.20 sq.m. So the deed's "RERA
Carpet" already includes the balcony. Any carpet figure that equals a stated
total, or equals carpet + balcony from another reading, is treated as a total
and the balcony is **not** added again. Both routes to that flat now give
115.2 m², matching the verified hand-off.

Carpet preference is RERA Carpet → MOFA Carpet → plain Carpet. Built-up,
Super Built-up and Saleable are recorded but **never used as the carpet
area** — they measure something larger. Balcony and dry balcony are summed
into the balcony figure. Where a `Total Area` is also stated, it's checked
against carpet + balcony and any mismatch is reported rather than one being
quietly preferred.

**Never extracted as a flat area:** Land Area, CTS Area, Survey Area, Plot
Area, Parking Area or dimensions, Open Space, Garden Area, common/amenity
areas, Stamp Duty, Registration Charges, Consideration Amount. This is
enforced both in the prompt and in code, because confusing a land area with a
flat area is the most damaging mistake this extraction could make.

- **A photo or screenshot of just the page stating the area** — the cheapest
  and fastest option, since nothing has to be searched. JPEG, PNG, WEBP, TIFF
  and iPhone HEIC all work. Photos are EXIF-rotated (so a portrait photo
  isn't read sideways) and downscaled to a 1568 px long edge before sending,
  which typically takes a 4 MB phone photo down to ~15 KB with no loss of
  legibility.
- **A full agreement PDF** — pages are keyword-shortlisted, then only the
  ones mentioning an area are sent.

The unit number is read from inside the document; the filename is only a
cross-check, and a mismatch is reported. For a photo that crops the unit
number out, the filename is used as a fallback and flagged.

Several photos of one flat are fine — results are merged by unit number. Two
files giving one flat two *different* areas are both reported rather than
silently resolved.

### Verifying a photo

A photo has no text layer, so the usual check — does this value literally
appear in the document text — is impossible. Two things stand in for it:

1. The model must transcribe **the exact phrase it read the area from**. That
   phrase goes into Section 4's Notes (`reads: "admeasuring 1250 sq. ft. RERA
   Carpet Area"`), so checking a reading means comparing one short line
   against the photo rather than re-reading the page. The filename also goes
   into the `Area-text screenshot` column, since the photo *is* that evidence.
2. The value must appear **inside that transcribed phrase**. If the model
   reports 1250 but its own quote says 1227, the two halves of its answer
   disagree and the reading is discarded, not used.

Photo-sourced rows are always marked unverified in Final Output. Each row records the area type
**verbatim as worded** in the agreement, the exact value, the floor, the
series, and the filename plus page in Notes. If you upload more agreements
than the template has rows, the section grows and Sections 5–6 shift down
rather than being overwritten.

### Source priority

Final Output resolves **per floor**, not per series:

    1. Agreement   2. CRE transaction   3. Builder inventory   4. Brochure

An agreement applies to **its own flat only** — it does not propagate across
the series. Combined with the tolerance that means an agreement matching its
neighbours within tolerance just merges into their row, and only one that
genuinely differs splits off a row of its own. Whatever a lower-priority
source said is never silently dropped: Mapping Status names the source and
the exact floors, e.g. `Needs Review — CRE, RERA, Brochure differ on
floor(s) 12`. `Evidence Ref` carries the agreement filename and page.

A change of measurement **basis** is flagged separately (RERA carpet vs MOFA
carpet vs built-up), because that matters more than a couple of square feet.
Balcony Area now comes from the best source that actually states one, which
is independent of which source won the carpet area.

### What Section 1 shows on a conflict

Section 1 is defined as "carpet area exactly as in the CRE Marathi text", so
**the CRE number stays there**. A conflicting agreement highlights the cell
(purple) and writes the detail into Notes:
`Agreement says 1250 sq.ft (CRE 1227 sq.ft) — agreement used in Final Output`.
Only Final Output switches to the agreement's figure.

### Tolerance is now a setting

The "same area" tolerance is a field in the UI, defaulting to 5 sq ft. It
applies uniformly to every source including agreements — a 2 sq ft
difference merges, a 20 sq ft difference splits and is flagged.

### Guard rails

- Every area the model returns is checked to **literally appear in that
  page's text** before being accepted; anything else is discarded and
  counted in a warning.
- A scanned document has no text layer to check against, so its values are
  accepted but the row is marked `read from a page image, unverified`.
- **A brochure that doesn't state which floors its plan covers is not spread
  across the whole tower.** It's limited to floors another source has shown
  to be real units; if there are none, the row is emitted with no floor
  range and flagged `brochure only; no floors stated`.
- Two agreements for one flat: both listed in Section 4 as evidence, the
  later-dated one used in Final Output.
- **A photo showing only a balcony area is valid evidence, not a failure.** It
  gets its own Section 4 row labelled `Balcony Area`, and its figure is used
  as the balcony — never as the carpet area, since an 8 sq m balcony in place
  of a 273 sq m flat would be a serious error. A carpet photo and a balcony
  photo of the same flat merge into one complete record.
- An agreement whose stated tower doesn't match the sheet is **applied anyway
  when the project has only one tower**, with a warning — a wing letter read
  off a document ("B") rarely equals a tower value derived from an inventory
  file ("SKYVISTASBLUEZ"), and dropping it silently lost evidence.
- An agreement whose unit number can't be placed on the grid is reported,
  not guessed onto a cell.

## The four sheets

Every run produces one workbook with four sheets, all viewable **on screen**
in a tab per sheet as well as downloadable.

**Stack View** — values and legend colours only. The `Notes / Anomaly`
columns are deliberately left empty: the grid is for reading data, and all
commentary now lives in the Review Tracker where it can be sorted and closed
off.

**Final Output** — the NocoDB hand-off (see above).

**Agreements to Download** — what to fetch, and why. One agreement per
*unique area per series*, not one per flat, which is what the template's
checklist means by "every unique area/type has agreement evidence" — the
difference between a handful of documents and hundreds. Each row names a
specific flat to download, and the flat chosen is a **mid-stack** floor that
actually has a registered transaction: first and top floors are the most
likely to be atypical (setbacks, terraces), so a middle flat is better
evidence for the group. Where no floor in the group has a registration, that
is said plainly instead of naming a flat whose agreement can't be obtained.
Priority is High when 5+ flats share the unconfirmed area, Medium for 2–4,
Low for one. With AI on, the "Why it's needed" line is rewritten in one
batched call — cosmetic only, so a failure leaves the plain wording.

**Review Tracker** — the reviewer's own tracker layout, one row per issue,
sorted by severity. Fed by everything the run noticed:

| Issue Type | Severity |
|---|---|
| Agreement vs CRE, Stated total mismatch, CRE vs RERA, Unverified AI reading | High |
| Area differs within series, Area not in Marathi text, No CRE transaction, Area type unconfirmed, Brochure only | Medium |
| Undecodable unit number, No data | Low |

`Raised By`, `Resolution / Action Taken`, `Date Sent for Review` and
`Reviewed Date` are left empty — those are the reviewer's to fill, and
guessing them would be worse than blank.

## Validated against a human-verified sheet

`tests/test_gurukrupa_final_output.py` reproduces the verified Gurukrupa Vyom
hand-off from the CRE / RERA / agreement figures that produced it:

```bash
python tests/test_gurukrupa_final_output.py
```

It pins down the four things that were previously wrong:

1. **An agreement's balcony must be added, as CRE's already is.** Agreements
   state RERA carpet *excluding* balcony. Because agreements outrank CRE,
   forgetting this pulled whole rows ~10 m² low and split off phantom extra
   rows. Every wrong value in the first run was off by exactly the balcony
   printed in the next column.
2. **Cluster representatives follow source priority.** Support is counted
   *per priority level*, so a value backed by 1 agreement + 6 CRE floors beats
   one backed by 1 agreement + 7 RERA floors. Comparing raw floor counts let
   Section 2 override Section 1 — the exact opposite of the priority chain.
3. **Duplex agreements collapse to one row.** A file named `1503-1603`
   (same position, adjacent floors) becomes floors `15/16` with the two areas
   summed; `1302-1303` (same floor, adjacent positions) is a jodi and is left
   alone. The convention is read from your own filenames.
4. **Hand-off formatting matches the verified sheet**: 17 columns, Series
   `01`, Tower `Standalone`, `115.21 m²`, `1,240 ft²` rounded to whole feet,
   rows ordered by floor, and series with no data omitted instead of written
   blank. Balcony Area is left empty because it is already inside Final
   Carpet — the breakdown stays visible in the Section 1 notes.

Unit Type is filled **only from the brochure**. The template's `Unit Type →`
row holds illustrative labels, so copying them would state something
unverified as fact.

## Known limitations / things to revisit

- The highlight colors live in `core/colors.py` — swap in different hex
  codes there any time and both the cells and the legend swatches follow.
  Duplex, Refuge floor, Fake Jodi and Agreement-to-download are defined and
  get legend swatches (the legend documents the whole scheme), but nothing
  applies them to cells yet.
- AI never *replaces* the deterministic path, it only fills its gaps — so a
  missing key, a network failure or a malformed reply degrades to the
  pattern-matched result and can never fail a run.
- **Merged cells / original template formatting are not reproduced** — same
  reason, that information doesn't exist in a CSV. If you'd rather work from
  the real `.xlsx` template (with its actual formatting) instead of a CSV
  export, this can be upgraded to preserve it properly.
- Refuge floor / Duplex / Fake Jodi highlighting, and Sections 5–6, are not
  automated yet.
- The template's own pre-filled Section 3 rows are **not** used as a brochure
  source — only an uploaded brochure is. If you'd rather the app read the
  brochure figures already typed into the template, that's a small change.
- Agreements are read per run and not stored. Re-upload them next time, or
  say the word and they can be kept under `projects/<name>/agreements/`.
- **The gold "area differs within series" highlight is still unit-blind and
  exact-match.** It compares raw numbers, so a row stating sq.m among sq.ft
  rows gets flagged even when it is the same area, and 525 vs 528 gets
  flagged even though the Final Output sheet treats those as identical.
  Left as-is deliberately for now — say the word and it can use the same
  normalisation + 5 sq ft tolerance as the hand-off sheet.

## Project structure

```
app.py                  Streamlit UI
config.json             API key + default model (gitignored -- edit this, not the UI)
core/
  decode.py              Flat/unit number -> (floor, position); tower-letter normalization
  template_model.py       Parses template structure (auto-detects floor/series layout)
  aliases.py              Flexible column-name matching + column_aliases.json override loader
  area_extract.py         Regex + Claude API area/unit extraction from description text
  inventory_pdf.py        Extracts tower/flat/carpet-area data from inventory disclosure PDFs
  ai_assist.py            Optional AI: PDF column mapping, unit-number decoding, table rescue
  doc_extract.py          Reads brochure (Section 3) and agreement (Section 4) PDFs
  final_output.py         Builds the "Final Output" NocoDB hand-off sheet (cross-source reconciliation)
  cleaning.py             Input cleaning (whitespace, dupes, type coercion)
  colors.py               Editable anomaly highlight colors + legend wiring
  agreement.py            Priority-tiered agreement search (schedule pages first)
  area_type.py            Cross-validates area TYPE: agreement match -> description -> series
  review.py               Review Tracker + Agreements-to-Download sheets
  txn_normalise.py        Unit/tower extraction by search priority; optional CSV cleaning
  numbering.py            Infers the unit-numbering convention from the data
  config.py               Loads the API key/model from config.json, .env or the environment
  pipeline.py             Orchestrates everything; one sheet per tower in multi-tower projects
projects/
  Gurukrupa_Vyom/template.csv                (included -- 40 floors, 6 series in order)
  Shelton_Elite/template.csv                 (included -- 40 floors, 6 series out of order)
  <ProjectName>/template.csv                 (single-tower project)
  <ProjectName>/<TowerLetter>/template.csv   (multi-tower project, one folder per tower)
```
