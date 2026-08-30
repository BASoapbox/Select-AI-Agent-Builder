"""
modules/conversation.py
Menu options 4 (new) and 5 (resume) — conversational agent builder.
Phase 1: Discovery conversation → spec JSON
Phase 2: Code generation
Phase 3: Execution
"""

import copy
import getpass
import json
import re
import sys
from pathlib import Path

from core import config as cfg_module
from core import llm as llm_module
from core import state as state_module
from core import db as db_module


# ─────────────────────────────────────────────────────────────────────────────
# Step tracker — infers current step from assistant response content
# ─────────────────────────────────────────────────────────────────────────────

# Each step: (number, title, what_it_does, examples list)
# The builder asks one sub-question at a time — examples show what to expect.
STEPS = [
    (1, "Project name and schema",
     "Give your project a human-readable name and confirm the database schema. "
     "The agent DB object name is collected separately in Step 6.",
     []),
    (2, "Data sources",
     "Tell the builder what kind of data your agent will use. "
     "This determines which tool types are created.",
     [
         "Database tables  →  generates a SQL (NL2SQL) tool",
         "Documents  →  generates a RAG tool connected to Object Storage",
         "Both  →  generates both a SQL tool and a RAG tool",
     ]),
    (3, "Document or table details",
     "Provide the specifics about your data source so the builder can configure the right profile.",
     [
         "RAG:  Subject matter (e.g. pet insurance policies)",
         "RAG:  Object Storage URL (e.g. https://objectstorage.../o/insurance-kb/)",
         "SQL:  Which tables (e.g. SALES_FACTS, CUSTOMER_DIM)",
         "SQL:  What questions users will ask (e.g. revenue trends, top products)",
     ]),
    (4, "Object names and vector index configuration",
     "Name each database object, then set vector index parameters.",
     [
         "RAG profile name  (e.g. ACME_ANALYST_RAG_PROFILE)",
         "Vector index name  (e.g. ACME_ANALYST_VECTOR_IDX)",
         "Chunk size  (e.g. 1024) — how much source text is stored in each vector-search chunk",
         " Larger chunks keep more context together; smaller chunks can improve focused retrieval",
         "Chunk overlap  (e.g. 128) — how much text is repeated between neighboring chunks",
         " Overlap helps preserve context when an answer spans a chunk boundary",
         "RAG tool name  (e.g. ACME_ANALYST_RAG_TOOL)",
         "NL2SQL profile name  (e.g. ACME_ANALYST_NL2SQL_PROFILE)  — if Both",
         "SQL tool name  (e.g. ACME_ANALYST_SQL_TOOL)  — if Both",
     ]),
    (5, "Additional analysis tools",
     "Choose optional deterministic tools beyond the core RAG/SQL tools.",
     [
         "Trend analysis",
         "Forecasting",
         "Anomaly detection",
         "Python/statistical analysis",
         "All, none, or custom",
     ]),
    (6, "Agent name, role and persona",
     "Name the agent database object, then describe its identity, responsibilities, and rules — this is its system prompt.",
     [
         "Agent name  (e.g. ACME_INSURANCE_AGENT — the database object name)",
         "Role/persona  (e.g. ‘You are an AI support agent for ACME Inc. pet insurance. "
         "Use the RAG tool to retrieve policy information. "
         "If not found, direct the user to customer support.’)",
     ]),
    (7, "Task and team structure",
     "Three questions — one at a time.",
     [
         "Task instruction  (e.g. Answer pet insurance questions using the RAG knowledge base)",
         "Task name  (e.g. ACME_INSURANCE_TASK)",
         "Team name  (e.g. ACME_INSURANCE_TEAM)",
         "Team structure is always sequential — only option in ADW currently",
     ]),
]

# Keyword patterns that indicate the AI has moved into a given step
STEP_SIGNALS = {
    1: ["project name", "schema", "database schema"],
    2: ["data source", "database tables", "documents", "pdf", "both"],
    3: ["subject matter", "object storage", "which tables", "file type",
        "location", "bucket", "what kind of"],
    4: ["vector index", "chunk size", "chunk overlap", "embedding model",
        "index name", "profile name", "object naming", "moving to object naming",
        "step 3 complete"],
    5: ["trend", "forecast", "anomaly", "python tool", "analysis capabilit",
        "beyond basic", "additional tool", "statistical"],
    6: ["role", "persona", "behav", "responsible for", "agent should"],
    7: ["task instruction", "team structure", "team name", "sequential",
        "what should the agent do with each", "agent name",
        "what should the agent be named", "task and team setup",
        "moving to task and team"],
}


NO_CHANGE_COMMANDS = {"cancel", "discard", "quit no change", "q!"}
SAVE_COMMANDS = {"save", "quit"}


def _maybe_save(cfg, project: dict, edit_mode: bool = False):
    """Save project unless running a transactional edit session."""
    if not edit_mode:
        return state_module.save_project(cfg, project)
    return None


def _set_workflow(project: dict, current_step: int = None,
                  last_completed_step: int = None, edit_mode: bool = None) -> dict:
    """Use explicit workflow state instead of inferring from assistant text."""
    if hasattr(state_module, "set_workflow_step"):
        return state_module.set_workflow_step(project, current_step, last_completed_step, edit_mode)
    workflow = project.setdefault("workflow", {})
    if current_step is not None:
        workflow["current_step"] = max(1, min(7, int(current_step)))
    if last_completed_step is not None:
        workflow["last_completed_step"] = max(1, min(7, int(last_completed_step)))
    if edit_mode is not None:
        workflow["edit_mode"] = bool(edit_mode)
    return project


def _get_last_completed_step(project: dict) -> int:
    """Return the application-owned last completed step with legacy fallbacks."""
    if hasattr(state_module, "get_last_completed_step"):
        try:
            return state_module.get_last_completed_step(project)
        except Exception:
            pass
    workflow = project.get("workflow") or {}
    try:
        step = int(workflow.get("last_completed_step", 1))
    except Exception:
        step = 1
    if project.get("spec") and project.get("phase") in ("review", "complete"):
        step = max(step, 7)
    # Legacy fallback: use stored step numbers, not keyword inference.
    for turn in project.get("conversation", []):
        try:
            step = max(step, int(turn.get("step", 0)))
        except Exception:
            pass
    return max(1, min(7, step))


def _semantic_snapshot(project: dict) -> str:
    """Compare meaningful project content while ignoring logs/session/current edit cursor."""
    keep = {
        "facts", "_naming", "_step4_complete",
        "spec", "generated_sql", "build_log", "display_name",
        "project_name", "schema", "rag_url",
    }
    # phase/workflow/conversation are session/control state. They should not turn
    # an edit-mode no-op into a saved rollback. Actual changes are captured in
    # facts, names, spec, generated_sql, or build_log.
    payload = {k: project.get(k) for k in sorted(keep) if k in project}
    return json.dumps(payload, sort_keys=True, default=str)


def _trim_conversation_for_step(project: dict, go_to: int) -> dict:
    """Trim only the working copy conversation before the selected step."""
    history = project.get("conversation", [])
    trimmed = []
    for turn in history:
        stored_step = turn.get("step")
        if stored_step is not None:
            try:
                if int(stored_step) >= go_to:
                    break
            except Exception:
                pass
        else:
            if turn.get("role") == "ASSISTANT":
                detected = _detect_step(turn.get("text", ""), 1)
                if detected >= go_to:
                    break
        trimmed.append(turn)
    while trimmed and trimmed[-1].get("role") == "USER":
        trimmed.pop()
    project["conversation"] = trimmed
    return project



def _detect_step(response_text: str, current_step: int = 1) -> int:
    """
    Infer the current step from assistant response keywords.
    Never goes backwards — only advances or stays at current step.
    Returns 1-7.
    """
    text = response_text.lower()
    detected = current_step  # start from current, never go back
    for step, signals in STEP_SIGNALS.items():
        if step > detected and any(s in text for s in signals):
            detected = step
    return detected


def _print_spec_summary(spec: dict, display):
    """Print a human-readable summary of the collected spec before code generation."""
    C = display.C
    display.blank()
    print(f"  {C.BOLD}{'─' * 60}{C.RESET}")
    print(f"  {C.BOLD}CONFIGURATION SUMMARY{C.RESET}")
    print(f"  {'─' * 60}")

    print(f"  {C.BOLD}Agent{C.RESET}")
    agents = spec.get("agents", [])
    if agents:
        a = agents[0]
        print(f"    Name      : {a.get('name', '—')}")
        role = a.get("role", "")
        if role:
            # Word-wrap at 80 chars per line
            words = role.split()
            line, lines = [], []
            for w in words:
                if sum(len(x) + 1 for x in line) + len(w) > 80:
                    lines.append(" ".join(line))
                    line = [w]
                else:
                    line.append(w)
            if line:
                lines.append(" ".join(line))
            print(f"    Role      : {lines[0]}")
            for l in lines[1:]:
                print(f"                {l}")
    display.blank()

    print(f"  {C.BOLD}Tools{C.RESET}")
    for t in spec.get("tools", []):
        print(f"    {t.get('name','—'):<35} {C.DIM}{t.get('type','')}{C.RESET}")
        instr = t.get("instruction", "")
        if instr:
            print(f"    {C.DIM}  {instr[:70]}{C.RESET}")
    display.blank()

    print(f"  {C.BOLD}Profiles & Indexes{C.RESET}")
    for p in spec.get("profiles", []):
        print(f"    {p.get('name','—'):<35} {C.DIM}{p.get('type','')}{C.RESET}")
    for v in spec.get("vector_indexes", []):
        print(f"    {v.get('name','—'):<35} {C.DIM}chunk {v.get('chunk_size',1024)} / overlap {v.get('chunk_overlap',128)}{C.RESET}")
    display.blank()

    print(f"  {C.BOLD}Task & Team{C.RESET}")
    for t in spec.get("tasks", []):
        instr = t.get("instruction", "")
        print(f"    Task      : {t.get('name', '—')}")
        if instr:
            words = instr.split()
            line, lines = [], []
            for w in words:
                if sum(len(x) + 1 for x in line) + len(w) > 76:
                    lines.append(" ".join(line))
                    line = [w]
                else:
                    line.append(w)
            if line:
                lines.append(" ".join(line))
            print(f"    Instruction: {lines[0]}")
            for l in lines[1:]:
                print(f"                 {l}")
    for t in spec.get("teams", []):
        print(f"    Team      : {t.get('name', '—')}  ({t.get('process', 'sequential')})")
    print(f"  {'─' * 60}")
    display.blank()




def _split_ack_and_question(response: str) -> tuple:
    """
    Split an assistant response into (acknowledgment, question).
    The LLM often acknowledges the previous answer then asks the next question
    in the same message. We split at the first question mark or blank line
    that separates the two parts so we can insert the step header in between.
    Returns (ack_part, question_part). If no clear split, returns ("", response).
    """
    # Try splitting on a double newline (blank line between ack and question)
    parts = response.split("\n\n", 1)
    if len(parts) == 2:
        ack, question = parts
        # Only split if the first part reads like an acknowledgment
        ack_lower = ack.lower()
        if any(w in ack_lower for w in
               ("got it", "noted", "acknowledged", "perfect", "great",
                "understood", "thanks", "noted", "sure", "okay", "ok,")):
            return ack.strip(), question.strip()
    # Try splitting after the first sentence that ends with a period
    # and is followed by a question
    import re
    match = re.search(r"^(.+?[.!])\s+(\*{0,2}What|Which|How|Do|Can|"
                      r"Is|Are|Would|Could)",
                      response, re.DOTALL | re.IGNORECASE)
    if match:
        ack      = match.group(1).strip()
        question = response[match.start(2):].strip()
        ack_lower = ack.lower()
        if any(w in ack_lower for w in
               ("got it", "noted", "acknowledged", "perfect", "great",
                "understood", "thanks", "sure", "okay", "ok,")):
            return ack, question
    return "", response


def _print_step_header(step: int, display):
    """Print [Step N of 7] with what-it-does and examples."""
    C = display.C
    _, label, what, examples = STEPS[step - 1]
    print(f"  {C.DIM}{'─' * 60}{C.RESET}")
    print(f"  {C.BOLD}[Step {step} of {len(STEPS)}]  {label}{C.RESET}")
    if what:
        print(f"       {C.DIM}{what}{C.RESET}")
    for ex in examples:
        # Lines starting with figure space ( ) are continuations — no bullet
        if ex.startswith(" "):
            print(f"              {ex.lstrip()}")
        else:
            print(f"       {C.CYAN}▸{C.RESET}  {ex}")
    print(f"  {C.DIM}{'─' * 60}{C.RESET}")


def _print_ds_checklist(display):
    """Print a concise data-scientist readiness checklist before discovery starts."""
    C = display.C
    print(f"  {C.BOLD}{'─' * 60}{C.RESET}")
    print(f"  {C.BOLD}DS CHECKLIST (Please have these ready before you proceed){C.RESET}")
    print(f"  {'─' * 60}")
    checklist = [
        "1. Instructions — agent role/persona, task instruction, and any tool-routing rules",
        "2. SQL tables/views — owner and object names for the SQL tool object_list",
        "3. Comments — useful table/column descriptions, joins, valid values, filters, and aggregation rules",
        "4. RAG bucket — bucket name, prefix/subfolder, and optional local folder of files to upload",
        "5. RAG URL — Object Storage URL, e.g. https://objectstorage.<region>.oraclecloud.com/n/<namespace>/b/<bucket>/o/<prefix>/",
        "6. Object names — optional names for profiles, vector index, tools, agent, task, and team",
        "7. Vector settings — optional chunk size and chunk overlap values; defaults are usually fine",
        "8. Advanced tools — optional trend, forecast, anomaly, Python/statistical, or custom tool requirements",
    ]
    for item in checklist:
        print(f"  {C.DIM}{item}{C.RESET}")
    print(f"  {'─' * 60}")


def _print_roadmap(display):
    """Print the full 7-step roadmap with hints at the start of a conversation."""
    C = display.C
    print(f"  {C.BOLD}{'─' * 60}{C.RESET}")
    print(f"  {C.BOLD}SETUP ROADMAP{C.RESET}")
    print(f"  {'─' * 60}")
    for num, label, what, examples in STEPS:
        print(f"  {C.BOLD}{num}.{C.RESET}  {label}")
        if what:
            print(f"       {C.DIM}{what}{C.RESET}")
    print(f"  {'─' * 60}")
    _print_ds_checklist(display)
    print(f"  {C.DIM}Commands: 'save' = save progress  |  'quit' = exit and save{C.RESET}")
    print(f"  {'─' * 60}")


# ─────────────────────────────────────────────────────────────────────────────

def _load_template(name: str) -> str:
    template_dir = Path(__file__).parent.parent / "templates"
    path = template_dir / name
    if path.exists():
        return path.read_text()
    return ""


