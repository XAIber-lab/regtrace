import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf4llm

RE_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff]")
RE_SOFT_HYPHEN_SPLIT = re.compile(r"(?<=\w)\u00ad\s+(?=\w)")
RE_SOFT_HYPHEN = re.compile(r"\u00ad")
RE_HYPHENATION = re.compile(r"(?<=\w)-\n(?=\w)")

RE_BROKEN_PARAGRAPH_LINE = re.compile(r"(?<![.!?:;,\)\]\}])\n(?=[a-zàèéìòù])")
RE_MULTISPACE = re.compile(r"[ \t]{2,}")
RE_HEADER_SPACE = re.compile(r"^(#{1,6})[ \t]+")
RE_MULTINEWLINE = re.compile(r"\n{3,}")

RE_IMAGE_PLACEHOLDER = re.compile(
    r"^\*\*==>\s*picture\s*\[\s*\d+\s*x\s*\d+\s*\]\s*"
    r"intentionally omitted\s*<==\*\*$",
    re.IGNORECASE,
)

ALLOWED_RULE_ACTIONS = {
    "remove_line",
    "remove_inline_marker",
}

ALLOWED_NOISE_TYPES = {
    "page_header_footer",
    "page_number",
    "version_marker",
    "extraction_artifact",
    "image_placeholder",
    "picture_text_marker",
    "eurlex_consolidation_marker",
    "markdown_formatting",
    "line_reflow",
    "other",
}

BLOCKED_RULE_NAMES = {
    "remove_empty_lines",
    "normalize_list_indentation",
}


@dataclass
class CompiledRule:
    rule_id: str
    action: str
    pattern: re.Pattern


def extract_chunk_text(chunk) -> str:
    """
    PyMuPDF4LLM with page_chunks=True usually returns dictionaries with the
    page Markdown under "text". This remains tolerant to small version changes.
    """
    if isinstance(chunk, str):
        return chunk

    if not isinstance(chunk, dict):
        return str(chunk)

    for key in ("text", "markdown", "md"):
        value = chunk.get(key)
        if isinstance(value, str):
            return value

    raise ValueError(
        "Could not find page text in PyMuPDF4LLM page chunk. "
        f"Available keys: {list(chunk.keys())}"
    )


def get_page_number(chunk, fallback: int) -> int:
    """Return the 1-based PDF page number."""
    if not isinstance(chunk, dict):
        return fallback

    metadata = chunk.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    candidates = [
        ("page_number", metadata.get("page_number")),
        ("page", metadata.get("page")),
        ("page_index", metadata.get("page_index")),
        ("page_number", chunk.get("page_number")),
        ("page", chunk.get("page")),
        ("page_index", chunk.get("page_index")),
    ]

    for key, value in candidates:
        if not isinstance(value, int):
            continue

        if key == "page_index":
            return value + 1

        if value == fallback - 1:
            return value + 1

        return value

    return fallback


