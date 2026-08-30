"""
modules/step7.py
Application-owned Step 6 (agent name) and Step 7 (task/team finalization).

These steps are handled entirely by the application — not the LLM — to prevent
object name drift, renaming, or flow changes introduced by the model.

Extracted from modules/conversation.py in v6.0.
"""

import json

from core import state as state_module


NO_CHANGE_COMMANDS = {"cancel", "discard", "quit no change", "q!"}
SAVE_COMMANDS = {"save", "quit"}


def _safe_db_name(value: str, default: str = "OBJECT") -> str:
    import re
    value = (value or "").strip().upper()
    value = re.sub(r"[^A-Z0-9_]+", "_", value).strip("_")
    return value or default


def _project_prefix(project: dict) -> str:
    raw = project.get("display_name") or project.get("project_name") or "PROJECT"
    return _safe_db_name(raw, "PROJECT")


def ask_db_name(display, prompt: str, default: str,
                edit_mode: bool = False, project: dict = None) -> str | None:
    """Prompt the user for an Oracle DB object name with a default shown.

    Returns the entered/defaulted name, or None if the user cancelled/saved
    (only relevant in edit_mode).
    """
    C = display.C
    try:
        raw = input(
            f"  {C.BOLD}{prompt}{C.RESET}"
            f"  {C.DIM}[Press Enter for {default}]{C.RESET}: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        raw = "quit no change" if edit_mode else ""

    low = raw.lower()
    if edit_mode and project is not None:
        if low in NO_CHANGE_COMMANDS:
            project["_edit_cancelled"] = True
            display.info("Edit cancelled — no changes will be saved")
            return None
        if low in SAVE_COMMANDS:
            project["_edit_committed"] = True
            display.ok("Edit changes marked for save")
            return None

    return _safe_db_name(raw or default, default)


def ask_text(display, prompt: str, default: str = "",
             edit_mode: bool = False, project: dict = None) -> str | None:
    """Prompt the user for free-form text (e.g. task instruction).

    Returns the text, or None if the user cancelled/saved in edit_mode.
    """
    C = display.C
    suffix = f" {C.DIM}[Press Enter for default]{C.RESET}" if default else ""
    try:
        raw = input(f"  {C.BOLD}{prompt}{C.RESET}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = "quit no change" if edit_mode else ""

    low = raw.lower()
    if edit_mode and project is not None:
        if low in NO_CHANGE_COMMANDS:
            project["_edit_cancelled"] = True
            display.info("Edit cancelled — no changes will be saved")
            return None
        if low in SAVE_COMMANDS:
            project["_edit_committed"] = True
            display.ok("Edit changes marked for save")
            return None

    return raw or default


def run_step6_agent_name(project: dict, cfg, display, history: list,
                          run_log=None, edit_mode: bool = False) -> str:
    """Collect the agent DB object name (Step 6 sub-step, application-owned).

    Returns 'ok', 'cancel', or 'save'.
    """
    from core import spec_builder as sb
    facts = sb.ensure_facts(project)
    prefix = _project_prefix(project)
    existing = facts.get("names", {}).get("agent_name") or f"{prefix}_AGENT"

    display.info("Step 6a — Agent DB object name (application-owned)")
    agent_name = ask_db_name(
        display, "Agent name", existing, edit_mode=edit_mode, project=project
    )
    if project.get("_edit_cancelled"):
        return "cancel"
    if project.get("_edit_committed"):
        return "save"
    if not agent_name:
        agent_name = existing

    names = facts.setdefault("names", {})
    names["agent_name"] = agent_name
    task = facts.setdefault("task", {})
    task["agent_name"] = agent_name

    if run_log:
        run_log.log_state("accepted_step6_agent_name", {"agent_name": agent_name})
    return "ok"


def run_step7_finalize(project: dict, cfg, display,
                        run_log=None, edit_mode: bool = False) -> dict:
    """Application-owned Step 7: collect task instruction, task name, team name.

    Builds the spec deterministically from facts once all values are confirmed.
    Returns the updated project dict.
    """
    from core import spec_builder as sb
    from core import spec_validator

    facts = sb.ensure_facts(project)
    prefix = _project_prefix(project)
    task = facts.setdefault("task", {})

    display.info("Step 7 is handled by the application to prevent LLM renaming or flow changes.")
    agent_name = task.get("agent_name") or facts.get("names", {}).get("agent_name") or f"{prefix}_AGENT"
    agent_name = _safe_db_name(agent_name, f"{prefix}_AGENT")
    task["agent_name"] = agent_name

    instruction_default = facts.get("agent_role") or "Answer user requests using the configured tools."
    task_instruction = ask_text(
        display, "Task instruction", instruction_default, edit_mode=edit_mode, project=project
    )
    if project.get("_edit_cancelled") or project.get("_edit_committed"):
        return project

    task_name = ask_db_name(
        display, "Task name", f"{prefix}_TASK", edit_mode=edit_mode, project=project
    )
    if project.get("_edit_cancelled") or project.get("_edit_committed"):
        return project

    team_name = ask_db_name(
        display, "Team name", f"{prefix}_TEAM", edit_mode=edit_mode, project=project
    )
    if project.get("_edit_cancelled") or project.get("_edit_committed"):
        return project

    task.update({
        "agent_name":  agent_name,
        "instruction": task_instruction,
        "task_name":   task_name,
        "team_name":   team_name,
        "process":     "sequential",
    })

    summary = "Step 7 finalized by user: " + ", ".join(f"{k}={v}" for k, v in task.items())
    project = state_module.add_to_conversation(project, "USER", summary, 7)

    C = display.C
    print(f"  {C.BOLD}Accepted Step 7 task/team values:{C.RESET}")
    print(f"    agent_name        : {agent_name}  (from Step 6)")
    print(f"    task_instruction  : {task_instruction}")
    print(f"    task_name         : {task_name}")
    print(f"    team_name         : {team_name}")

    # Resolve tool names so the display is informative
    tool_names = [t.get("name") for t in project.get("spec", {}).get("tools", []) if t.get("name")]
    if tool_names:
        print(f"    task tools        : {', '.join(tool_names)}")

    if run_log:
        run_log.log("STEP7_FINALIZED: " + json.dumps(task, sort_keys=True))
        run_log.log_state("accepted_step7_names", task)

    # Build + validate spec
    spec = sb.build_spec_from_project(project, cfg)
    spec = sb.canonicalize_spec(spec, project, cfg)
    try:
        spec_validator.validate_spec(spec, sb.ensure_facts(project))
    except spec_validator.SpecValidationError as ex:
        display.err(f"Spec validation failed: {ex}")
        display.info("The project will remain in discovery so you can correct the missing value.")
        project = state_module.update_phase(project, "discovery")
        return project

    project["spec"] = spec
    from core import state as sm
    sm.set_workflow_step(project, current_step=7, last_completed_step=7)
    return project