def _silent_preflight(cfg, clients, display):
    """
    Run ADW grant checks and show a WHAT YOU CAN DO summary — consistent
    with Option 2. Uses the same password resolution (Vault / env / prompt).
    Never blocks — if checks fail for any reason, silently continues.
    """
    C = display.C
    try:
        from modules.preflight import CheckResult, _check_adw
        r = CheckResult()
        _check_adw(cfg, r)   # uses core/db.py connect() — Vault / env / prompt

        # ── WHAT YOU CAN DO summary ───────────────────────────────────────────
        rag_grants  = [s for s, l, d in r.items
                       if "DBMS_CLOUD" in l and "AI" not in l
                       and s == CheckResult.OK]
        rag_cred    = [s for s, l, d in r.items
                       if "Vault credential" in l and s == CheckResult.OK]
        rag_ok      = bool(rag_grants) and bool(rag_cred)

        agent_grants = [s for s, l, d in r.items
                        if "DBMS_CLOUD_AI" in l and s == CheckResult.OK]
        agent_views  = [s for s, l, d in r.items
                        if any(v in l for v in ("USER_AI_AGENT_TOOLS", "USER_AI_AGENTS",
                                                "USER_AI_AGENT_TEAMS",
                                                "USER_CLOUD_AI_PROFILES"))
                        and s == CheckResult.OK]
        agent_ok = bool(agent_grants) and len(agent_views) >= 2

        display.blank()
        print(f"  {C.BOLD}WHAT YOU CAN DO:{C.RESET}")
        print(f"  {'─' * 60}")

        if rag_ok:
            print(f"  {C.GREEN}✓{C.RESET}  {C.BOLD}Object Storage / RAG{C.RESET}"
                  f"  — you can upload documents and build RAG tools")
        else:
            missing = []
            if not rag_grants: missing.append("EXECUTE on DBMS_CLOUD")
            if not rag_cred:   missing.append("Vault credential")
            print(f"  {C.YELLOW}⚠{C.RESET}  {C.BOLD}Object Storage / RAG{C.RESET}"
                  f"  — not fully ready ({', '.join(missing)} needed)")

        if agent_ok:
            print(f"  {C.GREEN}✓{C.RESET}  {C.BOLD}Agent resources{C.RESET}"
                  f"  — you can create tools, agents, tasks, and teams")
        else:
            missing = []
            if not agent_grants:    missing.append("EXECUTE on DBMS_CLOUD_AI")
            if len(agent_views) < 2: missing.append("agent framework views")
            print(f"  {C.RED}✗{C.RESET}  {C.BOLD}Agent resources{C.RESET}"
                  f"  — not ready ({', '.join(missing)} needed)")
            display.blank()
            print(f"  {C.DIM}Agent creation will fail until these are resolved.")
            print(f"  Run Option 2 (Pre-flight check) for full details.{C.RESET}")

        print(f"  {'─' * 60}")
        display.blank()

    except Exception:
        pass  # Never block the user — checks are advisory only


