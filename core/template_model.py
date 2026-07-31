"""
Parses a Stack View template's *structure* directly out of the template file
itself -- floor list, series-column positions, where the tally/notes cells
live -- so a brand-new project just needs a template file dropped in, with NO
separate per-project config to maintain.

This works because every template follows the same section skeleton:
    SECTION 1 · CRE DATA — STACK VIEW
        <description row>
        FLOOR, <series col>, <series col>, ..., Notes / Anomaly
        Unit Type →, ...
        Area Type →, ...
        <floor number rows...>
        Tally →, No-txn, <n>, Area missing, <n>, Refuge floors, <n>

    SECTION 2 · MahaRERA INVENTORY — STACK VIEW
        (same shape as Section 1)

    SECTION 3 · ...
"""

import csv
import re


def _norm_label(s):
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


class TemplateSection:
    def __init__(self, header_row, unit_type_row, area_type_row,
                 floor_rows, tally_row, series_cols, notes_col):
        self.header_row = header_row          # row index of "FLOOR,..." row
        self.unit_type_row = unit_type_row
        self.area_type_row = area_type_row
        self.floor_rows = floor_rows          # {floor_number: row_index}
        self.tally_row = tally_row            # row index of "Tally ->" row, or None
        self.series_cols = series_cols        # [col_index, ...] in left-to-right order
        self.notes_col = notes_col            # col index of "Notes / Anomaly", or None

    @property
    def num_series(self):
        return len(self.series_cols)


class TableSection:
    """A plain list-style section (Sections 3-6): a header row of column
    labels, then a block of rows available for data."""

    def __init__(self, header_row, cols, first_data_row, last_data_row):
        self.header_row = header_row
        self.cols = cols                    # {normalized label: col_index}
        self.first_data_row = first_data_row
        self.last_data_row = last_data_row  # inclusive

    @property
    def capacity(self):
        return max(0, self.last_data_row - self.first_data_row + 1)

    def col(self, *label_fragments):
        """Column index for the first label containing any of these
        fragments -- so 'Down-loaded?' still matches 'loaded'."""
        for frag in label_fragments:
            frag = _norm_label(frag)
            for label, idx in self.cols.items():
                if frag and frag in label:
                    return idx
        return None


class TemplateModel:
    def __init__(self, rows):
        self.rows = rows  # raw grid: list of list of str
        self.section1 = None
        self.section2 = None
        self.section3 = None   # builder brochure (series-level)
        self.section4 = None   # agreements (one row per agreement)
        self._parse()

    @classmethod
    def from_csv(cls, path):
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            rows = [list(row) for row in reader]
        return cls(rows)

    def _find_row(self, needle, start=0, end=None):
        end = end if end is not None else len(self.rows)
        needle = needle.lower()
        for i in range(start, end):
            for cell in self.rows[i]:
                if cell and needle in cell.lower():
                    return i
        return None

    def _parse_section(self, section_start, section_end):
        header_row = None
        for i in range(section_start, section_end):
            row = self.rows[i]
            if row and row[0].strip().upper() == "FLOOR":
                header_row = i
                break
        if header_row is None:
            return None

        header = self.rows[header_row]

        # Series columns: consecutive non-empty cells starting at col 1,
        # stopping at the "Notes / Anomaly" column (or first empty cell).
        series_cols = []
        notes_col = None
        for c in range(1, len(header)):
            cell = header[c].strip() if header[c] else ""
            if not cell:
                continue
            if "notes" in cell.lower() or "anomaly" in cell.lower():
                notes_col = c
                break
            series_cols.append(c)

        unit_type_row = header_row + 1
        area_type_row = header_row + 2

        # Floor rows: consecutive rows below area_type_row whose col0 is a
        # valid integer, until we hit a non-numeric row (the Tally row).
        floor_rows = {}
        r = area_type_row + 1
        while r < section_end:
            row = self.rows[r]
            col0 = row[0].strip() if row and row[0] else ""
            if not col0:
                r += 1
                continue
            try:
                floor_num = int(col0)
            except ValueError:
                break
            floor_rows[floor_num] = r
            r += 1

        tally_row = None
        if r < section_end and self.rows[r] and "tally" in (self.rows[r][0] or "").lower():
            tally_row = r

        return TemplateSection(
            header_row=header_row,
            unit_type_row=unit_type_row,
            area_type_row=area_type_row,
            floor_rows=floor_rows,
            tally_row=tally_row,
            series_cols=series_cols,
            notes_col=notes_col,
        )

    def _parse(self):
        s1_start = self._find_row("SECTION 1")
        s2_start = self._find_row("SECTION 2")
        s3_start = self._find_row("SECTION 3")

        if s1_start is None or s2_start is None:
            raise ValueError(
                "Could not find 'SECTION 1' and 'SECTION 2' markers in the "
                "template. This parser expects the standard Stack View "
                "section layout."
            )

        section3_or_end = s3_start if s3_start is not None else len(self.rows)

        self.section1 = self._parse_section(s1_start, s2_start)
        self.section2 = self._parse_section(s2_start, section3_or_end)

        if self.section1 is None:
            raise ValueError("Could not parse Section 1's stack grid (no 'FLOOR' header row found).")
        if self.section2 is None:
            raise ValueError("Could not parse Section 2's stack grid (no 'FLOOR' header row found).")

        # Sections 3 and 4 are optional -- a template without them simply
        # can't take brochure/agreement data, which is not an error.
        self.section3 = self._parse_table_section(s3_start, "series")
        s4_start = self._find_row("SECTION 4")
        self.section4 = self._parse_table_section(s4_start, "#")

    def _parse_table_section(self, section_start, header_first_cell):
        """Locate a list-style section by its marker row and the first cell of
        its header row (e.g. 'Series' for Section 3, '#' for Section 4)."""
        if section_start is None:
            return None
        wanted = _norm_label(header_first_cell)
        header_row = None
        for i in range(section_start, len(self.rows)):
            row = self.rows[i]
            if not row:
                continue
            if _norm_label(row[0]) == wanted:
                header_row = i
                break
            # Don't run past this section into the next one.
            if i > section_start and row[0] and "section" in row[0].lower():
                return None
        if header_row is None:
            return None

        cols = {}
        for c, cell in enumerate(self.rows[header_row]):
            label = _norm_label(cell)
            if label and label not in cols:
                cols[label] = c

        # Data block runs until the next SECTION marker (or end of file).
        last = len(self.rows) - 1
        for i in range(header_row + 1, len(self.rows)):
            row = self.rows[i]
            if row and row[0] and "section" in row[0].lower():
                last = i - 1
                break
        return TableSection(header_row, cols, header_row + 1, last)

    def tally_value_col(self, section: TemplateSection, label: str):
        """Find the column immediately after the cell containing `label` in
        the tally row, where the numeric count should be written."""
        if section.tally_row is None:
            return None
        row = self.rows[section.tally_row]
        for c, cell in enumerate(row):
            if cell and label.lower() in cell.lower():
                return c + 1
        return None
