"""
This script evaluates generated regulatory-change annotations against the
official locations listed in the initial "RIEPILOGO DEGLI AGGIORNAMENTI".

Optionally, the script can also compute a page-level metric. In that case,
predicted pages are taken from diff hunk target-line numbers and the
line-to-page map. Expected page ranges are taken from the target PDF outline
/bookmarks. The PDF outline is used only to map already-extracted riepilogo
locations to page ranges; it is not used as a source of expected affected
locations.

For cumulative/non-consecutive comparisons, for example 48 -> 51, the expected
locations are the union of the riepilogo entries for updates 49, 50 and 51.

The evaluation is location-based only. It checks whether generated annotations
fall inside officially affected locations. It does not judge whether the
semantic interpretation or classification of each annotation is correct.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

from dataclasses import dataclass
from pathlib import Path


GRANULARITY = "chapter"

PARTS = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
    "prima": "1",
    "seconda": "2",
    "terza": "3",
    "quarta": "4",
    "quinta": "5",
    "sesta": "6",
    "settima": "7",
    "ottava": "8",
    "nona": "9",
    "decima": "10",
}

PART_WORDS = r"prima|seconda|terza|quarta|quinta|sesta|settima|ottava|nona|decima|viii|vii|vi|iv|iii|ii|ix|x|v|i|\d+"
SUFFIX_WORDS = r"bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies"
SUFFIX = rf"(?:\s*-?\s*(?:{SUFFIX_WORDS}))?"
ROMAN_OR_NUM = rf"(?:\d+|[ivxlcdm]+){SUFFIX}"
ANNEX_ID = rf"(?:\d+|[ivxlcdm]+|[a-z]{{1,3}}){SUFFIX}(?![a-z0-9])"
DOT_ID = r"(?:\d+|[ivxlcdm]+)(?![a-z0-9])"

RE_RIEPILOGO = re.compile(r"\briepilogo\s+degli\s+aggiornamenti\b", re.I)
RE_UPDATE_HEADING = re.compile(
    r"^\s*#{0,6}\s*\**\s*(\d+)\s*°\s*aggiornamento\s+(?:del|dell['’]?|della|dei|degli)\s*",
    re.I,
)

RE_BODY_START = re.compile(
    rf"^\s*#{{0,6}}\s*\**\s*("
    rf"parte\s+(?:{PART_WORDS})\s*[-–—]|"
    rf"titolo\s+(?:{ROMAN_OR_NUM})\s*[-–—]|"
    rf"capitolo\s+(?:{ROMAN_OR_NUM})\s*[-–—]|"
    rf"sezione\s+(?:{ROMAN_OR_NUM})\s*[-–—]"
    rf")",
    re.I,
)


def is_body_start_line(line: str) -> bool:
    """
    Detect the first real body heading after the initial riepilogo.

    This intentionally does NOT treat riepilogo summary lines such as
    "Disposizioni introduttive. Modificate pagine..." as body headings.
    """
    line = clean(line)
    if not line:
        return False

    nline = norm(line)
    if nline in {"premessa", "disposizioni introduttive"}:
        return True

    return bool(RE_BODY_START.match(line))

RE_PART = re.compile(rf"\bparte\s+({PART_WORDS})\b", re.I)
RE_TITLE = re.compile(rf"\btitolo\s+({ROMAN_OR_NUM})\b", re.I)
RE_CHAPTER = re.compile(rf"\b(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b", re.I)
RE_SECTION_LIST = re.compile(
    rf"\b(?:sez\.|sezione|sezioni)\s+((?:{ROMAN_OR_NUM})(?:\s*(?:,|e|ed|and|&)\s*(?:{ROMAN_OR_NUM}))*)",
    re.I,
)
RE_ANNEX_LIST = re.compile(
    rf"\b(?:allegato|allegati)\s+((?:{ANNEX_ID})(?:\s*(?:,|e|ed|and|&)\s*(?:{ANNEX_ID}|allegato\s+{ANNEX_ID}))*)",
    re.I,
)

# Page-style references sometimes appear in older riepilogo entries:
# Parte Prima.IV.1.2  -> part 1, title iv, chapter 1
# Parte Seconda.10.1  -> part 2, chapter 10
RE_DOT_LOCATION = re.compile(
    rf"\bparte\s+({PART_WORDS})\s*\.\s*({DOT_ID})(?:\s*\.\s*({DOT_ID}))?",
    re.I,
)

FRONT_LOCATIONS = {
    "disposizioni introduttive": "front:disposizioni_introduttive",
    "sigle e abbreviazioni": "front:sigle_e_abbreviazioni",
    "ambito di applicazione": "front:ambito_di_applicazione",
    "definizioni": "front:definizioni",
}

IGNORE_FRONT_LOCATIONS = {
    "indice",
    "premessa",
    "sommario",
}

EXCLUDED_GENERATED_TERMS = {
    "indice",
    "riepilogo",
    "sommario",
    "summary",
    "table of contents",
    "document navigation",
    "elenco aggiornamenti",
}

BASELINE_CHANGE_TYPE_TO_OPERATION = {
    "addition": "create",
    "deletion": "delete",
    "modification": "update",
    "renumbering": "update",
    "relocation": "update",
    "other": "update",
}

STRUCTURAL_STOP_WORDS = {
    "parte",
    "titolo",
    "capitolo",
    "cap",
    "sez",
    "sezione",
    "sezioni",
    "par",
    "paragrafo",
    "comma",
    "art",
    "articolo",
}

NOISE_IDS = {
    "un",
    "una",
    "il",
    "lo",
    "la",
    "gli",
    "le",
    "i",
    "nuovo",
    "nuova",
    "nuovi",
    "nuove",
    "modificato",
    "modificata",
    "modificati",
    "modificate",
    "aggiunto",
    "aggiunta",
    "aggiunti",
    "aggiunte",
    "inserito",
    "inserita",
    "inseriti",
    "inserite",
    "eliminato",
    "eliminata",
    "eliminati",
    "eliminate",
}


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text))
        if not unicodedata.combining(ch)
    )


def clean(text: str) -> str:
    text = str(text).strip()
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = text.replace("*", "").replace("`", "")
    text = text.replace("_", " ")
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def norm(text: str) -> str:
    text = strip_accents(clean(text))
    text = text.replace("°", "").replace("ª", "").replace("º", "")
    text = re.sub(r"\s*-\s*", "-", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def value(text: str | None) -> str | None:
    if not text:
        return None

    out = strip_accents(str(text)).lower()
    out = out.replace("°", "").replace("ª", "").replace("º", "")
    out = re.sub(r"\s*-\s*", "-", out)
    out = re.sub(rf"^([a-z0-9ivxlcdm]+)\s+({SUFFIX_WORDS})$", r"\1-\2", out.strip())
    out = re.sub(r"\s+|\.", "", out)
    return out or None


def part_value(text: str | None) -> str | None:
    out = value(text)
    return PARTS.get(out, out) if out else None


def format_value(text: str | None, annex: bool = False) -> str:
    if not text:
        return ""

    if "-" in text:
        head, suffix = text.split("-", 1)
        head = head.upper() if annex or head.isalpha() else head
        return f"{head}-{suffix.lower()}"

    return text.upper() if text.isalpha() else text


def contains_any(text: str, terms: set[str]) -> bool:
    ntext = norm(text)
    return any(term in ntext for term in terms)


@dataclass(frozen=True)
class Location:
    part: str | None = None
    title: str | None = None
    chapter: str | None = None
    section: str | None = None
    annex: str | None = None
    front: str | None = None

    def normalized(self) -> "Location":
        return Location(
            part_value(self.part),
            value(self.title),
            value(self.chapter),
            value(self.section),
            value(self.annex),
            self.front,
        )

    def base_key(self) -> str | None:
        loc = self.normalized()

        if loc.front:
            return loc.front

        if not loc.part:
            return None

        parts = [f"part:{loc.part}"]

        if loc.title:
            parts.append(f"title:{loc.title}")

        if loc.chapter:
            parts.append(f"chapter:{loc.chapter}")

        return "|".join(parts) if loc.chapter else None

    def key(self) -> str | None:
        loc = self.normalized()
        base = loc.base_key()

        if loc.front:
            return loc.front

        if loc.annex:
            return f"{base}|annex:{loc.annex}" if base else f"annex:{loc.annex}"

        if GRANULARITY == "section" and base and loc.section:
            return f"{base}|section:{loc.section}"

        return base

    def label(self) -> str:
        loc = self.normalized()

        if loc.front:
            return loc.front.replace("front:", "").replace("_", " ").title()

        parts = []

        if loc.part:
            parts.append(f"Parte {loc.part}")

        if loc.title:
            parts.append(f"Titolo {format_value(loc.title)}")

        if loc.chapter:
            parts.append(f"Capitolo {format_value(loc.chapter)}")

        if loc.annex:
            parts.append(f"Allegato {format_value(loc.annex, annex=True)}")
        elif GRANULARITY == "section" and loc.section:
            parts.append(f"Sezione {format_value(loc.section)}")

        return ", ".join(parts) or "Unknown location"


def key_parts(key: str) -> dict[str, str]:
    return dict(piece.split(":", 1) for piece in key.split("|") if ":" in piece)


def title_is_ambiguous(da: dict[str, str], db: dict[str, str]) -> bool:
    """
    Treat omitted titles as ambiguous when both sides identify the same
    part/chapter. Circ. 285 repeats chapter numbers under different titles.
    """
    if not (da.get("part") and db.get("part") and da.get("chapter") and db.get("chapter")):
        return False

    if da["part"] != db["part"] or da["chapter"] != db["chapter"]:
        return False

    return bool(da.get("title")) != bool(db.get("title"))


def overlap(a: str, b: str) -> bool:
    if a == b:
        return True

    # Front matter locations should match only exactly.
    if a.startswith("front:") or b.startswith("front:"):
        return False

    da, db = key_parts(a), key_parts(b)

    for level in ["part", "title", "chapter", "section", "annex"]:
        if da.get(level) and db.get(level) and da[level] != db[level]:
            return False

    if title_is_ambiguous(da, db):
        return False

    return any(level in da and level in db for level in ["part", "annex"])


def add_title_mapping(
    out: dict[tuple[str, str], set[str]],
    part: str | None,
    title: str | None,
    chapter: str | None,
) -> None:
    part = part_value(part)
    title = value(title)
    chapter = value(chapter)

    if part and title and chapter:
        out.setdefault((part, chapter), set()).add(title)


def comparable_title(text: str) -> str:
    text = norm(text)
    text = re.sub(r"^\s*[0-9ivxlcdm]+\s*[\.\)-]\s*", "", text)
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def add_chapter_name_mapping(
    out: dict[tuple[str, str, str], set[str]],
    part: str | None,
    title: str | None,
    chapter: str | None,
    chapter_name: str | None,
) -> None:
    part = part_value(part)
    title = value(title)
    chapter = value(chapter)
    comparable = comparable_title(chapter_name or "")

    if part and title and chapter and comparable:
        out.setdefault((part, chapter, comparable), set()).add(title)


def document_part_chapter_titles(path: Path) -> dict[tuple[str, str], set[str]]:
    """
    Infer unambiguous Part/Chapter -> Title mappings from the selected full text.

    Some riepilogo entries omit "Titolo" after writing "Parte Prima" once, while
    annotations often use the fully qualified Part/Title/Chapter path. We keep
    titleless matching strict, but canonicalize official titleless locations when
    the selected markdown itself gives a single clear title for that chapter.
    """
    mapping: dict[tuple[str, str], set[str]] = {}
    current_part: str | None = None
    current_title: str | None = None

    inline_ref_re = re.compile(
        rf"\bparte\s+({PART_WORDS})\b\s*,?\s*"
        rf"(?:tit\.?|titolo)\s+({ROMAN_OR_NUM})\b\s*,?\s*"
        rf"(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b",
        re.I,
    )
    title_re = re.compile(rf"\btitolo\s+({ROMAN_OR_NUM})\b", re.I)
    chapter_re = re.compile(rf"\b(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b", re.I)
    part_re = re.compile(rf"\bparte\s+({PART_WORDS})\b", re.I)

    for raw_line in read_lines(path):
        line = norm(raw_line)

        for match in inline_ref_re.finditer(line):
            add_title_mapping(mapping, match.group(1), match.group(2), match.group(3))

        if not raw_line.lstrip().startswith("#"):
            continue

        part_match = part_re.search(line)
        if part_match:
            current_part = part_value(part_match.group(1))
            current_title = None

        title_match = title_re.search(line)
        if title_match:
            current_title = value(title_match.group(1))

        chapter_match = chapter_re.search(line)
        if chapter_match:
            add_title_mapping(
                mapping,
                current_part,
                current_title,
                chapter_match.group(1),
            )

    return mapping


def document_chapter_name_titles(path: Path) -> dict[tuple[str, str, str], set[str]]:
    mapping: dict[tuple[str, str, str], set[str]] = {}
    lines = read_lines(path)
    current_part: str | None = None
    current_title: str | None = None
    named_ref_re = re.compile(
        rf"(?P<name>[^()\n]{{3,180}}?)\(\s*"
        rf"parte\s+({PART_WORDS})\b\s*,?\s*"
        rf"(?:tit\.?|titolo)\s+({ROMAN_OR_NUM})\b\s*,?\s*"
        rf"(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b\s*\)",
        re.I,
    )
    part_re = re.compile(rf"\bparte\s+({PART_WORDS})\b", re.I)
    title_re = re.compile(rf"\btitolo\s+({ROMAN_OR_NUM})\b", re.I)
    chapter_re = re.compile(rf"\b(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b", re.I)

    for index, line in enumerate(lines):
        nline = norm(line)
        for match in named_ref_re.finditer(nline):
            add_chapter_name_mapping(
                mapping,
                match.group(2),
                match.group(3),
                match.group(4),
                match.group("name"),
            )

        if not line.lstrip().startswith("#"):
            continue

        part_match = part_re.search(nline)
        if part_match:
            current_part = part_value(part_match.group(1))
            current_title = None

        title_match = title_re.search(nline)
        if title_match:
            current_title = value(title_match.group(1))

        chapter_match = chapter_re.search(nline)
        if not (current_part and current_title and chapter_match):
            continue

        for next_line in lines[index + 1 : index + 7]:
            candidate = clean(next_line).strip(" #*_")
            comparable = comparable_title(candidate)

            if not comparable:
                continue
            if any(token in comparable for token in ["aggiornamento", "circolare", "titolo"]):
                continue
            if comparable.startswith("sezione "):
                continue

            add_chapter_name_mapping(
                mapping,
                current_part,
                current_title,
                chapter_match.group(1),
                candidate,
            )
            break

    return mapping


def chapter_title_mentions(text: str, chapter: str) -> set[str]:
    mentions = set()
    pattern = re.compile(
        rf"\b(?:cap\.?|capitolo)\s+{re.escape(chapter)}\b(?P<context>.{{0,260}})",
        re.I,
    )

    for match in pattern.finditer(norm(text)):
        context = match.group("context")
        next_chapter = re.search(r"\b(?:cap\.?|capitolo)\s+\d+\b", context[1:], re.I)
        if next_chapter:
            context = context[: next_chapter.start() + 1]

        for quoted in re.finditer(r"[\"“”']([^\"“”']{3,140})[\"“”']", context):
            mentions.add(quoted.group(1))

    return mentions


def location_label_from_key(key: str) -> str:
    parts = key_parts(key)
    return Location(
        part=parts.get("part"),
        title=parts.get("title"),
        chapter=parts.get("chapter"),
        section=parts.get("section"),
        annex=parts.get("annex"),
        front=key if key.startswith("front:") else None,
    ).label()


def key_with_inferred_title(key: str, title: str) -> str:
    parts = key_parts(key)
    out = []

    for level in ["part", "title", "chapter", "section", "annex"]:
        if level == "title":
            out.append(f"title:{title}")
            continue
        if parts.get(level):
            out.append(f"{level}:{parts[level]}")

    return "|".join(out)


def canonicalize_titleless_affected_locations(
    affected: dict[str, dict],
    source_path: Path,
) -> dict[str, dict]:
    title_map = document_part_chapter_titles(source_path)
    chapter_name_map = document_chapter_name_titles(source_path)

    if not title_map and not chapter_name_map:
        return affected

    canonicalized: dict[str, dict] = {}

    for key, row in affected.items():
        parts = key_parts(key)
        new_key = key
        part = parts.get("part", "")
        chapter = parts.get("chapter", "")
        titles = title_map.get((part, chapter))

        if titles and len(titles) > 1:
            matched_titles = set()
            inference_text = row.get("_title_inference_text") or row.get("first_line_text", "")
            for chapter_name in chapter_title_mentions(inference_text, chapter):
                matched_titles |= chapter_name_map.get((part, chapter, comparable_title(chapter_name)), set())
            titles = matched_titles or titles

        if (
            part
            and chapter
            and not parts.get("title")
            and titles
            and len(titles) == 1
        ):
            title = next(iter(titles))
            new_key = key_with_inferred_title(key, title)

        new_row = {
            **row,
            "key": new_key,
            "label": location_label_from_key(new_key),
        }

        if new_key != key:
            new_row["canonicalized_from"] = key
            new_row["title_inference_file"] = source_path.name
            sources = list(new_row.get("sources", []))
            if "document_title_inference" not in sources:
                sources.append("document_title_inference")
            new_row["sources"] = sources

        new_row.pop("_title_inference_text", None)
        canonicalized = merge_locations(canonicalized, {new_key: new_row})

    return canonicalized


def add_location(
    out: dict[str, dict],
    location: Location,
    line_number: int,
    line_text: str,
    source: str,
    update_number: int | None = None,
    file_name: str | None = None,
) -> None:
    key = location.key()

    if not key:
        return

    row = out.setdefault(
        key,
        {
            "key": key,
            "label": location.label(),
            "first_line_number": line_number,
            "first_line_text": clean(line_text),
            "occurrences": 0,
            "sources": [],
            "update_numbers": [],
            "update_files": [],
        },
    )

    row["occurrences"] += 1

    if source not in row["sources"]:
        row["sources"].append(source)

    if update_number is not None and update_number not in row["update_numbers"]:
        row["update_numbers"].append(update_number)

    if file_name and file_name not in row["update_files"]:
        row["update_files"].append(file_name)

    if line_number and line_number < row.get("first_line_number", 10**12):
        row["first_line_number"] = line_number
        row["first_line_text"] = clean(line_text)


def split_ids(text: str) -> list[str]:
    text = norm(text)
    text = re.sub(r"\ballegat[oi]\s+", " ", text)
    text = re.sub(r"\b(?:sez\.|sezione|sezioni)\s+", " ", text)

    out = []

    for piece in re.split(r"\s*(?:,|\be\b|\bed\b|\band\b|&)\s*", text):
        x = value(piece)

        if not x:
            continue

        if x in NOISE_IDS:
            continue

        if any(x == word or x.startswith(word) for word in STRUCTURAL_STOP_WORDS):
            continue

        if re.fullmatch(r"[a-z0-9ivxlcdm]+(?:-[a-z]+)?", x):
            out.append(x)

    return out


def parse_front_locations(text: str) -> dict[str, dict]:
    out = {}
    ntext = norm(text)

    for label in IGNORE_FRONT_LOCATIONS:
        # Explicitly ignored so it does not affect recall.
        ntext = ntext.replace(label, "")

    for label, key in FRONT_LOCATIONS.items():
        if label in ntext:
            add_location(out, Location(front=key), 0, text, "riepilogo_front")

    return out


def parse_dot_locations(text: str) -> dict[str, dict]:
    out = {}

    for match in RE_DOT_LOCATION.finditer(norm(text)):
        p = part_value(match.group(1))
        second = value(match.group(2))
        third = value(match.group(3))

        if not p or not second:
            continue

        # For Parte Prima.IV.1 the second element is a title and third is chapter.
        # For Parte Seconda.10.1 the second element is usually the chapter.
        if third and re.fullmatch(r"[ivxlcdm]+", second or ""):
            add_location(out, Location(p, second, third), 0, text, "riepilogo_dot_location")
        else:
            add_location(out, Location(p, None, second), 0, text, "riepilogo_dot_location")

    return out


def parse_text_locations(text: str) -> dict[str, dict]:
    """
    Parse locations from a riepilogo entry or an annotation location string.

    The parser is stateful because the riepilogo often writes locations as:
        Parte Prima, Titolo IV. Interamente sostituito il Capitolo 4.
    In that case the chapter inherits the current Parte and Titolo.
    """
    out: dict[str, dict] = {}
    ntext = norm(text)

    token_re = re.compile(
        rf"(?P<part>\bparte\s+({PART_WORDS})\b)"
        rf"|(?P<title>\btitolo\s+({ROMAN_OR_NUM})\b)"
        rf"|(?P<chapter>\b(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b)"
        rf"|(?P<section>\b(?:sez\.|sezione|sezioni)\s+((?:{ROMAN_OR_NUM})(?:\s*(?:,|e|ed|and|&)\s*(?:{ROMAN_OR_NUM}))*))"
        rf"|(?P<annex>\b(?:allegato|allegati)\s+((?:{ANNEX_ID})(?:\s*(?:,|e|ed|and|&)\s*(?:{ANNEX_ID}|allegato\s+{ANNEX_ID}))*))",
        re.I,
    )

    current = Location()
    found_base = False

    for match in token_re.finditer(ntext):
        kind = match.lastgroup
        token = match.group(0)

        if kind == "part":
            current = Location(part=part_value(match.group(2)))
            continue

        if kind == "title":
            current = Location(current.part, value(match.group(4)))
            continue

        if kind == "chapter":
            current = Location(current.part, current.title, value(match.group(6)))
            add_location(out, current, 0, text, "riepilogo_chapter")
            found_base = True
            continue

        if kind == "section":
            if current.part and current.chapter:
                for section in split_ids(match.group(8)):
                    add_location(out, Location(current.part, current.title, current.chapter, section=section), 0, text, "riepilogo_section")
            continue

        if kind == "annex":
            if current.part and current.chapter:
                for annex in split_ids(match.group(10)):
                    add_location(out, Location(current.part, current.title, current.chapter, annex=annex), 0, text, "riepilogo_annex")
            else:
                for annex in split_ids(match.group(10)):
                    add_location(out, Location(annex=annex), 0, text, "riepilogo_annex")
            continue

    # Add front matter and old dot-style page references too.
    out = merge_locations(out, parse_front_locations(text), parse_dot_locations(text))

    # Avoid returning only front/index noise when there are no actual locations.
    if not found_base and not out:
        return {}

    return out


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def find_riepilogo_start(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if RE_RIEPILOGO.search(norm(line)):
            return i
    return None


def extract_riepilogo_entries(path: Path) -> dict[int, list[tuple[int, str]]]:
    """
    Extract update-number -> riepilogo block.

    Only the initial "RIEPILOGO DEGLI AGGIORNAMENTI" section is used. Page
    headers such as "50° aggiornamento" are ignored because valid update entries
    must contain "aggiornamento del".
    """
    lines = read_lines(path)
    start = find_riepilogo_start(lines)

    if start is None:
        raise ValueError(f"No 'RIEPILOGO DEGLI AGGIORNAMENTI' section found in {path}.")

    headings = []

    for i in range(start + 1, len(lines)):
        line = clean(lines[i])

        if match := RE_UPDATE_HEADING.match(line):
            headings.append((i, int(match.group(1))))
            continue

        if headings and is_body_start_line(line):
            break

    if not headings:
        raise ValueError(f"No update entries found inside the riepilogo section of {path}.")

    entries: dict[int, list[tuple[int, str]]] = {}

    for pos, (heading_idx, update_number) in enumerate(headings):
        next_heading_idx = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)

        # Include the heading itself because some riepilogo entries place the
        # affected location on the same line as the update heading.
        # Example: "18° Aggiornamento del ... Parte Prima, Titolo II, Capitolo 1."
        block = [(heading_idx + 1, clean(lines[heading_idx]))]

        for j in range(heading_idx + 1, next_heading_idx):
            line = clean(lines[j])
            nline = norm(line)

            if not line:
                continue

            if len(block) > 1 and is_body_start_line(line):
                break

            if nline.startswith("circolare n"):
                continue

            block.append((j + 1, line))

        entries[update_number] = block

    return entries


def update_numbers_from_args(args: argparse.Namespace) -> list[int]:
    if args.update_number:
        return args.update_number

    if args.source_version is not None and args.target_version is not None:
        if args.target_version <= args.source_version:
            raise ValueError("--target-version must be greater than --source-version.")
        return list(range(args.source_version + 1, args.target_version + 1))

    # Last-resort convenience for single-update files.
    numbers = []
    for path in args.aggiornamento_md:
        match = re.search(r"(?:aggiornamento[-_ ]?n[-_ ]?|agg[-_ ]?n[-_ ]?|al[-_ ])(\d+)", Path(path).name, re.I)
        if match:
            numbers.append(int(match.group(1)))

    if numbers:
        return numbers

    raise ValueError("Provide --update-number or --source-version/--target-version.")


def merge_locations(*sets: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}

    for locations in sets:
        for key, value_dict in locations.items():
            if key not in out:
                out[key] = {
                    **value_dict,
                    "sources": list(value_dict.get("sources", [])),
                    "update_numbers": list(value_dict.get("update_numbers", [])),
                    "update_files": list(value_dict.get("update_files", [])),
                }
                continue

            out[key]["occurrences"] += value_dict.get("occurrences", 0)

            for list_key in ["sources", "update_numbers", "update_files"]:
                out[key][list_key] = sorted(set(out[key].get(list_key, [])) | set(value_dict.get(list_key, [])))

            if value_dict.get("first_line_number", 10**12) < out[key].get("first_line_number", 10**12):
                out[key]["first_line_number"] = value_dict.get("first_line_number")
                out[key]["first_line_text"] = value_dict.get("first_line_text")

    return out


def latest_complete_riepilogo(
    paths: list[Path],
    requested_updates: list[int],
) -> tuple[Path, dict[int, list[tuple[int, str]]]]:
    complete = []
    skipped = []
    requested = set(requested_updates)

    for index, path in enumerate(paths):
        try:
            entries = extract_riepilogo_entries(path)
        except ValueError as error:
            skipped.append(f"{path.name}: {error}")
            continue

        missing = sorted(requested - set(entries))
        if missing:
            skipped.append(
                f"{path.name}: missing requested update(s) "
                + ", ".join(map(str, missing))
            )
            continue

        complete.append((max(entries), index, path, entries))

    if not complete:
        details = "\n".join(f"- {item}" for item in skipped)
        message = (
            "No provided aggiornamento Markdown file contains a full riepilogo "
            "for requested update(s): "
            + ", ".join(map(str, requested_updates))
        )
        if details:
            message += "\nChecked files:\n" + details
        raise ValueError(message)

    _, _, path, entries = max(complete, key=lambda item: (item[0], item[1]))
    return path, entries


def affected_locations_from_riepilogo(paths: list[Path], requested_updates: list[int]) -> tuple[dict[str, dict], dict]:
    all_locations = []
    found_updates: dict[int, list[str]] = {}

    path, entries = latest_complete_riepilogo(paths, requested_updates)

    for update_number in requested_updates:
        block = entries[update_number]
        text = " ".join(line for _, line in block)
        locations = parse_text_locations(text)

        for row in locations.values():
            row["first_line_number"] = block[0][0] if block else 0
            row["first_line_text"] = text[:500]
            row["_title_inference_text"] = text
            row["update_numbers"] = [update_number]
            row["update_files"] = [path.name]

        all_locations.append(locations)
        found_updates.setdefault(update_number, []).append(path.name)

    missing_updates = [n for n in requested_updates if n not in found_updates]

    if missing_updates:
        raise ValueError(
            "The riepilogo did not contain entries for requested update(s): "
            + ", ".join(map(str, missing_updates))
        )

    affected = merge_locations(*all_locations)
    affected = canonicalize_titleless_affected_locations(affected, path)

    if not affected:
        raise ValueError("No affected locations could be extracted from the requested riepilogo entries.")

    metadata = {
        "expected_location_source": "riepilogo_degli_aggiornamenti_only",
        "requested_update_numbers": requested_updates,
        "updates_found": sorted(found_updates),
        "files_with_riepilogo": [path.name],
        "update_entry_sources": found_updates,
    }

    return affected, metadata


def collect_texts(value_obj) -> list[str]:
    if value_obj is None:
        return []

    if isinstance(value_obj, str):
        return [value_obj]

    if isinstance(value_obj, list):
        return [text for item in value_obj for text in collect_texts(item)]

    if not isinstance(value_obj, dict):
        return []

    texts = []

    if value_obj.get("label"):
        texts.append(str(value_obj["label"]))

    if isinstance(value_obj.get("path"), list):
        for item in value_obj["path"]:
            if isinstance(item, dict) and item.get("value"):
                texts.append(f"{item.get('level', '')} {item['value']}")

    # Baseline reports use a deliberately generic location object. Include only
    # its textual document reference here so the existing location parser can
    # evaluate it without converting the raw model output to the project schema.
    for key in ["document_section", "page_or_reference"]:
        texts.extend(collect_texts(value_obj.get(key)))

    for key in ["target_location", "source_location"]:
        texts.extend(collect_texts(value_obj.get(key)))

    return texts


def preferred_location(change: dict):
    location = change.get("location")

    if not isinstance(location, dict):
        return location

    operation = inferred_change_operation(change)
    source_location = location.get("source_location")
    target_location = location.get("target_location")

    if operation == "delete":
        return source_location or target_location or location

    return target_location or source_location or location


def predicted_keys(change: dict) -> tuple[set[str], str]:
    raw = " ".join(collect_texts(preferred_location(change)))

    if not raw or contains_any(raw, EXCLUDED_GENERATED_TERMS):
        return set(), raw

    return set(parse_text_locations(raw)), raw


def classification_value(change: dict, key: str) -> str:
    classification = change.get("classification", {}) if isinstance(change.get("classification"), dict) else {}
    out = classification.get(key, "")

    if key == "change_operation" and not out:
        out = inferred_change_operation(change)

    if key == "confidence_score" and not out:
        out = classification.get("confidence", change.get("confidence_score", ""))

    if isinstance(out, dict):
        return "; ".join(f"{k}={v}" for k, v in out.items())

    if isinstance(out, list):
        return "; ".join(map(str, out))

    return str(out)


def change_summary(change: dict) -> str:
    """Return the summary field used by either supported annotation format."""
    return str(change.get("summary", change.get("change_summary", ""))).replace("\n", " ").strip()


def inferred_change_operation(change: dict) -> str:
    """Map a baseline edit type to Layer 2's operation field when necessary."""
    classification = change.get("classification", {})
    if isinstance(classification, dict) and classification.get("change_operation"):
        return str(classification["change_operation"])

    return BASELINE_CHANGE_TYPE_TO_OPERATION.get(str(change.get("change_type", "")).lower(), "")


