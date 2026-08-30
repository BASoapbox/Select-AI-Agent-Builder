"""
modules/comments.py
Manage NL2SQL table/column comments for Select AI Agent Builder projects.

Comments are stored in project["facts"]["sql"]["comments"] and can be
scanned from the database, generated as drafts, imported/exported as CSV/JSON,
or emitted as COMMENT ON SQL.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Dict, Tuple

from core import db as db_module
from core import state as state_module
from core import config as cfg_module


SQL_TABLE_COMMENTS = """
SELECT owner, table_name, comments
FROM   all_tab_comments
WHERE  owner = :owner
AND    table_name IN ({placeholders})
ORDER  BY owner, table_name
"""

SQL_COLUMN_COMMENTS = """
SELECT owner, table_name, column_name, comments
FROM   all_col_comments
WHERE  owner = :owner
AND    table_name IN ({placeholders})
ORDER  BY owner, table_name, column_name
"""

SQL_COLUMNS = """
SELECT owner, table_name, column_name, data_type, nullable
FROM   all_tab_columns
WHERE  owner = :owner
AND    table_name IN ({placeholders})
ORDER  BY owner, table_name, column_id
"""

SQL_DISTINCT_VALUES = """
SELECT DISTINCT TO_CHAR({col}) AS val
FROM   {owner}.{table}
WHERE  {col} IS NOT NULL
  AND  ROWNUM <= 60
ORDER  BY TO_CHAR({col})
"""

SQL_FK_CONSTRAINTS = """
SELECT ac.table_name           AS fk_table,
       acc.column_name          AS fk_column,
       arc.table_name           AS ref_table,
       arcc.column_name         AS ref_column
FROM   all_constraints  ac
JOIN   all_cons_columns acc  ON acc.constraint_name = ac.constraint_name
                             AND acc.owner           = ac.owner
JOIN   all_constraints  arc  ON arc.constraint_name  = ac.r_constraint_name
                             AND arc.owner            = ac.r_owner
JOIN   all_cons_columns arcc ON arcc.constraint_name = arc.constraint_name
                             AND arcc.owner           = arc.owner
                             AND arcc.position        = acc.position
