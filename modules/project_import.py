"""
modules/project_import.py
Import a Select AI Agent Builder project from a pipe-delimited CSV file.

The CSV has exactly two columns (Key | Value) and maps to the project dict
that run_new normally builds through the conversational builder.

Called from run_new() when the user chooses option 2 (Import from CSV).
After import the project goes straight to _discovery_loop at Step 7 for
final confirmation and spec generation — the user can still review and
edit before any SQL is generated.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Optional

from core import state as state_module
from core import config as cfg_module


# ── Key aliases ───────────────────────────────────────────────────────────────
# Normalise messy or variant key names from the CSV to canonical internal keys.
# Keys are normalised to lowercase-no-spaces before lookup, so aliases should
# be lowercase-no-spaces as well.

_KEY_ALIASES: dict[str, str] = {
    # Project / schema
    "project":                          "project",
    "projectname":                      "project",
    "schema":                           "schema",
    "databaseschema":                   "schema",

    # Data source
    "datasources":                      "data_source",
    "datasource":                       "data_source",

    # RAG
    "ragurl":                           "rag_url",
    "rag-objectstorageurl":             "rag_url",
    "rag_objectstorageurl":             "rag_url",
    "ragobjectstorageurl":              "rag_url",
    "rag-subjectmatter":                "rag_subject",
    "rag_subjectmatter":                "rag_subject",
    "ragsubjectmatter":                 "rag_subject",
    "ragsubject":                       "rag_subject",
    "rag-whatfiletypesareintheknowledgebase?": "rag_file_types",
    "rag_whatfiletypesareintheknowledgebase?": "rag_file_types",
    "ragfiletypes":                     "rag_file_types",
    "ragfiletypesknowledgebase":        "rag_file_types",

    # SQL
    "sql-whichtables":                  "sql_tables",
    "sql_whichtables":                  "sql_tables",
    "sqlwhichtables":                   "sql_tables",
    "sqltables":                        "sql_tables",
    "sql-whatquestionsuserswillask":    "sql_question_types",
    "sql_whatquestionsuserswillask":    "sql_question_types",
    "sqlwhatquestionsuserswillask":     "sql_question_types",
    "sqlquestiontypes":                 "sql_question_types",
    "sqlquestions":                     "sql_question_types",

    # Object names
    "ragprofileprofile":                "rag_profile",
    "ragprofile":                       "rag_profile",
    "rag_profile":                      "rag_profile",
    "vectorindex":                      "vector_index",
    "vector_index":                     "vector_index",
    "chunksize":                        "chunk_size",
    "chunk_size":                       "chunk_size",
    "chunkoverlap":                     "chunk_overlap",
    "chunk_overlap":                    "chunk_overlap",
    "ragtool":                          "rag_tool",
    "rag_tool":                         "rag_tool",
    "nl2sqlprofile":                    "nl2sql_profile",
    "nl2sql_profile":                   "nl2sql_profile",
    "sqltool":                          "sql_tool",
    "sql_tool":                         "sql_tool",

    # Comments / analysis tools
    "nl2sqlmetadatacomments":           "nl2sql_comments",
    "nl2sql_metadatacomments":          "nl2sql_comments",
    "nl2sqlcomments":                   "nl2sql_comments",
    "additionalanalysistools":          "analysis_tools",
    "additional_analysistools":         "analysis_tools",
    "analysistools":                    "analysis_tools",

    # Agent / task / team
    "agentname":                        "agent_name",
    "agent_name":                       "agent_name",
    "agentinstructions":                "agent_instructions",
    "agent_instructions":               "agent_instructions",
    "agentinstruction":                 "agent_instructions",
    "agentpersona":                     "agent_instructions",
    "agentrole":                        "agent_instructions",
    "taskinstruction":                  "task_instruction",
    "task_instruction":                 "task_instruction",
    "taskname":                         "task_name",
    "task_name":                        "task_name",
    "teamname":                         "team_name",
    "team_name":                        "team_name",
}

# Canonical keys that map to task_instruction — first wins unless overridden
_TASK_INSTRUCTION_KEYS = {"task_instruction"}
_AGENT_INSTRUCTION_KEYS = {"agent_instructions"}


# ── Normalise key ─────────────────────────────────────────────────────────────

def _norm_key(raw: str) -> str:
    """Lowercase, strip whitespace, collapse punctuation."""
    return re.sub(r"[\s\-_/\\]+", "", raw.strip().lower())


# ── Parse CSV ─────────────────────────────────────────────────────────────────

def parse_csv(path: str | Path) -> tuple[dict[str, str], list[str]]:
    """Parse a pipe-delimited Key|Value CSV.

    Returns:
        (data, warnings)
        data     — {canonical_key: raw_value_string}
        warnings — list of human-readable issues found during parsing
    """
    path = Path(path).expanduser().resolve()
    data: dict[str, str] = {}
    warnings: list[str] = []
    seen_raw: dict[str, list[str]] = {}   # raw_key → [values] for dup detection

    with open(path, newline="", encoding="utf-8-sig") as fh:
        # Auto-detect delimiter: try | first then fall back to csv.Sniffer
        sample = fh.read(4096)
        fh.seek(0)
        if "|" in sample:
            delimiter = "|"
        else:
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",\t|;").delimiter
            except csv.Error:
                delimiter = ","

        reader = csv.reader(fh, delimiter=delimiter)
        header_skipped = False

        for lineno, row in enumerate(reader, 1):
            # Skip blank rows
            if not any(c.strip() for c in row):
                continue

            # Skip header row if it looks like "Key | Value"
            if not header_skipped:
                if len(row) >= 2:
                    k0 = row[0].strip().lower()
                    v0 = row[1].strip().lower() if len(row) > 1 else ""
                    if k0 in ("key", "field", "name") and v0 in ("value", "val", ""):
                        header_skipped = True
                        continue
                header_skipped = True   # treat first content row as data

            if len(row) < 2:
                warnings.append(f"Line {lineno}: only {len(row)} column(s) — skipped")
                continue

            raw_key = row[0].strip()
            raw_val = "|".join(c.strip() for c in row[1:]).strip()  # rejoin if value contained |

            norm = _norm_key(raw_key)
            canonical = _KEY_ALIASES.get(norm)

            if not canonical:
                warnings.append(f"Line {lineno}: unrecognised key '{raw_key}' — skipped")
                continue

            seen_raw.setdefault(canonical, []).append(raw_val)
            if canonical in data and raw_val != data[canonical]:
                warnings.append(
                    f"Duplicate key '{raw_key}' (canonical: {canonical}): "
                    f"earlier value '{data[canonical]}' overridden by '{raw_val}'"
                )
            data[canonical] = raw_val   # last value wins

    return data, warnings


# ── Convert raw values → typed ────────────────────────────────────────────────

def _bool_val(raw: str) -> bool:
    return raw.strip().lower() in ("true", "yes", "1", "y")


def _list_val(raw: str) -> list[str]:
    """Split a comma-or-semicolon-separated string into a list of stripped non-empty items."""
    items = re.split(r"[,;]+", raw)
    return [i.strip().upper() for i in items if i.strip()]


def _db_name(raw: str, default: str = "") -> str:
    """Sanitise an Oracle object name."""
    val = re.sub(r"[^A-Z0-9_]+", "_", raw.strip().upper()).strip("_")
    return val or default


def _data_source(raw: str) -> str:
    """Normalise data source to 'tables', 'documents', or 'both'."""
    low = raw.strip().lower()
    if low in ("both", "all"):
        return "both"
    if any(w in low for w in ("table", "sql", "db", "nl2sql")):
        return "tables"
    if any(w in low for w in ("doc", "rag", "pdf", "file")):
        return "documents"
    return "both"   # safe default


# ── Build project dict ────────────────────────────────────────────────────────

def build_project(data: dict[str, str], cfg, schema_default: str = "") -> dict:
    """Translate the parsed CSV data dict into a project dict compatible with
    state_module.new_project() and ready for _discovery_loop / _run_step7_finalize.
    """
    # Project identity — prefer explicit "Project Name" field, fall back to
    # agent_name (which is always present in the doc format), then
    # generic default. This prevents imported projects being named
    # "Imported Project" when the doc has an agent name to use.
    display_name = (
        data.get("project")
        or data.get("agent_name")
        or "Imported Project"
    ).strip()
    # "SQL Target Schema" in the Word doc canonically maps to "sql_schema"
    # (see _DOCX_EXTRA_ALIASES / _KEY_ALIASES) — check that first. "schema"
    # is kept as a secondary check for any caller that sets it directly
    # (e.g. a future CSV column literally named "schema"), but the doc's
    # own explicit value was previously being silently ignored here since
    # nothing ever populated "schema" itself.
    schema = (data.get("sql_schema") or data.get("schema")
              or schema_default or "YOUR_SCHEMA").strip().upper()

    import re as _re
    file_slug = _re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")

    project = state_module.new_project(file_slug, schema)
    project["display_name"] = display_name

    facts = project["facts"]

    # Data source
    if "data_source" in data:
        facts["data_source"] = _data_source(data["data_source"])

    # RAG
    rag_url = (data.get("rag_url") or "").strip()
    if rag_url:
        facts["rag"]["object_storage_url"] = rag_url
    if "rag_subject" in data:
        facts["rag"]["subject"] = data["rag_subject"].strip()
    if "rag_file_types" in data:
        facts["rag"]["file_types"] = _list_val(data["rag_file_types"])

    # SQL
    if "sql_tables" in data and data["sql_tables"].strip():
        facts["sql"]["tables"] = _list_val(data["sql_tables"])
    if "sql_question_types" in data:
        facts["sql"]["question_types"] = data["sql_question_types"].strip()

    # Object names
    names = facts.setdefault("names", {})
    for csv_key, names_key in [
        ("rag_profile",   "rag_profile"),
        ("vector_index",  "vector_index"),
        ("rag_tool",      "rag_tool"),
        ("nl2sql_profile","nl2sql_profile"),
        ("sql_tool",      "sql_tool"),
    ]:
        if csv_key in data and data[csv_key].strip():
            names[names_key] = _db_name(data[csv_key])

    if "chunk_size" in data:
        try:
            names["chunk_size"] = int(data["chunk_size"])
        except ValueError:
            pass
    if "chunk_overlap" in data:
        try:
            names["chunk_overlap"] = int(data["chunk_overlap"])
        except ValueError:
            pass

    # NL2SQL comments mode
    if "nl2sql_comments" in data:
        mode = "existing" if _bool_val(data["nl2sql_comments"]) else "skipped"
        facts["sql"].setdefault("comments", {
            "mode": mode, "status": "scanned" if mode == "existing" else "skipped",
            "objects": {}, "coverage": {}
        })

    # Analysis tools
    if "analysis_tools" in data:
        if not _bool_val(data["analysis_tools"]):
            facts["analysis_tools"] = []
        # If True the user will be prompted in Step 5 — leave empty for now

    # Agent name
    if "agent_name" in data:
        agent_name = _db_name(data["agent_name"])
        facts["task"]["agent_name"] = agent_name
        names["agent"] = agent_name

    # Agent instructions / role
    if "agent_instructions" in data:
        facts["agent_role"] = data["agent_instructions"].strip()

    # Task
    if "task_instruction" in data:
        facts["task"]["instruction"] = data["task_instruction"].strip()
    elif "agent_instructions" in data:
        # Fall back to agent instructions if task instruction not separately specified
        facts["task"]["instruction"] = data["agent_instructions"].strip()
    if "task_name" in data:
        facts["task"]["task_name"] = _db_name(data["task_name"])
    if "team_name" in data:
        facts["task"]["team_name"] = _db_name(data["team_name"])

    # Mark workflow as having all steps pre-filled so _discovery_loop starts at Step 7
    project["workflow"]["current_step"] = 7
    project["workflow"]["last_completed_step"] = 6
    project["_imported_from_csv"] = True

    return project


# ── Validation ────────────────────────────────────────────────────────────────

REQUIRED_KEYS = {"project", "schema"}
RECOMMENDED_KEYS = {
    "data_source", "agent_name", "agent_instructions",
    "task_name", "team_name",
}

def validate_project(project: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a freshly imported project."""
    errors: list[str] = []
    warnings: list[str] = []
    facts = project.get("facts", {})

    if not project.get("display_name"):
        errors.append("Project name is missing")
    if not project.get("schema"):
        errors.append("Schema is missing")

    ds = facts.get("data_source")
    if not ds:
        warnings.append("data_source not set — defaulting to 'both'")
        facts["data_source"] = "both"
        ds = "both"

    if ds in ("documents", "both"):
        if not facts["rag"].get("object_storage_url"):
            warnings.append("RAG URL not set — required for RAG tool")
        if not facts["rag"].get("subject"):
            warnings.append("RAG subject not set — the LLM will ask for it in conversation")

    if ds in ("tables", "both"):
        if not facts["sql"].get("tables"):
            warnings.append("SQL tables not set — you will be prompted for them in conversation")

    task = facts.get("task", {})
    if not task.get("agent_name"):
        warnings.append("Agent name not set — a default will be generated from the project name")
    if not task.get("instruction"):
        warnings.append("Task instruction not set — defaults to agent_instructions if present")
    if not task.get("task_name"):
        warnings.append("Task name not set — a default will be generated")
    if not task.get("team_name"):
        warnings.append("Team name not set — a default will be generated")

    return errors, warnings


