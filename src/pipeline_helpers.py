import subprocess
import sys
from pathlib import Path


def run_command(command: list[str], description: str) -> None:
    print(f"\n>>> Running: {description}...")
    print(f"Command: {' '.join(str(part) for part in command)}")

    result = subprocess.run(command, capture_output=False, text=True)
    if result.returncode != 0:
        print(
            f"Error: {description} failed with exit code {result.returncode}",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def ask_llm_choice() -> str:
    while True:
        choice = input("Which LLM do you want to use? [gemini/openai]: ").strip().lower()
        if choice in {"gemini", "openai"}:
            return choice
        print("Invalid choice. Please type 'gemini' or 'openai'.")


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        print(f"Error: {label} not found: {path}", file=sys.stderr)
        sys.exit(1)
