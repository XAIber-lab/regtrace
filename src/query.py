import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".diff",
    ".patch",
    ".xml",
    ".html",
    ".tsv",
    ".htm",
}

JSON_CODE_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.IGNORECASE | re.DOTALL,
)

FINAL_REMINDER = """

FINAL CHECK BEFORE ANSWERING:
Return valid JSON only.
Use only evidence from the numbered unified diff.
Do not include weak or very_weak changes.
Do not use ellipses (...) in evidence fields.
Do not include index, table of contents, header, footer, or page-number noise.
"""


LAST_OPENAI_RESPONSE_METADATA: dict[str, Any] | None = None


def model_dump_if_available(value: Any) -> Any:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def ask_llm_choice() -> str:
    valid_choices = {"gemini", "openai"}

    while True:
        choice = input("Which LLM do you want to use? [gemini/openai]: ").strip().lower()

        if choice in valid_choices:
            return choice

        print("Invalid choice. Please type 'gemini' or 'openai'.")


def read_prompt_file(prompt_path: str) -> str:
    path = Path(prompt_path)

    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    if path.suffix.lower() != ".txt":
        raise ValueError("Prompt file must be a .txt file")

    return path.read_text(encoding="utf-8", errors="ignore")


def read_json_schema(schema_path: str) -> dict[str, Any]:
    path = Path(schema_path)

    if not path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    if path.suffix.lower() != ".json":
        raise ValueError("Schema file must be a .json file")

    return json.loads(path.read_text(encoding="utf-8"))


def schema_for_gemini(schema: Any) -> Any:
    """
    Convert a standard JSON Schema into the subset accepted by the Gemini
    generate_content response_schema path used by google-genai.

    Main conversion:
    {"type": ["string", "null"]} -> {"type": "string", "nullable": True}

    This avoids google-genai validation errors where type arrays containing null
    are rejected by the SDK Schema model.
    """
    unsupported_keys = {
        "$schema",
        "$id",
        "$defs",
        "definitions",
        "additionalProperties",
        "patternProperties",
        "unevaluatedProperties",
        "examples",
        "default",
        "name",
    }

    if isinstance(schema, list):
        return [schema_for_gemini(item) for item in schema]

    if not isinstance(schema, dict):
        return schema

    converted: dict[str, Any] = {}

    for key, value in schema.items():
        if key in unsupported_keys:
            continue

        if key == "type" and isinstance(value, list):
            has_null = "null" in value
            non_null_types = [item for item in value if item != "null"]

            if len(non_null_types) == 1:
                converted["type"] = non_null_types[0]
                if has_null:
                    converted["nullable"] = True
            elif len(non_null_types) > 1:
                converted["anyOf"] = [
                    {"type": item} for item in non_null_types
                ]
                if has_null:
                    converted["nullable"] = True
            elif has_null:
                converted["type"] = "null"
            continue

        if key == "properties" and isinstance(value, dict):
            converted[key] = {
                property_name: schema_for_gemini(property_schema)
                for property_name, property_schema in value.items()
            }
            continue

        if key in {"items", "anyOf"}:
            converted[key] = schema_for_gemini(value)
            continue

        converted[key] = schema_for_gemini(value)

    return converted


def infer_file_label(path: Path, index: int) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()

    if suffix in {".diff", ".patch"}:
        return "NUMBERED UNIFIED DIFF"

    if "taxonomy" in name:
        return "TAXONOMY OF CHANGE TYPES"

    return f"FILE {index}"


def extract_json_text(response_text: str) -> str:
    text = response_text.strip()

    code_block_match = JSON_CODE_BLOCK_RE.search(text)
    if code_block_match:
        return code_block_match.group(1).strip()

    first_object = text.find("{")
    last_object = text.rfind("}")
    first_array = text.find("[")
    last_array = text.rfind("]")

    object_candidate = None
    array_candidate = None

    if first_object != -1 and last_object != -1 and last_object > first_object:
        object_candidate = text[first_object : last_object + 1].strip()

    if first_array != -1 and last_array != -1 and last_array > first_array:
        array_candidate = text[first_array : last_array + 1].strip()

    if object_candidate:
        return object_candidate

    if array_candidate:
        return array_candidate

    return text


def parse_json_response(response_text: str) -> Any:
    json_text = extract_json_text(response_text)

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError("The model response could not be parsed as JSON.") from error


