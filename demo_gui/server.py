from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUI_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = GUI_ROOT / "static"
ONTOLOGY_SOURCE = Path("configs/frozen/baseline-v3/requirement-ontology.json")
BASELINE_SOURCE = Path("configs/frozen/baseline-v3/baseline.json")
ANNOTATION_SOURCE = GUI_ROOT / "ontology_annotations.json"
SCENARIO_ROOT = GUI_ROOT / "ontology_scenarios"
DEFAULT_CONFIG = PROJECT_ROOT / "configs/agent/demo-openai.json"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts/runs/demo-gui"
MAX_TASK_BYTES = 16_384
MAX_ANSWER_BYTES = 8_192
MAX_PATCH_BYTES = 524_288
TERMINAL_STATES = frozenset({"completed", "failed", "stopped"})
STOPPED_REASONS = frozenset({
    "step_budget", "tool_budget", "repeated_action", "invalid_output_limit",
    "wall_clock_timeout", "model_refusal", "patch_limit", "max_steps_exceeded",
})

# Import adaptive routing
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from reqagent.adaptive import route_task  # noqa: E402
from reqagent.workspace import preflight_workspace_source  # noqa: E402


class FrozenDataError(RuntimeError):
    pass


class WorkspaceError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrozenDataError(f"Unable to read {path.name}") from error
    if not isinstance(value, dict):
        raise FrozenDataError(f"Invalid object in {path.name}")
    return value


