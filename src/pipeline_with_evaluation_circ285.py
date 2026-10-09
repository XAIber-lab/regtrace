import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from pipeline_helpers import ask_llm_choice, require_file, run_command
from project_paths import PROMPTS_DIR, REFERENCE_DIR, SCHEMAS_DIR, SOURCE_DIR


DEFAULT_ANNOTATION_PROMPT = PROMPTS_DIR / "annotation_prompt.txt"
DEFAULT_ANNOTATION_SCHEMA = SCHEMAS_DIR / "schema_strict.json"
DEFAULT_BASELINE_PROMPT = PROMPTS_DIR / "baseline_change_prompt.txt"
DEFAULT_BASELINE_SCHEMA = SCHEMAS_DIR / "baseline_change_schema.json"
DEFAULT_TAXONOMY = REFERENCE_DIR / "taxonomy.tsv"
DEFAULT_BASELINE_MAX_OUTPUT_TOKENS = 30000


def infer_update_number(path: Path) -> int | None:
    """
    Infer Circular 285 update number from filenames such as:

    Circ_285_Testo_integrale_26_aggiornamento.pdf -> 26
    Circ-285-28-aggiornamento.pdf -> 28
    Aggiornamento-27-26.06.2019.pdf -> 27
    Circolare_285_aggiornamento_n._28.pdf -> 28
    Circ_285_19_Aggto_Testo_integrale.pdf -> 19

    It intentionally does not return the fixed Circular number 285.
    """
    name = path.stem.lower()

    patterns = [
        r"(?<!\d)al[-_\s]*(\d+)(?:°|º|o)?[-_\s]*aggiornamento(?![a-z0-9])",
        r"(?<!\d)(\d+)(?:°|º|o)?[-_\s]*aggiornamento(?![a-z0-9])",
        r"(?<!\d)(\d+)[-_\s]*aggto(?![a-z0-9])",
        r"(?<!\d)(\d+)agg(?![a-z0-9])",
        r"aggiornamento[-_\s]*(?:n[._-]?[-_\s]*)?(\d+)(?!\d)",
        r"agg[-_\s]*(?:n[._-]?[-_\s]*)?(\d+)(?!\d)",
    ]

    for pattern in patterns:
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            number = int(match.group(1))
            if number != 285:
                return number

    return None


def experiment_folder_name(
    source_version: int,
    target_version: int,
    baseline: bool = False,
) -> str:
    suffix = "baseline" if baseline else "script"
    return f"circ{source_version}to{target_version}_{suffix}"


