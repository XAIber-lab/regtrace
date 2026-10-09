import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


RE_DIFF_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)


def validate_markdown_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} file does not exist: {path}")

    if path.suffix.lower() != ".md":
        raise ValueError(f"{label} file must have .md extension: {path}")


def read_markdown_as_is(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip() + "\n" if text.strip() else ""


def hunk_count(value: str | None) -> int:
    # In unified diffs, omitted count means 1 line.
    return int(value) if value is not None else 1


def parse_hunk_header(line: str) -> tuple[int, int, int, int] | None:
    match = RE_DIFF_HUNK_HEADER.match(line)
    if not match:
        return None

    source_start = int(match.group(1))
    source_count = hunk_count(match.group(2))
    target_start = int(match.group(3))
    target_count = hunk_count(match.group(4))
    return source_start, source_count, target_start, target_count


def load_line_to_page(map_path: Path | None) -> dict[int, int]:
    if map_path is None:
        return {}

    data = json.loads(map_path.read_text(encoding="utf-8"))
    raw = data.get("line_to_page", {})

    line_to_page = {}
    for line_number, page_number in raw.items():
        try:
            line_to_page[int(line_number)] = int(page_number)
        except (TypeError, ValueError):
            continue

    return line_to_page


def pages_for_range(
    line_to_page: dict[int, int],
    start_line: int,
    line_count: int,
) -> list[int]:
    if not line_to_page or line_count <= 0:
        return []

    pages = {
        line_to_page[line_number]
        for line_number in range(start_line, start_line + line_count)
        if line_number in line_to_page
    }
    return sorted(pages)


def number_diff_hunks_and_collect_pages(
    diff_text: str,
    source_line_to_page: dict[int, int],
    target_line_to_page: dict[int, int],
) -> tuple[str, list[dict]]:
    """
    Add stable hunk identifiers and optionally collect source/target page spans.

    The original @@ header is preserved once, immediately after the hunk ID:

    [HUNK hunk_001]
    @@ -120,8 +120,12 @@ optional section heading
    """
    if not diff_text.strip():
        return diff_text, []

    numbered_lines = []
    hunk_pages = []
    hunk_counter = 0

    for line in diff_text.splitlines():
        parsed_hunk = parse_hunk_header(line)

        if parsed_hunk is not None:
            hunk_counter += 1
            hunk_id = f"hunk_{hunk_counter:03d}"
            source_start, source_count, target_start, target_count = parsed_hunk

            if numbered_lines and numbered_lines[-1] != "":
                numbered_lines.append("")

            numbered_lines.append(f"[HUNK {hunk_id}]")

            hunk_pages.append(
                {
                    "hunk_id": hunk_id,
                    "diff_range": line,
                    "source_line_range": {
                        "start": source_start,
                        "count": source_count,
                        "end": source_start + source_count - 1 if source_count > 0 else None,
                    },
                    "target_line_range": {
                        "start": target_start,
                        "count": target_count,
                        "end": target_start + target_count - 1 if target_count > 0 else None,
                    },
                    "source_pages": pages_for_range(
                        source_line_to_page,
                        source_start,
                        source_count,
                    ),
                    "target_pages": pages_for_range(
                        target_line_to_page,
                        target_start,
                        target_count,
                    ),
                }
            )

        numbered_lines.append(line)

    return "\n".join(numbered_lines).rstrip() + "\n", hunk_pages


def run_git_diff(
    source_file: Path,
    target_file: Path,
    output_file: Path,
    working_dir: Path,
    numbered_hunks: bool,
    source_line_to_page: dict[int, int],
    target_line_to_page: dict[int, int],
) -> list[dict]:
    if not shutil.which("git"):
        print("Error: git is required but was not found in PATH.", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--no-color",
            source_file.name,
            target_file.name,
        ],
        cwd=working_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    # git diff --no-index returns:
    # 0 = no differences
    # 1 = differences found
    # >1 = execution error
    if result.returncode not in (0, 1):
        print("Git diff failed.", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)

    if numbered_hunks:
        diff_text, hunk_pages = number_diff_hunks_and_collect_pages(
            result.stdout,
            source_line_to_page,
            target_line_to_page,
        )
    else:
        diff_text = result.stdout
        _, hunk_pages = number_diff_hunks_and_collect_pages(
            result.stdout,
            source_line_to_page,
            target_line_to_page,
        )

    output_file.write_text(diff_text, encoding="utf-8")
    return hunk_pages


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare two already-prepared Markdown files using git unified diff. "
            "This script does not normalize input text, so line-to-page maps remain aligned."
        )
    )

    parser.add_argument("source_md", help="Path to the prepared source Markdown file")
    parser.add_argument("target_md", help="Path to the prepared target Markdown file")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional output directory. Defaults to the source file folder.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Output the original Git diff without LLM hunk numbering.",
    )
    parser.add_argument(
        "--source-map",
        default=None,
        help="Optional source line-to-page JSON produced by pdf_to_md_mapped.py.",
    )
    parser.add_argument(
        "--target-map",
        default=None,
        help="Optional target line-to-page JSON produced by pdf_to_md_mapped.py.",
    )
    parser.add_argument(
        "--hunk-map-output",
        default=None,
        help="Optional JSON output path for hunk-to-page mapping.",
    )

    args = parser.parse_args()

    source_path = Path(args.source_md)
    target_path = Path(args.target_md)

    try:
        validate_markdown_file(source_path, "Source")
        validate_markdown_file(target_path, "Target")
    except (FileNotFoundError, ValueError) as error:
        print(f"Validation error: {error}", file=sys.stderr)
        sys.exit(1)

    source_map_path = Path(args.source_map) if args.source_map else None
    target_map_path = Path(args.target_map) if args.target_map else None

    for label, map_path in (("Source map", source_map_path), ("Target map", target_map_path)):
        if map_path is not None and not map_path.exists():
            print(f"{label} file does not exist: {map_path}", file=sys.stderr)
            sys.exit(1)

    source_line_to_page = load_line_to_page(source_map_path)
    target_line_to_page = load_line_to_page(target_map_path)

    output_dir = Path(args.output_dir) if args.output_dir else source_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f"{source_path.stem}_TO_{target_path.stem}.diff"
    hunk_map_output = (
        Path(args.hunk_map_output)
        if args.hunk_map_output
        else output_file.with_name(f"{output_file.stem}_hunk_pages.json")
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        source_tmp = tmp_path / source_path.name
        target_tmp = tmp_path / target_path.name

        source_tmp.write_text(read_markdown_as_is(source_path), encoding="utf-8")
        target_tmp.write_text(read_markdown_as_is(target_path), encoding="utf-8")

        hunk_pages = run_git_diff(
            source_file=source_tmp,
            target_file=target_tmp,
            output_file=output_file,
            working_dir=tmp_path,
            numbered_hunks=not args.plain,
            source_line_to_page=source_line_to_page,
            target_line_to_page=target_line_to_page,
        )

    if source_map_path or target_map_path or args.hunk_map_output:
        hunk_map = {
            "diff_file": str(output_file),
            "source_markdown_file": str(source_path),
            "target_markdown_file": str(target_path),
            "source_line_page_map": str(source_map_path) if source_map_path else None,
            "target_line_page_map": str(target_map_path) if target_map_path else None,
            "hunks": hunk_pages,
        }
        hunk_map_output.write_text(
            json.dumps(hunk_map, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Created hunk page map: {hunk_map_output}")

    print(f"Created diff file: {output_file}")


if __name__ == "__main__":
    main()
