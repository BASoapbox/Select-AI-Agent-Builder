#!/usr/bin/env python3
"""
agent_builder.py
================
Select AI Agent Builder v6.0 — interactive tool for data scientists to build,
manage, and test Oracle Select AI Agent configurations.

Usage:
  python agent_builder.py
  python agent_builder.py --config /path/to/config.ini
  python agent_builder.py --option 1    # run specific option directly (skips menu)

Requires:
  pip install oci oracledb
"""

import argparse
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import oci
except ImportError:
    print("ERROR: oci not installed.  Run: pip install oci")
    sys.exit(1)

from core import config as cfg_module
from core import oci_clients


# ─────────────────────────────────────────────────────────────────────────────
# Display helper
# ─────────────────────────────────────────────────────────────────────────────

class Display:
    class C:
        RED    = "\033[91m"
        GREEN  = "\033[92m"
        YELLOW = "\033[93m"
        CYAN   = "\033[96m"
        BLUE   = "\033[94m"
        BOLD   = "\033[1m"
        DIM    = "\033[2m"
        RESET  = "\033[0m"

    def ok(self, msg):   print(f"  {self.C.GREEN}✓{self.C.RESET}  {msg}")
    def warn(self, msg): print(f"  {self.C.YELLOW}⚠{self.C.RESET}  {msg}")
    def err(self, msg):  print(f"  {self.C.RED}✗{self.C.RESET}  {msg}")
    def info(self, msg): print(f"  {self.C.CYAN}→{self.C.RESET}  {msg}")
    def blank(self):     print()
    def head(self, msg): print(f"\n{self.C.BOLD}{msg}{self.C.RESET}\n" + "─" * 64)


display = Display()
C = Display.C


# ─────────────────────────────────────────────────────────────────────────────
# Menu structure
# ─────────────────────────────────────────────────────────────────────────────

MENU = [
    # (key, label, group)
    ("check_config",  "Check config file for missing or empty values",   "SETUP"),
    ("preflight",     "Pre-flight check — verify DE setup is complete",  "SETUP"),
    ("list_models",   "View available OCI GenAI models and update LLM",  "SETUP"),
    ("storage",       "Create bucket / upload documents to Object Storage (RAG)",   "OBJECT STORAGE"),
    ("new_project",   "Start new agent project","BUILD"),
    ("resume",        "Resume existing project",                          "BUILD"),
    ("review_menu",   "Review & manage ▸",                               "REVIEW & MANAGE"),
    ("debug",         "Debug & diagnostics",                             "DEBUG"),
    ("quit",          "Quit",                                            ""),
]

SUBMENUS = {
    "review_menu": {
        "title": "REVIEW & MANAGE",
        "label": "list / view / update / delete / rebuild / test",
        "items": [
            ("list",    "List existing tools / agents / tasks / teams"),
            ("view",    "View object detail and tool invocation history"),
            ("update",  "Update tool instruction or agent role or task instruction"),
            ("comments", "Manage NL2SQL comments"),
            ("delete",  "Delete tool / agent / task / team"),
            ("rebuild", "Rebuild agent stack (drop + recreate)"),
            ("test",    "Run test prompt against a team"),
            ("back",    "← Back"),
            ("quit",    "Quit"),
        ],
    },
}


def print_banner(config_path: str):
    print(f"\n{C.BOLD}{'═' * 64}{C.RESET}")
    print(f"{C.BOLD}  Select AI Agent Builder v6.0{C.RESET}")
    print(f"{'═' * 64}")
    print(f"  Config : {C.CYAN}{config_path}{C.RESET}")
    print(f"{'─' * 64}")


