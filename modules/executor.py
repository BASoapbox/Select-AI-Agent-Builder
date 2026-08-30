"""
modules/executor.py
Phase 3: Execute generated PL/SQL against ADW.

Parses the script into individual statements, executes them in sequence,
and reports success/failure per statement.

Extracted from modules/conversation.py in v6.0.
"""

import re
import time

from core import db as db_module
from core import config as cfg_module
from core import state as state_module


def _split_statements(sql: str) -> list[str]:
    """Split a PL/SQL script into individual executable statements.

    Handles:
    - Standalone DDL terminated by ';'
    - PL/SQL blocks terminated by '/' on its own line
    - Comment-only lines (skipped)
    """
    statements = []
    current: list[str] = []
    in_block = False

    for line in sql.splitlines():
        stripped = line.strip()

        # Skip pure comment lines when not inside a block
        if not in_block and (stripped.startswith("--") or not stripped):
            current.append(line)
            continue

        # PL/SQL block start markers
        if re.match(r"^\s*(BEGIN|DECLARE)\b", stripped, re.IGNORECASE):
            in_block = True

        current.append(line)

        # Block terminator — standalone '/'
        if in_block and stripped == "/":
            stmt = "\n".join(current).strip()
            if stmt and stmt != "/":
                statements.append(stmt)
            current = []
            in_block = False
            continue

        # DDL / standalone statement — ends with ';' and not inside a block
        if not in_block and stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []

    # Flush any remaining content
    remainder = "\n".join(current).strip()
    if remainder and not all(l.strip().startswith("--") or not l.strip() for l in current):
        statements.append(remainder)

    return [s for s in statements if s.strip() and not s.strip().startswith("--")]


def run(project: dict, cfg, display, sql: str, run_log=None) -> bool:
    """Execute all statements in *sql* against ADW.

    Returns True if all statements succeeded, False if any failed.
    """
    C = display.C
    display.blank()
    display.head("Executing PL/SQL against ADW")

    statements = _split_statements(sql)
    if not statements:
        display.warn("No executable statements found in the script.")
        return False

    display.info(f"Found {len(statements)} statement(s) to execute.")
    display.blank()

    conn = None
    all_ok = True
    try:
        conn = db_module.connect(cfg)
        display.ok("ADW connection established.")

        for i, stmt in enumerate(statements, 1):
            # Show a short preview (first non-comment line)
            preview = next(
                (l.strip() for l in stmt.splitlines() if l.strip() and not l.strip().startswith("--")),
                stmt[:60]
            )
            print(f"  {C.DIM}[{i}/{len(statements)}]{C.RESET} {preview[:72]}{'...' if len(preview) > 72 else ''}")

            t0 = time.monotonic()
            success, error = db_module.execute_statement(conn, stmt)
            duration_ms = int((time.monotonic() - t0) * 1000)

            if success:
                print(f"  {C.GREEN}✓{C.RESET}  OK  ({duration_ms}ms)")
            else:
                print(f"  {C.RED}✗{C.RESET}  FAILED  ({duration_ms}ms)")
                print(f"      {C.RED}{error}{C.RESET}")
                all_ok = False

            if run_log:
                run_log.log_sql(i, stmt, success, error, duration_ms)

    except Exception as ex:
        display.err(f"Connection error: {ex}")
        if run_log:
            run_log.log_exception("executor.run", ex)
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    display.blank()
    if all_ok:
        display.ok(f"All {len(statements)} statement(s) executed successfully.")
        project = state_module.update_phase(project, "complete")
    else:
        display.warn("One or more statements failed. Review errors above.")
        display.info("You can re-run or fix the SQL snapshot in the projects/<name>/sql/ folder.")

    return all_ok