WHERE  ac.constraint_type = 'R'
AND    ac.owner           = :owner
AND    ac.table_name      IN ({placeholders})
ORDER  BY ac.table_name, acc.column_name
"""


def _safe_name(value: str) -> str:
    value = (value or "").strip().upper()
    value = re.sub(r"[^A-Z0-9_$#.]", "_", value)
    return value.strip("_")


def _split_owner_name(raw: str, default_owner: str) -> Tuple[str, str]:
    raw = _safe_name(raw)
    if "." in raw:
        owner, name = raw.split(".", 1)
        return _safe_name(owner), _safe_name(name)
    return _safe_name(default_owner), raw


def _ensure_comments(project: dict) -> dict:
    facts = project.setdefault("facts", {})
    sql = facts.setdefault("sql", {})
    comments = sql.setdefault("comments", {})
    comments.setdefault("mode", "unspecified")
    comments.setdefault("status", "not_started")
    comments.setdefault("objects", {})
    comments.setdefault("coverage", {})
    # Remove any orphan keys where the table name is empty (e.g. "ACME_CORP.")
    # These can be created by FK queries that don't select the source table name.
    bad_keys = [k for k in comments["objects"] if not k.rstrip(".").split(".")[-1]]
    for k in bad_keys:
        del comments["objects"][k]
    return comments


def _selected_tables(project: dict) -> list[Tuple[str, str]]:
    facts = project.setdefault("facts", {})
    sql = facts.setdefault("sql", {})
    default_owner = project.get("schema") or sql.get("owner") or ""
    tables = sql.get("tables") or []
    out = []
    seen = set()
    for t in tables:
        owner, name = _split_owner_name(str(t), default_owner)
        if owner and name and (owner, name) not in seen:
            seen.add((owner, name))
            out.append((owner, name))
    return out


def _object_key(owner: str, table_name: str) -> str:
    return f"{_safe_name(owner)}.{_safe_name(table_name)}"


def _ensure_object(comments: dict, owner: str, table_name: str) -> dict:
    key = _object_key(owner, table_name)
    obj = comments.setdefault("objects", {}).setdefault(key, {})
    obj.setdefault("table_comment", "")
    obj.setdefault("columns", {})
    return obj


def _in_clause_params(tables: list[Tuple[str, str]]) -> tuple[str, dict]:
    params = {}
    placeholders = []
    for i, (_, name) in enumerate(tables):
        key = f"t{i}"
        placeholders.append(f":{key}")
        params[key] = name
    return ",".join(placeholders), params


# Column name patterns that strongly suggest a coded / enumerated value set.
_CODED_NAME_PATTERNS = (
    "TYPE", "CODE", "STATUS", "FLAG", "CATEGORY", "ROLE",
    "CLASS", "KIND", "MODE", "STATE", "PHASE", "STAGE",
)

# Column name patterns that suggest date/period values — never hardcode these.
_PERIOD_NAME_PATTERNS = (
    "PERIOD", "FISCAL", "QUARTER", "MONTH", "YEAR",
)


def _is_coded_column(col: str, dtype: str) -> bool:
    """Return True if the column is likely to hold a small fixed set of values."""
    col_up = col.upper()
    return dtype.upper() in ("VARCHAR2", "CHAR", "NVARCHAR2") and any(
        pat in col_up for pat in _CODED_NAME_PATTERNS
    )


def _is_period_column(col: str, dtype: str) -> bool:
    """Return True if the column likely holds date/period values (never hardcode)."""
    col_up = col.upper()
    return any(pat in col_up for pat in _PERIOD_NAME_PATTERNS) or dtype.upper() == "DATE"


def _scan_distinct_values(conn, owner: str, table: str, col: str) -> list[str]:
    """Return up to 20 distinct non-null values for a coded column, or [] on error."""
    try:
        sql = SQL_DISTINCT_VALUES.format(
            col=col, owner=owner, table=table
        )
        rows = db_module.query_all(conn, sql)
        vals = [r["val"] for r in rows if r.get("val") is not None]
        # If there are too many distinct values it's not really a coded column
        if len(vals) > 20:
            return []
        return vals
    except Exception:
        return []


def _scan_fk_constraints(conn, owner: str, tables: list[Tuple[str, str]]) -> dict:
    """Return {table_name: [(fk_column, ref_table, ref_column), ...]} for selected tables."""
    if not tables:
        return {}
    placeholders, params = _in_clause_params(tables)
    params["owner"] = owner
    sql = SQL_FK_CONSTRAINTS.format(placeholders=placeholders)
    result: dict[str, list] = {}
    try:
        for row in db_module.query_all(conn, sql, params):
            tbl = (row.get("fk_table") or row.get("FK_TABLE") or "").strip().upper()
            if not tbl:
                continue  # guard against empty table name — skip malformed rows
            result.setdefault(tbl, []).append((
                row.get("fk_column") or row.get("FK_COLUMN", ""),
                row.get("ref_table") or row.get("REF_TABLE", ""),
                row.get("ref_column") or row.get("REF_COLUMN", ""),
            ))
    except Exception:
        pass
    return result


def scan_existing_comments(cfg, project: dict, display=None) -> dict:
    """Scan ALL_TAB_COMMENTS / ALL_COL_COMMENTS plus enriched metadata.

    In addition to existing DB comments this also fetches:
    - FK constraints (stored as fk_constraints on each table object)
    - Distinct values for coded VARCHAR2 columns (TYPE/CODE/STATUS/FLAG etc.)
      stored in column_metadata[col][distinct_values] for use by the LLM generator
    Period/date columns are never scanned for distinct values.
    """
    comments = _ensure_comments(project)
    tables = _selected_tables(project)
    if not tables:
        raise ValueError("No SQL tables were captured for this project")

    owners = sorted({owner for owner, _ in tables})
    conn = db_module.connect(cfg)
    try:
        for owner in owners:
            owner_tables = [(o, n) for o, n in tables if o == owner]
            placeholders, params = _in_clause_params(owner_tables)
            params["owner"] = owner

            tab_sql  = SQL_TABLE_COMMENTS.format(placeholders=placeholders)
            col_sql  = SQL_COLUMN_COMMENTS.format(placeholders=placeholders)
            meta_sql = SQL_COLUMNS.format(placeholders=placeholders)

            for row in db_module.query_all(conn, tab_sql, params):
                obj = _ensure_object(comments, row["owner"], row["table_name"])
                if row.get("comments"):
                    obj["table_comment"] = str(row["comments"])

            for row in db_module.query_all(conn, col_sql, params):
                obj = _ensure_object(comments, row["owner"], row["table_name"])
                if row.get("comments"):
                    obj.setdefault("columns", {})[row["column_name"]] = str(row["comments"])

            # Column metadata — data type + nullable
            for row in db_module.query_all(conn, meta_sql, params):
                obj = _ensure_object(comments, row["owner"], row["table_name"])
                cols_meta = obj.setdefault("column_metadata", {})
                cols_meta[row["column_name"]] = {
                    "data_type": row.get("data_type", ""),
                    "nullable":  row.get("nullable", ""),
                }

            # FK constraints between selected tables
            fk_map = _scan_fk_constraints(conn, owner, owner_tables)
            for tbl, fk_list in fk_map.items():
                obj = _ensure_object(comments, owner, tbl)
                obj["fk_constraints"] = fk_list

            # Distinct values for coded columns (not period/date columns)
            for o, tbl in owner_tables:
                obj = _ensure_object(comments, o, tbl)
                for col, meta in list(obj.get("column_metadata", {}).items()):
                    dtype = meta.get("data_type", "")
                    if _is_coded_column(col, dtype) and not _is_period_column(col, dtype):
                        vals = _scan_distinct_values(conn, o, tbl, col)
                        if vals:
                            obj["column_metadata"][col]["distinct_values"] = vals

        comments["mode"] = "existing"
        comments["status"] = "scanned"
        comments["coverage"] = calculate_coverage(comments)
        if display:
            display.ok("Existing database comments scanned")
        return comments
    finally:
        conn.close()


def generate_llm_comments(project: dict, cfg, clients: dict, display=None) -> dict:
    """Generate NL2SQL comments using the OCI GenAI LLM.

    Pipeline:
    1. scan_existing_comments() — fetches column metadata, FK constraints,
       and distinct values for coded columns.
    2. Build a structured prompt from the schema + agent purpose.
    3. Call the LLM once per table (or in a single batch for small schemas).
    4. Parse the JSON response into the comments store.
    5. Apply item 4 fix: period/date columns always get a "do not assume fixed
       range" instruction rather than enumerated values.
    """
    from core import llm as llm_module

    comments = _ensure_comments(project)
    tables = _selected_tables(project)
    if not tables:
        raise ValueError("No SQL tables were captured for this project")

    # Step 1 — fetch enriched metadata (FK, coded values, existing comments)
    if display:
        display.info("Scanning schema metadata...")
    try:
        scan_existing_comments(cfg, project, display=None)
    except Exception as scan_err:
        if display:
            display.warn(f"Metadata scan error: {scan_err}. Continuing with available data.")

    facts   = project.get("facts", {})
    purpose = (
        facts.get("sql", {}).get("question_types", "")
        or facts.get("agent_role", "")
        or "general business queries"
    ).strip()

    # Step 2+3+4 — call LLM per table
    total = len(tables)
    for idx, (owner, table) in enumerate(tables, 1):
        key = f"{owner}.{table}"
        obj = _ensure_object(comments, owner, table)
        meta = obj.get("column_metadata", {})
        fk_list = obj.get("fk_constraints", [])

        if display:
            display.info(f"  Generating comments for {key} ({idx}/{total})...")

        # Build a lookup of FK columns for this table so the prompt gets
        # "describe the join" instead of "enumerate values" for those columns.
        # This prevents maintenance-prone value lists on FK columns — the join
        # relationship is a schema-level fact that won't go stale when data changes.
        fk_col_map = {
            fk_col.upper(): (ref_table, ref_col)
            for fk_col, ref_table, ref_col in fk_list
        }

        # Build schema summary for prompt
        col_lines = []
        for col, detail in meta.items():
            dtype     = detail.get("data_type", "")
            nullable  = "nullable" if detail.get("nullable") == "Y" else "NOT NULL"
            vals      = detail.get("distinct_values", [])
            is_period = _is_period_column(col, dtype)
            is_fk     = col.upper() in fk_col_map

            if is_fk:
                ref_table_name, ref_col_name = fk_col_map[col.upper()]
                col_lines.append(
                    f"  - {col} {dtype} {nullable}"
                    f"  [FK → {ref_table_name}.{ref_col_name}"
                    f" — describe the join, do not enumerate values]"
                )
            elif is_period:
                col_lines.append(
                    f"  - {col} {dtype} {nullable}"
                    f"  [PERIOD/DATE: do not enumerate values]"
                )
            elif vals:
                vals_str = ", ".join(str(v) for v in vals[:20])
                col_lines.append(
                    f"  - {col} {dtype} {nullable}"
                    f"  [known values: {vals_str}]"
                )
            else:
                col_lines.append(f"  - {col} {dtype} {nullable}")

        fk_lines = []
        for fk_col, ref_table, ref_col in fk_list:
            fk_lines.append(f"  - {fk_col} → {ref_table}.{ref_col} (foreign key)")

        schema_block = "\n".join(col_lines)
        fk_block     = ("\nForeign keys:\n" + "\n".join(fk_lines)) if fk_lines else ""

        existing_table_comment = obj.get("table_comment", "")

        prompt = f"""You are writing Oracle database comments to improve NL2SQL accuracy for a Select AI agent.