def annotation_input_format(annotations: dict) -> str:
    """Identify the model output shape for transparent evaluation reports."""
    changes = annotations.get("changes", [])
    if isinstance(changes, list) and any(
        isinstance(change, dict) and "change_summary" in change for change in changes
    ):
        return "baseline_change_report"

    return "regulatory_change_annotations"



# ---------------------------------------------------------------------------
# Optional page-level metric
# ---------------------------------------------------------------------------

RE_HUNK_HEADER = re.compile(r"@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


def load_line_page_map(path: Path) -> dict[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}

    for line, page in data.get("line_to_page", {}).items():
        try:
            out[int(line)] = int(page)
        except (TypeError, ValueError):
            continue

    return out


def pages_for_lines(line_to_page: dict[int, int], start: int, end: int) -> set[int]:
    if start <= 0 or end < start:
        return set()

    return {
        line_to_page[line]
        for line in range(start, end + 1)
        if line in line_to_page
    }


def valid_pages(pages) -> set[int]:
    out = set()

    for page in pages or []:
        try:
            page = int(page)
        except (TypeError, ValueError):
            continue

        if page > 0:
            out.add(page)

    return out


def extract_target_line_ranges(obj) -> list[tuple[int, int]]:
    ranges = []

    if isinstance(obj, list):
        for item in obj:
            ranges.extend(extract_target_line_ranges(item))
        return ranges

    if isinstance(obj, str):
        for match in RE_HUNK_HEADER.finditer(obj):
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count > 0:
                ranges.append((start, start + count - 1))
        return ranges

    if not isinstance(obj, dict):
        return ranges

    for value_obj in obj.values():
        if isinstance(value_obj, (str, list, dict)):
            ranges.extend(extract_target_line_ranges(value_obj))

    start = (
        obj.get("target_start_line")
        or obj.get("target_start")
        or obj.get("target_line_start")
        or obj.get("target_new_start")
    )
    end = (
        obj.get("target_end_line")
        or obj.get("target_end")
        or obj.get("target_line_end")
        or obj.get("target_new_end")
    )
    count = (
        obj.get("target_line_count")
        or obj.get("target_count")
        or obj.get("new_line_count")
    )

    if isinstance(start, int):
        if isinstance(end, int):
            ranges.append((start, end))
        elif isinstance(count, int) and count > 0:
            ranges.append((start, start + count - 1))
        else:
            ranges.append((start, start))

    return ranges


def predicted_pages(change: dict, line_to_page: dict[int, int]) -> set[int]:
    raw_text = change.get("raw_text", {}) if isinstance(change.get("raw_text"), dict) else {}
    diff_links = raw_text.get("diff_links", change.get("diff_links", []))

    # In the neutral baseline schema, page_or_reference can contain the unified
    # diff header. That preserves a generic traceability reference while making
    # the existing optional page metric available without a taxonomy adapter.
    if not diff_links:
        location = change.get("location", {})
        if isinstance(location, dict):
            diff_links = location.get("page_or_reference", [])

    pages = set()

    for start, end in extract_target_line_ranges(diff_links):
        pages |= pages_for_lines(line_to_page, start, end)

    return pages


def toc_ranges(target_pdf: Path) -> list[dict]:
    doc = pymupdf.open(str(target_pdf))
    total_pages = len(doc)
    toc = doc.get_toc(simple=True)
    doc.close()

    if not toc:
        raise ValueError(
            f"No PDF outline/bookmarks found in {target_pdf}. "
            "Page metric cannot be computed."
        )

    rows = []

    for level, title, page in toc:
        try:
            level = int(level)
            page = int(page)
        except (TypeError, ValueError):
            continue

        if page <= 0 or page > total_pages:
            continue

        rows.append({
            "level": level,
            "title": clean(title),
            "start_page": page,
            "end_page": total_pages,
            "order": len(rows),
        })

    rows.sort(key=lambda row: (row["start_page"], row["order"]))

    for i, row in enumerate(rows):
        for later in rows[i + 1:]:
            if later["level"] <= row["level"]:
                row["end_page"] = max(row["start_page"], later["start_page"] - 1)
                break

    return rows


def toc_locations(title: str, ctx: Location) -> tuple[Location, list[Location]]:
    """
    Parse a PDF-outline title using the current structural context.

    This is only used to map riepilogo locations to pages. The riepilogo remains
    the only source of expected affected locations.
    """
    ntext = norm(title)
    current = ctx
    locations = []

    front_key = FRONT_LOCATIONS.get(ntext)
    if front_key:
        locations.append(Location(front=front_key))

    token_re = re.compile(
        rf"(?P<part>\bparte\s+({PART_WORDS})\b)"
        rf"|(?P<title>\btitolo\s+({ROMAN_OR_NUM})\b)"
        rf"|(?P<chapter>\b(?:cap\.?|capitolo)\s+({ROMAN_OR_NUM})\b)"
        rf"|(?P<annex>\b(?:allegato|allegati)\s+((?:{ANNEX_ID})(?:\s*(?:,|e|ed|and|&)\s*(?:{ANNEX_ID}|allegato\s+{ANNEX_ID}))*))",
        re.I,
    )
    shorthand_re = re.compile(
        rf"\b({PART_WORDS})\s+([ivxlcdm]+)\s+(\d+)(?!\d)",
        re.I,
    )

    for match in shorthand_re.finditer(ntext):
        location = Location(
            part=part_value(match.group(1)),
            title=value(match.group(2)),
            chapter=value(match.group(3)),
        )
        current = location
        if location.key():
            locations.append(location)

    for match in token_re.finditer(ntext):
        kind = match.lastgroup

        if kind == "part":
            current = Location(part=part_value(match.group(2)))
            continue

        if kind == "title":
            current = Location(current.part, value(match.group(4)))
            continue

        if kind == "chapter":
            current = Location(current.part, current.title, value(match.group(6)))
            if current.key():
                locations.append(current)
            continue

        if kind == "annex":
            if current.part and current.chapter:
                for annex in split_ids(match.group(8)):
                    locations.append(Location(current.part, current.title, current.chapter, annex=annex))
            else:
                for annex in split_ids(match.group(8)):
                    locations.append(Location(annex=annex))
            continue

    return current, locations


def build_toc_page_index(target_pdf: Path) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    ctx = Location()

    for row in toc_ranges(target_pdf):
        ctx, locations = toc_locations(row["title"], ctx)
        pages = valid_pages(range(row["start_page"], row["end_page"] + 1))

        if not pages:
            continue

        for location in locations:
            key = location.key()
            if key:
                index.setdefault(key, set()).update(pages)

    return index


def key_specificity(key: str) -> int:
    return len(key_parts(key))


def expected_pages_for_key(key: str, page_index: dict[str, set[int]]) -> set[int]:
    if key in page_index:
        return valid_pages(page_index[key])

    matches = [candidate for candidate in page_index if overlap(key, candidate)]

    if not matches:
        return set()

    best = max(key_specificity(candidate) for candidate in matches)
    return valid_pages(
        set().union(
            *(page_index[candidate] for candidate in matches if key_specificity(candidate) == best)
        )
    )


def split_key_field(value_obj) -> set[str]:
    return {
        key.strip()
        for key in str(value_obj or "").split(";")
        if key.strip()
    }


def structurally_compatible_except_title(predicted_key: str, affected_key: str) -> bool:
    predicted = key_parts(predicted_key)
    affected = key_parts(affected_key)

    if predicted_key.startswith("front:") or affected_key.startswith("front:"):
        return False

    if not (predicted.get("part") and predicted.get("chapter")):
        return False

    if predicted.get("part") != affected.get("part") or predicted.get("chapter") != affected.get("chapter"):
        return False

    if predicted.get("title") and affected.get("title") and predicted["title"] != affected["title"]:
        return False

    if bool(predicted.get("title")) == bool(affected.get("title")):
        return False

    for level in ["section", "annex"]:
        if predicted.get(level) and affected.get(level) and predicted[level] != affected[level]:
            return False

    return True


def expected_pages_for_affected_key(
    key: str,
    page_index: dict[str, set[int]],
    predicted: set[int] | None = None,
    predicted_key: str | None = None,
) -> set[int]:
    expected = expected_pages_for_key(key, page_index)

    if expected or not predicted:
        return expected

    parts = key_parts(key)
    predicted_parts = key_parts(predicted_key or "")
    if key.startswith("front:") or not (parts.get("part") and parts.get("chapter")) or parts.get("title"):
        return expected

    candidates = []
    for candidate, pages in page_index.items():
        candidate_parts = key_parts(candidate)

        if candidate_parts.get("part") != parts["part"] or candidate_parts.get("chapter") != parts["chapter"]:
            continue

        if not candidate_parts.get("title"):
            continue

        if predicted_parts.get("title") and candidate_parts.get("title") != predicted_parts["title"]:
            continue

        for level in ["section", "annex"]:
            if parts.get(level) and candidate_parts.get(level) and parts[level] != candidate_parts[level]:
                break
        else:
            candidate_pages = valid_pages(pages)
            if candidate_pages & predicted:
                candidates.append(candidate_pages)

    return valid_pages(set().union(*candidates)) if candidates else set()


def page_disambiguated_overlap(
    predicted_key: str,
    affected_key: str,
    page_index: dict[str, set[int]],
    predicted: set[int],
) -> bool:
    if overlap(predicted_key, affected_key):
        return True

    if not structurally_compatible_except_title(predicted_key, affected_key):
        return False

    expected = expected_pages_for_affected_key(
        affected_key,
        page_index,
        predicted,
        predicted_key=predicted_key,
    )
    return bool(expected & predicted)


def row_expected_pages(
    row: dict,
    report: dict,
    page_index: dict[str, set[int]],
    predicted: set[int],
) -> tuple[set[int], set[str], bool]:
    affected_keys = {
        location.get("key", "")
        for location in report.get("affected_locations", [])
        if location.get("key")
    }
    matched_keys = split_key_field(row.get("matched_predicted_keys"))
    predicted_keys_for_row = split_key_field(row.get("predicted_keys"))
    page_disambiguated = False

    expected_keys = set(matched_keys)
    expected_key_sources: dict[str, set[str]] = {}

    if not expected_keys and predicted_keys_for_row and predicted:
        for predicted_key in predicted_keys_for_row:
            for affected_key in affected_keys:
                if page_disambiguated_overlap(predicted_key, affected_key, page_index, predicted):
                    expected_keys.add(affected_key)
                    expected_key_sources.setdefault(affected_key, set()).add(predicted_key)
                    if not overlap(predicted_key, affected_key):
                        page_disambiguated = True

    expected = set()
    for key in expected_keys:
        source_keys = expected_key_sources.get(key)
        if source_keys:
            for source_key in source_keys:
                expected |= expected_pages_for_affected_key(
                    key,
                    page_index,
                    predicted,
                    predicted_key=source_key,
                )
        else:
            expected |= expected_pages_for_affected_key(key, page_index, predicted)

    return valid_pages(expected), expected_keys, page_disambiguated


def format_pages(pages: set[int]) -> str:
    return "; ".join(map(str, sorted(pages)))


def page_distance(predicted: set[int], expected: set[int]) -> int | None:
    if not predicted or not expected:
        return None

    return min(abs(p - e) for p in predicted for e in expected)


def page_context_from_args(args: argparse.Namespace) -> dict | None:
    if not args.target_pdf and not args.target_line_pages:
        return None

    if not args.target_pdf or not args.target_line_pages:
        print(
            "Warning: Page metric disabled because --target-pdf and "
            "--target-line-pages must be provided together.",
            file=sys.stderr,
        )
        return None

    target_pdf = Path(args.target_pdf)
    target_line_pages = Path(args.target_line_pages)

    if not target_pdf.exists():
        print(
            f"Warning: Page metric disabled because target PDF file was not found: {target_pdf}",
            file=sys.stderr,
        )
        return None

    if not target_line_pages.exists():
        print(
            f"Warning: Page metric disabled because target line-to-page map was not found: {target_line_pages}",
            file=sys.stderr,
        )
        return None

    try:
        return {
            "line_to_page": load_line_page_map(target_line_pages),
            "location_page_index": build_toc_page_index(target_pdf),
        }
    except Exception as error:
        print(
            f"Warning: Page metric disabled because it could not be computed: {error}",
            file=sys.stderr,
        )
        return None


def add_page_metric(report: dict, rows: list[dict], annotations: dict, page_context: dict | None) -> None:
    if page_context is None:
        report["page_metric_enabled"] = False
        return

    report["page_metric_enabled"] = True
    report["expected_page_source"] = "target_pdf_outline_for_riepilogo_locations"

    line_to_page = page_context["line_to_page"]
    location_page_index = page_context["location_page_index"]

    counts = {
        "evaluable": 0,
        "inside": 0,
        "outside": 0,
        "without_predicted_pages": 0,
        "without_expected_pages": 0,
        "not_evaluated_location_miss": 0,
    }

    miss_distances = []

    for row, change in zip(rows, annotations.get("changes", [])):
        row.setdefault("predicted_pages", "")
        row.setdefault("expected_pages", "")
        row.setdefault("page_result", "not_enabled")
        row.setdefault("page_reason", "page_metric_not_enabled")
        row.setdefault("page_distance", "")

        if row.get("inclusion_result") == "excluded":
            counts["not_evaluated_location_miss"] += 1
            row["page_result"] = "not_evaluated"
            row["page_reason"] = "excluded_generated_change"
            continue

        predicted = predicted_pages(change, line_to_page)
        expected, _, page_disambiguated = row_expected_pages(
            row,
            report,
            location_page_index,
            predicted,
        )

        row["predicted_pages"] = format_pages(predicted)
        row["expected_pages"] = format_pages(expected)

        if not predicted:
            counts["without_predicted_pages"] += 1
            row["page_result"] = "not_evaluable"
            row["page_reason"] = "no_target_page_evidence"
            continue

        if not expected:
            if row.get("inclusion_result") != "yes":
                counts["not_evaluated_location_miss"] += 1
                row["page_result"] = "not_evaluated"
                row["page_reason"] = "location_not_page_disambiguated"
            else:
                counts["without_expected_pages"] += 1
                row["page_result"] = "not_evaluable"
                row["page_reason"] = "no_valid_expected_pages_for_location"
            continue

        counts["evaluable"] += 1
        distance = page_distance(predicted, expected)
        row["page_distance"] = "" if distance is None else str(distance)

        if distance == 0:
            counts["inside"] += 1
            row["page_result"] = "yes"
            row["page_reason"] = (
                "predicted_pages_overlap_expected_pages_after_title_disambiguation"
                if page_disambiguated
                else "predicted_pages_overlap_expected_pages"
            )
        else:
            counts["outside"] += 1
            if distance is not None:
                miss_distances.append(distance)
            row["page_result"] = "no"
            row["page_reason"] = (
                "predicted_pages_outside_expected_pages_after_title_disambiguation"
                if page_disambiguated
                else "predicted_pages_outside_expected_pages"
            )

    report.update({
        "page_evaluable_generated_changes": counts["evaluable"],
        "generated_changes_inside_expected_pages": counts["inside"],
        "generated_changes_outside_expected_pages": counts["outside"],
        "generated_changes_without_page_evidence": counts["without_predicted_pages"],
        "generated_changes_without_expected_pages": counts["without_expected_pages"],
        "generated_changes_not_page_evaluated_location_miss": counts["not_evaluated_location_miss"],
        "page_inclusion_precision": counts["inside"] / counts["evaluable"] if counts["evaluable"] else None,
        "average_page_distance_for_misses": (
            sum(miss_distances) / len(miss_distances)
            if miss_distances
            else None
        ),
        "target_location_page_index": {
            key: sorted(pages)
            for key, pages in sorted(location_page_index.items())
        },
    })



def evaluate(annotations: dict, affected: dict[str, dict], metadata: dict) -> tuple[dict, list[dict]]:
    affected_keys = set(affected)
    all_predicted_keys = set()
    matched_predicted_keys = set()
    rows = []
    counts = {"inside": 0, "outside": 0, "no_location": 0, "excluded": 0, "evaluated": 0}

    for change in annotations.get("changes", []):
        base = {
            "change_id": change.get("change_id", "<missing_change_id>"),
            "change_summary": change_summary(change),
            "scope": classification_value(change, "scope"),
            "change_operation": classification_value(change, "change_operation"),
            "semantic": classification_value(change, "semantic"),
            "change_nature": classification_value(change, "change_nature"),
            "confidence_score": classification_value(change, "confidence_score"),
        }

        combined = " ".join([base["change_id"], base["change_summary"], " ".join(collect_texts(change.get("location")))])

        if contains_any(combined, EXCLUDED_GENERATED_TERMS):
            counts["excluded"] += 1
            rows.append({
                **base,
                "predicted_keys": "",
                "matched_predicted_keys": "",
                "unmatched_predicted_keys": "",
                "inclusion_result": "excluded",
                "reason": "excluded_index_or_recap_location",
            })
            continue

        counts["evaluated"] += 1
        keys, raw_location = predicted_keys(change)

        if not keys:
            counts["no_location"] += 1
            rows.append({
                **base,
                "predicted_keys": raw_location,
                "matched_predicted_keys": "",
                "unmatched_predicted_keys": "",
                "inclusion_result": "no",
                "reason": "no_parseable_location",
            })
            continue

        matched = {key for key in keys if any(overlap(key, affected_key) for affected_key in affected_keys)}

        all_predicted_keys |= keys
        matched_predicted_keys |= matched

        rows.append({
            **base,
            "predicted_keys": "; ".join(sorted(keys)),
            "matched_predicted_keys": "; ".join(sorted(matched)),
            "unmatched_predicted_keys": "; ".join(sorted(keys - matched)),
            "inclusion_result": "yes" if matched else "no",
            "reason": "inside_affected_location" if matched else "outside_affected_location",
        })

        counts["inside" if matched else "outside"] += 1

    covered_affected = {
        affected_key
        for affected_key in affected_keys
        if any(overlap(predicted_key, affected_key) for predicted_key in all_predicted_keys)
    }

    union = affected_keys | all_predicted_keys

    report = {
        "evaluation_type": "layer_2_inclusion",
        "annotation_input_format": annotation_input_format(annotations),
        "evaluation_mode": "cumulative" if len(metadata["requested_update_numbers"]) > 1 else "single_update",
        "granularity": GRANULARITY,
        **metadata,
        "total_generated_changes": len(annotations.get("changes", [])),
        "evaluated_generated_changes": counts["evaluated"],
        "excluded_generated_changes": counts["excluded"],
        "generated_changes_inside_affected_locations": counts["inside"],
        "generated_changes_outside_affected_locations": counts["outside"],
        "generated_changes_without_parseable_location": counts["no_location"],
        "total_affected_locations": len(affected_keys),
        "covered_affected_locations": len(covered_affected),
        "change_level_inclusion_precision": counts["inside"] / counts["evaluated"] if counts["evaluated"] else 0.0,
        "predicted_location_precision": len(matched_predicted_keys) / len(all_predicted_keys) if all_predicted_keys else 0.0,
        "affected_location_recall": len(covered_affected) / len(affected_keys) if affected_keys else 0.0,
        "jaccard_location_overlap": len(covered_affected) / len(union) if union else 0.0,
        "affected_locations": [
            {**affected[key], "covered": key in covered_affected}
            for key in sorted(affected_keys)
        ],
        "predicted_location_keys": sorted(all_predicted_keys),
        "matched_predicted_location_keys": sorted(matched_predicted_keys),
    }

    add_update_coverage(report)
    return report, rows


def add_update_coverage(report: dict) -> None:
    by_update: dict[str, dict] = {}

    for location in report.get("affected_locations", []):
        for update in location.get("update_numbers", []) or [None]:
            key = str(update) if update is not None else "unknown"
            row = by_update.setdefault(
                key,
                {
                    "affected_locations": 0,
                    "covered_locations": 0,
                    "missed_locations": 0,
                    "missed_location_keys": [],
                    "missed_location_labels": [],
                },
            )

            row["affected_locations"] += 1

            if location.get("covered"):
                row["covered_locations"] += 1
            else:
                row["missed_locations"] += 1
                row["missed_location_keys"].append(location.get("key", ""))
                row["missed_location_labels"].append(location.get("label", ""))

    for row in by_update.values():
        total = row["affected_locations"]
        row["coverage"] = row["covered_locations"] / total if total else 0.0

    report["affected_location_coverage_by_update"] = by_update


def build_errors(report: dict, rows: list[dict]) -> dict:
    errors = []

    for row in rows:
        if row.get("inclusion_result") == "no":
            errors.append({
                "error_type": row.get("reason", "location_miss"),
                "change_id": row.get("change_id", ""),
                "change_summary": row.get("change_summary", ""),
                "predicted_keys": row.get("predicted_keys", ""),
                "matched_predicted_keys": row.get("matched_predicted_keys", ""),
                "unmatched_predicted_keys": row.get("unmatched_predicted_keys", ""),
                "expected_affected_locations": "; ".join(loc.get("label", "") for loc in report.get("affected_locations", [])),
            })

        if report.get("page_metric_enabled") and row.get("page_result") in {"no", "not_evaluable"}:
            errors.append({
                "error_type": f"page_{row.get('page_reason', 'miss')}",
                "change_id": row.get("change_id", ""),
                "change_summary": row.get("change_summary", ""),
                "matched_predicted_keys": row.get("matched_predicted_keys", ""),
                "predicted_pages": row.get("predicted_pages", ""),
                "expected_pages": row.get("expected_pages", ""),
                "page_distance": row.get("page_distance", ""),
            })

    for location in report.get("affected_locations", []):
        if not location.get("covered"):
            errors.append({
                "error_type": "missed_affected_location",
                "affected_key": location.get("key", ""),
                "affected_label": location.get("label", ""),
                "update_numbers": location.get("update_numbers", []),
                "update_files": location.get("update_files", []),
                "first_line_number": location.get("first_line_number", ""),
                "first_line_text": location.get("first_line_text", ""),
            })

    return {
        "total_errors": len(errors),
        "errors": errors,
    }


def add_report_columns(rows: list[dict], report: dict) -> list[dict]:
    affected = report.get("affected_locations", [])

    report_columns = {
        "evaluation_type": report.get("evaluation_type", ""),
        "evaluation_mode": report.get("evaluation_mode", ""),
        "expected_location_source": report.get("expected_location_source", ""),
        "requested_update_numbers": "; ".join(map(str, report.get("requested_update_numbers", []))),
        "updates_found": "; ".join(map(str, report.get("updates_found", []))),
        "total_generated_changes": report.get("total_generated_changes", 0),
        "evaluated_generated_changes": report.get("evaluated_generated_changes", 0),
        "excluded_generated_changes": report.get("excluded_generated_changes", 0),
        "generated_changes_inside_affected_locations": report.get("generated_changes_inside_affected_locations", 0),
        "generated_changes_outside_affected_locations": report.get("generated_changes_outside_affected_locations", 0),
        "generated_changes_without_parseable_location": report.get("generated_changes_without_parseable_location", 0),
        "total_affected_locations": report.get("total_affected_locations", 0),
        "covered_affected_locations": report.get("covered_affected_locations", 0),
        "change_level_inclusion_precision": f"{report.get('change_level_inclusion_precision', 0.0):.4f}",
        "predicted_location_precision": f"{report.get('predicted_location_precision', 0.0):.4f}",
        "affected_location_recall": f"{report.get('affected_location_recall', 0.0):.4f}",
        "jaccard_location_overlap": f"{report.get('jaccard_location_overlap', 0.0):.4f}",
        "affected_location_labels": "; ".join(loc.get("label", "") for loc in affected),
        "covered_affected_location_labels": "; ".join(loc.get("label", "") for loc in affected if loc.get("covered")),
        "uncovered_affected_location_labels": "; ".join(loc.get("label", "") for loc in affected if not loc.get("covered")),
        "predicted_location_keys": "; ".join(report.get("predicted_location_keys", [])),
        "matched_predicted_location_keys": "; ".join(report.get("matched_predicted_location_keys", [])),
        "page_metric_enabled": report.get("page_metric_enabled", False),
    }

    if report.get("page_metric_enabled"):
        page_precision = report.get("page_inclusion_precision")

        report_columns.update({
            "expected_page_source": report.get("expected_page_source", ""),
            "page_evaluable_generated_changes": report.get("page_evaluable_generated_changes", 0),
            "generated_changes_inside_expected_pages": report.get("generated_changes_inside_expected_pages", 0),
            "generated_changes_outside_expected_pages": report.get("generated_changes_outside_expected_pages", 0),
            "generated_changes_without_page_evidence": report.get("generated_changes_without_page_evidence", 0),
            "generated_changes_without_expected_pages": report.get("generated_changes_without_expected_pages", 0),
            "page_inclusion_precision": "" if page_precision is None else f"{page_precision:.4f}",
            "average_page_distance_for_misses": (
                "" if report.get("average_page_distance_for_misses") is None
                else f"{report.get('average_page_distance_for_misses'):.4f}"
            ),
        })

    if not rows:
        rows = [{"reason": "no_generated_changes"}]

    return [{**row, **report_columns} for row in rows]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Riepilogo-only Layer 2 evaluator for Circular 285 annotations."
    )

    parser.add_argument(
        "--aggiornamento-md",
        nargs="+",
        required=True,
        help="Markdown file(s) containing the initial RIEPILOGO DEGLI AGGIORNAMENTI.",
    )
    parser.add_argument("--annotations-json", required=True, help="Generated annotation JSON file.")
    parser.add_argument("--update-number", nargs="+", type=int, help="Update number(s) to evaluate.")
    parser.add_argument("--source-version", type=int, help="Source version number for cumulative evaluation.")
    parser.add_argument("--target-version", type=int, help="Target version number for cumulative evaluation.")
    parser.add_argument("--target-pdf", help="Optional target consolidated PDF for page metric.")
    parser.add_argument("--target-line-pages", help="Optional line-to-page JSON map for the target Markdown used in the diff.")
    parser.add_argument("--output-json", default="inclusion_report.json")
    parser.add_argument("--output-csv", default="inclusion_evaluation_table.csv")
    parser.add_argument("--output-errors", default="inclusion_errors.json")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    annotation_path = Path(args.annotations_json)
    aggiornamento_paths = [Path(path) for path in args.aggiornamento_md]

    if not annotation_path.exists():
        print(f"Annotation JSON file not found: {annotation_path}", file=sys.stderr)
        sys.exit(1)

    for path in aggiornamento_paths:
        if not path.exists():
            print(f"Aggiornamento Markdown file not found: {path}", file=sys.stderr)
            sys.exit(1)

    try:
        requested_updates = update_numbers_from_args(args)
        affected, metadata = affected_locations_from_riepilogo(aggiornamento_paths, requested_updates)
        annotations = json.loads(annotation_path.read_text(encoding="utf-8"))
        report, rows = evaluate(annotations, affected, metadata)
        add_page_metric(report, rows, annotations, page_context_from_args(args))
    except (ValueError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

    Path(args.output_json).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_csv(Path(args.output_csv), add_report_columns(rows, report))
    Path(args.output_errors).write_text(
        json.dumps(build_errors(report, rows), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Evaluation report written to: {args.output_json}")
    print(f"Combined evaluation table written to: {args.output_csv}")
    print(f"Error file written to: {args.output_errors}")
    print()
    print(f"Expected location source: {report['expected_location_source']}")
    print(f"Requested updates: {', '.join(map(str, report['requested_update_numbers']))}")
    print(f"Affected locations: {report['total_affected_locations']}")
    print(f"Total generated changes: {report['total_generated_changes']}")
    print(f"Evaluated generated changes: {report['evaluated_generated_changes']}")
    print(f"Excluded generated changes: {report['excluded_generated_changes']}")
    print()
    print(f"Change-level inclusion precision: {report['change_level_inclusion_precision']:.3f}")
    print(f"Predicted-location precision: {report['predicted_location_precision']:.3f}")
    print(f"Affected-location recall: {report['affected_location_recall']:.3f}")
    print(f"Jaccard location overlap: {report['jaccard_location_overlap']:.3f}")

    if report.get("page_metric_enabled"):
        print()
        page_precision = report.get("page_inclusion_precision")
        if page_precision is None:
            print("Page inclusion precision: n/a")
        else:
            print(f"Page inclusion precision: {page_precision:.3f}")
        print(f"Page-evaluable generated changes: {report['page_evaluable_generated_changes']}")
        print(f"Generated changes inside expected pages: {report['generated_changes_inside_expected_pages']}")
        print(f"Generated changes outside expected pages: {report['generated_changes_outside_expected_pages']}")


if __name__ == "__main__":
    main()
