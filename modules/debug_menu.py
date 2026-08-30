"""
modules/debug_menu.py
Option 8 — Debug & Diagnostics menu.
"""

import json
import time
from pathlib import Path
from datetime import datetime

from core import config as cfg_module
from core import state as state_module


def run(cfg, config_path: str, clients, display):
    C = display.C

    while True:
        debug_on = state_module.is_debug(cfg)
        display.head("DEBUG & DIAGNOSTICS")
        print(f"  Debug mode : {C.GREEN}ON{C.RESET}" if debug_on else
              f"  Debug mode : {C.DIM}OFF{C.RESET}")
        print()
        print(f"   1.  Toggle debug mode  (currently: {'ON' if debug_on else 'OFF'})")
        print(f"   2.  View project logs")
        print(f"   3.  View last SQL snapshot")
        print(f"   4.  Test DB connection")
        print(f"   5.  Test LLM connection")
        print(f"   6.  Show project folder structure")
        print(f"   b.  Back")
        print()

        try:
            choice = input(f"  {C.BOLD}[number / b=back]:{C.RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if choice in ("b", "back", "q"):
            return
        elif choice == "1":
            _toggle_debug(cfg, config_path, display, debug_on)
            # Reload config so the toggle takes effect in the current session
            try:
                cfg = cfg_module.load(config_path)
            except Exception:
                pass
        elif choice == "2":
            _view_logs(cfg, display)
        elif choice == "3":
            _view_last_sql(cfg, display)
        elif choice == "4":
            _test_db(cfg, display)
        elif choice == "5":
            _test_llm(cfg, clients, display)
        elif choice == "6":
            _show_structure(cfg, display)
        else:
            display.warn("Enter a number 1-6 or b")

        print()
        try:
            input(f"  {C.DIM}Press Enter to continue...{C.RESET}")
        except (EOFError, KeyboardInterrupt):
            return


# ── Toggle debug ──────────────────────────────────────────────────────────────

def _toggle_debug(cfg, config_path: str, display, currently_on: bool):
    new_val = "false" if currently_on else "true"
    cfg_module.update_value(config_path, "builder", "debug", new_val)
    state = "ON" if new_val == "true" else "OFF"
    display.ok(f"Debug mode set to {state}")
    if new_val == "true":
        display.info("Debug logs will be written to each project's folder.")
        display.info("Full SQL statements, LLM call details, and Python tracebacks are captured.")


# ── View logs ─────────────────────────────────────────────────────────────────

def _view_logs(cfg, display):
    C = display.C
    projects = state_module.list_projects(cfg)
    if not projects:
        display.warn("No projects found.")
        return

    display.blank()
    print(f"  {'#':<4}  {'Project':<35}  {'Phase':<12}")
    print(f"  {'─'*4}  {'─'*35}  {'─'*12}")
    for i, p in enumerate(projects, 1):
        print(f"  {i:<4}  {p['name']:<35}  {p['phase']:<12}")

    display.blank()
    try:
        raw = input("  Select project [number / Enter=cancel]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not raw:
        return
    try:
        p = projects[int(raw) - 1]
    except (ValueError, IndexError):
        display.err("Invalid selection.")
        return

    proj_dir = p.get("dir") or state_module.project_dir(cfg, p["slug"])
    if not proj_dir or not Path(proj_dir).exists():
        display.err("Project folder not found.")
        return

    # List all log files
    log_files = sorted(Path(proj_dir).glob("*.log"), reverse=True)
    if not log_files:
        display.warn("No log files found for this project.")
        return

    display.blank()
    print(f"  Log files for: {p['name']}")
    print(f"  {'─'*60}")
    for i, lf in enumerate(log_files, 1):
        size = lf.stat().st_size
        mtime = datetime.fromtimestamp(lf.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        tag = " [debug]" if ".debug." in lf.name else ""
        print(f"  {i}.  {lf.name}{tag}  ({size:,} bytes, {mtime})")

    display.blank()
    try:
        raw = input("  View file [number / Enter=cancel]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not raw:
        return
    try:
        chosen = log_files[int(raw) - 1]
    except (ValueError, IndexError):
        display.err("Invalid selection.")
        return

    display.blank()
    print(f"  {C.BOLD}{'─'*60}{C.RESET}")
    print(f"  {chosen.name}")
    print(f"  {C.BOLD}{'─'*60}{C.RESET}")
    try:
        content = chosen.read_text(encoding="utf-8")
        # Show last 100 lines
        lines = content.splitlines()
        if len(lines) > 100:
            print(f"  {C.DIM}(showing last 100 of {len(lines)} lines){C.RESET}")
            lines = lines[-100:]
        for line in lines:
            print(f"  {line}")
    except Exception as ex:
        display.err(f"Could not read file: {ex}")
    print(f"  {C.BOLD}{'─'*60}{C.RESET}")


# ── View last SQL snapshot ─────────────────────────────────────────────────────

def _view_last_sql(cfg, display):
    C = display.C
    projects = state_module.list_projects(cfg)
    if not projects:
        display.warn("No projects found.")
        return

    # Find the most recent SQL file across all projects
    all_sql = []
    for p in projects:
        proj_dir = p.get("dir") or state_module.project_dir(cfg, p["slug"])
        sql_folder = Path(proj_dir) / "sql"
        if sql_folder.exists():
            for f in sql_folder.glob("*.sql"):
                all_sql.append((f, p["name"]))

    if not all_sql:
        display.warn("No SQL snapshots found.")
        return

    all_sql.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    latest, proj_name = all_sql[0]

    display.blank()
    print(f"  {C.BOLD}Most recent SQL snapshot{C.RESET}")
    print(f"  Project : {proj_name}")
    print(f"  File    : {latest.name}")
    print(f"  {'─'*60}")
    try:
        for line in latest.read_text(encoding="utf-8").splitlines():
            print(f"  {line}")
    except Exception as ex:
        display.err(f"Could not read file: {ex}")
    print(f"  {'─'*60}")


# ── Test DB connection ────────────────────────────────────────────────────────

def _test_db(cfg, display):
    display.info("Testing ADW connection...")
    try:
        from core.db import connect
        start = time.time()
        conn = connect(cfg)
        cur  = conn.cursor()
        cur.execute("SELECT 'OK' FROM DUAL")
        result = cur.fetchone()[0]
        ms = int((time.time() - start) * 1000)
        conn.close()
        if result == "OK":
            display.ok(f"DB connection successful ({ms}ms)")
        else:
            display.err(f"Unexpected result: {result}")
    except Exception as ex:
        import traceback
        display.err(f"DB connection failed: {ex}")
        display.blank()
        print(traceback.format_exc())


# ── Test LLM connection ───────────────────────────────────────────────────────

def _test_llm(cfg, clients, display):
    if clients is None:
        display.err("OCI clients not initialised — return to menu and they will be created.")
        return

    model = cfg_module.get(cfg, "llm", "chat_model", fallback="(not set)")
    display.info(f"Testing LLM: {model}")
    try:
        from core import llm as llm_module
        start = time.time()
        response = llm_module.generate(
            clients, cfg,
            prompt      = "Reply with exactly: OK",
            max_tokens  = 10,
            temperature = 0.0,
        )
        ms = int((time.time() - start) * 1000)
        display.ok(f"LLM responded in {ms}ms")
        display.info(f"Response: {response.strip()[:100]}")
    except Exception as ex:
        import traceback
        display.err(f"LLM test failed: {ex}")
        display.blank()
        print(traceback.format_exc())


# ── Show project structure ────────────────────────────────────────────────────

def _show_structure(cfg, display):
    C = display.C
    base = state_module.projects_dir(cfg)
    display.blank()
    print(f"  {C.BOLD}{base}/{C.RESET}")

    projects = state_module.list_projects(cfg)
    if not projects:
        print(f"    {C.DIM}(no projects){C.RESET}")
        return

    for p in projects:
        proj_dir = p.get("dir") or base / p["slug"]
        proj_dir = Path(proj_dir)
        print(f"  {C.CYAN}├── {p['slug']}/{C.RESET}  [{p['phase']}]")

        # JSON file
        jf = proj_dir / f"{p['slug']}.json"
        if jf.exists():
            print(f"  │   ├── {jf.name}  ({jf.stat().st_size:,}b)")

        # Log files
        logs = sorted(proj_dir.glob("*.log"), reverse=True)
        for lf in logs[:3]:
            tag = " [debug]" if ".debug." in lf.name else ""
            print(f"  │   ├── {lf.name}{tag}  ({lf.stat().st_size:,}b)")
        if len(logs) > 3:
            print(f"  │   ├── ... ({len(logs)-3} more log files)")

        # SQL snapshots
        sql_folder = proj_dir / "sql"
        if sql_folder.exists():
            sqls = sorted(sql_folder.glob("*.sql"), reverse=True)
            if sqls:
                print(f"  │   └── sql/")
                for sf in sqls[:2]:
                    print(f"  │       ├── {sf.name}  ({sf.stat().st_size:,}b)")
                if len(sqls) > 2:
                    print(f"  │       └── ... ({len(sqls)-2} more)")

    # Show archive folder if it exists
    deleted_dir = base / "_deleted"
    if deleted_dir.exists():
        archived = sorted(deleted_dir.iterdir(), reverse=True)
        if archived:
            print(f"  {C.DIM}├── _deleted/  ({len(archived)} archived project(s)){C.RESET}")
            for a in archived[:3]:
                print(f"  {C.DIM}│   └── {a.name}{C.RESET}")
            if len(archived) > 3:
                print(f"  {C.DIM}│   └── ... ({len(archived)-3} more){C.RESET}")