The agent answers: {purpose}

Table: {table}
Schema owner: {owner}
{f'Existing table comment: {existing_table_comment}' if existing_table_comment else ''}

Columns:
{schema_block}{fk_block}

Write concise, precise Oracle COMMENT ON TABLE and COMMENT ON COLUMN text for this table.

Rules:
- Table comment: one sentence describing what the table contains, its primary use case for NL2SQL queries, and any join relationships to other tables.
- Column comments: focus on columns where the name alone is ambiguous. Skip obvious columns like ID or DATE columns with no special semantics.
- For columns with known values (listed above): include all valid values in the format "Valid values: A, B, C."
- For FK columns (marked [FK → ...]): write "Join to <ref_table> on <fk_column>" — never list the values present in this table; use the reference table as the authority.
- For PERIOD/DATE columns: include the format (e.g. MON-YYYY) if inferable from the column name, and explicitly say "Query the actual data to determine which values exist — do not assume a fixed range."
- For amount/numeric columns: explain what the number represents and when to sum vs average it.
- Keep each comment under 200 characters where possible.
- Return ONLY valid JSON. No markdown, no explanation, no code fences.

JSON format:
{{
  "table_comment": "...",
  "columns": {{
    "COLUMN_NAME": "comment text",
    ...
  }}
}}

