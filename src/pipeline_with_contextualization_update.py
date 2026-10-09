import argparse
import sys
from pathlib import Path

from pipeline_helpers import ask_llm_choice, require_file, run_command
from project_paths import PROMPTS_DIR, REFERENCE_DIR, SCHEMAS_DIR, SOURCE_DIR


def default_output_dir(source_pdf: Path, target_pdf: Path) -> Path:
    return source_pdf.parent / f"{source_pdf.stem}_TO_{target_pdf.stem}_contextualization_update"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "End-to-end pipeline: PDF -> MD -> Diff -> annotation query -> "
            "contextualization prototype/control update"
        )
    )

    parser.add_argument("source_pdf", help="Path to the source PDF file")
    parser.add_argument("target_pdf", help="Path to the target PDF file")

    parser.add_argument(
        "--annotation-prompt",
        default=str(PROMPTS_DIR / "annotation_prompt.txt"),
        help="Prompt used for regulatory change annotation.",
    )
    parser.add_argument(
        "--annotation-schema",
        default=str(SCHEMAS_DIR / "schema_strict.json"),
        help="Strict JSON schema used for regulatory change annotation.",
    )
    parser.add_argument(
        "--taxonomy",
        default=str(REFERENCE_DIR / "taxonomy.tsv"),
        help="Taxonomy file passed to the annotation query.",
    )
    parser.add_argument(
        "--contextualization-prompt",
        default=str(PROMPTS_DIR / "update_contextualization_prompt.txt"),
        help="Prompt used for contextualization update.",
    )
    parser.add_argument(
        "--framework-core",
        default=str(REFERENCE_DIR / "Framework_v2.0_core_ITA.xlsx"),
        help="Framework core workbook. Defaults to Framework v2.0.",
    )
    parser.add_argument(
        "--prototype-xlsx",
        required=True,
        help="Contextualization prototype workbook to update.",
    )
    parser.add_argument(
        "--prototype-reference-column",
        help="Prototype column containing references to the selected regulatory source.",
    )
    parser.add_argument(
        "--controls-xlsx",
        required=True,
        help="Controls workbook to update.",
    )
    parser.add_argument(
        "--controls-reference-column",
        "--controls-filter-column",
        dest="controls_filter_column",
        help=(
            "Controls column containing references for the selected prototype, for example "
            "D for Payment Services or G for the General Prototype."
        ),
    )
    parser.add_argument(
        "--controls-prototype-flag-column",
        help=(
            "Controls column marking membership in the selected prototype, for example H "
            "for Payment Services or K for the General Prototype."
        ),
    )
    parser.add_argument(
        "--controls-reference-pattern",
        help=(
            "Case-insensitive regular expression that selected control references must match, "
            "for example '2015\\s*[/.-]\\s*2366' for PSD2."
        ),
    )
    parser.add_argument(
        "--no-circ285-filter",
        action="store_true",
        help="Disable the legacy default Circular 285 reference filter.",
    )
    parser.add_argument(
        "--max-controls-context",
        type=int,
        default=180,
        help="Maximum number of existing controls included in the contextualization LLM context.",
    )
    parser.add_argument(
        "--output-language",
        default="it",
        help="Language for generated contextualization workbook content. Default: it (Italian).",
    )
    parser.add_argument(
        "--llm",
        choices=["gemini", "openai"],
        help="LLM provider to use. If omitted, you will be asked in the terminal.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Output folder. If omitted, a folder is created next to the source PDF "
            "using the source and target file names."
        ),
    )
    parser.add_argument(
        "--contextualization-json",
        help=(
            "Optional existing contextualization JSON. If provided, the pipeline still "
            "creates annotations, but contextualization_update.py skips its LLM call."
        ),
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Create the contextualization JSON but do not create updated Excel workbooks.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "Also create clean, publication-ready workbooks; low-confidence and "
            "review-needed actions are excluded."
        ),
    )
    parser.add_argument(
        "--confidence-self-consistency",
        action="store_true",
        help=(
            "Run an independent second contextualization query and use matching actions "
            "as a limited confidence bonus."
        ),
    )

    args = parser.parse_args()

    source_pdf = Path(args.source_pdf)
    target_pdf = Path(args.target_pdf)
    annotation_prompt = Path(args.annotation_prompt)
    annotation_schema = Path(args.annotation_schema)
    taxonomy = Path(args.taxonomy)
    contextualization_prompt = Path(args.contextualization_prompt)
    framework_core = Path(args.framework_core)
    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(source_pdf, target_pdf)
    contextualization_dir = output_dir / "contextualization_update"

    llm = args.llm if args.llm else ask_llm_choice()

    require_file(source_pdf, "Source PDF")
    require_file(target_pdf, "Target PDF")
    require_file(annotation_prompt, "Annotation prompt")
    require_file(annotation_schema, "Annotation strict schema")
    require_file(taxonomy, "Taxonomy")
    require_file(contextualization_prompt, "Contextualization prompt")
    require_file(framework_core, "Framework core workbook")
    require_file(SOURCE_DIR / "pdf_to_md.py", "pdf_to_md.py")
    require_file(SOURCE_DIR / "diff.py", "diff.py")
    require_file(SOURCE_DIR / "query.py", "query.py")
    require_file(SOURCE_DIR / "contextualization_update.py", "contextualization_update.py")

    require_file(Path(args.prototype_xlsx), "Contextualization prototype workbook")
    require_file(Path(args.controls_xlsx), "Controls workbook")
    if args.contextualization_json:
        require_file(Path(args.contextualization_json), "Contextualization JSON")

    output_dir.mkdir(parents=True, exist_ok=True)
    contextualization_dir.mkdir(parents=True, exist_ok=True)

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

    run_command(
        [sys.executable, str(SOURCE_DIR / "diff.py"), str(source_md), str(target_md)],
        "Generating normalized git diff",
    )

    diff_output = source_md.parent / f"{source_md.stem}_TO_{target_md.stem}.diff"
    require_file(diff_output, "Expected diff file")

    annotation_output = output_dir / "annotations.json"
    annotation_cmd = [
        sys.executable,
        str(SOURCE_DIR / "query.py"),
        "--llm",
        llm,
        "--prompt",
        str(annotation_prompt),
        "--schema",
        str(annotation_schema),
        "--file",
        str(diff_output),
        str(taxonomy),
        "--output",
        str(annotation_output),
    ]

    run_command(annotation_cmd, f"Querying {llm} API for annotated changes")
    require_file(annotation_output, "Annotation JSON")

    update_cmd = [
        sys.executable,
        str(SOURCE_DIR / "contextualization_update.py"),
        "--annotations-json",
        str(annotation_output),
        "--framework-core",
        str(framework_core),
        "--prompt",
        str(contextualization_prompt),
        "--llm",
        llm,
        "--output-dir",
        str(contextualization_dir),
        "--max-controls-context",
        str(args.max_controls_context),
        "--output-language",
        args.output_language,
    ]

    update_cmd.extend(["--prototype-xlsx", args.prototype_xlsx])
    if args.prototype_reference_column:
        update_cmd.extend(["--prototype-reference-column", args.prototype_reference_column])
    update_cmd.extend(["--controls-xlsx", args.controls_xlsx])
    if args.controls_filter_column:
        update_cmd.extend(["--controls-reference-column", args.controls_filter_column])
    if args.controls_prototype_flag_column:
        update_cmd.extend(
            ["--controls-prototype-flag-column", args.controls_prototype_flag_column]
        )
    if args.controls_reference_pattern:
        update_cmd.extend(["--controls-reference-pattern", args.controls_reference_pattern])
    if args.no_circ285_filter:
        update_cmd.append("--no-circ285-filter")
    if args.contextualization_json:
        update_cmd.extend(["--contextualization-json", args.contextualization_json])
    if args.no_merge:
        update_cmd.append("--no-merge")
    if args.clean:
        update_cmd.append("--clean")
    if args.confidence_self_consistency:
        update_cmd.append("--confidence-self-consistency")

    run_command(update_cmd, "Running contextualization update")

    print("\nDone.")
    print(f"Output folder: {output_dir}")
    print(f"Source Markdown: {source_md}")
    print(f"Target Markdown: {target_md}")
    print(f"Diff: {diff_output}")
    print(f"Annotation JSON: {annotation_output}")
    print(f"Contextualization output folder: {contextualization_dir}")


if __name__ == "__main__":
    main()