def load_ontology(project_root: Path = PROJECT_ROOT, scenario: str | None = None) -> dict[str, object]:
    ontology_path = project_root / ONTOLOGY_SOURCE
    try:
        ontology_bytes = ontology_path.read_bytes()
        ontology = json.loads(ontology_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrozenDataError("Unable to read requirement-ontology.json") from error
    baseline = _read_json(project_root / BASELINE_SOURCE)
    expected = baseline.get("requirement_ontology_sha256")
    actual = hashlib.sha256(ontology_bytes).hexdigest()
    verified = isinstance(expected, str) and actual == expected
    response: dict[str, object] = {
        "baseline": baseline.get("name", "baseline-v3"), "version": ontology.get("version"),
        "source": ONTOLOGY_SOURCE.as_posix(), "expected_sha256": expected,
        "actual_sha256": actual, "verified": verified, "category_count": 0,
        "slot_count": 0, "tree": None, "annotations": None,
    }
    if not verified:
        response["integrity_error"] = "Frozen ontology integrity verification failed. Tree content is unavailable."
        return response
    source_tree = ontology.get("ontology")
    if not isinstance(source_tree, dict) or not isinstance(ontology.get("version"), str):
        raise FrozenDataError("Frozen ontology structure is invalid")
    annotations = _read_json(ANNOTATION_SOURCE)
    category_notes, slot_notes = annotations.get("categories"), annotations.get("slots")
    if not isinstance(category_notes, dict) or not isinstance(slot_notes, dict):
        raise FrozenDataError("Ontology annotations are invalid")
    normalized, expected_slots = [], set()
    for category_id, slots in source_tree.items():
        if not isinstance(category_id, str) or not isinstance(slots, list) or not all(isinstance(slot, str) for slot in slots):
            raise FrozenDataError("Frozen ontology structure is invalid")
        if category_id not in category_notes:
            raise FrozenDataError("Ontology category annotations do not match the frozen tree")
        children = []
        for slot_id in slots:
            expected_slots.add(slot_id)
            if slot_id not in slot_notes:
                raise FrozenDataError("Ontology slot annotations do not match the frozen tree")
            children.append({"id": slot_id, "type": "slot"})
        normalized.append({"id": category_id, "type": "category", "children": children})
    if set(category_notes) != set(source_tree) or set(slot_notes) != expected_slots:
        raise FrozenDataError("Ontology annotations do not correspond exactly to the frozen tree")
    response.update({"category_count": len(normalized), "slot_count": len(expected_slots),
                     "tree": {"id": "coding-requirement-ontology", "type": "root", "children": normalized},
                     "annotations": annotations})
    if scenario:
        scenario_path = SCENARIO_ROOT / f"{scenario}.json"
        scenario_data = _read_json(scenario_path)
        overlay = scenario_data.get("slots")
        if not isinstance(overlay, dict) or not set(overlay).issubset(expected_slots):
            raise FrozenDataError("Ontology scenario must reference frozen slots only")
        response["scenario"] = {key: value for key, value in scenario_data.items() if key != "slots"}
        response["scenario"]["slots"] = overlay
        response["scenario"]["slot_ids"] = sorted(overlay)
    return response


def validate_workspace(path: Path) -> Path:
    try:
        return preflight_workspace_source(path)
    except (OSError, ValueError) as error:
        raise WorkspaceError(str(error)) from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def status_for_stop_reason(stop_reason: object) -> str:
    if stop_reason == "submitted":
        return "completed"
    if stop_reason in STOPPED_REASONS:
        return "stopped"
    return "failed"


def _safe_text(value: object, limit: int = 480) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/)(?:[^\s]+[\\/])+[^\s]+", "[path]", text)
    text = re.sub(r"(?i)\b(api[_-]?key|auth(?:entication)?[_-]?token|password|secret)\b\s*[:=]\s*\S+", r"\1=[redacted]", text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


@dataclass
class TaskRecord:
    id: str
    task: str
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    # Interview fields
    interview_session: Any | None = field(default=None, repr=False)  # InterviewSession instance
    current_turn: Any | None = field(default=None, repr=False)  # InterviewTurn instance
    baseline: Any | None = field(default=None, repr=False)  # RequirementBaseline instance
    route_mode: str | None = None  # "fast" or "refine"
    route_reasons: tuple[str, ...] = ()
    selected_skills: tuple[str, ...] = ()


class TaskManager:
    def __init__(self, workspace: Path | None, config: Path, artifact_root: Path, *, project_root: Path = PROJECT_ROOT, python: str = sys.executable, in_place: bool = True, interview_adapter_factory=None, scenario: str | None = None):
        self.workspace = validate_workspace(workspace) if workspace is not None else None
        self.config = config.expanduser().resolve()
        self.in_place = in_place
        try:
            config_data = json.loads(self.config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WorkspaceError("Agent configuration is missing or invalid.") from error
        self.mode = str(config_data.get("mode", "unknown"))
        model = config_data.get("model", {})
        self.model = str(model.get("model", "unknown")) if isinstance(model, dict) else "unknown"
        self.artifact_root, self.project_root, self.python = artifact_root.expanduser().resolve(), project_root.resolve(), python
        self.tasks: dict[str, TaskRecord] = {}
        self.lock, self.active_id = threading.RLock(), None
        self.interview_adapter_factory = interview_adapter_factory
        self.scenario = scenario

    def runtime(self) -> dict[str, object]:
        with self.lock:
            active = bool(self.active_id and self.tasks[self.active_id].status not in TERMINAL_STATES)
            workspace_path = str(self.workspace) if self.workspace is not None else None
        return {"workspace_path": workspace_path, "ready": self.workspace is not None, "active": active}

    def set_workspace(self, path: Path) -> Path:
        with self.lock:
            if self.active_id and self.tasks[self.active_id].status not in TERMINAL_STATES:
                raise RuntimeError("Cannot change workspace while an Agent task is running.")
            self.workspace = validate_workspace(path)
            return self.workspace

    def start(self, task: str) -> TaskRecord:
        if self.workspace is None:
            raise WorkspaceError("Select a workspace before starting a task.")
        validate_workspace(self.workspace)
        with self.lock:
            if self.active_id and self.tasks[self.active_id].status not in TERMINAL_STATES:
                raise RuntimeError("An Agent task is already running.")

            # Route the task
            from reqagent.adaptive import route_task
            decision = route_task(task)

            record = TaskRecord(
                uuid.uuid4().hex,
                task,
                route_mode=decision.mode,
                route_reasons=decision.reasons,
                selected_skills=decision.selected_skills,
            )
            self.tasks[record.id], self.active_id = record, record.id

            # Fast path: start coding immediately
            if decision.mode == "fast":
                threading.Thread(target=self._run_coding_agent, args=(record,), daemon=True).start()
            else:
                # Refine path: start interview
                threading.Thread(target=self._start_interview, args=(record,), daemon=True).start()

        return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self.lock:
            return self.tasks.get(task_id)

    def _start_interview(self, record: TaskRecord) -> None:
        """Start an interactive requirement interview."""
        from demo_gui.interview import InterviewSession
        from reqagent.config import AgentConfig

        with self.lock:
            record.status = "interviewing"
            record.started_at = _now()

        try:
            # Create adapter using factory or default
            if self.interview_adapter_factory:
                adapter = self.interview_adapter_factory()
            else:
                from reqagent.openai_responses import OpenAIResponsesAdapter
                cfg = AgentConfig.load(self.config)
                adapter = OpenAIResponsesAdapter(cfg)

            # Load ontology version
            ontology_path = PROJECT_ROOT / ONTOLOGY_SOURCE
            ontology_version = hashlib.sha256(ontology_path.read_bytes()).hexdigest()

            # Create interview session
            session = InterviewSession(record.task, adapter, ontology_version, scenario=self.scenario)

            with self.lock:
                record.interview_session = session

            # Generate first question
            turn = session.generate_next_question()

            with self.lock:
                record.current_turn = turn
                record.status = "awaiting_user"

        except Exception as exc:
            with self.lock:
                record.status = "failed"
                record.error = f"Interview failed to start: {_safe_text(str(exc), 200)}"
                record.finished_at = _now()
                if self.active_id == record.id:
                    self.active_id = None

    def submit_answer(self, task_id: str, turn_id: str, answer: str) -> dict[str, Any]:
        """Submit user answer to the current interview question."""
        if not answer or len(answer.encode("utf-8")) > MAX_ANSWER_BYTES:
            raise ValueError("Answer is empty or too large")

        with self.lock:
            record = self.tasks.get(task_id)
            if not record:
                raise ValueError("Unknown task")
            if record.status != "awaiting_user":
                raise ValueError(f"Task is not awaiting user input (status: {record.status})")
            if not record.current_turn or record.current_turn.turn_id != turn_id:
                raise ValueError("Invalid or stale turn_id")

            record.status = "interviewing"

        # Run in background thread
        threading.Thread(target=self._process_answer, args=(record, turn_id, answer), daemon=True).start()

        return {"status": "interviewing"}

    def _process_answer(self, record: TaskRecord, turn_id: str, answer: str) -> None:
        """Process user answer and generate next question or baseline."""
        try:
            session = record.interview_session
            next_turn = session.submit_answer(turn_id, answer)

            if next_turn is None:
                # Interview complete, baseline formed
                with self.lock:
                    record.baseline = session.baseline
                    record.status = "awaiting_confirmation"
            else:
                # Next question ready
                with self.lock:
                    record.current_turn = next_turn
                    record.status = "awaiting_user"

        except Exception as exc:
            with self.lock:
                record.status = "failed"
                record.error = f"Interview processing failed: {_safe_text(str(exc), 200)}"
                record.finished_at = _now()
                if self.active_id == record.id:
                    self.active_id = None

    def confirm_baseline(self, task_id: str) -> dict[str, Any]:
        """Confirm requirement baseline and start coding agent."""
        with self.lock:
            record = self.tasks.get(task_id)
            if not record:
                raise ValueError("Unknown task")
            if record.status != "awaiting_confirmation":
                raise ValueError(f"Task is not awaiting confirmation (status: {record.status})")
            if not record.baseline:
                raise ValueError("No baseline to confirm")
            if record.process is not None:
                raise ValueError("Coding agent already started")

            # Mark baseline as confirmed
            record.baseline.confirmed_at = time.time()

            # Save interview artifacts
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            interview_dir = self.artifact_root / f"interview-{record.id}"
            interview_dir.mkdir(exist_ok=True)

            # Save transcript
            transcript_path = interview_dir / "interview-transcript.json"
            transcript = record.interview_session.to_transcript()
            transcript_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")

            # Save baseline
            baseline_path = interview_dir / "confirmed-requirement-baseline.json"
            baseline_data = record.baseline.to_dict()
            baseline_data["ontology_version"] = record.interview_session.ontology_version
            baseline_data["configured_model"] = record.interview_session.adapter.config.model["model"]
            baseline_data["actual_models"] = record.interview_session.actual_models
            baseline_path.write_text(json.dumps(baseline_data, ensure_ascii=False, indent=2), encoding="utf-8")

            # Create task file
            task_file = interview_dir / "final-task.txt"
            task_file.write_text(record.baseline.to_task_description(), encoding="utf-8")

            record.status = "queued"

        # Start coding agent with the confirmed baseline
        threading.Thread(target=self._run_coding_agent, args=(record, task_file), daemon=True).start()

        return {"status": "running"}

    def _run_coding_agent(self, record: TaskRecord, task_file: Path | None = None) -> None:
        """Run the coding agent subprocess."""
        with self.lock:
            record.status, record.started_at = "running", _now()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in self.artifact_root.iterdir() if p.is_dir()}

        # Build command
        argv = [self.python, "-m", "reqagent.cli", "run", "--workspace", str(self.workspace),
                "--config", str(self.config), "--artifact-root", str(self.artifact_root)]

        if task_file:
            argv.extend(["--task-file", str(task_file)])
        else:
            argv.extend(["--task", record.task])

        if self.in_place:
            argv.append("--in-place")

        capture_dir = tempfile.mkdtemp(prefix="reqagent-demo-capture-")
        stdout_path = Path(capture_dir) / "stdout.txt"
        stderr_path = Path(capture_dir) / "stderr.txt"
        try:
            environment = os.environ.copy()
            source_root = str(self.project_root / "src")
            environment["PYTHONPATH"] = source_root + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
            with open(stdout_path, "w", encoding="utf-8", errors="replace") as stdout_fp, open(
                stderr_path, "w", encoding="utf-8", errors="replace"
            ) as stderr_fp:
                process = subprocess.Popen(argv, cwd=self.project_root, env=environment, stdout=stdout_fp, stderr=stderr_fp)
                with self.lock:
                    record.process = process
                while process.poll() is None:
                    if record.run_id is None:
                        created = sorted((p for p in self.artifact_root.iterdir() if p.is_dir() and p.name not in before), key=lambda p: p.stat().st_mtime)
                        if created:
                            with self.lock:
                                record.run_id = created[-1].name
                    time.sleep(0.05)
                returncode = process.returncode
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            try:
                payload = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
            except (json.JSONDecodeError, IndexError):
                payload = None
            run_id = payload.get("run_id") if isinstance(payload, dict) else record.run_id
            if not isinstance(run_id, str):
                created = sorted((p for p in self.artifact_root.iterdir() if p.is_dir() and p.name not in before), key=lambda p: p.stat().st_mtime)
                run_id = created[-1].name if created else None
            with self.lock:
                record.run_id = run_id
                if returncode == 0 and isinstance(payload, dict):
                    record.status = status_for_stop_reason(payload.get("stop_reason"))
                    record.result = payload
                else:
                    detail = _safe_text((stderr_path.read_text(encoding="utf-8", errors="replace").splitlines() or [""])[-1], 160)
                    record.status = "failed"
                    record.error = f"Agent process exited with code {returncode}." + (f" {detail}" if detail else "")
        except (OSError, subprocess.SubprocessError):
            with self.lock:
                record.status, record.error = "failed", "Agent process could not be started or completed."
        finally:
            shutil.rmtree(capture_dir, ignore_errors=True)
            with self.lock:
                record.process, record.finished_at = None, _now()
                if self.active_id == record.id:
                    self.active_id = None

    def public_task(self, record: TaskRecord) -> dict[str, object]:
        result = record.result or {}
        submitted = result.get("submitted") if isinstance(result.get("submitted"), dict) else {}
        patch = result.get("patch") if isinstance(result.get("patch"), dict) else {}
        verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
        response = {
            "id": record.id, "task": record.task, "status": record.status, "created_at": record.created_at,
            "started_at": record.started_at, "finished_at": record.finished_at, "stop_reason": result.get("stop_reason"),
            "summary": _safe_text(submitted.get("summary"), 1200) if submitted else None,
            "limitations": _safe_text(submitted.get("limitations"), 600) if submitted else None,
            "patch": {key: patch.get(key, 0) for key in ("files", "additions", "deletions", "bytes")},
            "steps": result.get("steps", 0),
            "tool_calls": result.get("tool_calls", 0),
            "submitted_tests": [_safe_text(item, 320) for item in (submitted.get("tests", []) if isinstance(submitted.get("tests"), list) else [])],
            "unverified_test_claims": bool(submitted and submitted.get("tests") and verification.get("all_passed") is not True),
            "error": record.error,
            "route_mode": record.route_mode,
            "route_decision": {
                "mode": record.route_mode,
                "reasons": [_safe_text(item, 80) for item in record.route_reasons],
                "selected_skills": [_safe_text(item, 80) for item in record.selected_skills],
                "source": "interactive_router",
            },
        }

        if self.scenario:
            response["scenario"] = self.scenario

        session = record.interview_session
        if session:
            selected = {slot_id for turn in session.turns for slot_id in turn.selected_slot_ids}
            answered = {slot_id for turn in session.turns if turn.answer is not None for slot_id in turn.selected_slot_ids}
            final_states = record.baseline.slot_states if record.baseline else {}
            categories = []
            ontology = load_ontology(self.project_root)
            for category in (ontology.get("tree") or {}).get("children", []):
                slots = []
                for node in category.get("children", []):
                    slot_id = node["id"]
                    state = final_states.get(slot_id, {}) if isinstance(final_states, dict) else {}
                    status = state.get("state") if isinstance(state, dict) else None
                    if not status:
                        status = "confirmed" if slot_id in answered else ("unresolved" if slot_id in selected else "unexplored")
                    reasons = [t.selection_reason for t in session.turns if slot_id in t.selected_slot_ids]
                    slots.append({"id": slot_id, "name_zh": ontology["annotations"]["slots"][slot_id]["name_zh"], "status": status,
                                  "selected": slot_id in selected, "selection_reason": _safe_text("; ".join(reasons), 220)})
                categories.append({"id": category["id"], "name_zh": ontology["annotations"]["categories"][category["id"]]["name_zh"], "slots": slots})
            response["requirement_coverage"] = {"covered": sum(1 for c in categories for s in c["slots"] if s["status"] in {"confirmed", "rejected"}),
                                                  "total": sum(len(c["slots"]) for c in categories), "categories": categories,
                                                  "note": "并非每个任务都必须强行填满所有槽位"}

        # Add interview state
        if record.status in {"awaiting_user", "interviewing"}:
            if record.current_turn:
                response["current_question"] = {
                    "turn_id": record.current_turn.turn_id,
                    "question": record.current_turn.question,
                    "slot_ids": record.current_turn.selected_slot_ids,
                    "selection_reason": _safe_text(record.current_turn.selection_reason, 220),
                    "turn_number": len(record.interview_session.turns) if record.interview_session else 0,
                    "max_turns": record.interview_session.max_turns if record.interview_session else 3
                }

            if record.interview_session:
                response["interview_history"] = [
                    {
                        "question": t.question,
                        "answer": t.answer,
                        "slot_ids": t.selected_slot_ids
                        ,"selection_reason": _safe_text(t.selection_reason, 220)
                    }
                    for t in record.interview_session.turns if t.answer is not None
                ]

        elif record.status == "awaiting_confirmation":
            if record.baseline:
                response["baseline"] = {
                    "refined_summary": record.baseline.refined_summary,
                    "requirements": record.baseline.requirements,
                    "acceptance_criteria": record.baseline.acceptance_criteria,
                    "constraints": record.baseline.constraints,
                    "excluded_scope": record.baseline.excluded_scope,
                    "assumptions": record.baseline.assumptions,
                    "unresolved_items": record.baseline.unresolved_items,
                    "slot_states": record.baseline.slot_states,
                }

        return response

    def _run_dir(self, record: TaskRecord) -> Path | None:
        if not record.run_id or not re.fullmatch(r"[A-Za-z0-9._-]+", record.run_id):
            return None
        path = (self.artifact_root / record.run_id).resolve()
        return path if path.parent == self.artifact_root and path.is_dir() else None

    def events(self, record: TaskRecord, after: int) -> tuple[list[dict[str, object]], int, bool]:
        run_dir = self._run_dir(record)
        path = run_dir / "events.jsonl" if run_dir else None
        if path is None or not path.is_file():
            return [], after, record.status in TERMINAL_STATES
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return [], after, record.status in TERMINAL_STATES
        events = [event for offset, line in enumerate(lines[after:], after)
                  if (event := self._sanitize_event(line, offset)) is not None]
        return events, len(lines), record.status in TERMINAL_STATES

    @staticmethod
    def _sanitize_event(line: str, offset: int) -> dict[str, object] | None:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        raw_phase = _safe_text(raw.get("phase") or "main", 32)

        def presentation_phase(tools: list[str]) -> str:
            if "submit" in tools:
                return "complete"
            if "run_command" in tools:
                return "verification"
            if "apply_patch" in tools:
                return "implementation"
            if any(tool in {"list_files", "read_file", "search_text"} for tool in tools):
                return "investigation"
            if "record_requirement_brief" in tools:
                return "baseline"
            if raw_phase == "refinement":
                return "refinement"
            if raw_phase == "main":
                return "implementation"
            return raw_phase

        if raw.get("kind") == "route_decision":
            mode = _safe_text(raw.get("mode"), 32)
            reasons = raw.get("reasons") if isinstance(raw.get("reasons"), list) else []
            selected_skills = raw.get("selected_skills") if isinstance(raw.get("selected_skills"), list) else []
            return {"offset": offset, "kind": "route_decision", "phase": "intake", "mode": mode,
                    "reasons": [_safe_text(r, 80) for r in reasons],
                    "selected_skills": [_safe_text(s, 80) for s in selected_skills]}
        if raw.get("kind") == "requirement_brief_recorded":
            return {"offset": offset, "kind": "requirement_brief_recorded", "phase": "baseline"}
        if raw.get("kind") == "model_response":
            response = raw.get("response") if isinstance(raw.get("response"), dict) else {}
            calls = response.get("tool_calls") if isinstance(response.get("tool_calls"), list) else []
            tools = [_safe_text(c.get("name"), 64) for c in calls if isinstance(c, dict) and c.get("name")]
            return {"offset": offset, "kind": "model_response", "phase": presentation_phase(tools), "sequence": raw.get("sequence"),
                    "text": _safe_text(response.get("text"), 700), "tools": tools,
                    "finish_reason": _safe_text(response.get("finish_reason"), 40)}
        if raw.get("kind") == "tool_result":
            result = raw.get("result") if isinstance(raw.get("result"), dict) else {}
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            tool = _safe_text(result.get("tool") or "tool", 64)
            ok = result.get("ok") is True
            summary = "Completed successfully."
            if not ok:
                summary = f"Failed: {_safe_text(error.get('kind') or 'tool_error', 80)}."
            elif result.get("truncated"):
                summary = "Completed successfully; output was truncated."
            elif tool == "run_command" and data.get("command"):
                summary = f"Command succeeded: {_safe_text(data.get('command'), 180)}"
            elif tool == "apply_patch":
                files = data.get("files") if isinstance(data.get("files"), int) else 0
                additions = data.get("additions") if isinstance(data.get("additions"), int) else 0
                deletions = data.get("deletions") if isinstance(data.get("deletions"), int) else 0
                summary = f"Patch applied: {files} files, +{additions}, -{deletions}."
            elif tool == "submit":
                summary = "Completion submitted."
            return {"offset": offset, "kind": "tool_result", "phase": presentation_phase([tool]), "sequence": raw.get("sequence"),
                    "tool": tool, "ok": ok, "summary": summary}
        return None

    def patch(self, record: TaskRecord) -> tuple[str, dict[str, object]]:
        run_dir = self._run_dir(record)
        path = run_dir / "agent.patch" if run_dir else None
        if path is None or not path.is_file():
            return "", {"files": 0, "additions": 0, "deletions": 0, "bytes": 0}
        data = path.read_bytes()
        if len(data) > MAX_PATCH_BYTES:
            raise ValueError("Patch is too large to preview.")
        patch = (record.result or {}).get("patch")
        stats = patch if isinstance(patch, dict) else {}
        return data.decode("utf-8", errors="replace"), {key: stats.get(key, 0) for key in ("files", "additions", "deletions", "bytes")}

    def shutdown_active(self) -> None:
        with self.lock:
            process = self.tasks[self.active_id].process if self.active_id and self.active_id in self.tasks else None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


class DemoServer(ThreadingHTTPServer):
    daemon_threads, allow_reuse_address = True, True

    def __init__(self, address: tuple[str, int], ontology: dict[str, object], tasks: TaskManager):
        super().__init__(address, DemoHandler)
        self.ontology, self.tasks = ontology, tasks

    def server_close(self) -> None:
        self.tasks.shutdown_active()
        super().server_close()


class DemoHandler(BaseHTTPRequestHandler):
    server_version, sys_version = "ReqCodingAgentDemo", ""
    static_files = {path: ("index.html", "text/html; charset=utf-8") for path in
                    ("/", "/settings", "/settings/ontology")}
    static_files.update({"/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
                         "/static/task.css": ("task.css", "text/css; charset=utf-8")})

    def log_message(self, format: str, *args: object) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed, path = urlsplit(self.path), unquote(urlsplit(self.path).path)
        if ".." in Path(path).parts:
            self._not_found(); return
        if path == "/api/health" and not parsed.query:
            verified = bool(self.server.ontology.get("verified"))
            self._json({"status": "ok" if verified else "integrity_failure", "read_only": False, "ontology_verified": verified}, 200 if verified else 409)
        elif path == "/api/ontology" and not parsed.query:
            self._json(self.server.ontology, 200 if self.server.ontology.get("verified") else 409)
        elif path == "/api/runtime" and not parsed.query:
            self._json(self.server.tasks.runtime())
        elif match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})(?:/(events|patch|patch/download))?", path):
            self._task_get(match.group(1), match.group(2), parsed.query)
        elif not parsed.query and path in self.static_files:
            filename, content_type = self.static_files[path]
            self._bytes((STATIC_ROOT / filename).read_bytes(), content_type)
        else:
            self._not_found()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.query:
            self._method_not_allowed(); return
        if parsed.path == "/api/workspace":
            self._set_workspace(); return

        # Check for interview endpoints
        if match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})/(answer|confirm)", parsed.path):
            self._interview_action(match.group(1), match.group(2)); return

        if parsed.path != "/api/tasks":
            self._method_not_allowed(); return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json({"error": "content type must be application/json"}, 415); return
        origin, host = self.headers.get("Origin"), self.headers.get("Host")
        if origin and origin.rstrip("/") != f"http://{host}":
            self._json({"error": "origin not allowed"}, 403); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_TASK_BYTES:
            self._json({"error": "invalid request size"}, 400); return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeError, json.JSONDecodeError):
            self._json({"error": "invalid JSON"}, 400); return
        if not isinstance(payload, dict) or set(payload) != {"task"} or not isinstance(payload["task"], str):
            self._json({"error": "body must contain only a task string"}, 400); return
        task = payload["task"].strip()
        if not task or len(task.encode("utf-8")) > MAX_TASK_BYTES:
            self._json({"error": "task must be non-empty and within the size limit"}, 400); return
        try:
            record = self.server.tasks.start(task)
        except (RuntimeError, WorkspaceError) as error:
            self._json({"error": str(error)}, 409); return
        self._json(self.server.tasks.public_task(record), 202)

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    do_PUT = do_HEAD
    do_PATCH = do_HEAD
    do_DELETE = do_HEAD

    def _set_workspace(self) -> None:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json({"error": "content type must be application/json"}, 415); return
        origin, host = self.headers.get("Origin"), self.headers.get("Host")
        if origin and origin.rstrip("/") != f"http://{host}":
            self._json({"error": "origin not allowed"}, 403); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_TASK_BYTES:
            self._json({"error": "invalid request size"}, 400); return
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeError, json.JSONDecodeError):
            self._json({"error": "invalid JSON"}, 400); return
        if not isinstance(payload, dict) or set(payload) != {"path"} or not isinstance(payload["path"], str):
            self._json({"error": "body must contain only a path string"}, 400); return
        path = payload["path"].strip()
        if not path or len(path.encode("utf-8")) > MAX_TASK_BYTES:
            self._json({"error": "path must be non-empty and within the size limit"}, 400); return
        try:
            self.server.tasks.set_workspace(Path(path))
        except (RuntimeError, WorkspaceError) as error:
            self._json({"error": str(error)}, 409); return
        self._json(self.server.tasks.runtime())

    def _interview_action(self, task_id: str, action: str) -> None:
        """Handle interview answer or confirm actions."""
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json({"error": "content type must be application/json"}, 415); return
        origin, host = self.headers.get("Origin"), self.headers.get("Host")
        if origin and origin.rstrip("/") != f"http://{host}":
            self._json({"error": "origin not allowed"}, 403); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1

        max_size = MAX_ANSWER_BYTES if action == "answer" else 1024
        if length < 0 or length > max_size:
            self._json({"error": "invalid request size"}, 400); return

        try:
            payload = json.loads(self.rfile.read(length)) if length > 0 else {}
        except (UnicodeError, json.JSONDecodeError):
            self._json({"error": "invalid JSON"}, 400); return

        if not isinstance(payload, dict):
            self._json({"error": "body must be an object"}, 400); return

        try:
            if action == "answer":
                if set(payload) != {"turn_id", "answer"}:
                    self._json({"error": "body must contain turn_id and answer"}, 400); return
                if not isinstance(payload["turn_id"], str) or not isinstance(payload["answer"], str):
                    self._json({"error": "turn_id and answer must be strings"}, 400); return
                result = self.server.tasks.submit_answer(task_id, payload["turn_id"], payload["answer"])
                self._json(result, 202)
            elif action == "confirm":
                if payload:  # Confirm should have empty body
                    self._json({"error": "body must be empty for confirm"}, 400); return
                result = self.server.tasks.confirm_baseline(task_id)
                self._json(result, 202)
        except ValueError as error:
            self._json({"error": str(error)}, 400); return
        except RuntimeError as error:
            self._json({"error": str(error)}, 409); return

    def _task_get(self, task_id: str, resource: str | None, query: str) -> None:
        record = self.server.tasks.get(task_id)
        if record is None:
            self._not_found(); return
        if resource is None and not query:
            self._json(self.server.tasks.public_task(record)); return
        if resource == "events":
            values = parse_qs(query, keep_blank_values=True)
            try:
                if set(values) - {"after"}: raise ValueError
                after = int(values.get("after", ["0"])[0])
                if after < 0: raise ValueError
            except ValueError:
                self._json({"error": "after must be a non-negative integer"}, 400); return
            events, next_offset, complete = self.server.tasks.events(record, after)
            self._json({"events": events, "next_offset": next_offset, "complete": complete}); return
        if resource in {"patch", "patch/download"} and not query:
            try:
                patch, stats = self.server.tasks.patch(record)
            except ValueError as error:
                self._json({"error": str(error)}, 413); return
            if resource == "patch":
                self._json({"patch": patch, "stats": stats})
            else:
                self._bytes(patch.encode(), "text/x-diff; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="reqagent-{task_id[:8]}.patch"'})
            return
        self._not_found()

    def _json(self, payload: object, status: int = 200) -> None:
        self._bytes(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), "application/json; charset=utf-8", status)

    def _bytes(self, body: bytes, content_type: str, status: int = 200, *, headers: dict[str, str] | None = None) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items(): self.send_header(name, value)
        self.end_headers(); self.wfile.write(body)

    def _not_found(self) -> None:
        self._json({"error": "not found"}, 404)

    def _method_not_allowed(self) -> None:
        self.send_response(405); self.send_header("Allow", "GET, POST")
        body = b'{"error":"method not allowed"}'; self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