def is_line_page_map(path: Path) -> bool:
    """Return True if the JSON file looks like a line-to-page map."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return isinstance(data, dict) and isinstance(data.get("line_to_page"), dict)


def expected_line_pages_path(output_md: Path) -> Path:
    """This must match pdf_to_md.py exactly."""
    return output_md.with_name(f"{output_md.stem}_line_pages.json")


def get_target_line_pages(target_md: Path, override: str | None = None) -> Path:
    if override:
        path = Path(override)
    else:
        path = expected_line_pages_path(target_md)

    if not path.exists():
        print(f"Error: Target line-to-page map not found: {path}", file=sys.stderr)
        sys.exit(1)

    if not is_line_page_map(path):
        print(f"Error: File does not look like a line-to-page map: {path}", file=sys.stderr)
        sys.exit(1)

    return path


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def unique_destination_path(destination: Path) -> Path:
    if not destination.exists():
        return destination

    counter = 2

    while True:
        candidate = destination.with_name(
            f"{destination.stem}_{counter}{destination.suffix}"
        )

        if not candidate.exists():
            return candidate

        counter += 1


def move_pdf_to_output_dir(pdf_path: Path, output_dir: Path) -> Path:
    pdf_path = pdf_path.resolve()
    destination = output_dir / pdf_path.name

    if same_file(pdf_path, destination):
        return destination

    destination = unique_destination_path(destination)
    shutil.move(str(pdf_path), str(destination))
    return destination


def move_input_pdfs_to_output_dir(
    output_dir: Path,
    source_pdf: Path,
    target_pdf: Path,
    aggiornamento_pdfs: list[str] | None,
) -> list[Path]:
    moved = []
    seen = set()

    pdfs = [source_pdf, target_pdf]

    if aggiornamento_pdfs:
        pdfs.extend(Path(path) for path in aggiornamento_pdfs)

    for pdf_path in pdfs:
        resolved = pdf_path.resolve()

        if resolved in seen:
            continue

        seen.add(resolved)
        moved.append(move_pdf_to_output_dir(pdf_path, output_dir))

    return moved


def convert_aggiornamento_pdfs_to_md(
    aggiornamento_pdfs: list[str] | None,
    output_dir: Path,
    source_pdf: Path,
    target_pdf: Path,
    source_md: Path,
    target_md: Path,
) -> list[Path]:
    if not aggiornamento_pdfs:
        return []

    out = []

    for pdf_arg in aggiornamento_pdfs:
        pdf_path = Path(pdf_arg)
        require_file(pdf_path, "Aggiornamento PDF ground-truth file")

        if pdf_path.suffix.lower() != ".pdf":
            print(f"Error: Aggiornamento ground-truth file must be a PDF: {pdf_path}", file=sys.stderr)
            sys.exit(1)

        if same_file(pdf_path, source_pdf):
            out.append(source_md)
            continue

        if same_file(pdf_path, target_pdf):
            out.append(target_md)
            continue

        output_md = output_dir / f"{pdf_path.stem}.md"
        run_command(
            [sys.executable, str(SOURCE_DIR / "pdf_to_md.py"), str(pdf_path), "--output-md", str(output_md)],
            f"Converting aggiornamento PDF ({pdf_path.name}) to Markdown",
        )
        require_file(output_md, "Generated aggiornamento Markdown ground-truth file")
        out.append(output_md)

    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Circular 285 pipeline: PDF -> MD -> Diff -> LLM Query -> "
            "evaluation_circ285 with page metric enabled"
        )
    )

    parser.add_argument("source_pdf", help="Path to the source Circular 285 PDF file")
    parser.add_argument("target_pdf", help="Path to the target Circular 285 PDF file")

    parser.add_argument(
        "--baseline",
        action="store_true",
        help=(
            "Run the neutral baseline: use the baseline prompt and schema by default, "
            "do not send the project taxonomy, and suppress the regulatory final reminder."
        ),
    )

    parser.add_argument(
        "--prompt",
        help=(
            "Path to the prompt file. Defaults to baseline_change_prompt.txt with "
            "--baseline; otherwise annotation_prompt.txt."
        ),
    )
    parser.add_argument(
        "--schema",
        help=(
            "Path to the strict JSON schema file. Defaults to baseline_change_schema.json "
            "with --baseline; otherwise schema_strict.json."
        ),
    )
    parser.add_argument(
        "--taxonomy",
        help="Path to the taxonomy file. Ignored with --baseline.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help=(
            "OpenAI output-token cap. With --baseline it defaults to 30000 to keep "
            "large-diff requests below the API request limit."
        ),
    )
    parser.add_argument(
        "--aggiornamento-pdf",
        nargs="+",
        help=(
            "PDF file(s) containing the initial RIEPILOGO DEGLI AGGIORNAMENTI "
            "used as evaluation ground truth. Each PDF is converted to Markdown first. "
            "If omitted, the generated target Markdown is used."
        ),
    )
    parser.add_argument(
        "--aggiornamento-md",
        nargs="+",
        help=(
            "Optional pre-converted Markdown ground-truth file(s). Usually you should "
            "use --aggiornamento-pdf instead."
        ),
    )
    parser.add_argument(
        "--llm",
        choices=["gemini", "openai"],
        help="LLM provider to use. If omitted, you will be asked in the terminal.",
    )
    parser.add_argument(
        "--source-version",
        type=int,
        help="Source update number. If omitted, inferred from the source filename.",
    )
    parser.add_argument(
        "--target-version",
        type=int,
        help="Target update number. If omitted, inferred from the target filename.",
    )
    parser.add_argument(
        "--update-number",
        nargs="+",
        type=int,
        help=(
            "Specific update number(s) to evaluate. If omitted, the script uses "
            "--source-version/--target-version and therefore supports non-consecutive updates."
        ),
    )
    parser.add_argument(
        "--target-line-pages",
        help=(
            "Optional explicit target line-to-page JSON map. If omitted, the script uses "
            "the exact name produced by pdf_to_md.py: <target_md_stem>_line_pages.json."
        ),
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Experiment output folder. If omitted, the folder is named like "
            "circ23to24_baseline with --baseline, otherwise circ23to24_script."
        ),
    )

    args = parser.parse_args()

    source_pdf = Path(args.source_pdf)
    target_pdf = Path(args.target_pdf)
    prompt_file = Path(
        args.prompt
        or (DEFAULT_BASELINE_PROMPT if args.baseline else DEFAULT_ANNOTATION_PROMPT)
    )
    schema_file = Path(
        args.schema
        or (DEFAULT_BASELINE_SCHEMA if args.baseline else DEFAULT_ANNOTATION_SCHEMA)
    )
    taxonomy_file = None if args.baseline else Path(args.taxonomy or DEFAULT_TAXONOMY)

    llm = args.llm if args.llm else ask_llm_choice()

    require_file(source_pdf, "Source PDF")
    require_file(target_pdf, "Target PDF")
    require_file(prompt_file, "Prompt file")
    require_file(schema_file, "Strict schema file")
    if taxonomy_file is not None:
        require_file(taxonomy_file, "Taxonomy file")
    require_file(SOURCE_DIR / "pdf_to_md.py", "pdf_to_md.py")
    require_file(SOURCE_DIR / "diff.py", "diff.py")
    require_file(SOURCE_DIR / "query.py", "query.py")
    require_file(SOURCE_DIR / "evaluation_circ285.py", "evaluation_circ285.py")

    source_version = args.source_version or infer_update_number(source_pdf)
    target_version = args.target_version or infer_update_number(target_pdf)

    if source_version is None or target_version is None:
        print(
            "Error: Could not infer source/target update numbers. "
            "Pass --source-version and --target-version.",
            file=sys.stderr,
        )
        sys.exit(1)

    if target_version <= source_version:
        print("Error: target version must be greater than source version.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(
        args.output_dir or experiment_folder_name(source_version, target_version, args.baseline)
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    source_md = output_dir / f"{source_pdf.stem}.md"
    target_md = output_dir / f"{target_pdf.stem}.md"

    run_command(
        [sys.executable, str(SOURCE_DIR / "pdf_to_md.py"), str(source_pdf), "--output-md", str(source_md)],
        f"Converting source PDF ({source_pdf.name}) to Markdown",
    )

    run_command(
        [sys.executable, str(SOURCE_DIR / "pdf_to_md.py"), str(target_pdf), "--output-md", str(target_md)],
        f"Converting target PDF ({target_pdf.name}) to Markdown",
    )

    require_file(source_md, "Source Markdown")
    require_file(target_md, "Target Markdown")

    target_line_pages = get_target_line_pages(target_md, args.target_line_pages)

    aggiornamento_md_files = convert_aggiornamento_pdfs_to_md(
        args.aggiornamento_pdf,
        output_dir,
        source_pdf,
        target_pdf,
        source_md,
        target_md,
    )

    if args.aggiornamento_md:
        for path_arg in args.aggiornamento_md:
            md_path = Path(path_arg)
            require_file(md_path, "Aggiornamento Markdown ground-truth file")
            aggiornamento_md_files.append(md_path)

    if not aggiornamento_md_files:
        aggiornamento_md_files = [target_md]

    run_command(
        [sys.executable, str(SOURCE_DIR / "diff.py"), str(source_md), str(target_md)],
        "Generating normalized git diff",
    )

    diff_output = source_md.parent / f"{source_md.stem}_TO_{target_md.stem}.diff"
    require_file(diff_output, "Expected diff file")

    annotation_output = output_dir / "response.json"

    query_cmd = [
        sys.executable,
        str(SOURCE_DIR / "query.py"),
        "--llm",
        llm,
        "--prompt",
        str(prompt_file),
        "--schema",
        str(schema_file),
        "--file",
        str(diff_output),
        "--output",
        str(annotation_output),
    ]

    if taxonomy_file is not None:
        query_cmd.insert(-2, str(taxonomy_file))
    else:
        query_cmd.extend(
            [
                "--no-final-reminder",
                "--max-output-tokens",
                str(args.max_output_tokens or DEFAULT_BASELINE_MAX_OUTPUT_TOKENS),
            ]
        )

    run_command(query_cmd, f"Querying {llm} API with the resulting diff")
    require_file(annotation_output, "Annotation output")

    evaluation_report = output_dir / "inclusion_report.json"
    evaluation_csv = output_dir / "inclusion_evaluation_table.csv"
    evaluation_errors = output_dir / "inclusion_errors.json"

    evaluation_cmd = [
        sys.executable,
        str(SOURCE_DIR / "evaluation_circ285.py"),
        "--aggiornamento-md",
        *map(str, aggiornamento_md_files),
        "--annotations-json",
        str(annotation_output),
        "--target-pdf",
        str(target_pdf),
        "--target-line-pages",
        str(target_line_pages),
        "--output-json",
        str(evaluation_report),
        "--output-csv",
        str(evaluation_csv),
        "--output-errors",
        str(evaluation_errors),
    ]

    if args.update_number:
        evaluation_cmd.extend(["--update-number", *map(str, args.update_number)])
    else:
        evaluation_cmd.extend([
            "--source-version",
            str(source_version),
            "--target-version",
            str(target_version),
        ])

    run_command(evaluation_cmd, "Running Circ. 285 evaluation with page metric")

    moved_input_pdfs = move_input_pdfs_to_output_dir(
        output_dir,
        source_pdf,
        target_pdf,
        args.aggiornamento_pdf,
    )

    print("\nDone.")
    print(f"Experiment folder: {output_dir}")
    print(f"Input PDFs moved to experiment folder: {', '.join(map(str, moved_input_pdfs))}")
    print(f"Source Markdown: {source_md}")
    print(f"Target Markdown: {target_md}")
    print(f"Diff: {diff_output}")
    print(f"Target line-page map: {target_line_pages}")
    print(f"Aggiornamento MD ground truth: {', '.join(map(str, aggiornamento_md_files))}")
    print(f"Annotation JSON: {annotation_output}")
    print(f"Evaluation report: {evaluation_report}")
    print(f"Evaluation table: {evaluation_csv}")
    print(f"Evaluation errors: {evaluation_errors}")


if __name__ == "__main__":
    main()