def write_json_file(data: Any, output_path: str) -> Path:
    path = Path(output_path)

    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return path


def write_raw_response(response_text: str, output_path: str) -> Path:
    path = Path(output_path)

    if path.suffix:
        raw_path = path.with_suffix(".raw.txt")
    else:
        raw_path = Path(str(path) + ".raw.txt")

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(response_text, encoding="utf-8")

    return raw_path


def validate_json_locally(data: Any, schema: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print(
            "Note: install jsonschema for local schema validation: pip install jsonschema"
        )
        return

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))

    if not errors:
        return

    print("Error: output JSON does not validate against the strict schema.", file=sys.stderr)
    for error in errors[:10]:
        location = ".".join(str(part) for part in error.path) or "<root>"
        print(f"- {location}: {error.message}", file=sys.stderr)

    if len(errors) > 10:
        print(f"... and {len(errors) - 10} more validation errors", file=sys.stderr)

    sys.exit(1)


def build_text_block(label: str, file_name: str, text: str) -> str:
    return f"\n\n--- {label}: {file_name} ---\n{text}"


def query_gemini(
    prompt: str,
    schema: dict[str, Any],
    file_paths: list[str] | None = None,
    final_reminder: str | None = FINAL_REMINDER,
) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

    gemini_schema = schema_for_gemini(schema)

    generation_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=gemini_schema,
        system_instruction=prompt,
    )

    parts = []

    if file_paths:
        for i, file_path in enumerate(file_paths, start=1):
            path = Path(file_path)
            suffix = path.suffix.lower()
            label = infer_file_label(path, i)

            print(f"Processing file {i}: {path.name} as {label}")

            if suffix in TEXT_EXTENSIONS:
                file_text = path.read_text(encoding="utf-8", errors="ignore")
                parts.append(
                    types.Part.from_text(
                        text=build_text_block(label, path.name, file_text)
                    )
                )
            else:
                uploaded_file = client.files.upload(file=str(path))
                parts.append(
                    types.Part.from_text(
                        text=f"\n\n--- {label}: {path.name} ---\n"
                    )
                )
                parts.append(
                    types.Part.from_uri(
                        file_uri=uploaded_file.uri,
                        mime_type=uploaded_file.mime_type,
                    )
                )
    else:
        parts.append(types.Part.from_text(text="No files were provided."))

    if final_reminder:
        parts.append(types.Part.from_text(text=final_reminder))

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=generation_config,
    )

    return response.text


def query_openai(
    prompt: str,
    schema: dict[str, Any],
    file_paths: list[str] | None = None,
    final_reminder: str | None = FINAL_REMINDER,
    max_output_tokens: int | None = None,
) -> str:
    from openai import OpenAI

    global LAST_OPENAI_RESPONSE_METADATA

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
    max_output_tokens = max_output_tokens or int(
        os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "60000")
    )
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip()

    content = []

    if file_paths:
        for i, file_path in enumerate(file_paths, start=1):
            path = Path(file_path)
            suffix = path.suffix.lower()
            label = infer_file_label(path, i)

            print(f"Processing file {i}: {path.name} as {label}")

            if suffix in TEXT_EXTENSIONS:
                file_text = path.read_text(encoding="utf-8", errors="ignore")
                content.append(
                    {
                        "type": "input_text",
                        "text": build_text_block(label, path.name, file_text),
                    }
                )
            else:
                with open(path, "rb") as file_handle:
                    uploaded_file = client.files.create(
                        file=file_handle,
                        purpose="user_data",
                    )

                content.append(
                    {
                        "type": "input_text",
                        "text": f"\n\n--- {label}: {path.name} ---\n",
                    }
                )
                content.append(
                    {
                        "type": "input_file",
                        "file_id": uploaded_file.id,
                    }
                )
    else:
        content.append(
            {
                "type": "input_text",
                "text": "No files were provided.",
            }
        )

    if final_reminder:
        content.append(
            {
                "type": "input_text",
                "text": final_reminder,
            }
        )

    schema_name = re.sub(
        r"[^A-Za-z0-9_]+",
        "_",
        str(schema.get("name") or "structured_response"),
    ).strip("_")[:64] or "structured_response"

    request_options: dict[str, Any] = {}
    if reasoning_effort:
        request_options["reasoning"] = {"effort": reasoning_effort}

    response = client.responses.create(
        model=model,
        instructions=prompt,
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        max_output_tokens=max_output_tokens,
        **request_options,
    )

    LAST_OPENAI_RESPONSE_METADATA = {
        "provider": "openai",
        "endpoint": "responses.create",
        "requested_model": model,
        "returned_model": getattr(response, "model", None),
        "response_id": getattr(response, "id", None),
        "status": getattr(response, "status", None),
        "created_at": getattr(response, "created_at", None),
        "reasoning_effort_requested": reasoning_effort or None,
        "reasoning": model_dump_if_available(getattr(response, "reasoning", None)),
        "max_output_tokens_requested": max_output_tokens,
        "usage": model_dump_if_available(getattr(response, "usage", None)),
    }

    return response.output_text