def create_server(host: str = "127.0.0.1", port: int = 8765, project_root: Path = PROJECT_ROOT, *,
                  workspace: Path | None = None, config: Path | None = None,
                  artifact_root: Path | None = None, python: str = sys.executable, in_place: bool = True,
                  scenario: str | None = None) -> DemoServer:
    root = project_root.resolve()
    task_kwargs = {"project_root": root, "python": python, "in_place": in_place}
    if scenario is not None:
        task_kwargs["scenario"] = scenario
    tasks = TaskManager(workspace, config or root / "configs/agent/demo-openai.json",
                        artifact_root or root / "artifacts/runs/demo-gui", **task_kwargs)
    return DemoServer((host, port), load_ontology(root, scenario), tasks)


def _apply_provider_env(config: Path) -> None:
    if config.resolve() != (PROJECT_ROOT / "configs/agent/demo-openai.json").resolve():
        return
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("Warning: OPENAI_API_KEY is not set; live Agent tasks will fail until it is configured.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the ReqCodingAgent local demo GUI")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1",))
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--workspace", default=None, type=Path)
    parser.add_argument("--config", default=DEFAULT_CONFIG, type=Path)
    parser.add_argument("--artifact-root", default=DEFAULT_ARTIFACT_ROOT, type=Path)
    parser.add_argument("--demo-scenario", choices=("stock-search",), default=None,
                        help="Enable an explicit deterministic presentation scenario.")
    args = parser.parse_args(argv)
    config = args.config.expanduser().resolve()
    _apply_provider_env(config)
    try:
        server = create_server(args.host, args.port, workspace=args.workspace, config=config, artifact_root=args.artifact_root,
                               scenario=args.demo_scenario)
    except (FrozenDataError, WorkspaceError) as error:
        print(f"Cannot start: {error}"); return 1
    print(f"ReqCodingAgent demo available at http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
