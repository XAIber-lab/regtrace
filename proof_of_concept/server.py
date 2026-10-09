from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import uuid

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = Path(__file__).parent / "public"
STORE = ROOT / "outputs" / "proof_of_concept"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
PYTHON = sys.executable
MAX_UPLOAD = 100 * 1024 * 1024
STAGES = ["normalization", "diff", "annotation", "contextualization"]


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def confined(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path is outside the selected artifact directory.")
    return path


def preset(run_id):
    if run_id == "sca":
        folder = ROOT / "reproducibility_artifacts" / "sca_case_study_20261004" / "example"
        return {"id": run_id, "title": "SCA (Regulation (EU) 2018/389)", "kind": "retained",
                "subtitle": "2018 original → 25 July 2023", "framework": "2.0",
                "description": "Article 30(4a): one prototype reference addition and one new interface-specification control. Stage 3 retains three annotations; Stage 4 follows change 003 only.",
                "root": folder, "annotation_path": folder / "annotations.json",
                "context_path": folder / "contextualization_updates.json",
                "bundle_path": folder / "contextualization_input_bundle.json",
                "stage_status": {**dict.fromkeys(STAGES, "retained"), "contextualization": "complete"},
                "status": "ready"}
    return None


def run_folder(run_id):
    if not re.fullmatch(r"run_[a-f0-9]{12}", run_id):
        raise ValueError("Unknown run.")
    path = confined(STORE, run_id)
    if not (path / "manifest.json").is_file():
        raise ValueError("Unknown run.")
    return path


def get_run(run_id):
    existing = preset(run_id)
    if existing:
        return existing
    folder = run_folder(run_id)
    run = read_json(folder / "manifest.json")
    run.update(root=folder, annotation_path=folder / "annotations.json",
               context_path=folder / "contextualization" / "contextualization_updates.json",
               bundle_path=folder / "contextualization" / "contextualization_input_bundle.json")
    return run


def list_runs():
    results = [preset("sca")]
    for path in sorted(STORE.glob("run_*/manifest.json"), reverse=True):
        results.append(read_json(path))
    return [{key: item.get(key) for key in ("id", "title", "subtitle", "kind", "status")} for item in results]


def artifacts(run):
    result = []
    for path in sorted(run["root"].rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".pdf", ".md", ".diff", ".json", ".xlsx", ".log", ".txt", ".tsv", ".py"}:
            continue
        relative = path.relative_to(run["root"]).as_posix()
        name = path.name
        if name.endswith(".tmp"):
            continue
        stage = "provenance"
        if path.suffix == ".pdf" or "inputs/" in relative:
            stage = "inputs"
        elif path.suffix == ".diff" or "hunk_pages" in name:
            stage = "diff"
        elif "line_pages" in name or path.suffix == ".md" and (name.startswith("CELEX_") or name in {"source.md", "target.md"}):
            stage = "normalization"
        elif name in {"annotations.json", "source_annotations.json"}:
            stage = "annotation"
        elif "contextualization" in name or path.suffix == ".xlsx":
            stage = "contextualization"
        result.append({"path": relative, "name": name, "size": path.stat().st_size, "stage": stage})
    return result


def parse_hunks(diff_text, source_map=None, target_map=None):
    results = []
    for match in re.finditer(r"^\[HUNK (hunk_\d+)\]\r?\n(.*?)(?=^\[HUNK hunk_\d+\]|\Z)", diff_text, re.M | re.S):
        body = match.group(2).rstrip()
        header = body.splitlines()[0]
        ranges = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
        pages = {"source_pages": [], "target_pages": []}
        if ranges:
            for label, mapping, offset in [("source", source_map, 1), ("target", target_map, 3)]:
                start = int(ranges.group(offset))
                count = int(ranges.group(offset + 1) or 1)
                lookup = (mapping or {}).get("line_to_page", {})
                pages[label + "_pages"] = sorted({lookup[str(i)] for i in range(start, start + count) if str(i) in lookup})
        results.append({"id": match.group(1), "header": header, "text": body, **pages})
    return results


def review_file(run_id):
    if not preset(run_id):
        run_folder(run_id)
    return STORE / "reviews" / (run_id + ".json")


def load_reviews(run_id):
    return read_json(review_file(run_id), {"run_id": run_id, "decisions": {}, "events": []})


def action_key(change_id, kind, index, action):
    content = json.dumps(action, ensure_ascii=False, sort_keys=True).encode()
    return f"{change_id}:{kind}:{index}:{hashlib.sha256(content).hexdigest()[:12]}"


def assemble(run_id):
    run = get_run(run_id)
    items = artifacts(run)
    can_read = lambda stage: run["stage_status"].get(stage) in {"retained", "complete"}
    annotations = read_json(run["annotation_path"], {}) if can_read("annotation") else {}
    contexts = read_json(run["context_path"], {}) if can_read("contextualization") else {}
    bundle = read_json(run["bundle_path"], {}) if can_read("contextualization") else {}
    diff_path = next((run["root"] / item["path"] for item in items if item["name"].endswith(".diff")), None)
    maps = {}
    for label in ("source", "target"):
        file_name = annotations.get("comparison", {}).get(label, {}).get("file_name", "")
        candidates = [item for item in items if item["name"].endswith("_line_pages.json")]
        selected = next((item for item in candidates if item["name"] == Path(file_name).stem + "_line_pages.json"), None)
        if not selected:
            selected = next((item for item in candidates if item["name"] == label + "_line_pages.json"), None)
        maps[label] = read_json(run["root"] / selected["path"], {}) if selected else {}
    hunks = parse_hunks(diff_path.read_text(encoding="utf-8-sig"), maps["source"], maps["target"]) if diff_path and can_read("diff") else []
    hunk_lookup = {h["id"]: h for h in hunks}
    context_lookup = {item["change_id"]: item for item in contexts.get("change_contextualizations", [])}
    changes, actions = [], []
    for change in annotations.get("changes", []):
        context = context_lookup.get(change["change_id"], {})
        enriched = {**change, "context": context, "actions": []}
        for kind in ("prototype", "control"):
            baseline_rows = bundle.get("contextualization_prototype_rows" if kind == "prototype" else "existing_control_rows", [])
            for index, action in enumerate(context.get(kind + "_actions", [])):
                row = action.get("existing_row" if kind == "prototype" else "existing_control_row")
                baseline = next((x for x in baseline_rows if row is not None and str(x.get("row_number")) == str(row)), None)
                entry = {"key": action_key(change["change_id"], kind, index, action), "change_id": change["change_id"],
                         "kind": kind, "baseline": baseline, **action}
                enriched["actions"].append(entry)
                actions.append(entry)
        links = change.get("raw_text", {}).get("diff_links", [])
        enriched["evidence_checks"] = [{"hunk_id": link["hunk_id"], "available": link["hunk_id"] in hunk_lookup,
                                        "range_matches": hunk_lookup.get(link["hunk_id"], {}).get("header") == link["diff_range"]} for link in links]
        changes.append(enriched)
    docs = []
    pdfs = [item for item in items if item["name"].endswith(".pdf")]
    for index, label in enumerate(("source", "target")):
        comparison = annotations.get("comparison", {}).get(label, {})
        original = run.get("input_names", {}).get(label)
        compared_name = comparison.get("file_name")
        pdf_name = Path(compared_name).with_suffix(".pdf").name if compared_name else ""
        selected = next((x for x in pdfs if x["name"] == pdf_name), None)
        if not selected and len(pdfs) > index:
            selected = pdfs[index]
        docs.append({"role": label, "name": original or comparison.get("file_name") or (selected or {}).get("name", "Not available"),
                     "version": comparison.get("version_label", ""), "date": comparison.get("date"),
                     "pdf": (selected or {}).get("path"), "pages": maps[label].get("total_pages"),
                     "lines": len(maps[label].get("line_to_page", {})) or None})
    schema_check = "not_checked"
    if annotations:
        try:
            import jsonschema
            jsonschema.validate(annotations, read_json(ROOT / "schemas" / "schema_strict.json"))
            schema_check = "valid"
        except ImportError:
            schema_check = "validator_unavailable"
        except Exception:
            schema_check = "invalid"
    return {**{key: run.get(key) for key in ("id", "title", "subtitle", "kind", "description", "provenance_label", "framework", "stage_status", "status", "error", "created_at", "config")},
            "documents": docs, "hunks": hunks, "changes": changes, "actions": actions, "artifacts": items,
            "reviews": load_reviews(run_id), "schema_check": schema_check,
            "output_language": contexts.get("language"),
            "counts": {"changes": len(changes), "hunks": len(hunks), "prototype": sum(a["kind"] == "prototype" for a in actions),
                       "controls": sum(a["kind"] == "control" for a in actions)},
            "source_selection": bundle.get("task", {}).get("controls_filter", {}),
            "log": (run["root"] / "pipeline.log").read_text(encoding="utf-8", errors="replace")[-18000:] if (run["root"] / "pipeline.log").is_file() else ""}


def save_review(run_id, payload):
    with LOCK:
        data = assemble(run_id)
        valid_keys = {a["key"] for a in data["actions"]}
        if payload.get("action_key") not in valid_keys:
            raise ValueError("The action is missing or has changed; reload the run.")
        if payload.get("decision") not in {"accepted", "revision_required", "rejected", "pending"}:
            raise ValueError("Invalid review decision.")
        reviewer = str(payload.get("reviewer", "")).strip()
        notes = str(payload.get("notes", "")).strip()
        if not reviewer or len(reviewer) > 150 or len(notes) > 8000:
            raise ValueError("Enter a reviewer name (up to 150 characters) and notes under 8,000 characters.")
        if payload["decision"] in {"revision_required", "rejected"} and not notes:
            raise ValueError("Explain the required revision or rejection in the review notes.")
        review = load_reviews(run_id)
        event = {"action_key": payload["action_key"], "decision": payload["decision"], "reviewer": reviewer, "notes": notes, "at": now()}
        review["events"].append(event)
        review["decisions"][event["action_key"]] = event
        write_json(review_file(run_id), review)
        return review


def provider_settings():
    values = {}
    try:
        from dotenv import dotenv_values
        values = dotenv_values(ROOT / ".env")
    except ImportError:
        pass
    def setting(key, default=""):
        return os.environ.get(key) or values.get(key) or default
    return {"openai": {"configured": bool(setting("OPENAI_API_KEY")), "model": setting("OPENAI_MODEL", "gpt-5.4-mini")},
            "gemini": {"configured": bool(setting("GEMINI_API_KEY")), "model": setting("GEMINI_MODEL", "gemini-3.5-flash")}}


def create_run(payload):
    if payload.get("verified") is not True:
        raise ValueError("Verify document identity and source-to-target chronology first.")
    title = str(payload.get("title", "")).strip()
    if not title or len(title) > 160:
        raise ValueError("Enter a run title (up to 160 characters).")
    uploaded = {}
    names = {}
    for role in ("source", "target", "prototype", "controls"):
        upload = payload.get(role)
        if not upload:
            if role in {"source", "target"}:
                raise ValueError("Both PDF versions are required.")
            continue
        if not re.fullmatch(r"[a-f0-9]{32}\.(pdf|xlsx)", str(upload)):
            raise ValueError("Invalid uploaded file.")
        path = confined(STORE / "uploads", upload)
        extension = ".pdf" if role in {"source", "target"} else ".xlsx"
        if not path.is_file() or path.suffix != extension:
            raise ValueError(f"A valid {extension} upload is required for {role}.")
        uploaded[role] = path
        names[role] = read_json(path.with_suffix(".metadata.json"), {}).get("name", path.name)
    if digest(uploaded["source"]) == digest(uploaded["target"]):
        raise ValueError("The source and target PDFs are identical. Choose two different versions.")
    config = payload.get("config", {})
    if not isinstance(config, dict):
        raise ValueError("Invalid run configuration.")
    framework = str(config.get("framework", "2.0"))
    if framework not in {"2.0", "2.1"}:
        raise ValueError("Select a supported framework core.")
    clean_config = {"framework": framework}
    for key in ("prototype_column", "controls_column", "flag_column"):
        value = str(config.get(key, "")).strip().upper()
        if value and not re.fullmatch(r"[A-Z]{1,3}|[1-9]\d{0,4}", value):
            raise ValueError("Workbook columns must be Excel letters or positive indices.")
        clean_config[key] = value

    clean_config["reference_contains"] = str(config.get("reference_contains", "285")).strip()[:200]
    clean_config["language"] = "en" if config.get("language") == "en" else "it"
    run_id = "run_" + uuid.uuid4().hex[:12]
    folder = STORE / run_id
    (folder / "inputs").mkdir(parents=True)
    for role, path in uploaded.items():
        shutil.copy2(path, folder / "inputs" / (role + path.suffix))
    framework_name = "Framework_v2.0_core_ITA.xlsx" if framework == "2.0" else "FNCDP_-_Edizione_2025_v2.1_-_Core.xlsx"
    shutil.copy2(ROOT / "reference" / framework_name, folder / "inputs" / "framework.xlsx")
    provenance = folder / "provenance"
    provenance.mkdir()
    for relative in ("src/pdf_to_md.py", "src/diff.py", "src/query.py", "src/contextualization_update.py", "src/contextualization_confidence.py", "src/project_paths.py", "prompts/annotation_prompt.txt", "prompts/update_contextualization_prompt.txt", "schemas/schema_strict.json", "reference/taxonomy.tsv"):
        shutil.copy2(ROOT / relative, provenance / Path(relative).name)
    manifest = {"id": run_id, "title": title, "kind": "local", "subtitle": "New local comparison",
                "description": "Local run using the thesis Python pipeline. Stage 3 and Stage 4 require explicit model-call actions.",
                "created_at": now(), "framework": framework, "config": clean_config,
                "input_names": names, "document_verification": {"confirmed": True, "at": now()},
                "stage_status": dict.fromkeys(STAGES, "pending"), "status": "ready", "commands": [],
                "input_hashes": {path.relative_to(folder).as_posix(): digest(path) for path in (folder / "inputs").iterdir()},
                "code_hashes": {path.name: digest(path) for path in provenance.iterdir()}}
    write_json(folder / "manifest.json", manifest)
    return manifest


def update_manifest(folder, **values):
    with LOCK:
        data = read_json(folder / "manifest.json")
        data.update(values)
        write_json(folder / "manifest.json", data)


def execute(folder, stage, args, env):
    with LOCK:
        manifest = read_json(folder / "manifest.json")
        manifest["stage_status"][stage] = "running"
        manifest["commands"].append({"stage": stage, "argv": args, "at": now()})
        write_json(folder / "manifest.json", manifest)
    with (folder / "pipeline.log").open("a", encoding="utf-8") as log:
        log.write(f"\n[{now()}] {stage}\n")
        log.flush()
        result = subprocess.run(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, timeout=7200,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(f"{stage.capitalize()} failed (exit {result.returncode}). Inspect the run log, correct the inputs or configuration, then retry.")


def worker(run_id, job, config):
    folder = run_folder(run_id)
    stage = "normalization" if job == "compare" else job
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    model = config.get("model", "")
    provider = config.get("provider", "openai")
    if model:
        env["OPENAI_MODEL" if provider == "openai" else "GEMINI_MODEL"] = model
    if provider == "openai" and config.get("reasoning"):
        env["OPENAI_REASONING_EFFORT"] = config["reasoning"]
    def done(name):
        with LOCK:
            manifest = read_json(folder / "manifest.json")
            manifest["stage_status"][name] = "complete"
            write_json(folder / "manifest.json", manifest)
    try:
        if job == "compare":
            for role in ("source", "target"):
                execute(folder, "normalization", [PYTHON, str(ROOT / "src/pdf_to_md.py"), str(folder / "inputs" / (role + ".pdf")), "--output-md", str(folder / (role + ".md"))], env)
                if not (folder / (role + ".md")).read_text(encoding="utf-8").strip():
                    raise ValueError(f"The {role} PDF produced no text. This text-first pipeline needs a readable text layer.")
            done("normalization")
            stage = "diff"
            execute(folder, stage, [PYTHON, str(ROOT / "src/diff.py"), str(folder / "source.md"), str(folder / "target.md"), "--source-map", str(folder / "source_line_pages.json"), "--target-map", str(folder / "target_line_pages.json")], env)
            done(stage)
        elif job == "annotation":
            execute(folder, stage, [PYTHON, str(ROOT / "src/query.py"), "--llm", provider, "--prompt", str(ROOT / "prompts/annotation_prompt.txt"), "--schema", str(ROOT / "schemas/schema_strict.json"), "--file", str(folder / "source_TO_target.diff"), str(ROOT / "reference/taxonomy.tsv"), "--output", str(folder / "annotations.json")], env)
            import jsonschema
            annotations = read_json(folder / "annotations.json")
            jsonschema.validate(annotations, read_json(ROOT / "schemas/schema_strict.json"))
            hunks = {h["id"]: h for h in parse_hunks((folder / "source_TO_target.diff").read_text(encoding="utf-8"))}
            ids = [c["change_id"] for c in annotations["changes"]]
            if len(set(ids)) != len(ids):
                raise ValueError("Annotation output has duplicate change IDs.")
            for change in annotations["changes"]:
                for link in change["raw_text"]["diff_links"]:
                    if hunks.get(link["hunk_id"], {}).get("header") != link["diff_range"]:
                        raise ValueError("Annotation output contains an unresolvable hunk or range. Output is retained for inspection; Stage 4 is blocked.")
            done(stage)
        elif job == "contextualization":
            settings = read_json(folder / "manifest.json")["config"]
            args = [PYTHON, str(ROOT / "src/contextualization_update.py"), "--annotations-json", str(folder / "annotations.json"), "--framework-core", str(folder / "inputs/framework.xlsx"), "--prototype-xlsx", str(folder / "inputs/prototype.xlsx"), "--controls-xlsx", str(folder / "inputs/controls.xlsx"), "--llm", provider, "--output-language", settings["language"], "--output-dir", str(folder / "contextualization")]
            for key, flag in [("prototype_column", "--prototype-reference-column"), ("controls_column", "--controls-reference-column"), ("flag_column", "--controls-prototype-flag-column")]:
                if settings.get(key):
                    args += [flag, settings[key]]
            if settings.get("reference_contains"):
                args += ["--controls-reference-pattern", re.escape(settings["reference_contains"])]
            else:
                raise ValueError("A source-reference selector is required for Stage 4.")
            execute(folder, stage, args, env)
            done(stage)
        update_manifest(folder, status="ready", error=None, updated_at=now())
    except Exception as error:
        with LOCK:
            data = read_json(folder / "manifest.json")
            data["stage_status"][stage] = "failed"
            data.update(status="failed", error=str(error), updated_at=now())
            write_json(folder / "manifest.json", data)


def start_job(run_id, payload):
    with LOCK:
        folder = run_folder(run_id)
        run = read_json(folder / "manifest.json")
        job = payload.get("job")
        if job not in {"compare", "annotation", "contextualization"}:
            raise ValueError("Unknown pipeline stage.")
        if run["status"] == "running":
            raise ValueError("This run is already processing a stage.")
        stage = "diff" if job == "compare" else job
        if run["stage_status"][stage] == "complete":
            raise ValueError("This stage is already complete. Start a new run to preserve its evidence.")
        for filename, expected in run.get("code_hashes", {}).items():
            original = next((ROOT / directory / filename for directory in ("src", "prompts", "schemas", "reference") if (ROOT / directory / filename).is_file()), None)
            if original is None or digest(original) != expected:
                raise ValueError("Pipeline code or configuration changed since this run was created. Start a new run to preserve reproducibility.")
        if job != "compare":
            required = "diff" if job == "annotation" else "annotation"
            if run["stage_status"][required] != "complete":
                raise ValueError("Complete the preceding stage first.")
            if payload.get("consent") is not True:
                raise ValueError("Explicitly authorize the selected hosted-model call.")
            provider = payload.get("provider")
            if provider not in {"openai", "gemini"}:
                raise ValueError("Select a supported provider.")
            if not provider_settings()[provider]["configured"]:
                raise ValueError(f"The {provider} API key is not configured in the local environment or .env file.")
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", str(payload.get("model", ""))):
                raise ValueError("Enter a valid model identifier.")
            if payload.get("reasoning", "") not in {"", "low", "medium", "high"}:
                raise ValueError("Invalid reasoning effort.")
            if job == "annotation" and not (folder / "source_TO_target.diff").read_text(encoding="utf-8").strip():
                raise ValueError("The normalized documents have no differences; no annotation call is needed.")
            if job == "contextualization":
                if payload.get("annotations_reviewed") is not True:
                    raise ValueError("Confirm review of the semantic annotations before Stage 4.")
                if not all((folder / "inputs" / name).is_file() for name in ("prototype.xlsx", "controls.xlsx")):
                    raise ValueError("Stage 4 requires both the existing prototype and controls workbooks. Include them in a new run.")
                if not run["config"].get("reference_contains"):
                    raise ValueError("Stage 4 requires a source-reference selector.")
            run.setdefault("model_calls", []).append({key: payload.get(key) for key in ("job", "provider", "model", "reasoning", "consent", "annotations_reviewed")})
        run.update(status="running", error=None)
        write_json(folder / "manifest.json", run)
        threading.Thread(target=worker, args=(run_id, job, payload), daemon=True).start()
        return {"started": True, "run_id": run_id}


def export_review(run_id):
    data = assemble(run_id)
    return {"format": "thesis-workflow-review/v1", "exported_at": now(), "run_id": run_id,
            "title": data["title"], "framework": data["framework"],
            "boundary": "Reviewer dispositions on candidate actions. This export does not apply changes to any workbook or certify compliance.",
            "actions": [{**action, "review": data["reviews"]["decisions"].get(action["key"], {"decision": "pending"})} for action in data["actions"]],
            "review_events": data["reviews"]["events"],
            "artifact_hashes": {a["path"]: digest(get_run(run_id)["root"] / a["path"]) for a in data["artifacts"]}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, body, status=200, content_type="application/json; charset=utf-8", filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; object-src 'none'")
        if filename:
            from urllib.parse import quote
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(body)

    def check_host(self):
        if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            raise ValueError("Only local requests are accepted.")

    def do_GET(self):
        try:
            self.check_host()
            route = urlparse(self.path)
            query = parse_qs(route.query)
            run_id = query.get("run", ["psd2"])[0]
            if route.path == "/api/config":
                return self.respond({"token": TOKEN, "providers": provider_settings(), "runs": list_runs()})
            if route.path == "/api/run":
                return self.respond(assemble(run_id))
            if route.path == "/api/export":
                return self.respond(json.dumps(export_review(run_id), ensure_ascii=False, indent=2).encode(), filename=run_id + "_review.json")
            if route.path == "/api/manifest":
                run = get_run(run_id)
                return self.respond({"run": run_id, "created_at": now(), "artifacts": [{**a, "sha256": digest(run["root"] / a["path"])} for a in artifacts(run)]})
            if route.path == "/api/artifact":
                run = get_run(run_id)
                relative = query.get("path", [""])[0]
                if relative not in {a["path"] for a in artifacts(run)}:
                    raise ValueError("Artifact is not registered in this run.")
                path = confined(run["root"], relative)
                if query.get("preview") == ["1"]:
                    if path.suffix.lower() not in {".md", ".diff", ".json", ".log", ".txt", ".tsv", ".py"}:
                        raise ValueError("This artifact is available as a download.")
                    content = path.read_text(encoding="utf-8-sig", errors="replace")
                    return self.respond({"text": content[:250000], "truncated": len(content) > 250000})
                return self.respond(path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", filename=None if path.suffix == ".pdf" else path.name)
            static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
            if route.path not in static:
                return self.respond({"error": "Not found"}, 404)
            path = PUBLIC / static[route.path]
            self.respond(path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0] or "text/plain")
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
            self.respond({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.respond({"error": str(error)}, 500)

    def do_POST(self):
        try:
            self.check_host()
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                raise ValueError("Cross-origin writes are disabled.")
            if self.headers.get("X-Workflow-Token") != TOKEN:
                return self.respond({"error": "Reload the local page before saving."}, 403)
            route = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            maximum = MAX_UPLOAD if route.path == "/api/upload" else 32000
            if not 0 < length <= maximum:
                raise ValueError("Request is empty or too large (file limit: 100 MB).")
            body = self.rfile.read(length)
            if route.path == "/api/upload":
                name = Path(parse_qs(route.query).get("name", [""])[0]).name
                ext = Path(name).suffix.lower()
                if ext not in {".pdf", ".xlsx"} or ext == ".pdf" and b"%PDF-" not in body[:1024] or ext == ".xlsx" and not body.startswith(b"PK"):
                    raise ValueError("Choose a PDF document or .xlsx workbook.")
                upload_id = uuid.uuid4().hex + ext
                path = STORE / "uploads" / upload_id
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                write_json(path.with_suffix(".metadata.json"), {"name": name})
                return self.respond({"id": upload_id, "name": name})
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Expected an object.")
            if route.path == "/api/runs":
                return self.respond(create_run(payload), 201)
            run_id = str(payload.get("run_id", ""))
            if route.path == "/api/job":
                return self.respond(start_job(run_id, payload), 202)
            if route.path == "/api/review":
                return self.respond(save_review(run_id, payload))
            return self.respond({"error": "Not found"}, 404)
        except (ValueError, FileNotFoundError) as error:
            self.respond({"error": str(error)}, 400)
        except Exception as error:
            self.respond({"error": str(error)}, 500)


def main():
    global PYTHON, STORE
    parser = argparse.ArgumentParser(description="Run the local proof of concept.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--python", default=sys.executable, help="Interpreter with requirements.txt installed")
    parser.add_argument("--data-dir", type=Path, default=STORE, help="Directory for new runs and review decisions")
    args = parser.parse_args()
    PYTHON, STORE = args.python, args.data_dir.resolve()
    STORE.mkdir(parents=True, exist_ok=True)
    for manifest_path in STORE.glob("run_*/manifest.json"):
        manifest = read_json(manifest_path)
        if manifest.get("status") == "running":
            manifest["status"] = "failed"
            manifest["error"] = "The server stopped during processing. Inspect retained outputs before retrying."
            manifest["stage_status"] = {key: "failed" if value == "running" else value for key, value in manifest["stage_status"].items()}
            write_json(manifest_path, manifest)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Proof of concept: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