def _pick_project(cfg, display) -> dict:
    """Let user pick an existing project to resume."""
    projects = state_module.list_projects(cfg)
    if not projects:
        display.warn("No saved projects found")
        return None

    display.blank()
    print(f"  {'#':<4}  {'Project':<35}  {'Phase':<15}  Modified")
    print(f"  {'─'*4}  {'─'*35}  {'─'*15}  {'─'*19}")
    for i, p in enumerate(projects, 1):
        ts = p['modified_at'][:19].replace("T", " ")
        print(f"  {i:<4}  {p['name']:<35}  {p['phase']:<15}  {ts}")
    display.blank()

    print(f"  {display.C.DIM}Enter a number to resume, d<number> to delete (e.g. d2), "
          f"d1,2,3 to delete multiple, dall to delete all, or Enter to cancel.{display.C.RESET}")
    display.blank()

    try:
        raw = input(f"  Choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if not raw:
        return None

    # Delete operations
    if raw.startswith("d"):
        import os
        suffix = raw[1:].strip()

        # dall — delete all
        if suffix == "all":
            try:
                confirm = input(
                    f"  Archive ALL {len(projects)} project(s)? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if confirm != "y":
                display.warn("Cancelled — no projects archived")
                return None
            archived = 0
            for p in projects:
                if state_module.delete_project(cfg, p["slug"]):
                    display.ok(f"Archived: {p['name']}")
                    archived += 1
                else:
                    display.warn(f"Could not archive {p['name']}")
            display.info(f"{archived} project(s) moved to _deleted/")
            return None

        # d1,2,3 — comma-separated indices
        if "," in suffix:
            try:
                indices = [int(n.strip()) - 1 for n in suffix.split(",")]
            except ValueError:
                display.warn("Invalid format — use d1,2,3")
                return None
            targets = []
            for idx in indices:
                if 0 <= idx < len(projects):
                    targets.append(projects[idx])
                else:
                    display.warn(f"  #{idx + 1} is out of range — skipped")
            if not targets:
                return None
            names = ", ".join(f"'{t['name']}'" for t in targets)
            try:
                confirm = input(
                    f"  Archive {len(targets)} project(s): {names}? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if confirm != "y":
                display.warn("Cancelled — no projects archived")
                return None
            archived = 0
            for t in targets:
                if state_module.delete_project(cfg, t["slug"]):
                    display.ok(f"Archived: {t['name']}")
                    archived += 1
                else:
                    display.warn(f"Could not archive {t['name']}")
            display.info(f"{archived} project(s) moved to _deleted/")
            return None

        # d1 — single delete
        try:
            idx = int(suffix) - 1
            if 0 <= idx < len(projects):
                target = projects[idx]
                try:
                    confirm = input(
                        f"  Archive '{target['name']}'? [y/N]: "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return None
                if confirm == "y":
                    if state_module.delete_project(cfg, target["slug"]):
                        display.ok(f"Archived: {target['name']}  (moved to _deleted/)")
                    else:
                        display.warn(f"Could not archive {target['name']}")
                else:
                    display.warn("Cancelled — project not archived")
            else:
                display.warn("Invalid project number")
        except (ValueError, OSError) as ex:
            display.warn(f"Could not archive: {ex}")
        return None

    # Resume: number
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(projects):
            return state_module.load_project(projects[idx]["file"])
        display.warn("Invalid selection")
        return None
    except ValueError:
        display.warn("Invalid input — enter a number, d<number>, d1,2,3, or dall")
        return None


def _run_step4_naming(project: dict, cfg, display, data_source: str,
                      history: list, run_log=None) -> dict:
    """
    Client-side Step 4 — ask for all object names directly, no LLM involved.
    Suggests names derived from the project display name.
    Stores results in project['_naming'] and sets project['_step4_complete'].
    """
    C   = display.C
    raw = project.get("display_name", project.get("project_name", "PROJECT"))
    # Build a default prefix from the display name
    import re as _re
    prefix = _re.sub(r'[^A-Z0-9]+', '_', raw.upper()).strip('_')

    print(f"  {C.DIM}Chunk size controls how much source text is stored in each vector-search chunk.{C.RESET}")
    print(f"  {C.DIM}  Larger chunks keep more context together; smaller chunks can improve focused retrieval.{C.RESET}")
    print(f"  {C.DIM}Chunk overlap controls how much text is repeated between neighboring chunks.{C.RESET}")
    print(f"  {C.DIM}  Overlap helps preserve context when an answer spans a chunk boundary.{C.RESET}")
    display.blank()

    _STEP4_QUIT = object()   # sentinel returned when user quits Step 4

    def _ask(prompt: str, default: str):
        """Ask one naming question with a default.
        Returns the sanitised name, or _STEP4_QUIT if the user typed q/quit/save.
        """
        try:
            val = input(
                f"  {C.BOLD}{prompt}{C.RESET}"
                f"  {C.DIM}[Press Enter for {default} | q=save & quit]{C.RESET}: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            val = "quit"
        if val.lower() in ("q", "quit", "save", "exit", "b", "back"):
            return _STEP4_QUIT
        val = val.upper()
        return _re.sub(r'[^A-Z0-9_]', '_', val) if val else default

    naming = {}
    is_rag = data_source in (None, "documents", "both")
    is_sql = data_source in ("tables", "both")

    if is_rag:
        v = _ask("RAG profile name", f"{prefix}_RAG_PROFILE")
        if v is _STEP4_QUIT:
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. Resume from Step 4 to re-enter names.")
            project["_step4_quit"] = True
            return project
        naming["rag_profile"] = v
        v = _ask("Vector index name", f"{prefix}_VECTOR_IDX")
        if v is _STEP4_QUIT:
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. Resume from Step 4 to re-enter names.")
            project["_step4_quit"] = True
            return project
        naming["vector_index"] = v
        try:
            cs = input(
                f"  {C.BOLD}Chunk size{C.RESET}"
                f"  {C.DIM}[Press Enter for 1024]{C.RESET}: "
            ).strip()
            naming["chunk_size"]    = int(cs) if cs.isdigit() else 1024
        except (EOFError, KeyboardInterrupt):
            naming["chunk_size"] = 1024
        try:
            co = input(
                f"  {C.BOLD}Chunk overlap{C.RESET}"
                f"  {C.DIM}[Press Enter for 128]{C.RESET}: "
            ).strip()
            naming["chunk_overlap"] = int(co) if co.isdigit() else 128
        except (EOFError, KeyboardInterrupt):
            naming["chunk_overlap"] = 128
        v = _ask("RAG tool name", f"{prefix}_RAG_TOOL")
        if v is _STEP4_QUIT:
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. Resume from Step 4 to re-enter names.")
            project["_step4_quit"] = True
            return project
        naming["rag_tool"] = v

    if is_sql:
        v = _ask("NL2SQL profile name", f"{prefix}_NL2SQL_PROFILE")
        if v is _STEP4_QUIT:
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. Resume from Step 4 to re-enter names.")
            project["_step4_quit"] = True
            return project
        naming["nl2sql_profile"] = v
        v = _ask("SQL tool name", f"{prefix}_SQL_TOOL")
        if v is _STEP4_QUIT:
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. Resume from Step 4 to re-enter names.")
            project["_step4_quit"] = True
            return project
        naming["sql_tool"] = v

    project["_naming"]         = naming
    project["_step4_complete"] = True
    _ensure_facts(project)["names"] = dict(naming)

    display.ok("Names saved — continuing to Step 5")
    print(f"  {C.BOLD}Accepted object names:{C.RESET}")
    for k, v in naming.items():
        print(f"    {k:<18}: {v}")
    if run_log:
        run_log.log_state("accepted_object_names", naming)
    return project

# ─────────────────────────────────────────────────────────────────────────────
# Canonical discovery facts and deterministic spec generation
# ─────────────────────────────────────────────────────────────────────────────

def _safe_db_name(value: str, default: str = "OBJECT") -> str:
    """Return a stable upper-snake Oracle object name."""
    raw = (value or default).strip().upper()
    name = re.sub(r"[^A-Z0-9_]+", "_", raw).strip("_")
    if not name:
        name = default
    if not re.match(r"^[A-Z]", name):
        name = f"A_{name}"
    return name


def _project_prefix(project: dict) -> str:
    raw = project.get("display_name") or project.get("project_name") or "PROJECT"
    return _safe_db_name(raw, "PROJECT")


def _ensure_facts(project: dict) -> dict:
    """Ensure the project has a canonical facts object."""
    facts = project.setdefault("facts", {})
    facts.setdefault("data_source", None)

    facts.setdefault("rag", {})
    facts["rag"].setdefault("subject", "")
    facts["rag"].setdefault("file_types", [])
    facts["rag"].setdefault("object_storage_url", project.get("rag_url", ""))

    facts.setdefault("sql", {})
    facts["sql"].setdefault("tables", [])
    facts["sql"].setdefault("question_types", "")
    facts["sql"].setdefault("comments", {
        "mode": "unspecified",
        "status": "not_started",
        "objects": {},
        "coverage": {},
    })

    facts.setdefault("names", {})
    facts.setdefault("analysis_tools", [])
    facts.setdefault("agent_role", "")

    facts.setdefault("task", {})
    facts["task"].setdefault("agent_name", "")
    facts["task"].setdefault("instruction", "")
    facts["task"].setdefault("task_name", "")
    facts["task"].setdefault("team_name", "")
    return facts


def _parse_table_names(text: str) -> list:
    """Extract table names while preserving user-provided identifiers as much as possible."""
    if not text:
        return []
    # Prefer comma/newline/semicolon separated values.
    parts = [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()]
    names = []
    if len(parts) > 1:
        for part in parts:
            # Remove common words but keep schema.table style identifiers.
            cleaned = re.sub(r"[^A-Za-z0-9_.$#]+", "", part).upper()
            if cleaned and re.search(r"[A-Z]", cleaned):
                names.append(cleaned)
    else:
        # Fallback: find Oracle-looking identifiers in free text.
        names = [m.group(0).upper() for m in re.finditer(r"\b[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*)?\b", text)]
        # Ignore obvious prose words when no comma was used.
        stop = {
            "THE", "TABLE", "TABLES", "WILL", "QUERY", "USE", "AND", "OR", "FOR",
            "THIS", "THAT", "ABOUT", "DATA", "DATABASE", "SCHEMA", "LIST", "ALL",
        }
        names = [n for n in names if n not in stop]
    # Dedupe while preserving order.
    out = []
    seen = set()
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _classify_analysis_tools(text: str, prefix: str) -> list:
    """Map Step 5 free text to deterministic optional tool specs."""
    t = (text or "").strip()
    if not t or t.lower() in {"no", "n", "none", "skip", "nope", "not needed", "na", "n/a"}:
        return []
    lower = t.lower()
    tool_types = []
    if any(w in lower for w in ("trend", "period comparison", "over time", "time series")):
        tool_types.append("TREND")
    if any(w in lower for w in ("forecast", "predict", "projection", "project future", "next quarter")):
        tool_types.append("FORECAST")
    if any(w in lower for w in ("anomaly", "outlier", "unusual", "detect unusual")):
        tool_types.append("ANOMALY")
    if any(w in lower for w in ("statistics", "statistical", "moving average", "correlation", "stddev", "standard deviation", "python")):
        tool_types.append("PYTHON")
    if not tool_types:
        tool_types.append("CUSTOM")
    # Dedupe while preserving order.
    deduped = []
    for typ in tool_types:
        if typ not in deduped:
            deduped.append(typ)
    return [_analysis_tool_spec(prefix, typ, t) for typ in deduped]


def _custom_tool_prefix(project: dict) -> str:
    """Pick a short prefix for optional analysis tools from confirmed SQL/RAG tool names."""
    facts = _ensure_facts(project)
    names = project.get("_naming", {}) or facts.get("names", {}) or {}
    for key, suffix in (("sql_tool", "_SQL_TOOL"), ("rag_tool", "_RAG_TOOL")):
        name = names.get(key, "")
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    prefix = _project_prefix(project)
    return prefix.split("_")[0] if "_" in prefix else prefix


def _analysis_tool_spec(base: str, typ: str, instruction: str = "") -> dict:
    """Create deterministic optional tool metadata for the final spec."""
    typ = typ.upper()
    defaults = {
        "TREND": {
            "name": f"{base}_TREND_TOOL",
            "function_name": f"{base}_TREND_ANALYSIS",
            "instruction": "Use this tool to analyze period-over-period trends and growth rates.",
            "inputs": [
                {"name": "P_DEPARTMENT", "description": "Department name. Leave empty for all departments."},
                {"name": "P_PERIODS", "description": "Number of historical periods to include. Default is 6."},
            ],
        },
        "FORECAST": {
            "name": f"{base}_FORECAST_TOOL",
            "function_name": f"{base}_EXPENSE_FORECAST",
            "instruction": "Use this tool to forecast future expenses using historical data.",
            "inputs": [
                {"name": "P_DEPARTMENT", "description": "Department name to forecast. Leave empty for all departments combined."},
                {"name": "P_FUTURE_PERIODS", "description": "Number of future periods to project. Default is 3."},
            ],
        },
        "ANOMALY": {
            "name": f"{base}_ANOMALY_TOOL",
            "function_name": f"{base}_ANOMALY_DETECT",
            "instruction": "Use this tool to detect unusual or anomalous spending patterns.",
            "inputs": [
                {"name": "P_THRESHOLD_PCT", "description": "Percentage deviation from average to flag as anomaly. Default is 20."},
            ],
        },
        "PYTHON": {
            "name": f"{base}_PYTHON_TOOL",
            "function_name": f"{base}_PYTHON_EXPENSE_ANALYSIS",
            "instruction": "Use this tool for advanced statistical analysis such as mean, standard deviation, moving averages, and peak periods.",
            "inputs": [
                {"name": "P_DEPARTMENT", "description": "Department name to analyze. Leave empty for all departments."},
                {"name": "P_THRESHOLD_PCT", "description": "Percentage deviation from mean to flag a period as unusual. Default is 20."},
            ],
        },
        "CUSTOM": {
            "name": f"{base}_CUSTOM_ANALYSIS_TOOL",
            "function_name": f"{base}_CUSTOM_ANALYSIS",
            "instruction": instruction or "Use this custom analysis tool for approved analysis requests.",
            "inputs": [{"name": "P_REQUEST", "description": "User analysis request."}],
        },
    }
    spec = dict(defaults.get(typ, defaults["CUSTOM"]))
    spec["type"] = typ
    if instruction and typ != "CUSTOM":
        spec["instruction"] = instruction
    return spec


def _print_draft_comments(project: dict, display) -> None:
    """Display the current draft comments to the user before asking for approval."""
    C = display.C
    facts = project.get("facts", {})
    objects = facts.get("sql", {}).get("comments", {}).get("objects", {})

    if not objects:
        display.warn("No comments were generated — the objects dict is empty.")
        display.info("This can happen if the schema name could not be resolved or all tables had errors.")
        display.info("You can add comments manually via Review & Manage → Manage NL2SQL Comments → option 4.")
        return

    display.blank()
    print(f"  {C.BOLD}Draft comments — review before approving:{C.RESET}")
    print(f"  {C.DIM}{'─' * 60}{C.RESET}")
    for key, obj in sorted(objects.items()):
        table_comment = (obj.get("table_comment") or "").strip()
        columns = obj.get("columns", {})
        print()
        print(f"  {C.BOLD}{key}{C.RESET}")
        if table_comment:
            print(f"    Table : {table_comment}")
        else:
            print(f"    Table : {C.DIM}(no comment){C.RESET}")
        if columns:
            for col, comment in sorted(columns.items()):
                comment_str = (comment or "").strip()
                if comment_str:
                    # Wrap long comments at 72 chars
                    if len(comment_str) > 72:
                        print(f"    {col:<28}: {comment_str[:72]}")
                        print(f"    {'':<28}  {comment_str[72:]}")
                    else:
                        print(f"    {col:<28}: {comment_str}")
        else:
            print(f"    {C.DIM}(no column comments){C.RESET}")
    print()
    print(f"  {C.DIM}{'─' * 60}{C.RESET}")
    coverage = facts.get("sql", {}).get("comments", {}).get("coverage", {})
    if coverage:
        tw = coverage.get("table_with_comments", 0)
        tt = coverage.get("table_total", 0)
        cw = coverage.get("column_with_comments", 0)
        ct = coverage.get("column_total", 0)
        print(f"  Coverage: {tw}/{tt} tables   {cw}/{ct} columns")
    display.blank()


def _run_comments_readiness(project: dict, cfg, display, clients=None, run_log=None) -> None:
    """Lightweight during-build comments prompt for SQL/NL2SQL metadata."""
    facts = _ensure_facts(project)
    if facts.get("data_source") not in ("tables", "both"):
        return
    sql = facts.setdefault("sql", {})
    comments = sql.setdefault("comments", {
        "mode": "unspecified", "status": "not_started", "objects": {}, "coverage": {}
    })

    display.blank()
    print(f"  {display.C.BOLD}NL2SQL metadata comments{display.C.RESET}")
    print("  Table and column comments improve NL2SQL accuracy and reduce hallucinations.")
    print("  What should the builder do for the selected SQL objects?")
    print("   1. Use existing database comments if present (no COMMENT ON SQL emitted)")
    print("   2. Generate comments using LLM")
    print("   3. Import comments from CSV / JSON now")
    print("   4. Enter comments manually now")
    print("   5. Skip for now")
    try:
        choice = input("  Choice [1/2/3/4/5, Enter=1 | q=skip]: ").strip().lower() or "1"
    except (EOFError, KeyboardInterrupt):
        choice = "5"
    if choice in ("q", "quit", "b", "back", "exit"):
        choice = "5"   # treat quit as skip — safest default

    try:
        from modules import comments as comments_module
        if choice == "1":
            comments_module.scan_existing_comments(cfg, project, display)
            state_module.save_project(cfg, project)
            display.info("Existing comments will be used by comments=true; no COMMENT ON SQL will be emitted unless you approve comments in Review & Manage.")
        elif choice == "2":
            comments_module.generate_llm_comments(project, cfg, clients, display)
            state_module.save_project(cfg, project)
            _print_draft_comments(project, display)
            try:
                approve = input("  Approve these draft comments for final SQL? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                approve = "n"
            if approve == "y":
                comments_module.approve_comments(project, display)
                state_module.save_project(cfg, project)
            else:
                display.warn("Draft comments saved but not approved. Approve later via Review & Manage → Manage NL2SQL Comments → option 7.")
        elif choice == "3":
            path = input("  CSV/JSON path: ").strip()
            comments_module.import_comments(project, path, display)
            state_module.save_project(cfg, project)
            _print_draft_comments(project, display)
            try:
                approve = input("  Approve imported comments for final SQL? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                approve = "n"
            if approve == "y":
                comments_module.approve_comments(project, display)
                state_module.save_project(cfg, project)
            else:
                display.warn("Imported comments saved but not approved. Approve later via Review & Manage → Manage NL2SQL Comments → option 7.")
        elif choice == "4":
            comments_module.enter_comments_manually(project, cfg=cfg, display=display)
            state_module.save_project(cfg, project)
            _print_draft_comments(project, display)
            try:
                approve = input("  Approve manual comments for final SQL? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                approve = "n"
            if approve == "y":
                comments_module.approve_comments(project, display)
                state_module.save_project(cfg, project)
            else:
                display.warn("Manual comments saved but not approved. Approve later via Review & Manage → Manage NL2SQL Comments → option 7.")
        else:
            comments["mode"] = "skipped"
            comments["status"] = "skipped"
            state_module.save_project(cfg, project)
            display.warn("Skipping comments for now. You can manage them later from Review & Manage.")
    except Exception as ex:
        comments["mode"] = comments.get("mode", "unspecified")
        comments["status"] = "error"
        comments["error"] = str(ex)
        state_module.save_project(cfg, project)
        display.warn(f"Comment setup encountered an error: {ex}")
        display.info("You can retry via Review & Manage → Manage NL2SQL Comments.")
        display.info("You can manage NL2SQL comments later from Review & Manage.")

    if run_log:
        run_log.log_state("sql_comments", sql.get("comments", {}))


def _run_step5_analysis_tools(project: dict, display, cfg=None, run_log=None, edit_mode: bool = False) -> str:
    """Client-side Step 5 — deterministic optional analysis tool selection.
    Returns: ok, cancel, or save when used in edit mode.
    """
    facts = _ensure_facts(project)
    base = _custom_tool_prefix(project)
    display.blank()
    print(f"  {display.C.BOLD}Additional analysis tools{display.C.RESET}")
    print("  Choose optional tools to add beyond the core RAG/SQL tools:")
    print("   1. Trend analysis")
    print("   2. Forecasting")
    print("   3. Anomaly detection")
    print("   4. Python/statistical analysis")
    print("   5. All of the above")
    print("   6. None")
    print("   7. Custom description")
    try:
        raw = input("  Choice [1,2,3,4,5,6,7 or comma list; Enter=6 | q=save & quit]: ").strip().lower() or "6"
    except (EOFError, KeyboardInterrupt):
        raw = "quit no change" if edit_mode else "quit"

    if not edit_mode and raw in ("q", "quit", "exit", "b", "back"):
        _maybe_save(cfg, project) if cfg else None
        display.warn("Quit — progress saved. You can resume from Step 5.")
        return "quit"

    if edit_mode and raw in NO_CHANGE_COMMANDS:
        project["_edit_cancelled"] = True
        display.info("Edit cancelled — no changes will be saved")
        if run_log:
            run_log.log("EDIT_CANCELLED at Step 5")
        return "cancel"
    if edit_mode and raw in SAVE_COMMANDS:
        project["_edit_committed"] = True
        display.ok("Edit changes marked for save")
        if run_log:
            run_log.log("EDIT_SAVE_REQUESTED at Step 5")
        return "save"

    selected = []
    custom_instruction = ""
    if raw in ("6", "n", "no", "none", "skip"):
        selected = []
    elif raw in ("5", "all", "a"):
        selected = ["TREND", "FORECAST", "ANOMALY", "PYTHON"]
    elif raw in ("7", "custom", "c"):
        custom_instruction = _ask_text(display, "Describe the custom analysis tool", "Custom analysis request")
        selected = ["CUSTOM"]
    else:
        mapping = {"1": "TREND", "2": "FORECAST", "3": "ANOMALY", "4": "PYTHON"}
        for part in re.split(r"[,/;\s]+", raw):
            typ = mapping.get(part)
            if typ and typ not in selected:
                selected.append(typ)
        if not selected:
            # Treat free text as a classification request, but still deterministic.
            facts["analysis_tools"] = _classify_analysis_tools(raw, base)
            if run_log:
                run_log.log_state("analysis_tools", facts["analysis_tools"])
            return "ok"

    facts["analysis_tools"] = [
        _analysis_tool_spec(base, typ, custom_instruction) for typ in selected
    ]
    if selected:
        display.ok("Analysis tools selected: " + ", ".join(selected))
    else:
        display.ok("No additional analysis tools selected")
    if run_log:
        run_log.log_state("analysis_tools", facts["analysis_tools"])
    return "ok"


def _last_assistant_text(history: list) -> str:
    return next((h.get("text", "") for h in reversed(history) if h.get("role") == "ASSISTANT"), "")


def _record_fact_from_user(project: dict, current_step: int, user_input: str, history: list) -> None:
    """Capture canonical facts from the current user turn before calling the LLM."""
    facts = _ensure_facts(project)
    text = (user_input or "").strip()
    lower = text.lower()

    if current_step == 2:
        if any(w in lower for w in ("both", "all", "sql and rag", "rag and sql", "documents and tables", "tables and documents")):
            facts["data_source"] = "both"
        elif any(w in lower for w in ("table", "tables", "sql", "database", "db")):
            facts["data_source"] = "tables"
        elif any(w in lower for w in ("doc", "docs", "document", "documents", "rag", "pdf", "file", "upload")):
            facts["data_source"] = "documents"
        return

    if current_step == 3:
        last_q = _last_assistant_text(history).lower()
        rag = facts.setdefault("rag", {})
        sql = facts.setdefault("sql", {})
        if "subject matter" in last_q:
            rag["subject"] = text
        elif "file types" in last_q or "knowledge base" in last_q:
            rag["file_types"] = [p.strip().upper() for p in re.split(r"[,;/]+", text) if p.strip()]
        elif "object storage" in last_q or "documents are stored" in last_q or "location" in last_q:
            rag["object_storage_url"] = text or project.get("rag_url", "")
        elif "which tables" in last_q or "agent query" in last_q or "table names" in last_q:
            tables = _parse_table_names(text)
            if tables:
                sql["tables"] = tables
        elif "what kinds of questions" in last_q or "users ask" in last_q:
            sql["question_types"] = text
        return

    if current_step == 5:
        facts["analysis_tools"] = _classify_analysis_tools(text, _project_prefix(project))
        return

    if current_step == 6:
        facts["agent_role"] = text
        return


def _facts_context(project: dict) -> str:
    facts = _ensure_facts(project)
    return (
        "CANONICAL FACTS - use exactly; do not rename or omit these values:\n"
        + json.dumps(facts, indent=2, sort_keys=True)
    )


def _history_with_facts(history: list, project: dict) -> list:
    """Add canonical facts to each LLM call so critical details survive history trimming."""
    return [{"role": "USER", "text": _facts_context(project)}] + list(history)


def _ask_db_name(display, prompt: str, default: str) -> str:
    C = display.C
    try:
        val = input(
            f"  {C.BOLD}{prompt}{C.RESET}"
            f"  {C.DIM}[Press Enter for {default}]{C.RESET}: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        val = ""
    return _safe_db_name(val or default, default)


def _ask_text(display, prompt: str, default: str = "") -> str:
    C = display.C
    suffix = f" {C.DIM}[Press Enter for {default}]{C.RESET}" if default else ""
    try:
        val = input(f"  {C.BOLD}{prompt}{C.RESET}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        val = ""
    return val or default


def _read_long_text(display, prompt: str, default: str = "",
                    sentinel: str = ".", allow_file: bool = True) -> str:
    """Collect potentially long text — agent role, task instruction, etc.

    Supports three input modes selected by what the user types first:

    1. FILE IMPORT  — user types a file path (starts with / or ~ or ./).
                      Content is read from the file, previewed (first 5 lines),
                      and confirmed before accepting. Any file path works;
                      the most natural workflow is keeping role/instruction text
                      in a .txt file alongside the project and pointing here.

    2. MULTI-LINE   — user types content across multiple lines. Input ends when
                      the user types the sentinel alone on a line (default ".").
                      Newlines inside the text are preserved. Paste any number
                      of lines; the sentinel is the only terminator so paste
                      buffer limits don't matter.

    3. SINGLE-LINE  — if the first line the user types is neither a path nor
                      followed by more lines (i.e. they type Enter immediately
                      after the sentinel), the single line is used as-is. This
                      is the old behaviour for short inputs.

    In all three modes the user can press Enter on the very first prompt to
    accept `default` (if one is supplied), and can type the sentinel alone
    immediately to also accept the default.
    """
    import os

    C = display.C
    print()
    print(f"  {C.BOLD}{prompt}{C.RESET}")
    print(f"  {C.DIM}Options:")
    if allow_file:
        print(f"    • Type a file path (e.g. ~/agent_role.txt) to import from file")
    print(f"    • Paste or type multiple lines — type {sentinel!r} alone on a line when done")
    if default:
        print(f"    • Press Enter on a blank line to use the existing value")
    print(f"  {C.RESET}", end="")

    lines = []
    first = True
    while True:
        try:
            line = input("  " if first else "  ")
        except (EOFError, KeyboardInterrupt):
            break
        first = False

        # Quit / cancel / save commands on first line — pass through unchanged
        # so _ask_text_step7 / the caller can handle them as edit-mode commands.
        if not lines and line.strip().lower() in (
            "q", "quit", "exit", "cancel", "save",
            "quit no change", "discard", "q!",
        ):
            return line.strip()

        # Sentinel alone → done
        if line.strip() == sentinel:
            break

        # Blank first line → accept default
        if not lines and line.strip() == "":
            if default:
                print(f"  {C.DIM}(using existing value — {len(default)} chars){C.RESET}")
                return default
            continue

        # File path on the very first non-blank line
        if allow_file and not lines:
            stripped = line.strip()
            if stripped.startswith(("/", "~", "./")):
                path = os.path.expanduser(stripped)
                if os.path.isfile(path):
                    try:
                        content = open(path, encoding="utf-8").read().rstrip()
                        preview_lines = content.splitlines()[:5]
                        print(f"  {C.DIM}File: {path}  ({len(content)} chars, "
                              f"{len(content.splitlines())} lines){C.RESET}")
                        print(f"  {C.DIM}Preview:{C.RESET}")
                        for pl in preview_lines:
                            print(f"    {pl}")
                        if len(content.splitlines()) > 5:
                            print(f"    ... ({len(content.splitlines()) - 5} more lines)")
                        try:
                            confirm = input(
                                f"  {C.BOLD}Accept this file content? [Y/n]: {C.RESET}"
                            ).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            confirm = "y"
                        if confirm in ("", "y", "yes"):
                            print(f"  {C.DIM}✓ Imported {len(content)} chars from file{C.RESET}")
                            return content
                        else:
                            print(f"  {C.DIM}File rejected — enter text below or try a different path{C.RESET}")
                            continue
                    except Exception as ex:
                        print(f"  {C.DIM}Could not read file: {ex} — enter text manually{C.RESET}")
                        continue
                else:
                    print(f"  {C.DIM}File not found: {path} — entering as text{C.RESET}")

        lines.append(line)

    result = "\n".join(lines).strip()
    return result if result else (default or "")



def _run_step6_agent_name(project: dict, cfg, display, history: list, run_log=None, edit_mode: bool = False) -> str:
    """Client-side Step 6 agent object name collection."""
    facts = _ensure_facts(project)
    task = facts.setdefault("task", {})
    prefix = _project_prefix(project)
    default = task.get("agent_name") or f"{prefix}_AGENT"
    C = display.C

    try:
        raw = input(
            f"  {C.BOLD}Agent name{C.RESET}"
            f"  {C.DIM}[Press Enter for {default}]{C.RESET}: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        raw = "quit no change" if edit_mode else ""

    low = raw.lower()
    if edit_mode and low in NO_CHANGE_COMMANDS:
        project["_edit_cancelled"] = True
        display.info("Edit cancelled — no changes will be saved")
        if run_log:
            run_log.log("EDIT_CANCELLED at Step 6 agent name")
        return "cancel"
    if edit_mode and low in SAVE_COMMANDS:
        project["_edit_committed"] = True
        display.ok("Edit changes marked for save")
        if run_log:
            run_log.log("EDIT_SAVE_REQUESTED at Step 6 agent name")
        return "save"

    if not edit_mode and low in ("q", "quit", "exit", "b", "back", "save"):
        _maybe_save(cfg, project)
        display.warn("Quit — progress saved. You can resume from Step 6.")
        project["_step6_quit"] = True
        return "quit"

    agent_name = _safe_db_name(raw or default, default)
    task["agent_name"] = agent_name
    facts.setdefault("names", {})["agent"] = agent_name

    summary = f"Step 6 agent name confirmed by user: agent_name={agent_name}"
    history.append({"role": "USER", "text": summary, "step": 6})
    project = state_module.add_to_conversation(project, "USER", summary, 6)

    print(f"  {C.BOLD}Accepted Step 6 agent name:{C.RESET} {agent_name}")
    if run_log:
        run_log.log_state("accepted_step6_agent_name", {"agent_name": agent_name})
    return "ok"


def _run_step7_finalize(project: dict, cfg, display, clients=None, run_log=None, edit_mode: bool = False) -> dict:
    """Client-side Step 7. Collect exact task/team values and build the spec deterministically."""
    facts = _ensure_facts(project)
    prefix = _project_prefix(project)
    task = facts.setdefault("task", {})

    display.info("Step 7 is handled by the application to prevent LLM renaming or flow changes.")
    agent_name = task.get("agent_name") or facts.get("names", {}).get("agent") or f"{prefix}_AGENT"
    agent_name = _safe_db_name(agent_name, f"{prefix}_AGENT")
    task["agent_name"] = agent_name
    def _ask_db_name_step7(prompt: str, default: str):
        C = display.C
        try:
            raw = input(
                f"  {C.BOLD}{prompt}{C.RESET}"
                f"  {C.DIM}[Press Enter for {default}]{C.RESET}: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            raw = "quit no change" if edit_mode else ""
        low = raw.lower()
        if edit_mode and low in NO_CHANGE_COMMANDS:
            project["_edit_cancelled"] = True
            display.info("Edit cancelled — no changes will be saved")
            return None
        if edit_mode and low in SAVE_COMMANDS:
            project["_edit_committed"] = True
            display.ok("Edit changes marked for save")
            return None
        if not edit_mode and low in ("q", "quit", "exit", "b", "back", "save"):
            project["_step7_quit"] = True
            return None
        return _safe_db_name(raw or default, default)

    def _ask_text_step7(prompt: str, default: str):
        """Like _ask_text but with multi-line / file-import support and
        edit-mode command handling for Step 7 long-text fields (task instruction).
        """
        text = _read_long_text(display, prompt, default=default)
        low = text.lower().strip()
        if edit_mode and low in NO_CHANGE_COMMANDS:
            project["_edit_cancelled"] = True
            display.info("Edit cancelled — no changes will be saved")
            return None
        if edit_mode and low in SAVE_COMMANDS:
            project["_edit_committed"] = True
            display.ok("Edit changes marked for save")
            return None
        if not edit_mode and low in ("q", "quit", "exit", "b", "back", "save"):
            project["_step7_quit"] = True
            return None
        return text or default

    instruction_default = facts.get("agent_role") or "Answer user requests using the configured tools."

    # When all Step 7 values are already populated (e.g. from a Word doc /
    # CSV import), skip re-prompting and go straight to the confirmation
    # summary — the user already provided these values in the document.
    existing_instruction = task.get("instruction", "").strip()
    existing_task_name   = task.get("task_name", "").strip()
    existing_team_name   = task.get("team_name", "").strip()
    _imported = project.get("_imported_from_docx") or project.get("_imported_from_csv")

    if _imported and existing_instruction and existing_task_name and existing_team_name and not edit_mode:
        task_instruction = existing_instruction
        task_name        = _safe_db_name(existing_task_name, f"{prefix}_TASK")
        team_name        = _safe_db_name(existing_team_name, f"{prefix}_TEAM")
        display.info("All Step 7 values already imported from document — skipping prompts.")
        print(f"  {display.C.BOLD}Imported Step 7 values:{display.C.RESET}")
        print(f"    agent_name        : {agent_name}  (from Step 6)")
        print(f"    task_name         : {task_name}")
        print(f"    team_name         : {team_name}")
        preview = task_instruction.replace("\n", " ")[:120]
        print(f"    task_instruction  : {preview}{'...' if len(task_instruction) > 120 else ''}")
        print()
        try:
            confirm = input("  Accept these values? [Y/n/edit]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "y"
        if confirm in ("n", "no"):
            project["_step7_quit"] = True
            _maybe_save(cfg, project)
            display.warn("Quit — resume from Step 7 to re-enter values.")
            return project
        if confirm in ("e", "edit"):
            # Fall through to normal prompts below
            task_instruction = _ask_text_step7("Task instruction", existing_instruction or instruction_default)
            if project.get("_edit_cancelled") or project.get("_edit_committed"):
                return project
            if project.get("_step7_quit"):
                _maybe_save(cfg, project)
                display.warn("Quit — progress saved. You can resume from Step 7.")
                return project
            task_name = _ask_db_name_step7("Task name", existing_task_name or f"{prefix}_TASK")
            if project.get("_edit_cancelled") or project.get("_edit_committed"):
                return project
            if project.get("_step7_quit"):
                _maybe_save(cfg, project)
                display.warn("Quit — progress saved. You can resume from Step 7.")
                return project
            team_name = _ask_db_name_step7("Team name", existing_team_name or f"{prefix}_TEAM")
            if project.get("_edit_cancelled") or project.get("_edit_committed"):
                return project
            if project.get("_step7_quit"):
                _maybe_save(cfg, project)
                display.warn("Quit — progress saved. You can resume from Step 7.")
                return project
        # else "y" / enter → accepted as-is, fall through to spec build
    else:
        task_instruction = _ask_text_step7("Task instruction", existing_instruction or instruction_default)
        if project.get("_edit_cancelled") or project.get("_edit_committed"):
            return project
        if project.get("_step7_quit"):
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. You can resume from Step 7.")
            return project
        task_name = _ask_db_name_step7("Task name", existing_task_name or f"{prefix}_TASK")
        if project.get("_edit_cancelled") or project.get("_edit_committed"):
            return project
        if project.get("_step7_quit"):
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. You can resume from Step 7.")
            return project
        team_name = _ask_db_name_step7("Team name", existing_team_name or f"{prefix}_TEAM")
        if project.get("_edit_cancelled") or project.get("_edit_committed"):
            return project
        if project.get("_step7_quit"):
            _maybe_save(cfg, project)
            display.warn("Quit — progress saved. You can resume from Step 7.")
            return project

    task.update({
        "agent_name": agent_name,
        "instruction": task_instruction,
        "task_name": task_name,
        "team_name": team_name,
        "process": "sequential",
    })

    summary = "Step 7 finalized by user: " + ", ".join(
        f"{k}={v}" for k, v in task.items()
    )
    project = state_module.add_to_conversation(project, "USER", summary, 7)
    print(f"  {display.C.BOLD}Accepted Step 7 task/team values:{display.C.RESET}")
    print(f"    agent_name        : {agent_name}  (from Step 6)")
    print(f"    task_name         : {task_name}")
    print(f"    team_name         : {team_name}")
    print(f"    task_instruction  : {task_instruction}")
    if run_log:
        run_log.log("STEP7_FINALIZED: " + json.dumps(task, sort_keys=True))
        run_log.log_state("accepted_step7_names", task)

    spec = _build_spec_from_facts(project, cfg)
    spec = _canonicalize_spec(spec, project)
    try:
        _validate_spec_against_facts(spec, project)
    except Exception as ex:
        display.err(f"Spec validation failed: {ex}")
        display.info("The project will remain in discovery so you can correct the missing value.")
        project = state_module.update_phase(project, "discovery")
        _maybe_save(cfg, project, edit_mode)
        return project
    project["spec"] = spec
    _set_workflow(project, current_step=7, last_completed_step=7, edit_mode=edit_mode)
    project = state_module.update_phase(project, "review")
    _maybe_save(cfg, project, edit_mode)
    display.ok("Specification complete")
    _print_spec_summary(spec, display)
    display.blank()
    print("  Review the summary above, and choose:")
    print("   1. Generate the code")
    print("   2. Edit (go back to conversation)")
    print("   3. Quit and save")
    display.blank()
    try:
        ans = input("  Select [1/2/3]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "3"
    if ans in ("2", "fix", "edit"):
        display.info("Going back to conversation — tell the assistant what to correct.")
        project = state_module.update_phase(project, "discovery")
        _maybe_save(cfg, project, edit_mode)
        return _edit_then_regenerate(project, cfg, clients, display, run_log=run_log)
    if ans == "3":
        _maybe_save(cfg, project, edit_mode)
        display.ok("Progress saved — use 'Resume project' to continue")
        return project
    return project


def _enhance_role_with_routing(role: str, spec: dict, facts: dict) -> str:
    """Append deterministic tool-routing guardrails without changing the user role."""
    role = (role or "").strip()
    pieces = [role] if role else []
    sql_tables = facts.get("sql", {}).get("tables", [])
    tools = {str(t.get("type", "")).upper(): t.get("name") for t in spec.get("tools", [])}
    if tools.get("SQL") and sql_tables:
        pieces.append(
            f"Use {tools['SQL']} for live SQL questions against approved objects: "
            + ", ".join(sql_tables) + "."
        )
    if tools.get("RAG"):
        pieces.append(f"Use {tools['RAG']} for document, policy, procedure, and compliance questions.")
    for typ, label in (("TREND", "period-over-period trend or growth-rate analysis"),
                       ("FORECAST", "forecasting and future-period projections"),
                       ("ANOMALY", "detecting unusual or anomalous patterns"),
                       ("PYTHON", "advanced statistical analysis such as moving averages and standard deviation")):
        if tools.get(typ):
            pieces.append(f"Use {tools[typ]} for {label}.")
    pieces.append("Only report data returned by tools. Never invent or estimate figures.")
    # Dedupe exact sentences while preserving order.
    out = []
    seen = set()
    for piece in pieces:
        if piece and piece not in seen:
            seen.add(piece)
            out.append(piece)
    return " ".join(out)


def _build_spec_from_facts(project: dict, cfg) -> dict:
    facts = _ensure_facts(project)
    names = project.get("_naming", {}) or facts.get("names", {}) or {}
    prefix = _project_prefix(project)
    data_source = facts.get("data_source") or "both"
    is_rag = data_source in ("documents", "both")
    is_sql = data_source in ("tables", "both")

    # Step 4 names, with deterministic fallbacks.
    rag_profile = names.get("rag_profile", f"{prefix}_RAG_PROFILE")
    vector_index = names.get("vector_index", f"{prefix}_VECTOR_IDX")
    rag_tool = names.get("rag_tool", f"{prefix}_RAG_TOOL")
    nl2sql_profile = names.get("nl2sql_profile", f"{prefix}_NL2SQL_PROFILE")
    sql_tool = names.get("sql_tool", f"{prefix}_SQL_TOOL")
    chunk_size = int(names.get("chunk_size", 1024))
    chunk_overlap = int(names.get("chunk_overlap", 128))

    task = facts.get("task", {})
    agent_name = task.get("agent_name", f"{prefix}_AGENT")
    task_name = task.get("task_name", f"{prefix}_TASK")
    team_name = task.get("team_name", f"{prefix}_TEAM")
    task_instruction = task.get("instruction") or facts.get("agent_role") or "Answer user requests using the configured tools."
    role = facts.get("agent_role") or task_instruction

    spec = {
        "project_name": project.get("display_name") or project.get("project_name"),
        "schema": project.get("schema", ""),
        "data_source": data_source,
        "comments": facts.get("sql", {}).get("comments", {}),
        "profiles": [],
        "vector_indexes": [],
        "tools": [],
        "agents": [],
        "tasks": [],
        "teams": [],
    }

    tools_for_agent = []

    if is_sql:
        tables = facts.get("sql", {}).get("tables", [])
        spec["profiles"].append({
            "name": nl2sql_profile,
            "type": "NL2SQL",
            "model": cfg_module.get(cfg, "llm", "chat_model", fallback=""),
            "tables": tables,
            "object_list": tables,
            "comments_enabled": facts.get("sql", {}).get("comments", {}).get("mode") != "skipped",
        })
        spec["tools"].append({
            "name": sql_tool,
            "type": "SQL",
            "profile_name": nl2sql_profile,
            "instruction": facts.get("sql", {}).get("question_types", "Answer questions by querying approved database objects."),
            "inputs": [{"name": "question", "description": "User question to translate to SQL"}],
        })
        tools_for_agent.append(sql_tool)

    if is_rag:
        rag = facts.get("rag", {})
        location = rag.get("object_storage_url") or project.get("rag_url") or cfg_module.get(cfg, "object_storage", "rag_location_url", fallback="")
        spec["profiles"].append({
            "name": rag_profile,
            "type": "RAG",
            "model": cfg_module.get(cfg, "llm", "chat_model", fallback=""),
            "vector_index_name": vector_index,
            "embed_model": cfg_module.get(cfg, "llm", "embed_model", fallback=""),
        })
        spec["vector_indexes"].append({
            "name": vector_index,
            "location": location,
            "profile_name": rag_profile,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        })
        spec["tools"].append({
            "name": rag_tool,
            "type": "RAG",
            "profile_name": rag_profile,
            "instruction": rag.get("subject", "Answer questions from the document knowledge base."),
            "inputs": [{"name": "question", "description": "User question for the RAG knowledge base"}],
        })
        tools_for_agent.append(rag_tool)

    for tool in facts.get("analysis_tools", []) or []:
        spec["tools"].append(tool)
        tools_for_agent.append(tool["name"])

    role = _enhance_role_with_routing(role, spec, facts)
    primary_profile = nl2sql_profile if is_sql else (rag_profile if is_rag else "")
    spec["agents"].append({
        "name": agent_name,
        "profile_name": primary_profile,
        "role": role,
        "tools": tools_for_agent,
    })
    spec["tasks"].append({
        "name": task_name,
        "agent_name": agent_name,
        "instruction": task_instruction,
        "tools": tools_for_agent,
    })
    spec["teams"].append({
        "name": team_name,
        "agents": [{"name": agent_name, "task": task_name}],
        "process": "sequential",
    })
    return spec


def _canonicalize_spec(spec: dict, project: dict) -> dict:
    """Apply canonical Python-owned facts to an LLM or deterministic spec."""
    _ensure_facts(project)
    naming = project.get("_naming", {}) or project.get("facts", {}).get("names", {}) or {}
    if naming:
        _apply_naming_to_spec(spec, naming)

    facts = project.get("facts", {})
    spec["comments"] = facts.get("sql", {}).get("comments", spec.get("comments", {}))
    sql_tables = facts.get("sql", {}).get("tables", [])
    if sql_tables:
        for p in spec.get("profiles", []):
            if p.get("type", "").upper() == "NL2SQL":
                p["tables"] = list(sql_tables)
                p["object_list"] = list(sql_tables)
                p["comments_enabled"] = facts.get("sql", {}).get("comments", {}).get("mode") != "skipped"

    rag_location = facts.get("rag", {}).get("object_storage_url") or project.get("rag_url")
    if rag_location:
        for vi in spec.get("vector_indexes", []):
            vi["location"] = rag_location

    task = facts.get("task", {})
    agent_name = task.get("agent_name")
    task_name = task.get("task_name")
    team_name = task.get("team_name")
    instruction = task.get("instruction")
    role = facts.get("agent_role")

    # Keep agent profile_name aligned with the primary NL2SQL profile when present.
    nl2sql_names = [p.get("name") for p in spec.get("profiles", []) if str(p.get("type", "")).upper() == "NL2SQL"]
    primary_profile = nl2sql_names[0] if nl2sql_names else ""

    if agent_name:
        for a in spec.get("agents", []):
            a["name"] = agent_name
            if primary_profile:
                a["profile_name"] = primary_profile
            if role:
                a["role"] = _enhance_role_with_routing(role, spec, facts)
        for t in spec.get("tasks", []):
            t["agent_name"] = agent_name
        for team in spec.get("teams", []):
            for ag in team.get("agents", []):
                ag["name"] = agent_name
    if task_name:
        for t in spec.get("tasks", []):
            t["name"] = task_name
        for team in spec.get("teams", []):
            for ag in team.get("agents", []):
                ag["task"] = task_name
    if team_name:
        for team in spec.get("teams", []):
            team["name"] = team_name
            team["process"] = "sequential"
    if instruction:
        for t in spec.get("tasks", []):
            t["instruction"] = instruction
    return spec


def _validate_spec_against_facts(spec: dict, project: dict) -> None:
    """Fail fast when spec diverges from canonical facts."""
    from core.spec_validator import validate_spec
    validate_spec(spec, facts=project.get("facts", {}))


def _discovery_loop(project: dict, cfg, clients: dict, display,
                    display_name: str = None, rag_url: str = None,
                    show_roadmap: bool = True, run_log=None,
                    start_step: int = None, edit_mode: bool = False) -> dict:
    """
    Run the Phase 1 discovery conversation.
    Returns updated project dict with spec populated.
    """
    full_system_prompt = _load_template("system_prompt.txt")
    if not full_system_prompt:
        display.err("System prompt template not found — expected at templates/system_prompt.txt")
        return project

    # For Q&A turns: send only the behavioural rules (before SPEC OUTPUT section).
    # This keeps per-turn token count small and reduces latency.
    # The full prompt (including SPEC OUTPUT schema) is sent on the final turn
    # so the LLM knows how to format the spec correctly.
    _spec_boundary = full_system_prompt.find("SPEC OUTPUT:")
    conversation_prompt = (
        full_system_prompt[:_spec_boundary].strip()
        if _spec_boundary > 0 else full_system_prompt
    )
    # The Python application owns Step 4, Step 7, and final spec generation.
    # The LLM only asks the next discovery question.
    conversation_prompt += (
        "\n\nThe application handles object naming, task/team naming, and final spec generation. "
        "Do not output <SPEC> unless explicitly asked by the application. "
        "Only ask the next single discovery question."
    )

    system_prompt = conversation_prompt  # used for Q&A turns

    C = display.C
    _set_workflow(project, edit_mode=edit_mode)

    # ── Print conversation header ─────────────────────────────────────────────
    display.blank()
    print(f"  {C.BOLD}Agent Builder Conversation{C.RESET}")
    display.blank()

    # Resume from existing conversation or start fresh
    history = [
        {"role": h["role"], "text": h["text"]}
        for h in project.get("conversation", [])
    ]

    current_step = 1

    # ── Initial greeting ──────────────────────────────────────────────────────
    # Use display_name if provided, else fall back to project file name
    agent_name = display_name or project.get("display_name") or project["project_name"]

    if not history and start_step is None:
        display.info("Starting conversation — Step 1 (name/schema) already complete...")
        _rag_url = rag_url or project.get("rag_url", "")
        if _rag_url:
            project["rag_url"] = _rag_url

        # Seed the history so the LLM has context from Step 1 onwards,
        # but do NOT call the LLM — just print Step 2 directly.
        # Step 2's question is always the same and needs no LLM involvement.
        step2_question = "What data sources? (database tables / documents / both)"
        seed_message = (
            f"I am building a Select AI Agent project called \"{agent_name}\". "
            f"The database schema is {project['schema']}."
            + (f" The Object Storage RAG URL is: {_rag_url}." if _rag_url else "") +
            f" Step 1 is complete. Starting at Step 2."
        )
        assistant_step2 = step2_question

        history.append({"role": "USER",      "text": seed_message,    "step": 1})
        history.append({"role": "ASSISTANT", "text": assistant_step2, "step": 2})
        project = state_module.add_to_conversation(project, "USER",      seed_message,    1)
        project = state_module.add_to_conversation(project, "ASSISTANT", assistant_step2, 2)

        current_step = 2
        _set_workflow(project, current_step=2, last_completed_step=1, edit_mode=edit_mode)
        _print_step_header(2, display)
        print(f"  {step2_question}\n")
        _pending_history = None

    elif not history and start_step is not None:
        # Imported project jumping straight to a specific step (e.g. Step 7
        # from docx/CSV import "Proceed"). Seed history silently so the LLM
        # has project context on any subsequent turns, but don't print the
        # Step 2 header — the start_step block below handles the display.
        _rag_url = rag_url or project.get("rag_url", "")
        if _rag_url:
            project["rag_url"] = _rag_url
        seed_message = (
            f"I am building a Select AI Agent project called \"{agent_name}\". "
            f"The database schema is {project['schema']}."
            + (f" The Object Storage RAG URL is: {_rag_url}." if _rag_url else "") +
            f" This project was imported — proceeding directly to Step {start_step}."
        )
        history.append({"role": "USER", "text": seed_message, "step": 1})
        project = state_module.add_to_conversation(project, "USER", seed_message, 1)
        _pending_history = None

    elif start_step is None:
        # Resuming — inject a no-acknowledgment reminder for the first LLM call only
        no_ack_reminder = (
            "REMINDER: Do not acknowledge the previous answer. "
            "Do not say 'Great', 'Excellent', 'Noted', or anything similar. "
            "Just ask the next single question directly with no preamble."
        )
        _pending_history = history + [{"role": "USER", "text": no_ack_reminder}]

        last_assistant = next(
            (h["text"] for h in reversed(history) if h["role"] == "ASSISTANT"), None
        )
        if last_assistant:
            if show_roadmap:
                _print_roadmap(display)
                display.blank()
            # Scan full history for highest step seen (last message may be a completion)
            current_step = 1
            for h in history:
                if h["role"] == "ASSISTANT":
                    current_step = _detect_step(h["text"], current_step)
            _print_step_header(current_step, display)
            print(f"  {C.CYAN}Resuming — last message:{C.RESET}")
            print(f"  {last_assistant}\n")

    else:
        # Explicit edit/resume start is handled below. Avoid printing stale
        # "Resuming — last message" content from trimmed history because that
        # can show the previous step header before the selected edit step.
        _pending_history = None

    # ── Conversation loop ─────────────────────────────────────────────────────
    facts = _ensure_facts(project)
    _data_source    = facts.get("data_source")  # "documents", "tables", or "both"
    _both_sql_asked = bool(facts.get("sql", {}).get("tables"))

    # Explicit edit/resume start. Do not infer the screen from trimmed chat text.
    if start_step is not None:
        try:
            current_step = max(1, min(7, int(start_step)))
        except Exception:
            current_step = _get_last_completed_step(project)
        _set_workflow(project, current_step=current_step, edit_mode=edit_mode)
        if show_roadmap:
            _print_roadmap(display)
            display.blank()
        _print_step_header(current_step, display)
        if edit_mode:
            print(f"  {C.DIM}Note: Edit mode commands: save/quit = save changes | cancel/discard/quit no change/q! = discard changes{C.RESET}")
            display.blank()

        # Application-owned steps must run directly. This fixes the Step 5 -> Step 4 regression.
        if current_step == 4:
            project = _run_step4_naming(project, cfg, display, _data_source, history, run_log=run_log)
            naming = project.get("_naming", {})
            summary = "Step 4 naming confirmed by user: " + ", ".join(f"{k}={v}" for k, v in naming.items())
            history.append({"role": "USER", "text": summary, "step": 4})
            project = state_module.add_to_conversation(project, "USER", summary, 4)
            _set_workflow(project, current_step=5, last_completed_step=4)
            _run_comments_readiness(project, cfg, display, clients=clients, run_log=run_log)
            current_step = 5
            _print_step_header(5, display)
            step5_status = _run_step5_analysis_tools(project, display, cfg=cfg, run_log=run_log, edit_mode=edit_mode)
            if step5_status in ("cancel", "save", "quit"):
                return project
            step5_summary = "Step 5 analysis tools confirmed by user: " + json.dumps(_ensure_facts(project).get("analysis_tools", []), sort_keys=True)
            history.append({"role": "USER", "text": step5_summary, "step": 5})
            project = state_module.add_to_conversation(project, "USER", step5_summary, 5)
            _set_workflow(project, current_step=6, last_completed_step=5)
            current_step = 6
            _print_step_header(6, display)
            step6_status = _run_step6_agent_name(project, cfg, display, history, run_log=run_log, edit_mode=edit_mode)
            if step6_status in ("cancel", "save", "quit"):
                return project
            step6_question = "Describe the agent role, responsibilities, and guardrails."
            print(f"  {step6_question}\n")
            history.append({"role": "ASSISTANT", "text": step6_question, "step": 6})
            project = state_module.add_to_conversation(project, "ASSISTANT", step6_question, 6)
        elif current_step == 5:
            step5_status = _run_step5_analysis_tools(project, display, cfg=cfg, run_log=run_log, edit_mode=edit_mode)
            if step5_status in ("cancel", "save", "quit"):
                return project
            step5_summary = "Step 5 analysis tools confirmed by user: " + json.dumps(_ensure_facts(project).get("analysis_tools", []), sort_keys=True)
            history.append({"role": "USER", "text": step5_summary, "step": 5})
            project = state_module.add_to_conversation(project, "USER", step5_summary, 5)
            _set_workflow(project, current_step=6, last_completed_step=5)
            current_step = 6
            _print_step_header(6, display)
            step6_status = _run_step6_agent_name(project, cfg, display, history, run_log=run_log, edit_mode=edit_mode)
            if step6_status in ("cancel", "save", "quit"):
                return project
            step6_question = "Describe the agent role, responsibilities, and guardrails."
            print(f"  {step6_question}\n")
            history.append({"role": "ASSISTANT", "text": step6_question, "step": 6})
            project = state_module.add_to_conversation(project, "ASSISTANT", step6_question, 6)
        elif current_step == 6:
            step6_status = _run_step6_agent_name(project, cfg, display, history, run_log=run_log, edit_mode=edit_mode)
            if step6_status in ("cancel", "save", "quit"):
                return project
            step6_question = "Describe the agent role, responsibilities, and guardrails."
            print(f"  {step6_question}\n")
            history.append({"role": "ASSISTANT", "text": step6_question, "step": 6})
            project = state_module.add_to_conversation(project, "ASSISTANT", step6_question, 6)
        elif current_step == 7:
            project = _run_step7_finalize(project, cfg, display, clients=clients, run_log=run_log, edit_mode=edit_mode)
            return project

    print(f"  {C.DIM}Tip: if you make a mistake, just say what it should be — "
          f"e.g. 'Actually the index name should be X'{C.RESET}")
    display.blank()

    while True:
        # Step 6 role/persona — long text, multi-line / file-import supported
        if current_step == 6:
            last_asst = next(
                (h["text"] for h in reversed(history) if h["role"] == "ASSISTANT"), ""
            )
            if "role" in last_asst.lower() or "guardrail" in last_asst.lower() or "persona" in last_asst.lower():
                existing_role = _ensure_facts(project).get("agent_role", "")
                user_input = _read_long_text(
                    display,
                    "Agent role, responsibilities, and guardrails",
                    default=existing_role,
                )
                if not user_input:
                    continue
                # Echo to log (same pattern as the standard input() path below)
                import sys as _sys
                if hasattr(_sys.stdout, "write_log_only"):
                    _sys.stdout.write_log_only(f"{user_input}\n")
                if run_log:
                    run_log.log_user(user_input, step=current_step)
            else:
                try:
                    user_input = input(f"  {C.BOLD}You:{C.RESET} ").strip()
                except (EOFError, KeyboardInterrupt):
                    user_input = "quit"
        else:
            try:
                user_input = input(f"  {C.BOLD}You:{C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                user_input = "quit"

        # Log user input to run log (Step 6 logs inline above; all other steps log here)
        if current_step != 6 and run_log and user_input and user_input.lower() not in ("save", "quit"):
            run_log.log_user(user_input, step=current_step)

        # Track data source from Step 2 answer
        if current_step == 2 and _data_source is None:
            _ui = user_input.lower()
            if any(w in _ui for w in ("both", "all", "sql and rag", "rag and sql")):
                _data_source = "both"
            elif any(w in _ui for w in ("table", "sql", "database", "db")):
                _data_source = "tables"
            elif any(w in _ui for w in ("doc", "rag", "pdf", "file", "upload")):
                _data_source = "documents"

        if not user_input:
            # If the last assistant message contained "[Press Enter for X]",
            # synthesise the default so the LLM sees a real answer, not blank.
            last_asst = next(
                (h["text"] for h in reversed(history) if h["role"] == "ASSISTANT"), ""
            )
            m = re.search(r"\[Press Enter for ([^\]]+)\]", last_asst, re.IGNORECASE)
            if m:
                user_input = m.group(1).strip()
                print(f"  {display.C.DIM}(using default: {user_input}){display.C.RESET}")
                if run_log:
                    run_log.log(f"(used default: {user_input})")
            else:
                continue

        # Only treat negative words as "accept default" when there's an active [Press Enter for X] default.
        # "none" is intentionally excluded — it's a valid answer to open-ended questions.
        _NEGATIVE = {"no", "n", "nope", "skip", "default"}
        if user_input.lower() in _NEGATIVE:
            last_asst = next(
                (h["text"] for h in reversed(history) if h["role"] == "ASSISTANT"), ""
            )
            m = re.search(r"\[Press Enter for ([^\]]+)\]", last_asst, re.IGNORECASE)
            if m:
                default_val = m.group(1).strip()
                user_input  = default_val
                print(f"  {display.C.DIM}(using default: {user_input}){display.C.RESET}")
                if run_log:
                    run_log.log(f"(used default: {user_input})")

        low_input = user_input.lower()
        if edit_mode and low_input in NO_CHANGE_COMMANDS:
            project["_edit_cancelled"] = True
            display.info("Edit cancelled — no changes will be saved")
            if run_log:
                run_log.log(f"EDIT_CANCELLED at Step {current_step}")
            return project

        if low_input in SAVE_COMMANDS:
            if edit_mode:
                project["_edit_committed"] = True
                display.ok("Edit changes marked for save")
                if run_log:
                    run_log.log(f"EDIT_SAVE_REQUESTED at Step {current_step}")
                return project
            state_module.save_project(cfg, project)
            if low_input == "save":
                display.ok("Progress saved")
                continue
            display.ok("Progress saved — use 'Resume project' to continue")
            return project

        # Capture canonical facts before the LLM sees the turn. These facts are
        # the source of truth for table lists, object names, RAG URL, and Step 7.
        _record_fact_from_user(project, current_step, user_input, history)
        facts = _ensure_facts(project)
        if facts.get("data_source"):
            _data_source = facts.get("data_source")
        _maybe_save(cfg, project, edit_mode)
        if run_log:
            run_log.log_state("facts", facts)

        # Step 7 is application-owned. Once the user answers Step 6, do not
        # call the LLM to decide how to collect agent/task/team fields.
        if current_step == 6:
            history.append({"role": "USER", "text": user_input, "step": current_step})
            project = state_module.add_to_conversation(project, "USER", user_input, current_step)
            current_step = 7
            _print_step_header(7, display)
            _set_workflow(project, current_step=7, last_completed_step=6, edit_mode=edit_mode)
            project = _run_step7_finalize(project, cfg, display, clients=clients, run_log=run_log, edit_mode=edit_mode)
            return project

        # Send to LLM — use reminder history on first resume call, then regular history
        if _pending_history is not None:
            _history      = _pending_history
            _pending_history = None   # clear — only used once
        else:
            _history = history
        # Send a bounded transcript plus a canonical facts block. Critical
        # values are never dependent on chat history alone.
        if len(_history) > 12:
            _history = _history[:2] + _history[-10:]
        _history = _history_with_facts(_history, project)

        # Step 7 is now handled by the application, not the LLM. The full prompt
        # remains only as a fallback if an older project already reaches SPEC.
        active_prompt = full_system_prompt if current_step >= 7 else system_prompt

        sys.stdout.write(f"  {C.DIM}Assistant: (thinking...)  {C.RESET}\r")
        sys.stdout.flush()
        try:
            response = llm_module.chat_turn(
                clients, cfg,
                history       = _history,
                user_message  = user_input,
                system_prompt = active_prompt,
                temperature   = 0.0,
            )
            sys.stdout.write(" " * 40 + "\r")
            sys.stdout.flush()
        except Exception as ex:
            import traceback
            display.err(f"LLM error: {ex}")
            display.err(traceback.format_exc())
            continue

        # Detect step FIRST so new_step is defined before it's used in recording
        new_step = _detect_step(response, current_step)

        # ── Both-mode SQL enforcer ────────────────────────────────────────────
        # If data source is "both" and the LLM just moved past Step 3 without
        # asking the SQL questions, inject them client-side before continuing.
        _SQL_SIGNALS = ["which tables", "what questions", "what kinds of questions"]
        _sql_was_asked = any(
            s in h["text"].lower()
            for h in history
            if h["role"] == "ASSISTANT"
            for s in _SQL_SIGNALS
        )
        # Also check if user already provided table names (comma-separated caps words)
        import re as _re2
        _user_gave_tables = any(
            _re2.search(r'\b[A-Z][A-Z0-9_]{2,},', h["text"])
            for h in history
            if h["role"] == "USER"
        )

        if (_data_source == "both"
                and not _both_sql_asked
                and not _sql_was_asked
                and not _user_gave_tables
                and new_step >= 4
                and current_step == 3):
            # LLM skipped SQL questions — inject them now
            display.warn("(SQL questions not yet asked — injecting now)")
            sql_q = "Which tables will the agent query? (list all table names)"
            history.append({"role": "USER",      "text": user_input, "step": 3})
            history.append({"role": "ASSISTANT", "text": response,   "step": 3})
            history.append({"role": "USER",
                            "text": "Wait — you haven't asked about which database tables "
                                    "the SQL tool will query. We chose 'both' data sources. "
                                    "Please ask the SQL questions before moving on.",
                            "step": 3})
            project = state_module.add_to_conversation(project, "USER",      user_input, 3)
            project = state_module.add_to_conversation(project, "ASSISTANT", response,   3)
            print(f"\n  {display.C.CYAN}Assistant:{display.C.RESET}")
            print(f"  {sql_q}\n")
            history.append({"role": "ASSISTANT", "text": sql_q, "step": 3})
            project = state_module.add_to_conversation(project, "ASSISTANT", sql_q, 3)
            _both_sql_asked = True
            new_step = 3  # stay in Step 3
            # Skip normal response display below
            current_step = new_step
            _set_workflow(project, current_step=current_step, last_completed_step=2, edit_mode=edit_mode)
            _maybe_save(cfg, project, edit_mode)
            continue
        # If we're at Step 5+ and the LLM response contains early-step keywords,
        # it has hallucinated backwards. Inject a correction and re-ask.
        _EARLY_STEP_SIGNALS = [
            "what data sources",
            "database tables / documents / both",
            "what is the subject matter of the documents",
            "which tables will the agent query",
        ]
        if current_step >= 5 and any(s in response.lower() for s in _EARLY_STEP_SIGNALS):
            correction = (
                f"You have already gathered the data source and document information "
                f"in Steps 2 and 3. We are currently at Step {current_step}. "
                f"Please continue from Step {current_step} — do not go back to earlier steps."
            )
            # Add correction to history so LLM sees it, but don't show it to user
            history.append({"role": "USER",      "text": user_input, "step": current_step})
            history.append({"role": "ASSISTANT", "text": response,   "step": current_step})
            history.append({"role": "USER",      "text": correction, "step": current_step})
            project = state_module.add_to_conversation(project, "USER",      user_input, current_step)
            project = state_module.add_to_conversation(project, "ASSISTANT", response,   current_step)
            display.warn(f"(step regression detected — correcting LLM and retrying...)")
            try:
                response = llm_module.chat_turn(
                    clients, cfg,
                    history       = _history_with_facts(history, project),
                    user_message  = correction,
                    system_prompt = active_prompt,
                    temperature   = 0.0,
                )
                new_step = _detect_step(response, current_step)
                history.append({"role": "ASSISTANT", "text": response, "step": new_step})
                project = state_module.add_to_conversation(project, "ASSISTANT", response, new_step)
            except Exception as ex:
                display.err(f"Correction call failed: {ex}")
                continue
            # History already recorded by regression guard — skip below
        else:
            history.append({"role": "USER",      "text": user_input, "step": current_step})
            history.append({"role": "ASSISTANT", "text": response,    "step": new_step})
            project = state_module.add_to_conversation(project, "USER",      user_input, current_step)
            project = state_module.add_to_conversation(project, "ASSISTANT", response,   new_step)
        if run_log:
            run_log.log_assistant(response, step=new_step)

        # If the step advances, split the response so the step header
        # appears between the acknowledgment and the new question.
        display.blank()
        if new_step != current_step:
            ack, question = _split_ack_and_question(response)
            if ack:
                print(f"  {C.CYAN}Assistant:{C.RESET}")
                print(f"  {ack}\n")
            _set_workflow(project, current_step=new_step, last_completed_step=current_step, edit_mode=edit_mode)
            current_step = new_step
            _print_step_header(current_step, display)

            # ── Step 7: client-side task/team finalization ───────────────────
            # This prevents the LLM from renaming objects or changing flow.
            if current_step == 7:
                project = _run_step7_finalize(project, cfg, display, clients=clients, run_log=run_log, edit_mode=edit_mode)
                return project

            # ── Step 4: client-side naming form ──────────────────────────────
            # All object naming is done here — no LLM needed for these questions.
            if current_step == 4:
                project = _run_step4_naming(
                    project, cfg, display, _data_source, history, run_log=run_log
                )
                if project.get("_step4_complete"):
                    # Inject Step 4 answers into history so LLM can build the spec
                    naming = project.get("_naming", {})
                    summary = (
                        f"Step 4 naming confirmed by user: "
                        + ", ".join(f"{k}={v}" for k, v in naming.items())
                    )
                    history.append({"role": "USER", "text": summary, "step": 4})
                    project = state_module.add_to_conversation(
                        project, "USER", summary, 4
                    )
                    _set_workflow(project, current_step=5, last_completed_step=4, edit_mode=edit_mode)
                    _maybe_save(cfg, project, edit_mode)
                    # Lightweight NL2SQL comments setup for SQL projects.
                    _run_comments_readiness(project, cfg, display, clients=clients, run_log=run_log)
                    _maybe_save(cfg, project, edit_mode)

                    # Step 5 is now application-owned so optional tools are deterministic.
                    current_step = 5
                    _print_step_header(5, display)
                    step5_status = _run_step5_analysis_tools(project, display, cfg=cfg, run_log=run_log, edit_mode=edit_mode)
                    if step5_status in ("cancel", "save"):
                        return project
                    step5_summary = (
                        "Step 5 analysis tools confirmed by user: "
                        + json.dumps(_ensure_facts(project).get("analysis_tools", []), sort_keys=True)
                    )
                    history.append({"role": "USER", "text": step5_summary, "step": 5})
                    project = state_module.add_to_conversation(project, "USER", step5_summary, 5)
                    _set_workflow(project, current_step=6, last_completed_step=5, edit_mode=edit_mode)

                    # Advance to Step 6 and ask the role/persona question.
                    current_step = 6
                    _print_step_header(6, display)
                    step6_status = _run_step6_agent_name(project, cfg, display, history, run_log=run_log, edit_mode=edit_mode)
                    if step6_status in ("cancel", "save"):
                        return project
                    step6_question = "Describe the agent role, responsibilities, and guardrails."
                    print(f"  {step6_question}\n")
                    history.append({"role": "ASSISTANT", "text": step6_question, "step": 6})
                    project = state_module.add_to_conversation(project, "ASSISTANT", step6_question, 6)
                    if run_log:
                        run_log.log_assistant(step6_question, step=6)
                    _maybe_save(cfg, project, edit_mode)
                continue
            # ─────────────────────────────────────────────────────────────────

            if question:
                print(f"  {question}\n")
        else:
            print(f"  {C.CYAN}Assistant:{C.RESET}")
            print(f"  {response}\n")

        # Check if spec is complete
        # Normalise LLM output — handle "< SPEC>" with space, missing closing tag, etc.
        normalised = re.sub(r'<\s*SPEC\s*>', '<SPEC>', response, flags=re.IGNORECASE)
        normalised = re.sub(r'<\s*/\s*SPEC\s*>', '</SPEC>', normalised, flags=re.IGNORECASE)
        normalised = re.sub(r'<\s*SPEC_COMPLETE\s*>', 'SPEC_COMPLETE', normalised, flags=re.IGNORECASE)

        # If SPEC_COMPLETE appears but </SPEC> is missing, insert it before SPEC_COMPLETE
        if "SPEC_COMPLETE" in normalised and "<SPEC>" in normalised and "</SPEC>" not in normalised:
            normalised = normalised.replace("SPEC_COMPLETE", "</SPEC>\nSPEC_COMPLETE")

        if "SPEC_COMPLETE" in normalised:
            display.ok("Specification complete — extracting spec...")
            try:
                match = re.search(r"<SPEC>(.*?)</SPEC>", normalised, re.DOTALL)
                if match:
                    spec_json = json.loads(match.group(1).strip())
                    spec_json = _canonicalize_spec(spec_json, project)
                    _validate_spec_against_facts(spec_json, project)
                    project["spec"] = spec_json
                    _set_workflow(project, current_step=7, last_completed_step=7, edit_mode=edit_mode)
                    project = state_module.update_phase(project, "review")
                    _maybe_save(cfg, project, edit_mode)
                    display.ok("Specification complete")
                    _print_spec_summary(spec_json, display)
                    display.blank()
                    print(f"  Review the summary above, and choose:")
                    print(f"   1. Generate the code")
                    print(f"   2. Edit (go back to conversation)")
                    print(f"   3. Quit and save")
                    display.blank()
                    try:
                        ans = input("  Select [1/2/3]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        ans = "3"
                    if ans in ("2", "fix", "edit"):
                        display.info("Going back to conversation — tell the assistant what to correct.")
                        project = state_module.update_phase(project, "discovery")
                        _maybe_save(cfg, project, edit_mode)
                        return _edit_then_regenerate(project, cfg, clients, display, run_log=run_log)
                    if ans == "3":
                        _maybe_save(cfg, project, edit_mode)
                        display.ok("Progress saved — use 'Resume project' to continue")
                        return project
                    # "1" or Enter → generate code
                    return project
                else:
                    display.warn("SPEC_COMPLETE detected but no <SPEC> tags found")
                    display.info("Ask the assistant to output the spec again")
            except json.JSONDecodeError as ex:
                display.err(f"Could not parse spec JSON: {ex}")
                display.info("Ask the assistant to reformat the spec")

    return project


def _apply_naming_to_spec(spec: dict, naming: dict):
    """
    Override LLM-generated names in the spec with user-confirmed names from Step 4.
    Renames all references consistently across profiles, vector_indexes, tools, agents.
    """
    renames = {}  # old_name -> new_name for cross-reference updates

    # RAG profile
    if "rag_profile" in naming:
        new_name = naming["rag_profile"]
        for p in spec.get("profiles", []):
            if p.get("type", "").upper() == "RAG":
                renames[p.get("name", "")] = new_name
                p["name"] = new_name
                break

    # NL2SQL profile
    if "nl2sql_profile" in naming:
        new_name = naming["nl2sql_profile"]
        for p in spec.get("profiles", []):
            if p.get("type", "").upper() == "NL2SQL":
                renames[p.get("name", "")] = new_name
                p["name"] = new_name
                break

    # Vector index
    if "vector_index" in naming:
        new_name = naming["vector_index"]
        for vi in spec.get("vector_indexes", []):
            renames[vi.get("name", "")] = new_name
            vi["name"] = new_name
            break
        # Update profile's vector_index_name reference
        for p in spec.get("profiles", []):
            if p.get("type", "").upper() == "RAG":
                p["vector_index_name"] = new_name

    # Chunk size / overlap
    if "chunk_size" in naming:
        for vi in spec.get("vector_indexes", []):
            vi["chunk_size"] = naming["chunk_size"]
    if "chunk_overlap" in naming:
        for vi in spec.get("vector_indexes", []):
            vi["chunk_overlap"] = naming["chunk_overlap"]

    # RAG tool
    if "rag_tool" in naming:
        new_name = naming["rag_tool"]
        for t in spec.get("tools", []):
            if t.get("type", "").upper() == "RAG":
                renames[t.get("name", "")] = new_name
                t["name"] = new_name
                break

    # SQL tool
    if "sql_tool" in naming:
        new_name = naming["sql_tool"]
        for t in spec.get("tools", []):
            if t.get("type", "").upper() == "SQL":
                renames[t.get("name", "")] = new_name
                t["name"] = new_name
                break

    # Update cross-references: profile_name in tools, tools list in agents
    for t in spec.get("tools", []):
        old_ref = t.get("profile_name", "")
        if old_ref in renames:
            t["profile_name"] = renames[old_ref]

    for a in spec.get("agents", []):
        a["tools"] = [renames.get(tn, tn) for tn in a.get("tools", [])]

    # Update profile_name in vector indexes
    for vi in spec.get("vector_indexes", []):
        old_ref = vi.get("profile_name", "")
        if old_ref in renames:
            vi["profile_name"] = renames[old_ref]



def _prompt_edit_start_step(project: dict, display, run_log=None) -> int | None:
    """Ask which step to edit using explicit workflow state, not message inference."""
    reached_step = _get_last_completed_step(project)
    display.blank()
    print(f"  You last completed Step {reached_step} of {len(STEPS)}.")
    display.blank()
    try:
        raw = input(
            f"  Press Enter to continue from Step {reached_step}, "
            f"enter a step number to go back [1-{reached_step}], "
            f"or q to return to menu without changes: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if raw in ("q", "quit", "cancel", "discard", "quit no change", "q!"):
        display.info("Returning to menu — no changes made")
        if run_log:
            run_log.log("EDIT_NOT_STARTED: user returned to menu")
        return None

    if not raw:
        go_to = reached_step
    elif raw.isdigit():
        go_to = int(raw)
    else:
        display.warn("Invalid input — returning without changes")
        return None

    if not (1 <= go_to <= reached_step):
        display.warn(f"Invalid step — must be between 1 and {reached_step}")
        return None

    if run_log:
        run_log.log(f"EDIT_START_STEP_SELECTED: {go_to}")
    return go_to


def _run_edit_session(project: dict, cfg, clients: dict, display,
                      run_log=None, start_step: int = None) -> tuple[dict, bool]:
    """Run transactional edit mode on a working copy and commit only on change."""
    if start_step is None:
        return project, False

    original = copy.deepcopy(project)
    original_snapshot = _semantic_snapshot(original)
    working = copy.deepcopy(project)
    working.pop("_edit_cancelled", None)
    working.pop("_edit_committed", None)
    _set_workflow(working, current_step=start_step, edit_mode=True)

    # Trim only the working copy for cleaner logs/context. The saved project is untouched.
    _trim_conversation_for_step(working, start_step)
    working = state_module.update_phase(working, "discovery")

    display.ok(f"Edit session started from Step {start_step} (working copy only)")
    if run_log:
        run_log.log(f"EDIT_SESSION_STARTED: start_step={start_step}")

    updated = _discovery_loop(
        working, cfg, clients, display,
        show_roadmap=False,
        run_log=run_log,
        start_step=start_step,
        edit_mode=True,
    )

    if updated.get("_edit_cancelled"):
        display.info("No changes saved — original project left unchanged")
        if run_log:
            run_log.log("EDIT_SESSION_DISCARDED")
        return original, False

    # Remove edit-only markers before comparing and saving.
    updated.pop("_edit_cancelled", None)
    updated.pop("_edit_committed", None)
    _set_workflow(updated, edit_mode=False)

    changed = _semantic_snapshot(updated) != original_snapshot
    if not changed:
        display.info("No changes detected — original project left unchanged")
        if run_log:
            run_log.log("EDIT_SESSION_NO_CHANGES")
        return original, False

    state_module.save_project(cfg, updated)
    display.ok("Edit changes saved")
    if run_log:
        run_log.log("EDIT_SESSION_COMMITTED")
    return updated, True


def _edit_then_regenerate(project: dict, cfg, clients: dict, display, run_log=None) -> dict:
    """Prompt for an edit start step, run transactional edit, then regenerate when complete."""
    go_to = _prompt_edit_start_step(project, display, run_log=run_log)
    if go_to is None:
        return project
    project, changed = _run_edit_session(project, cfg, clients, display, run_log=run_log, start_step=go_to)
    if changed and project.get("phase") == "review" and project.get("spec"):
        project = _generate_code(project, cfg, clients, display, run_log=run_log)
        project = _review_and_execute(project, cfg, clients, display, run_log=run_log)
    return project


# ─────────────────────────────────────────────────────────────────────────────
# OML password resolution — custom tool bodies (raw-pasted PL/SQL) may embed
# a placeholder like '<SET ACME_CORP PASSWORD HERE>' for a hardcoded OML
# token-refresh password (Oracle doesn't support retrieving a stored
# DBMS_CLOUD credential's password back out via SQL — see the ACME_CORP
# custom Python tool for the reference pattern this placeholder comes from).
# Rather than requiring a manual edit before every build, resolve it here:
# check config first (including this session's cache), prompt (masked) if
# not found. Never written to disk — see _resolve_oml_password docstring.
# ─────────────────────────────────────────────────────────────────────────────

_OML_PASSWORD_PLACEHOLDER_PAT = re.compile(r"<SET\s+\S+\s+PASSWORD\s+HERE>")


def _resolve_oml_password(spec_dict: dict, cfg, display) -> str:
    """
    If any raw-pasted custom tool body contains the OML password placeholder,
    resolve a real value for it: config first, then a masked interactive
    prompt.
    Returns "" if no placeholder is present (no-op) or the person declines
    to enter one (placeholder is left as-is, unchanged, for manual editing).

    Never written to disk (not even runtime.ini) — a live DB password is
    the last thing that should survive silently past the session it was
    entered in. Cached in-memory on `cfg` only, so re-generating the same
    project later in the same run doesn't re-prompt; a fresh process
    always prompts again.
    """
    tools = spec_dict.get("tools", [])
    needs_password = any(
        _OML_PASSWORD_PLACEHOLDER_PAT.search(t.get("raw_plsql") or "")
        for t in tools
    )
    if not needs_password:
        return ""

    existing = cfg_module.get(cfg, "de", "oml_password", fallback="")
    if existing:
        display.info("Using OML password entered earlier this session.")
        return existing

    display.blank()
    display.info("A custom tool in this project needs the OML token-refresh "
                  "password (Oracle doesn't support retrieving a stored "
                  "DBMS_CLOUD credential's password back out via SQL, so "
                  "it has to be supplied directly).")
    try:
        pwd = getpass.getpass("  Enter the OML/DB password (input hidden, "
                               "not saved — asked again next run, blank to "
                               "skip and edit manually later): ")
    except (EOFError, KeyboardInterrupt):
        display.blank()
        return ""

    if not pwd:
        display.warn("No password entered — placeholder left in generated "
                      "SQL. Edit it manually before executing.")
        return ""

    # In-memory only, for the rest of this run — never written to disk.
    if not cfg.has_section("de"):
        cfg.add_section("de")
    cfg.set("de", "oml_password", pwd)

    return pwd


def _apply_oml_password(spec_dict: dict, pwd: str) -> None:
    """Substitute the OML password placeholder into every raw-pasted tool
    body that contains it. Doubles embedded single quotes so the value
    stays a valid SQL string literal."""
    if not pwd:
        return
    escaped = pwd.replace("'", "''")
    for t in spec_dict.get("tools", []):
        body = t.get("raw_plsql")
        if body and _OML_PASSWORD_PLACEHOLDER_PAT.search(body):
            t["raw_plsql"] = _OML_PASSWORD_PLACEHOLDER_PAT.sub(escaped, body)


def _generate_code(project: dict, cfg, clients: dict, display, run_log=None) -> dict:
    """Phase 2 — generate PL/SQL from the spec."""
    display.head("PHASE 2 — CODE GENERATION")

    spec_dict = project.get("spec", {})
    if not spec_dict:
        display.err("No spec found — run the discovery conversation first")
        return project

    # ── Primary: deterministic sql_builder (no LLM, no markdown risk) ────────
    sql_script = None
    try:
        from core.sql_builder import build_full_sql

        # Canonicalize and validate immediately before SQL generation. This
        # protects against edited/legacy specs with renamed objects or dropped tables.
        spec_dict = _canonicalize_spec(spec_dict, project)
        try:
            _validate_spec_against_facts(spec_dict, project)
        except Exception as validation_err:
            display.err(f"Spec validation failed: {validation_err}")
            display.info("Fix the discovery answers before generating SQL. LLM fallback was skipped to avoid regenerating incorrect names.")
            return project
        project["spec"] = spec_dict
        state_module.save_project(cfg, project)

        # Resolve any OML password placeholder on a working copy — the
        # saved spec keeps the placeholder (so future regenerations still
        # prompt/reuse cleanly); only the generated SQL gets the real value.
        gen_spec_dict = copy.deepcopy(spec_dict)
        oml_pwd = _resolve_oml_password(gen_spec_dict, cfg, display)
        _apply_oml_password(gen_spec_dict, oml_pwd)

        sql_script = build_full_sql(gen_spec_dict, cfg)
        display.ok("PL/SQL generated via sql_builder (deterministic)")
    except Exception as primary_err:
        display.warn(f"sql_builder unavailable ({primary_err}) — falling back to LLM codegen")

    # ── Fallback: LLM codegen ─────────────────────────────────────────────────
    if not sql_script:
        template = _load_template("codegen_prompt.txt")
        if not template:
            display.err("Code generation template not found")
            return project

        spec_json   = json.dumps(spec_dict, indent=2)
        prompt      = template.replace("{{SPEC}}", spec_json)
        compartment = cfg_module.get(cfg, "compartment", "compartment_ocid")
        schema      = project.get("schema", cfg_module.get(cfg, "database", "db_user"))

        prompt += f"\n\nAdditional context:\n"
        prompt += f"- Schema: {schema}\n"
        prompt += f"- Compartment OCID: {compartment}\n"
        prompt += f"- Chat model: {cfg_module.get(cfg, 'llm', 'chat_model')}\n"
        prompt += f"- Embed model: {cfg_module.get(cfg, 'llm', 'embed_model')}\n"
        prompt += f"- Region: {cfg_module.get(cfg, 'oci', 'region')}\n"

        display.info("Generating PL/SQL script from spec via LLM...")
        try:
            sql_script = llm_module.generate(
                clients, cfg, prompt,
                temperature = 0.1,
                max_tokens  = 8000,
            )
        except Exception as ex:
            display.err(f"Code generation failed: {ex}")
            return project

        # Strip markdown fences the LLM may have added
        sql_script = re.sub(r"```(?:sql|plsql)?\s*", "", sql_script)
        sql_script = re.sub(r"```", "", sql_script).strip()

    project["generated_sql"] = sql_script
    project = state_module.update_phase(project, "review")
    state_module.save_project(cfg, project)

    # Save SQL snapshot to project's sql/ folder
    snap_path = state_module.save_sql_snapshot(cfg, project, sql_script)
    if run_log:
        run_log.log_section("CODE GENERATION")
        run_log.log(f"SQL snapshot saved: {snap_path.name}")

    display.ok("PL/SQL script generated")
    display.blank()

    print(f"  {'─' * 64}")
    for line in sql_script.splitlines():
        print(f"  {line}")
    print(f"  {'─' * 64}")
    display.blank()

    return project


def _review_and_execute(project: dict, cfg, clients: dict, display, run_log=None) -> dict:
    """Phase 3 — review generated SQL and optionally execute."""
    display.head("PHASE 3 — REVIEW & EXECUTE")
    C = display.C

    sql_script = project.get("generated_sql", "")
    if not sql_script:
        display.warn("No generated SQL found — run code generation first")
        return project

    line_count = len(sql_script.splitlines())
    display.blank()
    print(f"  Generated script has {line_count} lines. "
          f"Review it above before executing, then:")
    display.blank()
    print(f"   1. Execute")
    print(f"   2. Regenerate")
    print(f"   3. Edit (go back to conversation)")
    print(f"   4. Quit and save")
    display.blank()
    try:
        answer = input("  Select [1/2/3/4]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return project

    # Map old shortcuts to new numbers
    answer = {"e": "1", "s": "1", "r": "2", "c": "3", "q": "4"}.get(answer, answer)

    if answer == "3":
        return _edit_then_regenerate(project, cfg, clients, display, run_log=run_log)

    if answer == "2":
        project = _generate_code(project, cfg, clients, display)
        project = _review_and_execute(project, cfg, clients, display)
        return project

    if answer == "4":
        state_module.save_project(cfg, project)
        display.ok("Progress saved — use 'Resume project' to continue")
        return project

    if answer == "1":
        display.info("Connecting to ADW...")
        try:
            conn = db_module.connect(cfg)
        except Exception as ex:
            if run_log:
                run_log.log_exception("DB connect", ex)
            display.err(f"Connection failed: {ex}")
            return project

        statements = db_module.parse_sql_content(sql_script)
        display.info(f"Executing {len(statements)} statements...")
        display.blank()
        if run_log:
            run_log.log_section("SQL EXECUTION")
            run_log.log(f"Executing {len(statements)} statements")

        results = db_module.execute_many(
            conn, statements, ignore_errors=False, label=project["project_name"]
        )
        conn.close()

        # Log each statement result
        if run_log:
            for i, stmt in enumerate(statements, 1):
                error_entry = next(
                    (e for e in results.get("errors", []) if e.get("stmt") == i), None
                )
                success = error_entry is None
                error_msg = error_entry["error"] if error_entry else None
                run_log.log_sql(i, stmt, success, error=error_msg)

        display.blank()
        display.ok(f"Succeeded: {results['success']}")
        if results["skipped"]:
            display.warn(f"Skipped (already existed): {results['skipped']}")
        if results["failed"]:
            display.err(f"Failed: {results['failed']}")
            for e in results["errors"]:
                display.err(f"  Statement {e['stmt']}: {e['error'][:100]}")

        project["build_log"].append({
            "timestamp": state_module.datetime.now().isoformat()
            if hasattr(state_module, "datetime") else "",
            "results":   results,
        })

        if results["failed"] == 0:
            project = state_module.update_phase(project, "complete")
            display.ok("Agent stack built successfully")

            # Post-execution sanity check for custom function tools.
            # The executor counts /‑terminated block completions — but a block
            # can complete without error even if its inner body silently failed
            # (e.g. pyqScriptCreate inside a BEGIN...END that has its own
            # EXCEPTION handler). Check the actual database objects exist.
            custom_tools = [
                t for t in project.get("facts", {}).get("analysis_tools", [])
                if t.get("raw_plsql") or t.get("python_script")
            ]
            if custom_tools:
                conn = None
                try:
                    conn = db_module.connect(cfg)
                    missing_fns = []
                    missing_scripts = []
                    invalid_fns = []
                    for ct in custom_tools:
                        fn = ct.get("function_name", "").upper()
                        if fn:
                            row = db_module.query_one(conn,
                                "SELECT status FROM user_objects "
                                "WHERE object_name = :fn AND object_type IN ('FUNCTION','PROCEDURE','PACKAGE')",
                                {"fn": fn})
                            if not row:
                                missing_fns.append(fn)
                            elif row.get("status") != "VALID":
                                err_rows = db_module.query_all(conn,
                                    "SELECT line, position, text FROM user_errors "
                                    "WHERE name = :fn ORDER BY sequence",
                                    {"fn": fn})
                                detail = "; ".join(
                                    f"L{e['line']}: {e['text'].strip()}" for e in err_rows
                                ) or "no USER_ERRORS rows found"
                                invalid_fns.append(f"{fn} ({detail})")
                        script = (ct.get("pyqscript_name") or "").lower()
                        if script:
                            try:
                                row = db_module.query_one(conn,
                                    "SELECT COUNT(*) AS n FROM user_pyq_scripts "
                                    "WHERE name = :s",
                                    {"s": script})
                            except Exception:
                                # Column name varies by ADW version — try 'script_name'
                                try:
                                    row = db_module.query_one(conn,
                                        "SELECT COUNT(*) AS n FROM user_pyq_scripts "
                                        "WHERE script_name = :s",
                                        {"s": script})
                                except Exception:
                                    row = None
                            if not row or not row.get("n"):
                                missing_scripts.append(script)
                    if missing_fns:
                        display.warn(f"Custom function(s) not found in schema after build — "
                                     f"may have failed silently inside their PL/SQL block: "
                                     + ", ".join(missing_fns))
                        display.warn("Run the CREATE OR REPLACE FUNCTION block manually in "
                                     "SQL Developer to see the real compilation error.")
                    if invalid_fns:
                        display.err("Custom function(s) exist but failed to compile "
                                    "(object created, but INVALID):")
                        for detail in invalid_fns:
                            display.err(f"  {detail}")
                    if missing_scripts:
                        display.warn(f"OML4Py script(s) not registered after build: "
                                     + ", ".join(missing_scripts))
                    if not missing_fns and not invalid_fns and not missing_scripts and custom_tools:
                        display.ok(f"Custom function object(s) confirmed in schema ✓")
                except Exception:
                    pass  # verification is best-effort; don't block on it
                finally:
                    if conn:
                        try: conn.close()
                        except Exception: pass

        state_module.save_project(cfg, project)

    return project


def run_new(cfg, clients: dict, display):
    """Start a new agent builder project — conversational or CSV import."""
    display.head("NEW AGENT PROJECT")
    C = display.C
    display.blank()
    print(f"  {C.BOLD}How would you like to create this project?{C.RESET}")
    display.blank()
    print(f"   1. Conversational builder  — step-by-step guided conversation")
    print(f"   2. Import from CSV         — load project details from a pipe-delimited file")
    print(f"   3. Import from Word doc    — load project details from a .docx config document")
    print(f"   q. Cancel")
    display.blank()
    try:
        mode = input("  Choice [1/2/3/q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        mode = "q"

    if mode in ("q", "quit", "exit", "b", ""):
        display.warn("Cancelled")
        return

    if mode == "2":
        from modules.project_import import run_import
        project = run_import(cfg, clients, display)
        if not project:
            return
        run_log = project.pop("_run_log", None)
        project = _discovery_loop(project, cfg, clients, display,
                                  display_name=project.get("display_name"),
                                  run_log=run_log)
        if project.get("phase") == "review" and project.get("spec"):
            project = _generate_code(project, cfg, clients, display, run_log=run_log)
            project = _review_and_execute(project, cfg, clients, display, run_log=run_log)
        return

    if mode == "3":
        from modules.docx_import import run_import_docx
        project = run_import_docx(cfg, clients, display)
        if not project:
            return
        run_log = project.pop("_run_log", None)
        # Determine start step from workflow — option 1 (Proceed) sets
        # current_step=7; option 2 (Edit first) sets current_step=2.
        # Pass it explicitly so _discovery_loop doesn't re-seed from Step 2.
        start_step = project.get("workflow", {}).get("current_step", 7)
        project = _discovery_loop(project, cfg, clients, display,
                                  display_name=project.get("display_name"),
                                  run_log=run_log, start_step=start_step)
        if project.get("phase") == "review" and project.get("spec"):
            project = _generate_code(project, cfg, clients, display, run_log=run_log)
            project = _review_and_execute(project, cfg, clients, display, run_log=run_log)
        return

    # ── Option 1: Conversational builder (original run_new logic) ─────────────
    schema = cfg_module.get(cfg, "database", "db_user", fallback="YOUR_SCHEMA")

    display.blank()
    print(f"  {display.C.DIM}Give your project a name — this is used as the display name")
    print(f"  and to generate the project file. The agent DB object name is set in Step 6.{display.C.RESET}")
    display.blank()

    _print_roadmap(display)
    display.blank()

    try:
        display_name = input(
            f'  Project name (e.g. "ACME Insurance Assistant", "Sales Forecast Bot"): '
        ).strip()
        if not display_name or display_name.lower() in ("q", "quit", "exit"):
            display.warn("Cancelled")
            return

        schema_override = input(f"  Schema [{schema}]: ").strip()
        if schema_override.lower() in ("q", "quit", "exit"):
            display.warn("Cancelled")
            return
        if schema_override:
            schema = schema_override

    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    # Derive file slug from display name — lowercase, spaces to hyphens
    import re as _re
    file_slug = _re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")

    # Read RAG URL from runtime config if already set by option 4 upload
    rag_url = cfg_module.get(cfg, "object_storage", "rag_location_url", fallback="")

    display.blank()
    display.info(f"Project    : {display_name}")
    display.info(f"Schema     : {schema}")
    if rag_url:
        display.info(f"RAG URL    : {rag_url}")
    else:
        display.warn("RAG URL    : not set — run option 4 (Object Storage) first if using RAG")
    display.info(f"Saved as   : {file_slug}.json")

    # ── Conflict check — project file already exists ──────────────────────────
    existing_path = state_module.projects_dir(cfg) / f"{file_slug}.json"
    if existing_path.exists():
        try:
            existing = state_module.load_project(existing_path)
        except Exception:
            existing = {}
        display.blank()
        display.warn(f"A project named '{file_slug}' already exists")
        display.info(f"Phase    : {existing.get('phase', 'unknown')}")
        display.info(f"Modified : {existing.get('modified_at', '')[:19]}")
        display.blank()
        print(f"  [1]  Resume the existing project")
        print(f"  [2]  Overwrite it (existing conversation will be lost)")
        print(f"  [3]  Choose a different name")
        display.blank()
        try:
            conflict = input("  Choice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return
        if conflict == "1":
            display.ok("Resuming existing project...")
            project = existing
            project = _discovery_loop(project, cfg, clients, display)
            if project.get("phase") == "review" and project.get("spec"):
                project = _generate_code(project, cfg, clients, display)
                project = _review_and_execute(project, cfg, clients, display)
            return
        elif conflict == "2":
            display.warn("Overwriting existing project...")
        elif conflict == "3":
            display.warn("Please restart and choose a different name")
            return
        else:
            display.warn("Cancelled")
            return

    display.blank()
    project = state_module.new_project(file_slug, schema)
    project["display_name"] = display_name
    run_log = state_module.open_run_log(cfg, project)
    run_log.log_section("NEW PROJECT")
    run_log.log(f"Project: {display_name}  Schema: {schema}")
    display.info(f"Run log: {run_log.run_path}")
    if run_log.debug_enabled:
        display.info(f"Debug log: {run_log.debug_path}")

    project = _discovery_loop(project, cfg, clients, display,
                              display_name=display_name, rag_url=rag_url,
                              run_log=run_log)

    if project.get("phase") == "review" and project.get("spec"):
        project = _generate_code(project, cfg, clients, display, run_log=run_log)
        project = _review_and_execute(project, cfg, clients, display, run_log=run_log)


def run_resume(cfg, clients: dict, display):
    """Resume an existing agent builder project."""
    display.head("RESUME PROJECT")

    project = _pick_project(cfg, display)
    if not project:
        return

    display.ok(f"Loaded project: {project['project_name']}  (phase: {project['phase']})")

    # New session timestamp so resume gets its own log files
    project = state_module.refresh_session_ts(project)
    run_log = state_module.open_run_log(cfg, project)
    run_log.log_section("RESUME")
    run_log.log(f"Resumed from phase: {project['phase']}")
    display.info(f"Run log: {run_log.run_path}")
    if run_log.debug_enabled:
        display.info(f"Debug log: {run_log.debug_path}")

    if project["phase"] in ("discovery", "unknown"):
        # If spec exists in discovery phase, show same menu as review phase
        if project.get("spec"):
            has_sql = bool(project.get("generated_sql"))
            display.blank()
            if has_sql:
                print(f"  {display.C.BOLD}This project has a generated script ready.{display.C.RESET}")
            else:
                print(f"  {display.C.BOLD}This project has a saved spec (no SQL generated yet).{display.C.RESET}")
            display.blank()
            print(f"   1. Go to Review & Execute")
            print(f"   2. Edit (go back to conversation)")
            print(f"   3. Quit and save")
            display.blank()
            try:
                shortcut = input("  Select [1/2/3]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return
            shortcut = {"e": "1", "c": "2", "q": "3"}.get(shortcut, shortcut)
            if shortcut == "3":
                state_module.save_project(cfg, project)
                display.ok("Progress saved")
                return
            if shortcut == "2":
                project = _edit_then_regenerate(project, cfg, clients, display, run_log=run_log)
                return
            # Option 1 → generate code then review & execute
            project = _generate_code(project, cfg, clients, display, run_log=run_log)
            project = _review_and_execute(project, cfg, clients, display, run_log=run_log)
            return

        # ── Infer which step they reached from conversation history ──────────
        history = project.get("conversation", [])
        last_assistant = next(
            (h["text"] for h in reversed(history) if h["role"] == "ASSISTANT"), None
        )
        # Scan full history — last message may be a completion, not a step keyword
        reached_step = 1
        for h in history:
            if h["role"] == "ASSISTANT":
                reached_step = _detect_step(h["text"], reached_step)

        display.blank()
        _print_roadmap(display)
        display.blank()

        if reached_step > 1:
            print(f"  {display.C.BOLD}You reached Step {reached_step} of {len(STEPS)}{display.C.RESET}  "
                  f"— {STEPS[reached_step-1][1]}")
            display.blank()
            print(f"  [c]  Continue from Step {reached_step}")
            for i in range(1, reached_step):
                _, label, _, _ = STEPS[i-1]
                print(f"  [{i}]  Go back to Step {i}  — {label}")
            display.blank()
            try:
                choice = input(
                    f"  Continue or go back? [c / 1-{reached_step-1}]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return

            if choice in ("c", "continue", ""):
                pass   # resume normally from last position
            else:
                try:
                    go_to = int(choice)
                    if 1 <= go_to < reached_step:
                        # Trim conversation history back to the point before
                        # the chosen step — keep only turns from before it
                        # Trim using stored step numbers where available,
                        # fall back to keyword detection otherwise.
                        trimmed = []
                        for turn in history:
                            stored_step = turn.get("step")
                            if stored_step is not None:
                                # Reliable: stored when the turn was recorded
                                if stored_step >= go_to:
                                    break
                            else:
                                # Legacy turns without stored step — use detection
                                if turn["role"] == "ASSISTANT":
                                    detected = _detect_step(turn["text"], 1)
                                    if detected >= go_to:
                                        break
                            trimmed.append(turn)
                        # Drop any trailing USER turn at the boundary
                        while trimmed and trimmed[-1]["role"] == "USER":
                            trimmed.pop()
                        project["conversation"] = trimmed
                        state_module.save_project(cfg, project)
                        display.ok(f"Rolled back to before Step {go_to} — conversation trimmed")
                    else:
                        display.warn(f"Invalid step — continuing from Step {reached_step}")
                except ValueError:
                    display.warn("Invalid input — continuing from current step")
        else:
            display.blank()
            display.info("Starting from the beginning...")

        project = _discovery_loop(project, cfg, clients, display, show_roadmap=False, run_log=run_log)
        if project.get("phase") == "review" and project.get("spec"):
            project = _generate_code(project, cfg, clients, display, run_log=run_log)
            project = _review_and_execute(project, cfg, clients, display, run_log=run_log)

    elif project["phase"] == "review":
        display.blank()
        print(f"  {display.C.BOLD}This project has a generated script ready.{display.C.RESET}")
        display.blank()
        print(f"   1. Go to Review & Execute")
        print(f"   2. Edit (go back to conversation)")
        print(f"   3. Quit and save")
        display.blank()
        try:
            choice = input("  Select [1/2/3]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        choice = {"e": "1", "c": "2", "q": "3"}.get(choice, choice)
        if choice == "3":
            state_module.save_project(cfg, project)
            display.ok("Progress saved")
            return
        if choice == "2":
            project = _edit_then_regenerate(project, cfg, clients, display, run_log=run_log)
            return
        # "1" or anything else → regenerate then review & execute
        project = _generate_code(project, cfg, clients, display, run_log=run_log)
        project = _review_and_execute(project, cfg, clients, display, run_log=run_log)

    elif project["phase"] == "complete":
        display.ok("This project is already complete")
        display.blank()
        print(f"  [r]  Regenerate and re-execute")
        print(f"  [c]  Go back to conversation to make changes")
        display.blank()
        try:
            answer = input("  Choice [r/c/Enter to cancel]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if answer == "c":
            project = _edit_then_regenerate(project, cfg, clients, display, run_log=run_log)
        elif answer == "r":
            project = _generate_code(project, cfg, clients, display, run_log=run_log)
            project = _review_and_execute(project, cfg, clients, display, run_log=run_log)
