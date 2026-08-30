"""
modules/codegen.py
Phase 2: SQL code generation from a completed spec.

Takes a finalized project spec and calls the LLM (or deterministic sql_builder)
to produce the PL/SQL deployment script, then presents it to the user for review.

Extracted from modules/conversation.py in v6.0.
"""

import json
from pathlib import Path

from core import config as cfg_module
from core import llm as llm_module
from core import state as state_module
from core import sql_builder


def _load_template(name: str) -> str:
    path = Path(__file__).parent.parent / "templates" / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def generate_sql(project: dict, cfg, clients: dict, display, run_log=None) -> str:
    """Generate the PL/SQL script from project spec using the deterministic sql_builder.

    Falls back to LLM codegen only if the spec is missing required fields.
    Returns the generated SQL string (may be empty on failure).
    """
    C = display.C
    spec = project.get("spec") or {}
    if not spec:
        display.err("No spec found — complete the discovery conversation first.")
        return ""

    display.info("Generating PL/SQL from spec (deterministic builder)...")
    try:
        sql = sql_builder.build_full_sql(spec, cfg)
        display.ok("SQL generated successfully.")
        if run_log:
            run_log.log(f"CODEGEN: deterministic sql_builder produced {len(sql)} chars")
        return sql
    except Exception as ex:
        display.err(f"Deterministic SQL generation failed: {ex}")
        if run_log:
            run_log.log_exception("codegen.generate_sql", ex)

    # Fallback: LLM-based codegen
    display.warn("Falling back to LLM codegen...")
    try:
        codegen_prompt = _load_template("codegen_prompt.txt")
        spec_json = json.dumps(spec, indent=2)
        prompt = f"{codegen_prompt}\n\nSPEC:\n{spec_json}"
        llm_cfg = _llm_config(cfg)
        response = llm_module.chat(clients, llm_cfg, [{"role": "USER", "text": prompt}])
        sql = response.strip()
        display.ok("LLM codegen complete.")
        if run_log:
            run_log.log(f"CODEGEN: LLM fallback produced {len(sql)} chars")
        return sql
    except Exception as ex2:
        display.err(f"LLM codegen also failed: {ex2}")
        if run_log:
            run_log.log_exception("codegen.generate_sql LLM fallback", ex2)
        return ""


def review_and_confirm(project: dict, cfg, clients: dict, display,
                        sql: str, run_log=None) -> tuple[str, bool]:
    """Show the generated SQL to the user and ask whether to execute or save.

    Returns (sql, should_execute).  The user may edit/regenerate interactively.
    """
    C = display.C
    display.blank()
    print(f"  {C.BOLD}{'─' * 60}{C.RESET}")
    print(f"  {C.BOLD}Generated PL/SQL{C.RESET}")
    print(f"  {'─' * 60}")
    print()
    for line in sql.splitlines():
        print(f"    {line}")
    print()
    print(f"  {'─' * 60}")
    display.blank()

    # Save snapshot
    snap_path = state_module.save_sql_snapshot(cfg, project, sql)
    display.ok(f"SQL snapshot saved: {snap_path}")
    display.blank()

    while True:
        print(f"  {C.BOLD}Options:{C.RESET}")
        print(f"    {C.YELLOW}e{C.RESET}  Execute now against ADW")
        print(f"    {C.CYAN}s{C.RESET}  Save only (do not execute)")
        print(f"    {C.DIM}q{C.RESET}  Quit to menu")
        print()
        try:
            choice = input(f"  {C.BOLD}[e/s/q]:{C.RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return sql, False

        if choice == "e":
            return sql, True
        if choice == "s":
            return sql, False
        if choice == "q":
            return sql, False
        display.warn("Please enter e, s, or q.")


def _llm_config(cfg) -> dict:
    return {
        "model":       cfg_module.get(cfg, "llm", "chat_model",   fallback="meta.llama-3.3-70b-instruct"),
        "temperature": float(cfg_module.get(cfg, "llm", "temperature", fallback="0.3")),
        "max_tokens":  int(cfg_module.get(cfg, "llm", "max_tokens",   fallback="4000")),
        "region":      cfg_module.get(cfg, "oci", "region",           fallback="us-chicago-1"),
        "compartment": cfg_module.get(cfg, "compartment", "compartment_ocid", fallback=""),
    }
