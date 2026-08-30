"""
core/state.py
Project state management — load, save, update, and log agent projects.

Project folder structure:
  projects/
    acme-insurance-assistant/
      acme-insurance-assistant.json
      acme-insurance-assistant_20260428_1430.log
      acme-insurance-assistant_20260428_1430.debug.log  (debug mode only)
      sql/
        acme-insurance-assistant_20260428_1430.sql
"""

import json
import traceback
from datetime import datetime
from pathlib import Path

from core import config as cfg_module


# ── Directory helpers ─────────────────────────────────────────────────────────

def projects_dir(cfg) -> Path:
    d = Path(cfg_module.get(cfg, "builder", "projects_dir", fallback="./projects"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(cfg, slug: str) -> Path:
    d = projects_dir(cfg) / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def sql_dir(cfg, slug: str) -> Path:
    d = project_dir(cfg, slug) / "sql"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_debug(cfg) -> bool:
    return cfg_module.get(cfg, "builder", "debug", fallback="false").lower() == "true"


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _log_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Log file paths ────────────────────────────────────────────────────────────

def run_log_path(cfg, slug: str, session_ts: str) -> Path:
    return project_dir(cfg, slug) / f"{slug}_{session_ts}.log"


def debug_log_path(cfg, slug: str, session_ts: str) -> Path:
    return project_dir(cfg, slug) / f"{slug}_{session_ts}.debug.log"


def sql_snapshot_path(cfg, slug: str, session_ts: str) -> Path:
    return sql_dir(cfg, slug) / f"{slug}_{session_ts}.sql"


# ── RunLog class ──────────────────────────────────────────────────────────────

class RunLog:
    """
    Writes timestamped entries to the run log and optionally the debug log.
    Passed through conversation.py and db.py so all output is captured.
    """

    def __init__(self, cfg, project: dict):
        self._cfg       = cfg
        self._slug      = project.get("project_name", "unnamed")
        self._debug     = is_debug(cfg)
        self._session_ts = project.get("session_ts", _ts())
        self._run_path   = run_log_path(cfg, self._slug, self._session_ts)
        self._debug_path = debug_log_path(cfg, self._slug, self._session_ts) if self._debug else None
        self._turn_id    = 0
        self._active_turn_id = 0

        header = (
            f"\n{'=' * 60}\n"
            f"  Session : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"  Project : {project.get('display_name', self._slug)}\n"
            f"  Schema  : {project.get('schema', '')}\n"
            f"  Phase   : {project.get('phase', 'unknown')}\n"
            f"  Debug   : {'ON' if self._debug else 'OFF'}\n"
            f"{'=' * 60}\n"
        )
        self._write_run(header)
        if self._debug:
            self._write_debug(header)

    # ── Internal writers ──────────────────────────────────────────────────────

    def _write_run(self, text: str):
        try:
            with open(self._run_path, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _write_debug(self, text: str):
        if self._debug_path:
            try:
                with open(self._debug_path, "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass

    # ── Public API ────────────────────────────────────────────────────────────

    def log(self, line: str):
        """Write a plain timestamped line to the run log."""
        self._write_run(f"[{_log_ts()}] {line}\n")

    def log_section(self, title: str):
        entry = f"\n[{_log_ts()}] {'─' * 20} {title} {'─' * 20}\n"
        self._write_run(entry)
        if self._debug:
            self._write_debug(entry)

    def log_user(self, text: str, step: int = None):
        """Log one user turn. Increments the turn id exactly once per user input."""
        self._turn_id += 1
        self._active_turn_id = self._turn_id
        step_part = f" step={step}" if step is not None else ""
        self.log(f"TURN {self._active_turn_id:03d} USER{step_part}: {text}")

    def log_assistant(self, text: str, step: int = None):
        """Log the assistant response for the active user turn."""
        turn = self._active_turn_id or self._turn_id
        step_part = f" step={step}" if step is not None else ""
        self.log(f"TURN {turn:03d} ASSISTANT{step_part}: {text[:300]}{'...' if len(text) > 300 else ''}")

    def log_state(self, label: str, data):
        """Log structured state/facts changes for debugging."""
        try:
            payload = json.dumps(data, sort_keys=True)
        except Exception:
            payload = str(data)
        turn = self._active_turn_id or self._turn_id
        self.log(f"TURN {turn:03d} STATE {label}: {payload}")

    def log_sql(self, stmt_num: int, sql: str, success: bool,
                error: str = None, duration_ms: int = None):
        dur = f" ({duration_ms}ms)" if duration_ms is not None else ""
        status = "OK" if success else "FAILED"
        line = f"SQL stmt {stmt_num}: {status}{dur}"
        if error:
            line += f" — {error}"
        self.log(line)
        if self._debug:
            debug_entry = (
                f"\n[{_log_ts()}] SQL STATEMENT {stmt_num} — {status}{dur}\n"
                f"{'-' * 50}\n"
                f"{sql.strip()}\n"
            )
            if error:
                debug_entry += f"\nERROR:\n{error}\n"
            debug_entry += f"{'-' * 50}\n"
            self._write_debug(debug_entry)

    def log_llm(self, model: str, prompt_chars: int, response_chars: int,
                duration_ms: int, error: str = None):
        if not self._debug:
            return
        status = "OK" if not error else "FAILED"
        entry = (
            f"\n[{_log_ts()}] LLM CALL — {status} ({duration_ms}ms)\n"
            f"  Model         : {model}\n"
            f"  Prompt chars  : {prompt_chars}\n"
            f"  Response chars: {response_chars}\n"
        )
        if error:
            entry += f"  Error: {error}\n"
        self._write_debug(entry)

    def log_exception(self, context: str, exc: Exception):
        self.log(f"ERROR in {context}: {exc}")
        if self._debug:
            tb = traceback.format_exc()
            entry = (
                f"\n[{_log_ts()}] EXCEPTION in {context}\n"
                f"{'-' * 50}\n{tb}\n{'-' * 50}\n"
            )
            self._write_debug(entry)

    @property
    def run_path(self) -> Path:
        return self._run_path

    @property
    def debug_path(self) -> Path:
        return self._debug_path

    @property
    def debug_enabled(self) -> bool:
        return self._debug


def open_run_log(cfg, project: dict) -> RunLog:
    return RunLog(cfg, project)


# ── SQL snapshot ──────────────────────────────────────────────────────────────

def save_sql_snapshot(cfg, project: dict, sql: str) -> Path:
    slug       = project.get("project_name", "unnamed")
    session_ts = project.get("session_ts", _ts())
    path       = sql_snapshot_path(cfg, slug, session_ts)
    header = (
        f"-- ============================================================\n"
        f"-- Project  : {project.get('display_name', slug)}\n"
        f"-- Schema   : {project.get('schema', '')}\n"
        f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"-- ============================================================\n\n"
    )
    path.write_text(header + sql, encoding="utf-8")
    return path


# ── Project CRUD ──────────────────────────────────────────────────────────────

def list_projects(cfg) -> list:
    """Return list of saved project dicts sorted by last modified."""
    base     = projects_dir(cfg)
    projects = []

    # New structure: projects/<slug>/<slug>.json
    for subdir in base.iterdir():
        if not subdir.is_dir():
            continue
        if subdir.name == "_deleted":   # skip archive folder
            continue
        json_file = subdir / f"{subdir.name}.json"
        if not json_file.exists():
            jsons = list(subdir.glob("*.json"))
            if not jsons:
                continue
            json_file = jsons[0]
        try:
            data = json.loads(json_file.read_text())
            projects.append({
                "file":        json_file,
                "dir":         subdir,
                "name":        data.get("display_name") or data.get("project_name", subdir.name),
                "slug":        data.get("project_name", subdir.name),
                "phase":       data.get("phase", "unknown"),
                "created_at":  data.get("created_at", ""),
                "modified_at": data.get("modified_at", ""),
            })
        except Exception:
            pass

    # Legacy: flat .json files in projects/ root
    for f in base.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            slug = data.get("project_name", f.stem)
            if any(p["slug"] == slug for p in projects):
                continue
            projects.append({
                "file":        f,
                "dir":         None,
                "name":        data.get("display_name") or slug,
                "slug":        slug,
                "phase":       data.get("phase", "unknown"),
                "created_at":  data.get("created_at", ""),
                "modified_at": data.get("modified_at", ""),
            })
        except Exception:
            pass

    return sorted(projects, key=lambda p: p["modified_at"], reverse=True)


def load_project(path) -> dict:
    return json.loads(Path(path).read_text())


def save_project(cfg, project: dict) -> Path:
    slug = project.get("project_name", "unnamed")
    d    = project_dir(cfg, slug)
    path = d / f"{slug}.json"
    project["modified_at"] = datetime.now().isoformat()
    path.write_text(json.dumps(project, indent=2))
    return path


def new_project(project_name: str, schema: str) -> dict:
    ts = _ts()
    return {
        "project_name":  project_name,
        "schema":        schema,
        "created_at":    datetime.now().isoformat(),
        "modified_at":   datetime.now().isoformat(),
        "session_ts":    ts,
        "phase":         "discovery",
        "workflow": {
            "current_step": 1,
            "last_completed_step": 1,
            "edit_mode": False,
        },
        "conversation":  [],
        "facts": {
            "data_source": None,
            "rag": {
                "subject": "",
                "file_types": [],
                "object_storage_url": "",
            },
            "sql": {
                "tables": [],
                "question_types": "",
            },
            "names": {},
            "analysis_tools": [],
            "agent_role": "",
            "task": {
                "agent_name": "",
                "instruction": "",
                "task_name": "",
                "team_name": "",
            },
        },
        "spec":          {
            "profiles":       [],
            "vector_indexes": [],
            "tools":          [],
            "agents":         [],
            "tasks":          [],
            "teams":          [],
        },
        "generated_sql": "",
        "build_log":     [],
    }


def new_comment_workspace(project_name: str, schema: str, tables: list,
                           purpose: str = "") -> dict:
    """Create a minimal project shell for schema-direct NL2SQL comment work —
    no agent-build intent, just enough state for modules/comments.py to
    scan, profile, and draft comments against the given tables.

    This deliberately reuses new_project()'s shape rather than inventing a
    parallel structure, so every existing comments.py function (which reads
    project["schema"], project["facts"]["sql"]["tables"], etc.) works on it
    unchanged. phase="comments_only" distinguishes it from a real in-progress
    agent build in project listings (e.g. the Resume picker), though nothing
    prevents resuming it into a full build later if that's ever wanted —
    the workflow/spec scaffolding is already there.

    tables: table names, with or without schema prefix (e.g. both
    "ACME_GL_TRANSACTIONS" and "ACME_CORP.ACME_GL_TRANSACTIONS"
    are accepted) — unqualified names are assumed to belong to `schema`.
    """
    project = new_project(project_name, schema)
    project["phase"] = "comments_only"
    qualified = []
    for t in tables:
        t = str(t).strip()
        if not t:
            continue
        qualified.append(t if "." in t else f"{schema}.{t}")
    project["facts"]["sql"]["tables"] = qualified
    if purpose:
        project["facts"]["sql"]["question_types"] = purpose
    return project


def ensure_workflow(project: dict) -> dict:
    """Ensure explicit workflow state exists on a project."""
    workflow = project.setdefault("workflow", {})
    workflow.setdefault("current_step", 1)
    workflow.setdefault("last_completed_step", 1)
    workflow.setdefault("edit_mode", False)
    return workflow


def set_workflow_step(project: dict, current_step: int = None,
                      last_completed_step: int = None,
                      edit_mode: bool = None) -> dict:
    """Set explicit workflow state without inferring from conversation text."""
    workflow = ensure_workflow(project)
    if current_step is not None:
        workflow["current_step"] = max(1, min(7, int(current_step)))
    if last_completed_step is not None:
        workflow["last_completed_step"] = max(1, min(7, int(last_completed_step)))
    if edit_mode is not None:
        workflow["edit_mode"] = bool(edit_mode)
    return project


def get_last_completed_step(project: dict) -> int:
    """Return last completed workflow step with safe fallbacks for legacy projects."""
    workflow = ensure_workflow(project)
    try:
        step = int(workflow.get("last_completed_step", 1))
    except Exception:
        step = 1
    if project.get("spec") and project.get("phase") in ("review", "complete"):
        step = max(step, 7)
    return max(1, min(7, step))


def refresh_session_ts(project: dict) -> dict:
    """New session_ts for each resume so logs don't overwrite each other."""
    project["session_ts"] = _ts()
    return project


def update_phase(project: dict, phase: str) -> dict:
    ensure_workflow(project)
    project["phase"] = phase
    return project


def add_to_conversation(project: dict, role: str, text: str,
                        step: int = None) -> dict:
    turn = {
        "role":      role.upper(),
        "text":      text,
        "timestamp": datetime.now().isoformat(),
    }
    if step is not None:
        turn["step"] = step
    project["conversation"].append(turn)
    return project


def archive_dir(cfg) -> Path:
    """Return (and create) the _deleted archive folder inside projects/."""
    d = projects_dir(cfg) / "_deleted"
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_project(cfg, slug: str) -> bool:
    """
    Move a project to the _deleted archive folder rather than permanently deleting it.
    Folder is renamed with a timestamp so multiple deletes of the same name don't collide.
    Legacy flat JSON files are moved into _deleted/ directly.
    Returns True if something was moved, False if nothing found.
    """
    import shutil
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # New structure: projects/<slug>/ → projects/_deleted/<slug>_<ts>/
    d = projects_dir(cfg) / slug
    if d.exists() and d.is_dir():
        dest = archive_dir(cfg) / f"{slug}_{ts}"
        shutil.move(str(d), str(dest))
        return True

    # Legacy flat file: projects/<slug>.json → projects/_deleted/<slug>_<ts>.json
    f = projects_dir(cfg) / f"{slug}.json"
    if f.exists():
        dest = archive_dir(cfg) / f"{slug}_{ts}.json"
        shutil.move(str(f), str(dest))
        return True

    return False