def validate_input_files(file_paths: list[str] | None) -> None:
    if not file_paths:
        return

    for file_path in file_paths:
        if not Path(file_path).exists():
            raise FileNotFoundError(f"File not found: {file_path}")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "Query Gemini or OpenAI from the command line using a prompt file, "
            "a strict schema file, and labelled input files."
        )
    )

    parser.add_argument(
        "--llm",
        required=False,
        choices=["gemini", "openai"],
        help="Provider to use. If omitted, the terminal will ask you.",
    )

    parser.add_argument(
        "--prompt",
        required=True,
        help="Path to the .txt file containing the prompt.",
    )

    parser.add_argument(
        "--schema",
        required=True,
        help="Path to the strict JSON Schema file.",
    )

    parser.add_argument(
        "--file",
        required=False,
        nargs="+",
        help="Input file path(s), usually the diff and taxonomy.tsv. Do not pass the schema here.",
    )

    parser.add_argument(
        "--output",
        "-o",
        default="response.json",
        help="Path for the output JSON file. Defaults to response.json.",
    )

    parser.add_argument(
        "--print",
        action="store_true",
        help="Also print the parsed JSON to the console.",
    )

    parser.add_argument(
        "--skip-local-validation",
        action="store_true",
        help="Skip local validation with jsonschema after parsing the model response.",
    )

    parser.add_argument(
        "--final-reminder",
        default=None,
        help=(
            "Optional task-specific final reminder appended to the model input. "
            "If omitted, the default regulatory-annotation reminder is used."
        ),
    )

    parser.add_argument(
        "--no-final-reminder",
        action="store_true",
        help="Do not append any final reminder to the model input.",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help="Optional OpenAI output-token cap. If omitted, OPENAI_MAX_OUTPUT_TOKENS or 60000 is used.",
    )

    args = parser.parse_args()

    if args.max_output_tokens is not None and args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be greater than zero.")

    llm = args.llm if args.llm else ask_llm_choice()
    prompt = read_prompt_file(args.prompt)
    schema = read_json_schema(args.schema)
    validate_input_files(args.file)
    final_reminder = (
        None
        if args.no_final_reminder
        else args.final_reminder
        if args.final_reminder is not None
        else FINAL_REMINDER
    )

    try:
        if llm == "openai":
            response_text = query_openai(
                prompt,
                schema,
                args.file,
                final_reminder,
                args.max_output_tokens,
            )
        else:
            response_text = query_gemini(prompt, schema, args.file, final_reminder)
    except Exception as error:
        print(f"Error while querying {llm}: {error}", file=sys.stderr)
        sys.exit(1)

    try:
        parsed_json = parse_json_response(response_text)
    except ValueError as error:
        raw_output_path = write_raw_response(response_text, args.output)
        print(
            f"Error: The model response was not valid JSON. "
            f"Saved raw response to: {raw_output_path}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error

    if not args.skip_local_validation:
        validate_json_locally(parsed_json, schema)

    output_path = write_json_file(parsed_json, args.output)
    print(f"Created JSON file: {output_path}")

    if llm == "openai" and LAST_OPENAI_RESPONSE_METADATA is not None:
        metadata_path = output_path.with_name(
            f"{output_path.stem}.api_metadata.json"
        )
        metadata_path.write_text(
            json.dumps(
                LAST_OPENAI_RESPONSE_METADATA,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Created OpenAI API metadata file: {metadata_path}")

    if args.print:
        print("\n=== RESPONSE ===\n")
        print(json.dumps(parsed_json, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