def _render_items(items, show_groups=True):
    current_group = None
    # Number only non-navigation items
    numbered = [e for e in items if e[0] not in ("quit", "back")]
    num_map  = {e[0]: i for i, e in enumerate(numbered, 1)}

    for entry in items:
        key   = entry[0]
        label = entry[1]
        group = entry[2] if len(entry) > 2 else ""
        if show_groups and group and group != current_group:
            print(f"\n  {C.BOLD}{C.DIM}{group}{C.RESET}")
            current_group = group
        if key == "quit":
            print(f"  {C.DIM} q  {label}{C.RESET}")
            continue
        if key == "back":
            print(f"  {C.DIM} b  {label}{C.RESET}")
            continue
        num = f"{C.BOLD}{num_map[key]:2}.{C.RESET}"
        if key == "check_config":
            print(f"  {num}  {C.BLUE}{label}{C.RESET}")
        elif key == "preflight":
            print(f"  {num}  {C.GREEN}{label}{C.RESET}")
        elif key in ("new_project", "resume"):
            print(f"  {num}  {C.YELLOW}{label}{C.RESET}")
        elif key.endswith("_menu"):
            sub_hint = SUBMENUS[key]["label"]
            print(f"  {num}  {C.CYAN}{label}  {C.DIM}{sub_hint}{C.RESET}")
        else:
            print(f"  {num}  {label}")


def print_menu():
    print()
    _render_items(MENU)
    print()


def print_submenu(menu_key: str):
    sm = SUBMENUS[menu_key]
    print(f"\n  {C.BOLD}{sm['title']}{C.RESET}\n")
    _render_items(sm["items"], show_groups=False)
    print()