Only include columns in "columns" where the comment adds real value beyond the column name itself.
"""

        already_approved = (comments.get("status") == "approved")

        try:
            raw = llm_module.generate(
                clients, cfg,
                prompt=prompt,
                system_prompt="You are a precise database documentation assistant. Return only valid JSON.",
                temperature=0.1,
                max_tokens=1200,
            )
            # Strip any markdown fences the model may add despite instructions
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
            raw = raw.strip().rstrip("`").strip()

            parsed = json.loads(raw)

            # Apply table comment — skip if already approved, overwrite if draft/blank
            llm_table = (parsed.get("table_comment") or "").strip()
            if llm_table and not (already_approved and obj.get("table_comment", "").strip()):
                obj["table_comment"] = llm_table

            # Apply column comments — skip approved columns, overwrite draft/blank
            llm_cols = parsed.get("columns") or {}
            for col, comment in llm_cols.items():
                col_up = col.upper()
                if not comment or not comment.strip():
                    continue
                existing = obj.get("columns", {}).get(col_up, "")
                if already_approved and existing.strip():
                    continue  # never overwrite an approved comment
                obj.setdefault("columns", {})[col_up] = comment.strip()

        except json.JSONDecodeError as je:
            if display:
                display.warn(f"  LLM returned invalid JSON for {table}: {je}. Skipping LLM output for this table.")
        except Exception as ex:
            if display:
                display.warn(f"  LLM call failed for {table}: {ex}. Skipping LLM output for this table.")

        # Period/date safety pass — runs regardless of whether LLM succeeded.
        # Enforces "do not assume fixed range" on any period/date column.
        # Also corrects any pre-existing comment that hardcoded specific values.
        for col, detail in meta.items():
            dtype = detail.get("data_type", "")
            if not _is_period_column(col, dtype):
                continue
            if already_approved and col in obj.get("columns", {}):
                # Only enforce if the existing comment is still problematic
                existing = obj["columns"][col]
                if "do not assume" in existing.lower() or "query the actual" in existing.lower():
                    continue
            current = obj.get("columns", {}).get(col, "")
            # Strip any hardcoded value lists (e.g. "Data exists for: JAN-2025, ...")
            import re as _re
            current = _re.sub(r"[Dd]ata exists for[^.]*[.]", "", current).strip()
            fmt_hint = " Format: MON-YYYY (e.g. JAN-2025)." if "PERIOD" in col.upper() else ""
            safe_suffix = f" Query the actual data to determine which values exist — do not assume a fixed range.{fmt_hint}"
            if "do not assume" not in current.lower() and "query the actual" not in current.lower():
                safe_msg = (current.rstrip(". ") + "." + safe_suffix).lstrip(". ")
                obj.setdefault("columns", {})[col] = safe_msg

    comments["mode"]     = "generated"
    comments["status"]   = "draft"
    comments["coverage"] = calculate_coverage(comments)
    if display:
        display.ok("LLM comment generation complete")
    return comments


def import_comments(project: dict, file_path: str | Path, display=None) -> dict:
    """Import comments from CSV or JSON."""
    comments = _ensure_comments(project)
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "objects" in payload:
            comments.update(payload)
        else:
            comments["objects"] = payload
    else:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                owner = _safe_name(row.get("owner") or project.get("schema") or "")
                table = _safe_name(row.get("table_name") or row.get("table") or "")
                column = _safe_name(row.get("column_name") or row.get("column") or "")
                comment = (row.get("comment") or row.get("comments") or "").strip()
                if not owner or not table or not comment:
                    continue
                obj = _ensure_object(comments, owner, table)
                if column:
                    obj.setdefault("columns", {})[column] = comment
                else:
                    obj["table_comment"] = comment

    comments["mode"] = "imported"
    comments["status"] = "draft"
    comments["coverage"] = calculate_coverage(comments)
    if display:
        display.ok(f"Comments imported from {path}")
    return comments


def export_template(project: dict, output_path: str | Path) -> Path:
    """Export a CSV template containing selected tables and known columns."""
    comments = _ensure_comments(project)
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["owner", "table_name", "column_name", "comment"])
        writer.writeheader()
        for owner, table in _selected_tables(project):
            obj = _ensure_object(comments, owner, table)
            writer.writerow({
                "owner": owner,
                "table_name": table,
                "column_name": "",
                "comment": obj.get("table_comment", ""),
            })
            all_cols = sorted(set(obj.get("column_metadata", {}).keys()) | set(obj.get("columns", {}).keys()))
            for col in all_cols:
                writer.writerow({
                    "owner": owner,
                    "table_name": table,
                    "column_name": col,
                    "comment": obj.get("columns", {}).get(col, ""),
                })
    return path


def calculate_coverage(comments: dict) -> dict:
    objects = comments.get("objects", {})
    table_total = len(objects)
    table_with = sum(1 for obj in objects.values() if obj.get("table_comment"))
    col_total = 0
    col_with = 0
    for obj in objects.values():
        known_cols = set(obj.get("column_metadata", {}).keys()) | set(obj.get("columns", {}).keys())
        col_total += len(known_cols)
        col_with += sum(1 for c in known_cols if obj.get("columns", {}).get(c))
    return {
        "table_total": table_total,
        "table_with_comments": table_with,
        "column_total": col_total,
        "column_with_comments": col_with,
    }


def comment_sql_from_project(project: dict) -> str:
    comments = _ensure_comments(project)
    lines = [
        "-- ============================================================",
        "-- NL2SQL table and column comments",
        "-- Generated from project facts",
        "-- ============================================================",
        "",
    ]
    for key in sorted(comments.get("objects", {})):
        obj = comments["objects"][key]
        owner, table = key.split(".", 1)
        table_comment = (obj.get("table_comment") or "").strip()
        if table_comment:
            lines.append(f"COMMENT ON TABLE {owner}.{table} IS '{_sql_quote(table_comment)}';")
            lines.append("")
        for col, comment in sorted(obj.get("columns", {}).items()):
            comment = (comment or "").strip()
            if comment:
                lines.append(f"COMMENT ON COLUMN {owner}.{table}.{_safe_name(col)} IS '{_sql_quote(comment)}';")
        if obj.get("columns"):
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _sql_quote(text: str) -> str:
    return str(text).replace("'", "''")


def _create_schema_direct_workspace(cfg, display) -> dict | None:
    """Prompt for a schema, table list, and optional purpose, then create a
    lightweight non-agent project shell purely for NL2SQL comment management
    — the schema-direct counterpart to picking an existing agent project.
    """
    print()
    print("  New schema-direct workspace (no agent project)")
    try:
        schema = input("  Schema (e.g. ACME_CORP): ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        return None
    if not schema:
        display.warn("Schema is required")
        return None

    try:
        tables_raw = input("  Tables, comma-separated (no schema prefix): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    tables = [t.strip() for t in tables_raw.split(",") if t.strip()]
    if not tables:
        display.warn("At least one table is required")
        return None

    try:
        purpose = input(
            "  What will queries against these tables be about? "
            "[optional, improves LLM-drafted comments]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        purpose = ""

    default_name = f"{schema.lower()}_comments"
    try:
        name = input(f"  Workspace name [{default_name}]: ").strip() or default_name
    except (EOFError, KeyboardInterrupt):
        return None

    project = state_module.new_comment_workspace(name, schema, tables, purpose)
    state_module.save_project(cfg, project)
    display.ok(f"Workspace created: {name}  ({schema} — {len(tables)} table(s))")
    return project


def _pick_project(cfg, display) -> dict | None:
    projects = state_module.list_projects(cfg)
    print()
    print("  Select project:")
    if projects:
        for i, p in enumerate(projects, 1):
            print(f"  {i:2}. {p['name']:<35} {p['phase']}")
    else:
        print("  (no saved projects yet)")
    print("   n. New schema-direct workspace (no agent project)")
    print("   b. Back")
    try:
        raw = input("  Choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if raw in ("", "b", "q"):
        return None
    if raw == "n":
        return _create_schema_direct_workspace(cfg, display)
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(projects):
            return state_module.load_project(projects[idx]["file"])
    except ValueError:
        pass
    display.warn("Invalid project selection")
    return None


def _project_default_path(cfg, project: dict, suffix: str) -> Path:
    slug = project.get("project_name", "project")
    return state_module.project_dir(cfg, slug) / suffix


def _show_coverage(project: dict, display) -> None:
    comments = _ensure_comments(project)
    comments["coverage"] = calculate_coverage(comments)
    cov = comments["coverage"]
    print()
    print(f"  {display.C.BOLD}NL2SQL comment coverage{display.C.RESET}")
    print(f"  Mode/status     : {comments.get('mode')} / {comments.get('status')}")
    print(f"  Table comments  : {cov.get('table_with_comments',0)} / {cov.get('table_total',0)}")
    print(f"  Column comments : {cov.get('column_with_comments',0)} / {cov.get('column_total',0)}")


def enter_comments_manually(project: dict, cfg=None, display=None) -> dict:
    """Collect table and selected column comments directly from the user.

    This is intentionally lightweight for discovery/review mode:
    - table comments are prompted one table at a time
    - column comments are entered as COLUMN_NAME = comment lines
    - blank line moves to the next table
    - '-' clears an existing table/column comment

    Comments remain in draft status until explicitly approved.
    """
    comments = _ensure_comments(project)
    tables = _selected_tables(project)
    if not tables:
        if display:
            display.warn("No SQL tables captured for this project")
        return comments

    # If possible, load column metadata first so the prompt can show known columns.
    # This also pulls existing comments, which the user can keep, replace, or clear.
    if cfg is not None:
        try:
            scan_existing_comments(cfg, project, display=None)
        except Exception:
            pass

    if display:
        display.blank()
        print(f"  {display.C.BOLD}Manual NL2SQL comment entry{display.C.RESET}")
    else:
        print()
        print("  Manual NL2SQL comment entry")
    print("  Enter useful business comments. Leave prompts blank to keep current values.")
    print("  For column comments, use COLUMN_NAME = comment. Blank line moves to the next table.")
    print("  Type '-' as the comment to clear an existing value; type 'done' to finish.")

    done = False
    for owner, table in tables:
        if done:
            break
        obj = _ensure_object(comments, owner, table)
        print()
        if display:
            print(f"  {display.C.BOLD}{owner}.{table}{display.C.RESET}")
        else:
            print(f"  {owner}.{table}")

        current = obj.get("table_comment", "")
        try:
            val = input(f"  Table comment [{current or 'blank'}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            val = "done"

        low = val.lower()
        if low in ("done", "q", "quit", "cancel", "b", "back"):
            done = True
            break
        if val == "-":
            obj["table_comment"] = ""
        elif val:
            obj["table_comment"] = val

        known_cols = sorted(set(obj.get("column_metadata", {}).keys()) | set(obj.get("columns", {}).keys()))
        if known_cols:
            print("  Known columns:")
            line = ""
            for col in known_cols:
                token = col
                if len(line) + len(token) + 3 > 88:
                    print(f"    {line.rstrip(', ')}")
                    line = ""
                line += token + ", "
            if line:
                print(f"    {line.rstrip(', ')}")
        else:
            print("  Known columns were not loaded; you can still enter column names manually.")

        print("  Column comments for this table:")
        while True:
            try:
                raw = input("    COLUMN_NAME = comment [blank=next table]: ").strip()
            except (EOFError, KeyboardInterrupt):
                raw = ""
            if not raw:
                break
            if raw.lower() in ("done", "q", "quit", "cancel", "b", "back"):
                done = True
                break
            if "=" not in raw:
                if display:
                    display.warn("Use COLUMN_NAME = comment, or blank to move to the next table")
                else:
                    print("  Use COLUMN_NAME = comment, or blank to move to the next table")
                continue
            col_raw, comment_raw = raw.split("=", 1)
            col = _safe_name(col_raw)
            comment = comment_raw.strip()
            if not col:
                continue
            if known_cols and col not in known_cols:
                try:
                    confirm = input(f"    Column {col} is not in known metadata. Add anyway? [y/N]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    confirm = "n"
                if confirm != "y":
                    continue
            if comment == "-":
                obj.setdefault("columns", {}).pop(col, None)
            elif comment:
                obj.setdefault("columns", {})[col] = comment
        if done:
            break

    comments["mode"] = "manual"
    comments["status"] = "draft"
    comments["coverage"] = calculate_coverage(comments)
    if display:
        display.ok("Manual comments saved as draft")
    return comments


def _edit_interactively(project: dict, display) -> None:
    # Backward-compatible wrapper used by older menu code.
    enter_comments_manually(project, cfg=None, display=display)


def approve_comments(project: dict, display=None) -> dict:
    """Mark current comments as approved for inclusion in final generated SQL."""
    comments = _ensure_comments(project)
    if not comments.get("objects"):
        raise ValueError("No comments are available to approve")
    comments["status"] = "approved"
    comments["coverage"] = calculate_coverage(comments)
    if display:
        display.ok("Comments approved for final SQL generation")
    return comments


def _delete_comments_interactive(project: dict, display) -> dict:
    """Interactively clear table and/or column comments from the project's comment store.

    This clears comments from the local project JSON only.
    To remove comments already applied to the database, re-run the agent build
    with comments disabled, or issue COMMENT ON TABLE/COLUMN ... IS '' manually.
    """
    comments = _ensure_comments(project)
    objects = comments.get("objects", {})
    if not objects:
        display.warn("No comments are stored for this project.")
        return comments

    C = display.C
    display.blank()
    print(f"  {C.BOLD}Delete Comments{C.RESET}")
    print(f"  {C.DIM}This clears comments from the project file only.{C.RESET}")
    print(f"  {C.DIM}To remove comments already applied in the DB, use option 6 to generate a{C.RESET}")
    print(f"  {C.DIM}SQL file with blank IS '' values, then run it manually.{C.RESET}")
    display.blank()
    print("  Scope:")
    print("   1. Clear ALL comments for this project (table and column)")
    print("   2. Clear all comments for a specific table")
    print("   3. Clear a single column comment")
    print("   q. Cancel")
    display.blank()
    try:
        scope = input("  Choice [1/2/3/q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return comments

    if scope in ("q", "b", "quit", "cancel", ""):
        display.warn("Cancelled")
        return comments

    if scope == "1":
        try:
            confirm = input(
                f"  {C.YELLOW}Clear ALL comments for this project? [y/N]: {C.RESET}"
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return comments
        if confirm not in ("y", "yes"):
            display.warn("Cancelled")
            return comments
        for obj in objects.values():
            obj.pop("table_comment", None)
            obj.pop("columns", None)
        comments["status"] = "draft"
        comments["coverage"] = calculate_coverage(comments)
        display.ok("All comments cleared from project file.")
        return comments

    if scope == "2":
        display.blank()
        table_keys = sorted(objects.keys())
        for i, k in enumerate(table_keys, 1):
            print(f"   {i}.  {k}")
        display.blank()
        try:
            raw = input("  Table number (or q to cancel): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return comments
        if not raw or raw in ("q", "quit", "b"):
            display.warn("Cancelled")
            return comments
        if not raw.isdigit() or not (1 <= int(raw) <= len(table_keys)):
            display.warn("Invalid selection")
            return comments
        key = table_keys[int(raw) - 1]
        objects[key].pop("table_comment", None)
        objects[key].pop("columns", None)
        comments["coverage"] = calculate_coverage(comments)
        display.ok(f"Comments cleared for {key}")
        return comments

    if scope == "3":
        display.blank()
        try:
            key_raw = input("  Table (OWNER.TABLE_NAME, or q to cancel): ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return comments
        if not key_raw or key_raw.lower() in ("q", "quit", "b"):
            display.warn("Cancelled")
            return comments
        if key_raw not in objects:
            display.warn(f"Table '{key_raw}' not found in project comments")
            return comments
        cols = objects[key_raw].get("columns", {})
        if not cols:
            display.warn(f"No column comments stored for {key_raw}")
            return comments
        display.blank()
        col_list = sorted(cols.keys())
        for i, c in enumerate(col_list, 1):
            preview = cols[c][:60] + ("…" if len(cols[c]) > 60 else "")
            print(f"   {i}.  {c:<30}  {preview}")
        display.blank()
        try:
            raw = input("  Column number (or q to cancel): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return comments
        if not raw or raw in ("q", "quit", "b"):
            display.warn("Cancelled")
            return comments
        if not raw.isdigit() or not (1 <= int(raw) <= len(col_list)):
            display.warn("Invalid selection")
            return comments
        col = col_list[int(raw) - 1]
        cols.pop(col, None)
        comments["coverage"] = calculate_coverage(comments)
        display.ok(f"Column comment cleared: {key_raw}.{col}")
        return comments

    display.warn("Unknown scope — cancelled")
    return comments


def run(cfg, display, clients=None):
    """Interactive submenu entry point for comment management."""
    display.head("MANAGE NL2SQL COMMENTS")
    project = _pick_project(cfg, display)
    if not project:
        return

    while True:
        _show_coverage(project, display)
        print()
        print("  1. Scan existing database comments")
        print("  2. Generate comments using LLM")
        print("  3. Import comments from CSV / JSON")
        print("  4. Enter or edit comments manually")
        print("  5. Export CSV comment template")
        print("  6. Generate COMMENT ON SQL file")
        print("  7. Approve comments for final generated SQL")
        print("  8. Delete comments (clear table and/or column comments)")
        print("  9. Back")
        try:
            choice = input("  Choice [1-9]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "9"

        try:
            if choice == "1":
                scan_existing_comments(cfg, project, display)
                state_module.save_project(cfg, project)
            elif choice == "2":
                if clients:
                    generate_llm_comments(project, cfg, clients, display)
                else:
                    display.warn("LLM client not available — run from the main menu to enable LLM generation.")
                state_module.save_project(cfg, project)
            elif choice == "3":
                path = input("  CSV/JSON path: ").strip()
                import_comments(project, path, display)
                state_module.save_project(cfg, project)
            elif choice == "4":
                enter_comments_manually(project, cfg=cfg, display=display)
                state_module.save_project(cfg, project)
            elif choice == "5":
                default = _project_default_path(cfg, project, "nl2sql_comments_template.csv")
                path = input(f"  Output path [Enter for {default}]: ").strip() or str(default)
                out = export_template(project, path)
                display.ok(f"Template exported: {out}")
            elif choice == "6":
                default = _project_default_path(cfg, project, "nl2sql_comments.sql")
                path = input(f"  Output SQL path [Enter for {default}]: ").strip() or str(default)
                out = Path(path).expanduser().resolve()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(comment_sql_from_project(project), encoding="utf-8")
                display.ok(f"COMMENT ON SQL written: {out}")
            elif choice == "7":
                approve_comments(project, display)
                state_module.save_project(cfg, project)
            elif choice == "8":
                _delete_comments_interactive(project, display)
                state_module.save_project(cfg, project)
            elif choice in ("9", "b", "q", "back", "quit"):
                break
            else:
                display.warn("Enter a number from 1 to 9")
        except Exception as ex:
            display.err(f"Comment management failed: {ex}")
