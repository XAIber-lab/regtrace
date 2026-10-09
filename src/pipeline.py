import argparse
import sys
from pathlib import Path

from pipeline_helpers import ask_llm_choice, require_file, run_command
from project_paths import PROMPTS_DIR, REFERENCE_DIR, SCHEMAS_DIR, SOURCE_DIR


def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end pipeline: PDF -> MD -> Diff -> LLM Query"
    )

    parser.add_argument(
        "source_pdf",
        help="Path to the source PDF file",
    )

    parser.add_argument(
        "target_pdf",
        help="Path to the target PDF file",
    )

    parser.add_argument(
        "--prompt",
        default=str(PROMPTS_DIR / "annotation_prompt.txt"),
        help="Path to the prompt file. Defaults to annotation_prompt.txt.",
    )

    parser.add_argument(
        "--schema",
        default=str(SCHEMAS_DIR / "schema_strict.json"),
        help="Path to the strict JSON schema file. Defaults to schema_strict.json.",
    )

    parser.add_argument(
        "--llm",
        required=False,
        choices=["gemini", "openai"],
        help="LLM provider to use. If omitted, you will be asked in the terminal.",
    )

    args = parser.parse_args()

    source_pdf = Path(args.source_pdf)
    target_pdf = Path(args.target_pdf)
    prompt_file = Path(args.prompt)
    schema_file = Path(args.schema)
    taxonomy_file = REFERENCE_DIR / "taxonomy.tsv"

    llm = args.llm if args.llm else ask_llm_choice()

    required_files = [
        prompt_file,
        schema_file,
        taxonomy_file,
    ]

    for file_path in required_files:
        require_file(file_path, "Required file")

    require_file(source_pdf, "Source PDF")
    require_file(target_pdf, "Target PDF")

    run_command(
        [sys.executable, str(SOURCE_DIR / "pdf_to_md.py"), str(source_pdf)],
        f"Converting source PDF ({source_pdf.name}) to Markdown",
    )

    source_md = source_pdf.with_suffix(".md")

    run_command(
        [sys.executable, str(SOURCE_DIR / "pdf_to_md.py"), str(target_pdf)],
        f"Converting target PDF ({target_pdf.name}) to Markdown",
    )

    target_md = target_pdf.with_suffix(".md")

    run_command(
        [sys.executable, str(SOURCE_DIR / "diff.py"), str(source_md), str(target_md)],
        "Generating normalized git diff",
    )

    diff_output = source_md.parent / f"{source_md.stem}_TO_{target_md.stem}.diff"

    if not diff_output.exists():
        print(
            f"Error: Expected diff file was not created at {diff_output}",
            file=sys.stderr,
        )
        sys.exit(1)

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
        str(taxonomy_file),
    ]

    run_command(
        query_cmd,
        f"Querying {llm} API with the resulting diff",
    )


if __name__ == "__main__":
    main()