def _pick(items) -> str:
    # Navigation items use q/b shortcuts — numbered items are everything else
    keys      = [e[0] for e in items]
    has_back  = "back" in keys
    has_quit  = "quit" in keys
    numbered  = [e for e in items if e[0] not in ("quit", "back")]
    hint_parts = ["number"]
    if has_back: hint_parts.append("b=back")
    if has_quit: hint_parts.append("q=quit")
    hint = " / ".join(hint_parts)

    while True:
        try:
            raw = input(f"  {C.BOLD}[{hint}]:{C.RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        if raw == "q":
            return "quit"
        if raw == "b" and has_back:
            return "back"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(numbered):
                return numbered[idx][0]
            display.warn(f"Please enter a number between 1 and {len(numbered)}")
        except ValueError:
            display.warn(f"Enter a number{', b' if has_back else ''}, or q")


def get_menu_choice() -> str:
    return _pick(MENU)


def get_submenu_choice(menu_key: str) -> str:
    return _pick(SUBMENUS[menu_key]["items"])


# Sentinels — dispatch returns these to control the main loop
_SILENT = "__SILENT__"   # skip the "Press Enter" pause
_QUIT   = "__QUIT__"     # break the main loop entirely (quit from sub-menu)


def _unpack(result, current_clients):
    """
    Unpack dispatch result. Always returns (clients, skip_pause, do_quit).

    Handles every shape dispatch can return:
      None                → (current_clients, False, False) — show pause
      plain clients value → (clients, False, False)         — show pause
      (clients, _SILENT)  → (clients, True, False)          — skip pause
      (clients, _QUIT)    → (clients, True, True)           — skip + quit
    """
    if result is None:
        return current_clients, False, False
    if not isinstance(result, tuple):
        # Plain return value (clients object or anything else)
        return result, False, False
    if len(result) == 2:
        clients, flag = result
        if flag == _QUIT:
            return clients, True, True
        if flag == _SILENT:
            return clients, True, False
        # Unknown 2-tuple — treat as plain
        return clients, False, False
    # Unexpected shape — return current clients, show pause
    return current_clients, False, False


def run_submenu(menu_key: str, cfg, config_path: str, clients):
    """Loop a sub-menu until Back or Quit. Returns (clients, quit_flag)."""
    do_quit = False
    while True:
        print_banner(config_path)
        print_submenu(menu_key)
        choice = get_submenu_choice(menu_key)
        if choice == "quit":
            do_quit = True
            break
        if choice == "back":
            break
        display.blank()
        skip = False
        try:
            clients, skip, do_quit = _unpack(
                dispatch(choice, cfg, config_path, clients), clients)
            if do_quit:
                break
            if skip:
                continue
        except oci.exceptions.ServiceError as ex:
            display.err(f"OCI API error [{ex.status}]: {ex.message}")
            if ex.code:
                display.err(f"Code: {ex.code}")
        except Exception as ex:
            display.err(f"Error: {ex}")
        display.blank()
        try:
            input(f"  {C.DIM}Press Enter to continue...{C.RESET}")
        except (EOFError, KeyboardInterrupt):
            do_quit = True
            break
    return clients, do_quit


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def dispatch(choice: str, cfg, config_path: str, clients=None):
    """Run the chosen option. Initialises OCI clients lazily. Returns clients."""

    # Sub-menus — no clients needed to open them
    if choice in SUBMENUS:
        clients, do_quit = run_submenu(choice, cfg, config_path, clients)
        return clients, (_QUIT if do_quit else _SILENT)

    # Config check needs no clients
    if choice == "check_config":
        from modules.check_config import run
        run(cfg, config_path, display)
        return clients, _SILENT

    # All other options need OCI clients — init lazily
    if clients is None:
        display.info("Connecting to OCI...")
        try:
            clients = oci_clients.init(cfg)
            display.ok("OCI clients initialised")
        except Exception as ex:
            display.err(f"OCI init failed: {ex}")
            return clients

    # ── SETUP ─────────────────────────────────────────────────────────────────
    if choice == "preflight":
        from modules.preflight import run
        run(cfg, config_path, clients, display)
        return clients, _SILENT

    elif choice == "list_models":
        from modules.list_models import run
        run(cfg, config_path, clients, display)
        return clients, _SILENT

    # ── OBJECT STORAGE ────────────────────────────────────────────────────────
    elif choice == "storage":
        from modules.object_storage import run
        run(cfg, clients, display, config_path)
        return clients, _SILENT

    # ── BUILD ─────────────────────────────────────────────────────────────────
    elif choice == "new_project":
        from modules.conversation import run_new
        run_new(cfg, clients, display)
        return clients, _SILENT

    elif choice == "resume":
        from modules.conversation import run_resume
        run_resume(cfg, clients, display)
        return clients, _SILENT

    # ── REVIEW & MANAGE (sub-menu items) ──────────────────────────────────────
    elif choice == "list":
        from modules.review import run_list
        run_list(cfg, display)
        return clients, _SILENT

    elif choice == "view":
        from modules.review import run_view
        run_view(cfg, display)
        return clients, _SILENT

    elif choice == "update":
        from modules.review import run_update
        run_update(cfg, display)
        return clients, _SILENT

    elif choice == "comments":
        from modules.comments import run as run_comments
        run_comments(cfg, display, clients)
        return clients, _SILENT

    elif choice == "delete":
        from modules.review import run_delete
        run_delete(cfg, display)
        return clients, _SILENT

    elif choice == "rebuild":
        from modules.review import run_rebuild
        run_rebuild(cfg, display)
        return clients, _SILENT

    elif choice == "test":
        from modules.review import run_test
        run_test(cfg, display)
        return clients, _SILENT

    elif choice == "debug":
        from modules.debug_menu import run as debug_run
        debug_run(cfg, config_path, clients, display)
        return clients, _SILENT

    return clients


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Select AI Agent Builder v6.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="agent_builder_config.ini",
        help="Path to config file (default: agent_builder_config.ini)"
    )
    parser.add_argument(
        "--option", default=None, type=int,
        help="Run a specific menu option number directly (skips menu)"
    )
    args = parser.parse_args()

    try:
        cfg = cfg_module.load(args.config)
    except FileNotFoundError as ex:
        print(f"\n  {C.RED}✗{C.RESET}  {ex}")
        print(f"  Create {args.config} from the template and fill in your values.\n")
        sys.exit(1)

    # ── Open session-level runtime log ────────────────────────────────────────
    from core import state as state_module
    from datetime import datetime
    from pathlib import Path
    _log_dir = cfg_module.get(cfg, "builder", "log_dir", fallback="./logs")
    _log_path = Path(_log_dir)
    _log_path.mkdir(parents=True, exist_ok=True)
    _session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _runtime_log = _log_path / f"agent_builder_{_session_ts}.log"
    try:
        with open(_runtime_log, "w", encoding="utf-8") as _f:
            _f.write(f"Select AI Agent Builder v6.0 — Session Log\n")
            _f.write(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            _f.write(f"Config  : {args.config}\n")
            _f.write("=" * 60 + "\n\n")
    except Exception:
        pass  # Non-fatal if log dir is not writable
    # Redirect stdout to tee (screen + clean log file)
    import re as _re
    import sys as _sys

    _ANSI_ESCAPE = _re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJSTuhl]|\x1b\][^\x07]*\x07|\x1b[()][AB012]')
    _SPINNER_CLEAN = _re.compile(r'\s*\(thinking\.\.\.\)\s*')
    _BLANK_LINES = _re.compile(r'\n{3,}')

    def _clean_for_log(data: str) -> str:
        """Strip ANSI codes, spinner artifacts, and excess blank lines for the log file."""
        s = _ANSI_ESCAPE.sub('', data)      # strip all ANSI escape codes
        s = s.replace('\r', '')              # remove carriage returns (spinner overwrites)
        s = _SPINNER_CLEAN.sub('', s)       # remove thinking... residue
        s = s.replace('\x00', '')           # strip null bytes
        # Collapse 3+ consecutive blank lines to 2
        s = _BLANK_LINES.sub('\n\n', s)
        return s

    class _Tee:
        def __init__(self, terminal, logfile):
            self._terminal = terminal
            self._logfile  = logfile
            self._buf      = ""   # buffer for log to handle partial lines

        def write(self, data):
            # Terminal gets the original (with colours)
            try:
                self._terminal.write(data)
            except Exception:
                pass
            # Log file gets the cleaned version
            try:
                cleaned = _clean_for_log(data)
                if cleaned:
                    self._logfile.write(cleaned)
            except Exception:
                pass

        def flush(self):
            try: self._terminal.flush()
            except Exception: pass
            try: self._logfile.flush()
            except Exception: pass

        def write_log_only(self, data):
            """Write only to the clean session log. Used for input() echoes."""
            try:
                cleaned = _clean_for_log(data)
                if cleaned:
                    self._logfile.write(cleaned)
                    self._logfile.flush()
            except Exception:
                pass

        def isatty(self):
            return hasattr(self._terminal, 'isatty') and self._terminal.isatty()

    try:
        _log_fh = open(_runtime_log, "a", encoding="utf-8")
        _sys.stdout = _Tee(_sys.__stdout__, _log_fh)
        print(f"  Session log: {_runtime_log}")
    except Exception:
        pass

    # Direct option — no menu loop
    if args.option is not None:
        idx = args.option - 1
        if 0 <= idx < len(MENU):
            print_banner(args.config)
            dispatch(MENU[idx][0], cfg, args.config)
        else:
            display.err(f"Option must be 1–{len(MENU)}")
        return

    # Interactive menu loop
    clients = None
    while True:
        try:
            cfg = cfg_module.load(args.config)
        except FileNotFoundError:
            pass

        print_banner(args.config)
        print_menu()

        choice = get_menu_choice()

        if choice == "quit":
            print(f"\n  {C.DIM}Goodbye.{C.RESET}\n")
            break

        display.blank()
        skip_pause = False
        do_quit    = False
        try:
            clients, skip_pause, do_quit = _unpack(
                dispatch(choice, cfg, args.config, clients), clients)
        except oci.exceptions.ServiceError as ex:
            display.err(f"OCI API error [{ex.status}]: {ex.message}")
            if ex.code:
                display.err(f"Code: {ex.code}")
        except Exception as ex:
            display.err(f"Error: {ex}")

        if do_quit:
            print(f"\n  {C.DIM}Goodbye.{C.RESET}\n")
            break

        if not skip_pause:
            display.blank()
            try:
                input(f"  {C.DIM}Press Enter to return to menu...{C.RESET}")
            except (EOFError, KeyboardInterrupt):
                break


if __name__ == "__main__":
    main()