# ── Interactive summary display ───────────────────────────────────────────────

def print_import_summary(project: dict, warnings: list[str], display) -> None:
    """Print what was imported so the user can verify before proceeding."""
    C = display.C
    facts = project["facts"]
    names = facts.get("names", {})
    task  = facts.get("task", {})

    display.blank()
    print(f"  {C.BOLD}{'─'*60}{C.RESET}")
    print(f"  {C.BOLD}Import summary{C.RESET}")
    print(f"  {'─'*60}")

    def row(label, value, warn=False):
        color = C.YELLOW if warn else ""
        reset = C.RESET if warn else ""
        val_str = str(value) if value else f"{C.DIM}(not set){C.RESET}"
        print(f"  {label:<28}: {color}{val_str}{reset}")

    row("Project",        project.get("display_name"))
    row("Schema",         project.get("schema"))
    row("Data source",    facts.get("data_source"))
    display.blank()
    row("RAG URL",        facts["rag"].get("object_storage_url"),  warn=not facts["rag"].get("object_storage_url"))
    row("RAG subject",    facts["rag"].get("subject"))
    row("RAG file types", ", ".join(facts["rag"].get("file_types", [])))
    display.blank()
    row("SQL tables",     ", ".join(facts["sql"].get("tables", [])), warn=not facts["sql"].get("tables"))
    display.blank()
    row("RAG profile",    names.get("rag_profile"))
    row("Vector index",   names.get("vector_index"))
    row("Chunk size",     names.get("chunk_size", 1024))
    row("Chunk overlap",  names.get("chunk_overlap", 128))
    row("RAG tool",       names.get("rag_tool"))
    row("NL2SQL profile", names.get("nl2sql_profile"))
    row("SQL tool",       names.get("sql_tool"))
    display.blank()
    row("Agent name",     task.get("agent_name"), warn=not task.get("agent_name"))
    if facts.get("agent_role"):
        print(f"  {'Agent role':<28}: {facts['agent_role'][:72]}{'...' if len(facts.get('agent_role',''))>72 else ''}")
    row("Task name",      task.get("task_name"))
    row("Team name",      task.get("team_name"))
    if task.get("instruction"):
        print(f"  {'Task instruction':<28}: {task['instruction'][:72]}{'...' if len(task.get('instruction',''))>72 else ''}")
    display.blank()

    if warnings:
        print(f"  {C.YELLOW}Warnings:{C.RESET}")
        for w in warnings:
            print(f"    {C.YELLOW}⚠{C.RESET}  {w}")
        display.blank()

    print(f"  {'─'*60}")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_import(cfg, clients: dict, display) -> Optional[dict]:
    """Prompt for a CSV path, parse and validate it, show summary, and return
    the project dict ready for _discovery_loop (starting at Step 7) — or
    None if the user cancelled or validation failed fatally.
    """
    C = display.C
    display.head("IMPORT PROJECT FROM CSV")
    display.blank()
    print(f"  {C.DIM}The CSV must be pipe-delimited (|) with two columns: Key and Value.{C.RESET}")
    print(f"  {C.DIM}Download the template from the examples/ folder: project_template.csv{C.RESET}")
    display.blank()

    try:
        raw_path = input("  CSV file path (or q to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return None

    if not raw_path or raw_path.lower() in ("q", "quit", "exit", "b", "back"):
        display.warn("Cancelled")
        return None

    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        display.err(f"File not found: {path}")
        return None
    if not path.suffix.lower() in (".csv", ".txt", ".tsv"):
        display.warn(f"Unexpected extension '{path.suffix}' — attempting to parse anyway")

    # Parse
    display.info("Parsing CSV...")
    try:
        data, parse_warnings = parse_csv(path)
    except Exception as ex:
        display.err(f"Failed to read CSV: {ex}")
        return None

    if not data:
        display.err("No recognised fields found in the CSV. Check the file format.")
        return None

    # Build project
    schema_default = cfg_module.get(cfg, "database", "db_user", fallback="YOUR_SCHEMA")
    try:
        project = build_project(data, cfg, schema_default)
    except Exception as ex:
        display.err(f"Failed to build project from CSV data: {ex}")
        return None

    # Validate
    errors, val_warnings = validate_project(project)
    all_warnings = parse_warnings + val_warnings

    # Show summary
    print_import_summary(project, all_warnings, display)

    if errors:
        display.err("Import cannot continue — required fields are missing:")
        for e in errors:
            display.err(f"  • {e}")
        display.info("Fix the CSV and try again.")
        return None

    # Confirm
    display.blank()
    print(f"  {C.BOLD}Options:{C.RESET}")
    print(f"   1. Proceed — go to Step 7 to confirm names and generate SQL")
    print(f"   2. Re-enter conversation — start from the beginning and edit via chat")
    print(f"   3. Cancel")
    display.blank()
    try:
        choice = input("  Choice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return None

    if choice == "3" or choice.lower() in ("q", "quit", "cancel"):
        display.warn("Import cancelled")
        return None

    if choice == "2":
        # Reset workflow to start of conversation — user will fill in via chat
        project["workflow"]["current_step"] = 2
        project["workflow"]["last_completed_step"] = 1
        display.info("Starting conversation from Step 2. Your CSV values are pre-loaded as context.")

    # Check for existing project file
    existing_path = state_module.projects_dir(cfg) / f"{project['project_name']}.json"
    if existing_path.exists():
        display.blank()
        display.warn(f"A project named '{project['project_name']}' already exists")
        try:
            overwrite = input("  Overwrite it? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            overwrite = "n"
        if overwrite not in ("y", "yes"):
            display.warn("Import cancelled — choose a different project name in the CSV")
            return None

    # Save
    display.blank()
    state_module.save_project(cfg, project)
    run_log = state_module.open_run_log(cfg, project)
    run_log.log_section("PROJECT IMPORTED FROM CSV")
    run_log.log(f"Source: {path}")
    run_log.log(f"Project: {project['display_name']}  Schema: {project['schema']}")
    display.ok(f"Project saved: {project['project_name']}")
    display.info(f"Run log: {run_log.run_path}")

    project["_run_log"] = run_log   # pass run_log through to caller
    return project