def normalize_markdown(text: str) -> str:
    """
    Same default normalization used by the diff script.

    In this script it is applied page by page before line-to-page mapping is
    created. That makes the map match the Markdown that should be diffed.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    text = (
        text.replace("\u00a0", " ")
        .replace("\u2007", " ")
        .replace("\u202f", " ")
    )

    text = RE_ZERO_WIDTH.sub("", text)
    text = RE_SOFT_HYPHEN_SPLIT.sub("", text)
    text = RE_SOFT_HYPHEN.sub("", text)

    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = RE_HYPHENATION.sub("", text)
    text = RE_BROKEN_PARAGRAPH_LINE.sub(" ", text)

    normalized_lines = []

    for line in text.split("\n"):
        leading_match = re.match(r"^[ \t]*", line)
        leading = leading_match.group(0) if leading_match else ""
        content = line[len(leading):]

        content = RE_MULTISPACE.sub(" ", content)
        content = RE_HEADER_SPACE.sub(r"\1 ", content)

        normalized_lines.append(leading + content.rstrip())

    text = "\n".join(normalized_lines)
    text = RE_MULTINEWLINE.sub("\n\n", text)

    return text.strip() + "\n" if text.strip() else ""


def remove_builtin_noise(text: str) -> str:
    cleaned_lines = []

    for line in text.splitlines():
        stripped = line.strip()

        if RE_IMAGE_PLACEHOLDER.match(stripped):
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip() + "\n" if cleaned_lines else ""


def strip_json_code_fence(raw: str) -> str:
    raw = raw.strip()

    if not raw.startswith("```"):
        return raw

    lines = raw.splitlines()

    if lines and lines[0].startswith("```"):
        lines = lines[1:]

    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    return "\n".join(lines).strip()


def load_cleanup_rules(rules_path: Path) -> list[dict]:
    raw = rules_path.read_text(encoding="utf-8-sig")
    raw = strip_json_code_fence(raw)

    if not raw:
        raise ValueError(f"Rules JSON file is empty: {rules_path}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        preview = raw[:200].replace("\n", "\\n")
        raise ValueError(
            f"Invalid JSON in {rules_path}: {error}. "
            f"First 200 characters: {preview}"
        ) from error

    if isinstance(data, dict):
        rules = data.get("proposed_rules", data.get("rules", []))

        if not isinstance(rules, list):
            raise ValueError("'proposed_rules' or 'rules' must be a list.")

        return rules

    if isinstance(data, list):
        return data

    raise ValueError("Rules JSON must be an object with rules/proposed_rules or a list.")


def is_rule_allowed(rule: dict) -> bool:
    rule_name = rule.get("rule_name", "")
    risk_level = rule.get("risk_level", "").lower()
    action = rule.get("action", "")
    noise_type = rule.get("noise_type", "")

    return (
        risk_level == "low"
        and action in ALLOWED_RULE_ACTIONS
        and noise_type in ALLOWED_NOISE_TYPES
        and rule_name not in BLOCKED_RULE_NAMES
    )


def compile_cleanup_rules(rules: list[dict]) -> list[CompiledRule]:
    compiled_rules = []

    for rule in rules:
        rule_id = rule.get("rule_id", "<unknown>")

        if not is_rule_allowed(rule):
            continue

        regex = rule.get("regex")
        action = rule.get("action")

        if not regex or not action:
            print(
                f"Skipping incomplete rule {rule_id}: missing regex or action",
                file=sys.stderr,
            )
            continue

        try:
            pattern = re.compile(regex, re.IGNORECASE)
        except re.error as error:
            print(
                f"Skipping invalid regex in rule {rule_id}: {error}",
                file=sys.stderr,
            )
            continue

        compiled_rules.append(
            CompiledRule(
                rule_id=rule_id,
                action=action,
                pattern=pattern,
            )
        )

    return compiled_rules


def apply_cleanup_rules(text: str, compiled_rules: list[CompiledRule]) -> str:
    if not compiled_rules:
        return text

    cleaned_lines = []

    for line in text.splitlines():
        current_line = line
        remove_line = False

        for rule in compiled_rules:
            if rule.action == "remove_line" and rule.pattern.search(current_line.strip()):
                remove_line = True
                break

            if rule.action == "remove_inline_marker":
                current_line = rule.pattern.sub("", current_line)

        if remove_line:
            continue

        cleaned_lines.append(current_line)

    return "\n".join(cleaned_lines).strip() + "\n" if cleaned_lines else ""


def prepare_page_markdown(page_markdown: str, compiled_rules: list[CompiledRule]) -> str:
    normalized_text = normalize_markdown(page_markdown)
    cleaned_text = remove_builtin_noise(normalized_text)
    cleaned_text = apply_cleanup_rules(cleaned_text, compiled_rules)
    return cleaned_text


def build_line_to_page(page_ranges: list[dict]) -> dict[str, int]:
    line_to_page = {}

    for page_range in page_ranges:
        start = page_range["start_line"]
        end = page_range["end_line"]

        if start is None or end is None:
            continue

        for line_number in range(start, end + 1):
            line_to_page[str(line_number)] = page_range["page"]

    return line_to_page


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a PDF to normalized Markdown and create a line-to-page map "
            "that matches the Markdown exactly."
        )
    )
    parser.add_argument("input_pdf", help="Input PDF file.")
    parser.add_argument(
        "--output-md",
        help="Output Markdown file. Default: same name as PDF with .md extension.",
    )
    parser.add_argument(
        "--rules",
        default=None,
        help="Optional JSON file with approved or proposed cleanup rules.",
    )

    args = parser.parse_args()
    input_pdf = Path(args.input_pdf)

    if not input_pdf.exists():
        print(f"Error: file not found: {input_pdf}", file=sys.stderr)
        sys.exit(1)

    if input_pdf.suffix.lower() != ".pdf":
        print("Error: input file must be a PDF", file=sys.stderr)
        sys.exit(1)

    compiled_rules = []

    if args.rules:
        rules_path = Path(args.rules)

        if not rules_path.exists():
            print(f"Rules file does not exist: {rules_path}", file=sys.stderr)
            sys.exit(1)

        try:
            rules = load_cleanup_rules(rules_path)
            compiled_rules = compile_cleanup_rules(rules)
        except ValueError as error:
            print(f"Rules error: {error}", file=sys.stderr)
            sys.exit(1)

        print(f"Loaded {len(compiled_rules)} approved low-risk cleanup rules.")

    output_md = Path(args.output_md) if args.output_md else input_pdf.with_suffix(".md")
    output_map = output_md.with_name(f"{output_md.stem}_line_pages.json")

    print(f"Converting: {input_pdf}")

    page_chunks = pymupdf4llm.to_markdown(
        str(input_pdf),
        header=False,
        page_chunks=True,
        show_progress=True,
    )

    output_lines = []
    page_ranges = []

    for page_index, chunk in enumerate(page_chunks):
        page_number = get_page_number(chunk, page_index + 1)
        page_markdown = extract_chunk_text(chunk)
        prepared_page = prepare_page_markdown(page_markdown, compiled_rules)
        page_lines = prepared_page.rstrip().splitlines() if prepared_page.strip() else []

        if not page_lines:
            page_ranges.append(
                {
                    "page": page_number,
                    "start_line": None,
                    "end_line": None,
                    "line_count": 0,
                    "empty": True,
                }
            )
            continue

        start_line = len(output_lines) + 1
        output_lines.extend(page_lines)
        end_line = len(output_lines)

        page_ranges.append(
            {
                "page": page_number,
                "start_line": start_line,
                "end_line": end_line,
                "line_count": len(page_lines),
                "empty": False,
            }
        )

    markdown = "\n".join(output_lines).rstrip() + "\n" if output_lines else ""
    output_md.write_text(markdown, encoding="utf-8")

    line_map = {
        "source_pdf": str(input_pdf),
        "markdown_file": str(output_md),
        "mapping_basis": (
            "Line numbers refer to the normalized Markdown written by this script. "
            "Diff this Markdown as-is to keep page mapping aligned with hunk ranges."
        ),
        "normalization_scope": (
            "Normalization and cleanup are applied page by page. Cross-page line "
            "joins are intentionally avoided to preserve page traceability."
        ),
        "cleanup_rules_file": str(args.rules) if args.rules else None,
        "compiled_cleanup_rules": len(compiled_rules),
        "total_pages": len(page_chunks),
        "page_ranges": page_ranges,
        "line_to_page": build_line_to_page(page_ranges),
    }

    output_map.write_text(
        json.dumps(line_map, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("Conversion finished.")
    print(f"Saved normalized Markdown file: {output_md}")
    print(f"Saved matching line-to-page map: {output_map}")


if __name__ == "__main__":
    main()
