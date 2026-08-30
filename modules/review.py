"""
modules/review.py
Menu options 6-11 — review, inspect, update, delete, rebuild, and test
existing Select AI Agent objects in the database.
"""

from core import db as db_module
from core import config as cfg_module


# ── Queries against ADW agent views ──────────────────────────────────────────

SQL_LIST_TOOLS = """
SELECT tool_name, status,
       SUBSTR(attribute_value, 1, 60) AS tool_type
FROM   user_ai_agent_tools t
LEFT JOIN (
    SELECT tool_name AS tn, attribute_value
    FROM   user_ai_agent_tool_attributes
    WHERE  attribute_name = 'tool_type'
) a ON a.tn = t.tool_name
ORDER  BY tool_name
"""

SQL_LIST_AGENTS = """
SELECT agent_name, status,
       SUBSTR(TO_CHAR(attribute_value), 1, 80) AS role_preview
FROM   user_ai_agents ag
LEFT JOIN (
    SELECT agent_name AS an, attribute_value
    FROM   user_ai_agent_attributes
    WHERE  attribute_name = 'role'
) a ON a.an = ag.agent_name
ORDER  BY agent_name
"""

SQL_LIST_TASKS = """
SELECT task_name, status,
       SUBSTR(TO_CHAR(attribute_value), 1, 80) AS instruction_preview
FROM   user_ai_agent_tasks t
LEFT JOIN (
    SELECT task_name AS tn, attribute_value
    FROM   user_ai_agent_task_attributes
    WHERE  attribute_name = 'instruction'
) a ON a.tn = t.task_name
ORDER  BY task_name
"""

SQL_LIST_TEAMS = """
SELECT agent_team_name AS team_name, status
FROM   user_ai_agent_teams
ORDER  BY agent_team_name
"""

SQL_LIST_PROFILES = """
SELECT profile_name, status
FROM   user_cloud_ai_profiles
ORDER  BY profile_name
"""

SQL_PROFILE_DETAIL = """
SELECT attribute_name,
       TO_CHAR(attribute_value) AS attribute_value
FROM   user_cloud_ai_profile_attributes
WHERE  profile_name = :name
ORDER  BY attribute_name
"""

SQL_VECTOR_INDEX_DETAIL = """
SELECT attribute_name,
       TO_CHAR(attribute_value) AS attribute_value
FROM   user_cloud_ai_vector_index_attributes
WHERE  vector_index_name = :name
ORDER  BY attribute_name
"""

SQL_VECTOR_INDEX_STATUS = """
SELECT status,
       num_documents,
       TO_CHAR(created, 'YYYY-MM-DD HH24:MI:SS') AS created_at,
       TO_CHAR(updated, 'YYYY-MM-DD HH24:MI:SS') AS updated_at
FROM   user_cloud_ai_vector_indexes
WHERE  index_name = :name
"""

SQL_PROFILE_VECTOR_INDEX = """
SELECT TO_CHAR(attribute_value) AS vector_index_name
FROM   user_cloud_ai_profile_attributes
WHERE  profile_name    = :name
AND    attribute_name  = 'vector_index_name'
"""
SQL_TOOL_DETAIL = """
SELECT attribute_name,
       TO_CHAR(attribute_value) AS attribute_value
FROM   user_ai_agent_tool_attributes
WHERE  tool_name = :name
ORDER  BY attribute_name
"""

SQL_AGENT_DETAIL = """
SELECT attribute_name,
       TO_CHAR(attribute_value) AS attribute_value
FROM   user_ai_agent_attributes
WHERE  agent_name = :name
ORDER  BY attribute_name
"""

SQL_TASK_DETAIL = """
SELECT attribute_name,
       TO_CHAR(attribute_value) AS attribute_value
FROM   user_ai_agent_task_attributes
WHERE  task_name = :name
ORDER  BY attribute_name
"""

SQL_TOOL_HISTORY = """
SELECT tool_name, agent_name,
       TO_CHAR(start_date, 'YYYY-MM-DD HH24:MI:SS') AS called_at,
       SUBSTR(tool_output, 1, 100) AS output_preview
FROM   user_ai_agent_tool_history
ORDER  BY start_date DESC
FETCH  FIRST 20 ROWS ONLY
"""


def _lob_str(val, limit=2000) -> str:
    """Safely read a possible LOB/CLOB value into a plain string."""
    if val is None:
        return ""
    if hasattr(val, "read"):
        return val.read(limit) or ""
    return str(val)[:limit]


def _get_connection(cfg, display):
    """Get DB connection, showing error cleanly."""
    try:
        return db_module.connect(cfg)
    except Exception as ex:
        display.err(f"Connection failed: {ex}")
        return None


# ── Menu option 6: List all objects ──────────────────────────────────────────

def run_list(cfg, display):
    display.head("EXISTING AGENT OBJECTS")

    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        # Profiles
        profiles = db_module.query_all(conn, SQL_LIST_PROFILES)
        display.blank()
        print(f"  {display.C.BOLD}PROFILES  ({len(profiles)}){display.C.RESET}")
        if profiles:
            for p in profiles:
                status_col = display.C.GREEN if p["status"] == "ENABLED" else display.C.YELLOW
                print(f"  {status_col}●{display.C.RESET}  {p['profile_name']:<40}  {p['status']}")
        else:
            display.warn("  None found")

        # Tools — split user-created vs OML built-ins
        OML_PREFIXES = (
            "ANALYZE_", "AUTOMATED_", "BUILD_", "CREATE_VIEW",
            "DESCRIBE_", "DISCOVER_", "EVALUATE_", "INSPECT_",
            "MODEL_", "RANK_", "SELECT_BEST", "SHOW_",
            "SPLIT_", "TRAIN_",
        )
        tools      = db_module.query_all(conn, SQL_LIST_TOOLS)
        user_tools = [t for t in tools
                      if not any(t["tool_name"].upper().startswith(p)
                                 for p in OML_PREFIXES)]
        oml_tools  = [t for t in tools if t not in user_tools]

        display.blank()
        print(f"  {display.C.BOLD}YOUR TOOLS  ({len(user_tools)}){display.C.RESET}")
        if user_tools:
            for t in user_tools:
                sc = display.C.GREEN if t["status"] == "ENABLED" else display.C.YELLOW
                print(f"  {sc}●{display.C.RESET}  {t['tool_name']:<40}  {t['status']}")
        else:
            print(f"  {display.C.DIM}  None found{display.C.RESET}")

        if oml_tools:
            display.blank()
            print(f"  {display.C.BOLD}{display.C.DIM}OML BUILT-IN TOOLS  ({len(oml_tools)}){display.C.RESET}")
            for t in oml_tools:
                sc = display.C.GREEN if t["status"] == "ENABLED" else display.C.YELLOW
                print(f"  {sc}●{display.C.RESET}  "
                      f"{display.C.DIM}{t['tool_name']:<40}  {t['status']}{display.C.RESET}")

        # Agents — name and status only
        agents = db_module.query_all(conn, SQL_LIST_AGENTS)
        display.blank()
        print(f"  {display.C.BOLD}AGENTS  ({len(agents)}){display.C.RESET}")
        if agents:
            for a in agents:
                sc = display.C.GREEN if a["status"] == "ENABLED" else display.C.YELLOW
                print(f"  {sc}●{display.C.RESET}  {a['agent_name']:<40}  {a['status']}")
        else:
            display.warn("  None found")

        # Tasks — name and status only
        tasks = db_module.query_all(conn, SQL_LIST_TASKS)
        display.blank()
        print(f"  {display.C.BOLD}TASKS  ({len(tasks)}){display.C.RESET}")
        if tasks:
            for t in tasks:
                sc = display.C.GREEN if t["status"] == "ENABLED" else display.C.YELLOW
                print(f"  {sc}●{display.C.RESET}  {t['task_name']:<40}  {t['status']}")
        else:
            display.warn("  None found")

        # Teams
        teams = db_module.query_all(conn, SQL_LIST_TEAMS)
        display.blank()
        print(f"  {display.C.BOLD}TEAMS  ({len(teams)}){display.C.RESET}")
        if teams:
            for t in teams:
                status_col = display.C.GREEN if t["status"] == "ENABLED" else display.C.YELLOW
                print(f"  {status_col}●{display.C.RESET}  {t['team_name']:<40}  {t['status']}")
        else:
            display.warn("  None found")

    finally:
        conn.close()


# ── Menu option 7: View object detail ────────────────────────────────────────

def run_view(cfg, display):
    display.head("VIEW OBJECT DETAIL")

    display.blank()
    print("  Select object type then enter its exact name:")
    print(f"  {'─'*55}")
    print("  1  Profile  — e.g. ACME_INSURANCE_RAG_PROFILE")
    print("  2  Tool     — e.g. ACME_SQL_TOOL, ACME_RAG_TOOL")
    print("  3  Agent    — e.g. ACME_ANALYST")
    print("  4  Task     — e.g. ACME_ANALYST_TASK")
    print("  5  Team     — e.g. ACME_ANALYST_TEAM")
    print("  6  History  — last 20 tool invocations (no name needed)")
    print("  b  Back")
    print("  q  Quit to main menu")
    display.blank()

    VALID = {"1": "Profile", "2": "Tool", "3": "Agent", "4": "Task",
             "5": "Team", "6": "History"}
    try:
        obj_type = input("  Type [1-6/b/q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if obj_type in ("b", "q"):
        return
    if obj_type not in VALID:
        display.err(f"Invalid choice '{obj_type}' — enter 1-6, b, or q")
        return

    obj_name = ""
    if obj_type != "6":
        try:
            obj_name = input(f"  {VALID[obj_type]} name: ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            return
        if not obj_name:
            display.warn("No name entered — cancelled")
            return

    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        if obj_type == "1":
            rows = db_module.query_all(conn, SQL_PROFILE_DETAIL, {"name": obj_name})
            display.blank()
            print(f"  {display.C.BOLD}PROFILE: {obj_name}{display.C.RESET}")
            if not rows:
                display.warn(f"No attributes found for profile '{obj_name}'")
            else:
                print(f"  {'─'*64}")
                for r in rows:
                    attr_name = r.get("attribute_name", "")
                    attr_val  = _lob_str(r.get("attribute_value", ""))
                    if len(attr_val) > 70:
                        print(f"  {display.C.CYAN}{attr_name:<25}{display.C.RESET}")
                        for line in attr_val.splitlines():
                            print(f"    {line}")
                    else:
                        print(f"  {display.C.CYAN}{attr_name:<25}{display.C.RESET}  {attr_val}")

            # Also show linked vector index details if referenced
            vi_row = db_module.query_one(conn, SQL_PROFILE_VECTOR_INDEX, {"name": obj_name})
            if vi_row and vi_row.get("vector_index_name"):
                vi_name = vi_row["vector_index_name"]
                display.blank()
                print(f"  {display.C.BOLD}LINKED VECTOR INDEX: {vi_name}{display.C.RESET}")
                print(f"  {'─'*64}")
                try:
                    # Try status view first
                    try:
                        st = db_module.query_one(conn, SQL_VECTOR_INDEX_STATUS, {"name": vi_name})
                        if st:
                            print(f"  {'Status':<25}  {st.get('status','')}")
                            print(f"  {'Documents':<25}  {st.get('num_documents','')}")
                            print(f"  {'Created':<25}  {st.get('created_at','')}")
                            print(f"  {'Updated':<25}  {st.get('updated_at','')}")
                            print(f"  {'─'*64}")
                    except Exception:
                        pass  # View may not exist — skip silently

                    # Try attribute detail view
                    try:
                        vi_attrs = db_module.query_all(conn, SQL_VECTOR_INDEX_DETAIL, {"name": vi_name})
                        for r in vi_attrs:
                            attr_name = r.get("attribute_name", "")
                            attr_val  = _lob_str(r.get("attribute_value", ""))
                            if len(attr_val) > 70:
                                print(f"  {display.C.CYAN}{attr_name:<25}{display.C.RESET}")
                                for line in attr_val.splitlines():
                                    print(f"    {line}")
                            else:
                                print(f"  {display.C.CYAN}{attr_name:<25}{display.C.RESET}  {attr_val}")
                        if not vi_attrs:
                            display.info("  No additional attributes available for this index")
                    except Exception:
                        display.info(f"  Vector index view not accessible — check USER_CLOUD_AI_VECTOR_INDEXES exists")
                except Exception as ex:
                    display.warn(f"Could not retrieve vector index details: {ex}")
            rows = []  # prevent double-display below
        elif obj_type == "2":
            rows = db_module.query_all(conn, SQL_TOOL_DETAIL, {"name": obj_name})
            display.blank()
            print(f"  {display.C.BOLD}TOOL: {obj_name}{display.C.RESET}")
        elif obj_type == "3":
            rows = db_module.query_all(conn, SQL_AGENT_DETAIL, {"name": obj_name})
            display.blank()
            print(f"  {display.C.BOLD}AGENT: {obj_name}{display.C.RESET}")
        elif obj_type == "4":
            rows = db_module.query_all(conn, SQL_TASK_DETAIL, {"name": obj_name})
            display.blank()
            print(f"  {display.C.BOLD}TASK: {obj_name}{display.C.RESET}")
        elif obj_type == "6":
            rows = db_module.query_all(conn, SQL_TOOL_HISTORY)
            display.blank()
            print(f"  {display.C.BOLD}TOOL INVOCATION HISTORY (last 20){display.C.RESET}")
            print(f"  {'Tool':<30}  {'Agent':<25}  {'Called At':<20}  Output")
            print(f"  {'─'*30}  {'─'*25}  {'─'*20}  {'─'*30}")
            for r in rows:
                print(f"  {r['tool_name']:<30}  {r['agent_name']:<25}  "
                      f"{r['called_at']:<20}  {r['output_preview']}")
            return
        elif obj_type == "5":
            rows = db_module.query_all(conn, SQL_TEAM_DETAIL, {"name": obj_name})
            display.blank()
            print(f"  {display.C.BOLD}TEAM: {obj_name}{display.C.RESET}")
        else:
            display.warn("Unknown type — use 1-6, b, or q")
            return

        if not rows:
            display.warn(f"No attributes found for '{obj_name}'")
        else:
            print(f"  {'─'*64}")
            for r in rows:
                attr_name = r.get("attribute_name", "")
                attr_val  = _lob_str(r.get("attribute_value", ""))
                if len(attr_val) > 70:
                    print(f"  {display.C.CYAN}{attr_name:<25}{display.C.RESET}")
                    for line in attr_val.splitlines():
                        print(f"    {line}")
                else:
                    print(f"  {display.C.CYAN}{attr_name:<25}{display.C.RESET}  {attr_val}")

    finally:
        conn.close()


# ── Menu option 8: Update tool instruction or agent role ─────────────────────

def run_update(cfg, display):
    display.head("UPDATE TOOL INSTRUCTION OR AGENT ROLE")

    display.blank()
    print("  What would you like to update?")
    print("  [1] Tool instruction  — e.g. ACME_SQL_TOOL, ACME_RAG_TOOL")
    print("  [2] Agent role        — e.g. ACME_ANALYST")
    print("  [3] Task instruction  — e.g. ACME_ANALYST_TASK")
    print("  b   Back")
    display.blank()

    try:
        choice = input("  Choice [1/2/3/b]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if choice in ("b", "q"):
        return
    if choice not in ("1", "2", "3"):
        display.err(f"Invalid choice '{choice}' — enter 1, 2, 3, or b")
        return

    type_label = {"1": "Tool", "2": "Agent", "3": "Task"}[choice]
    try:
        name = input(f"  {type_label} name: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if not name:
        display.warn("No name entered — cancelled")
        return

    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        if choice == "1":
            # Show current
            rows = db_module.query_all(conn, SQL_TOOL_DETAIL, {"name": name})
            instr = _lob_str(next((r["attribute_value"] for r in rows
                          if r["attribute_name"] == "instruction"), ""))
            display.blank()
            display.info(f"Current instruction: {instr[:200]}")
            display.blank()
            try:
                new_val = input("  New instruction (Enter to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not new_val:
                return
            sql = f"""
            BEGIN
                DBMS_CLOUD_AI_AGENT.UPDATE_TOOL(
                    tool_name  => '{name}',
                    attributes => '{{"instruction": "{new_val}"}}'
                );
            END;
            """
            ok, err_msg = db_module.execute(conn, sql)
            if ok:
                display.ok(f"Tool '{name}' instruction updated")
            else:
                display.err(f"Update failed: {err_msg}")

        elif choice == "2":
            rows = db_module.query_all(conn, SQL_AGENT_DETAIL, {"name": name})
            role = _lob_str(next((r["attribute_value"] for r in rows
                         if r["attribute_name"] == "role"), ""))
            display.blank()
            display.info(f"Current role: {role[:200]}")
            display.blank()
            try:
                new_val = input("  New role (Enter to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not new_val:
                return
            sql = f"""
            BEGIN
                DBMS_CLOUD_AI_AGENT.UPDATE_AGENT(
                    agent_name => '{name}',
                    attributes => '{{"role": "{new_val}"}}'
                );
            END;
            """
            ok, err_msg = db_module.execute(conn, sql)
            if ok:
                display.ok(f"Agent '{name}' role updated")
            else:
                display.err(f"Update failed: {err_msg}")

        elif choice == "3":
            rows = db_module.query_all(conn, SQL_TASK_DETAIL, {"name": name})
            instr = _lob_str(next((r["attribute_value"] for r in rows
                          if r["attribute_name"] == "instruction"), ""))
            display.blank()
            display.info(f"Current instruction: {instr[:200]}")
            display.blank()
            try:
                new_val = input("  New instruction (Enter to cancel): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not new_val:
                return
            sql = f"""
            BEGIN
                DBMS_CLOUD_AI_AGENT.UPDATE_TASK(
                    task_name  => '{name}',
                    attributes => '{{"instruction": "{new_val}"}}'
                );
            END;
            """
            ok, err_msg = db_module.execute(conn, sql)
            if ok:
                display.ok(f"Task '{name}' instruction updated")
            else:
                display.err(f"Update failed: {err_msg}")

        else:
            display.warn("Invalid choice")

    finally:
        conn.close()


# ── Menu option 9: Delete ─────────────────────────────────────────────────────

def run_delete(cfg, display):
    """Delete one or more Select AI Agent objects from the database.

    Supports:
      - Profiles (DBMS_CLOUD_AI.DROP_PROFILE)
      - Vector indexes (DBMS_CLOUD_AI.DROP_VECTOR_INDEX)
      - Tools, Agents, Tasks, Teams (DBMS_CLOUD_AI_AGENT.DROP_*)
      - Multi-select by comma list or number range
      - "Delete all from project" — lists only objects matching a common
        prefix so objects from other stacks are never touched.
    """
    display.head("DELETE OBJECTS")

    C = display.C

    # ── Fetch live object lists ───────────────────────────────────────────────
    conn = _get_connection(cfg, display)
    if not conn:
        return

    OML_PREFIXES = (
        "ANALYZE_", "AUTOMATED_", "BUILD_", "CREATE_VIEW",
        "DESCRIBE_", "DISCOVER_", "EVALUATE_", "INSPECT_",
        "MODEL_", "RANK_", "SELECT_BEST", "SHOW_",
        "SPLIT_", "TRAIN_",
    )

    try:
        profiles   = [r["profile_name"]  for r in db_module.query_all(conn, SQL_LIST_PROFILES)]
        all_tools  = db_module.query_all(conn, SQL_LIST_TOOLS)
        user_tools = [t["tool_name"] for t in all_tools
                      if not any(t["tool_name"].upper().startswith(p) for p in OML_PREFIXES)]
        agents     = [r["agent_name"]    for r in db_module.query_all(conn, SQL_LIST_AGENTS)]
        tasks      = [r["task_name"]     for r in db_module.query_all(conn, SQL_LIST_TASKS)]
        teams      = [r["team_name"]     for r in db_module.query_all(conn, SQL_LIST_TEAMS)]

        vi_rows  = []
        try:
            SQL_LIST_VECTOR_INDEXES = (
                "SELECT index_name FROM user_cloud_ai_vector_indexes ORDER BY index_name"
            )
            vi_rows = db_module.query_all(conn, SQL_LIST_VECTOR_INDEXES)
        except Exception:
            pass  # view not present in this ADW version — vector indexes skipped
        vi_names = [r["index_name"] for r in vi_rows]
    finally:
        conn.close()

    # ── Mode selection ────────────────────────────────────────────────────────
    display.blank()
    print(f"  {C.BOLD}DELETE OBJECTS — Select mode:{C.RESET}")
    print(f"   1. Pick objects from a numbered list (multi-select supported)")
    print(f"   2. Delete all objects matching a project prefix")
    print(f"   3. Enter names manually (one at a time)")
    print(f"   b. Cancel")
    display.blank()
    try:
        mode = input("  Mode [1/2/3/b]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return
    if mode in ("b", "q", ""):
        return

    # ─────────────────────────────────────────────────────────────────────────
    # MODE 1 — numbered list with multi-select
    # ─────────────────────────────────────────────────────────────────────────
    if mode == "1":
        # Build a combined numbered catalogue
        catalogue = []   # list of (display_label, type, name)
        for n in teams:
            catalogue.append(("TEAM", n))
        for n in tasks:
            catalogue.append(("TASK", n))
        for n in agents:
            catalogue.append(("AGENT", n))
        for n in user_tools:
            catalogue.append(("TOOL", n))
        for n in profiles:
            # Skip AGENT$ orphan profiles — they're shown with their team
            if not n.upper().startswith("AGENT$"):
                catalogue.append(("PROFILE", n))
        for n in vi_names:
            catalogue.append(("VECTOR INDEX", n))
        # Add orphan AGENT$ profiles at the end
        for n in profiles:
            if n.upper().startswith("AGENT$"):
                catalogue.append(("PROFILE (orphan)", n))

        if not catalogue:
            display.warn("No deletable objects found in the schema.")
            return

        display.blank()
        print(f"  {C.BOLD}Objects available for deletion:{C.RESET}")
        print(f"  {C.DIM}⚠  Recommended delete order: team → task → agent → tool → profile → vector index{C.RESET}")
        display.blank()
        for i, (typ, name) in enumerate(catalogue, 1):
            print(f"  {C.DIM}{i:>3}.{C.RESET}  {typ:<20}  {name}")

        display.blank()
        print(f"  {C.DIM}Enter numbers to delete — e.g.  3   or  1,4,7   or  1-5{C.RESET}")
        print(f"  {C.DIM}Enter q or leave blank to cancel.{C.RESET}")
        try:
            raw = input("  Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return
        if not raw or raw.lower() in ("q", "quit", "b", "cancel"):
            display.warn("Cancelled")
            return

        # Parse selection: comma list, ranges, or single
        selected_idx = set()
        for token in raw.replace(" ", "").split(","):
            if "-" in token:
                parts = token.split("-", 1)
                if parts[0].isdigit() and parts[1].isdigit():
                    selected_idx.update(range(int(parts[0]), int(parts[1]) + 1))
            elif token.isdigit():
                selected_idx.add(int(token))

        to_delete = []
        for idx in sorted(selected_idx):
            if 1 <= idx <= len(catalogue):
                to_delete.append(catalogue[idx - 1])
            else:
                display.warn(f"Skipping invalid index {idx}")

        if not to_delete:
            display.warn("No valid objects selected")
            return

        _confirm_and_delete(to_delete, cfg, display)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # MODE 2 — project prefix (delete all matching objects safely)
    # ─────────────────────────────────────────────────────────────────────────
    if mode == "2":
        display.blank()
        display.info("Enter the common prefix for this project's objects.")
        display.info("Example: for ACME_ANALYST_TEAM / ACME_ANALYST_TASK / ACME_ANALYST enter 'ACME_ANALYST'")
        display.info("Only objects whose names START WITH the prefix are shown — nothing else is touched.")
        display.blank()
        try:
            prefix = input("  Prefix (uppercase, or q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return
        if not prefix or prefix.lower() in ("q", "quit", "b", "cancel"):
            display.warn("Cancelled")
            return
        prefix = prefix.upper()

        # Find all matching objects in drop order
        matching = []
        for n in teams:
            if n.upper().startswith(prefix):
                matching.append(("TEAM", n))
        for n in tasks:
            if n.upper().startswith(prefix):
                matching.append(("TASK", n))
        for n in agents:
            if n.upper().startswith(prefix):
                matching.append(("AGENT", n))
        for n in user_tools:
            if n.upper().startswith(prefix):
                matching.append(("TOOL", n))
        for n in profiles:
            if n.upper().startswith(prefix) or n.upper().startswith(f"AGENT${prefix}"):
                matching.append(("PROFILE", n))
        for n in vi_names:
            if n.upper().startswith(prefix):
                matching.append(("VECTOR INDEX", n))

        if not matching:
            display.warn(f"No objects found with prefix '{prefix}'")
            return

        display.blank()
        print(f"  {C.BOLD}Objects matching prefix '{prefix}':{C.RESET}")
        for typ, name in matching:
            print(f"    {typ:<20}  {name}")
        display.blank()

        _confirm_and_delete(matching, cfg, display)
        return

    # ─────────────────────────────────────────────────────────────────────────
    # MODE 3 — manual entry (original single-object behaviour, now with profiles)
    # ─────────────────────────────────────────────────────────────────────────
    if mode == "3":
        display.blank()
        print("  Object types:")
        print("   1.  Team              (DBMS_CLOUD_AI_AGENT.DROP_TEAM + AGENT$ orphan)")
        print("   2.  Task              (DBMS_CLOUD_AI_AGENT.DROP_TASK)")
        print("   3.  Agent             (DBMS_CLOUD_AI_AGENT.DROP_AGENT)")
        print("   4.  Tool              (DBMS_CLOUD_AI_AGENT.DROP_TOOL)")
        print("   5.  Profile           (DBMS_CLOUD_AI.DROP_PROFILE)")
        print("   6.  Vector Index      (DBMS_CLOUD_AI.DROP_VECTOR_INDEX)")
        print(f"  {C.DIM}⚠  Delete order: team → task → agent → tool → profile → vector index{C.RESET}")
        display.blank()
        try:
            obj_type_raw = input("  Type [1-6, q=cancel]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return
        if not obj_type_raw or obj_type_raw in ("q", "quit", "b", "cancel"):
            display.warn("Cancelled")
            return
        try:
            obj_name = input("  Name (uppercase, or q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return
        if not obj_name or obj_name.lower() in ("q", "quit", "b", "cancel"):
            display.warn("Cancelled")
            return
        obj_name = obj_name.upper()

        type_map = {
            "1": "TEAM", "2": "TASK", "3": "AGENT",
            "4": "TOOL", "5": "PROFILE", "6": "VECTOR INDEX",
        }
        typ = type_map.get(obj_type_raw)
        if not typ:
            display.warn("Invalid type — cancelled")
            return

        _confirm_and_delete([(typ, obj_name)], cfg, display)
        return

    display.warn("Unknown mode — cancelled")


def _confirm_and_delete(objects: list, cfg, display) -> None:
    """Show a confirmation prompt then execute drops in the given order."""
    C = display.C
    display.blank()
    print(f"  {C.YELLOW}⚠  The following objects will be deleted:{C.RESET}")
    for typ, name in objects:
        print(f"    {typ:<20}  {name}")
    display.blank()
    try:
        confirm = input(
            f"  {C.YELLOW}Proceed? [y/N]:{C.RESET} "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if confirm not in ("y", "yes"):
        display.warn("Cancelled — nothing deleted")
        return

    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        for typ, name in objects:
            _drop_one(conn, typ, name, display)
    finally:
        conn.close()


def _drop_one(conn, typ: str, name: str, display) -> None:
    """Execute the correct DROP call for a single object type."""
    C = display.C
    name_q = name.replace("'", "''")

    if typ == "TEAM":
        sqls = [
            (f"Team '{name}'",           f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_TEAM('{name_q}'); END;"),
            (f"Orphan profile 'AGENT${name}'",
             f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('AGENT${name_q}', force => TRUE); END;"),
        ]
    elif typ == "TASK":
        sqls = [(f"Task '{name}'", f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_TASK('{name_q}'); END;")]
    elif typ == "AGENT":
        sqls = [(f"Agent '{name}'", f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_AGENT('{name_q}'); END;")]
    elif typ == "TOOL":
        sqls = [(f"Tool '{name}'", f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_TOOL('{name_q}'); END;")]
    elif typ == "PROFILE" or typ == "PROFILE (orphan)":
        sqls = [(f"Profile '{name}'",
                 f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('{name_q}', force => TRUE); END;")]
    elif typ == "VECTOR INDEX":
        sqls = [(f"Vector index '{name}'",
                 f"BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX('{name_q}', force => TRUE); END;")]
    else:
        display.warn(f"Unknown type '{typ}' — skipped")
        return

    for label, sql in sqls:
        ok, err_msg = db_module.execute(conn, sql, ignore_errors=True)
        if ok:
            display.ok(f"Dropped: {label}")
        else:
            display.warn(f"Drop skipped or failed: {label} — {err_msg}")


# ── Menu option 10: Rebuild agent stack ───────────────────────────────────────

def run_rebuild(cfg, display):
    display.head("REBUILD AGENT STACK")
    display.blank()

    display.info("This drops and recreates the agent, task, and team.")
    display.info("Profiles, vector index, and tools are NOT affected.")
    display.blank()

    try:
        team_name  = input("  Team name (e.g. ACME_ANALYST_TEAM): ").strip().upper()
        task_name  = input("  Task name (e.g. ACME_ANALYST_TASK): ").strip().upper()
        agent_name = input("  Agent name (e.g. ACME_ANALYST): ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if not all([team_name, task_name, agent_name]):
        display.warn("All names required")
        return

    # Get current attribute values to preserve them
    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        agent_rows = db_module.query_all(
            conn, SQL_AGENT_DETAIL, {"name": agent_name})
        task_rows  = db_module.query_all(
            conn, SQL_TASK_DETAIL, {"name": task_name})

        agent_attrs = {r["attribute_name"]: r["attribute_value"]
                       for r in agent_rows}
        task_attrs  = {r["attribute_name"]: r["attribute_value"]
                       for r in task_rows}

        profile  = agent_attrs.get("profile_name", "")
        role     = agent_attrs.get("role", "").replace("'", "''")
        tools    = agent_attrs.get("tools", "[]")
        instr    = task_attrs.get("instruction", "").replace("'", "''")

        display.blank()
        display.info(f"Agent profile : {profile}")
        display.info(f"Tools         : {tools[:80]}")
        display.info(f"Role preview  : {role[:80]}...")
        display.info(f"Instruction   : {instr[:80]}...")
        display.blank()

        try:
            confirm = input(
                f"  {display.C.YELLOW}Proceed with rebuild? [y/N]:{display.C.RESET} "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if confirm not in ("y", "yes"):
            display.warn("Cancelled")
            return

        # Drop sequence
        drops = [
            f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_TEAM('{team_name}'); EXCEPTION WHEN OTHERS THEN NULL; END;",
            f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('AGENT${team_name}', force=>TRUE); EXCEPTION WHEN OTHERS THEN NULL; END;",
            f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_TASK('{task_name}'); EXCEPTION WHEN OTHERS THEN NULL; END;",
            f"BEGIN DBMS_CLOUD_AI_AGENT.DROP_AGENT('{agent_name}'); EXCEPTION WHEN OTHERS THEN NULL; END;",
        ]
        for sql in drops:
            db_module.execute(conn, sql, ignore_errors=True)
        display.ok("Existing stack dropped")

        # Recreate agent
        sql_agent = f"""
        BEGIN
            DBMS_CLOUD_AI_AGENT.CREATE_AGENT(
                agent_name  => '{agent_name}',
                attributes  => '{{
                    "profile_name"      : "{profile}",
                    "role"              : "{role}",
                    "enable_human_tool" : "False",
                    "tools"             : {tools}
                }}'
            );
        END;
        """
        ok, err = db_module.execute(conn, sql_agent)
        display.ok(f"Agent '{agent_name}' created") if ok else display.err(err)

        # Recreate task
        sql_task = f"""
        BEGIN
            DBMS_CLOUD_AI_AGENT.CREATE_TASK(
                task_name   => '{task_name}',
                attributes  => '{{"instruction": "{instr}"}}'
            );
        END;
        """
        ok, err = db_module.execute(conn, sql_task)
        display.ok(f"Task '{task_name}' created") if ok else display.err(err)

        # Recreate team
        sql_team = f"""
        BEGIN
            DBMS_CLOUD_AI_AGENT.CREATE_TEAM(
                team_name   => '{team_name}',
                attributes  => '{{
                    "agents"  : [{{"name": "{agent_name}", "task": "{task_name}"}}],
                    "process" : "sequential"
                }}'
            );
        END;
        """
        ok, err = db_module.execute(conn, sql_team)
        display.ok(f"Team '{team_name}' created") if ok else display.err(err)

    finally:
        conn.close()


# ── Menu option: Update profile attribute ──────────────────────────────────────

_COMMON_ATTRS = [
    ("max_tokens",   "Max tokens for LLM responses — increase if responses are truncated (e.g. 4000)"),
    ("temperature",  "LLM temperature — lower = more deterministic SQL (e.g. 0.1 for SQL, 0.3 for RAG)"),
    ("model",        "Chat model override (e.g. xai.grok-4-1-fast-non-reasoning)"),
    ("comments",     "Use table/column comments for NL2SQL — true or false"),
    ("conversation", "Enable multi-turn conversation mode — true or false"),
]

def run_update_profile(cfg, display):
    display.head("UPDATE PROFILE ATTRIBUTE")

    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        # List existing profiles
        rows = db_module.query_all(conn, """
            SELECT profile_name, status
            FROM   user_cloud_ai_profiles
            ORDER  BY profile_name
        """)
        if not rows:
            display.warn("No profiles found in this schema")
            return

        print()
        for i, r in enumerate(rows, 1):
            print(f"  {i:2}. {r['profile_name']:<40} {r['status']}")
        print()
        try:
            raw = input("  Profile number (or q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if raw.lower() in ("q", ""):
            return
        try:
            profile_name = rows[int(raw) - 1]["profile_name"]
        except (ValueError, IndexError):
            display.warn("Invalid selection")
            return

        # Show current attributes for this profile
        attrs = db_module.query_all(conn, """
            SELECT attribute_name, attribute_value
            FROM   user_cloud_ai_profile_attributes
            WHERE  profile_name = :name
            ORDER  BY attribute_name
        """, {"name": profile_name})
        if attrs:
            print(f"\n  Current attributes for {profile_name}:")
            for a in attrs:
                print(f"    {a['attribute_name']:<30} = {a['attribute_value']}")
        print()

        # Suggest common attributes
        print("  Common attributes to update:")
        for i, (attr, desc) in enumerate(_COMMON_ATTRS, 1):
            print(f"    {i}. {attr:<15} — {desc}")
        print()
        try:
            attr_input = input("  Attribute name (or number from list above): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not attr_input or attr_input.lower() == "q":
            return
        try:
            attr_name = _COMMON_ATTRS[int(attr_input) - 1][0]
        except (ValueError, IndexError):
            attr_name = attr_input.upper()

        try:
            attr_value = input(f"  New value for {attr_name}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not attr_value:
            display.warn("Value required")
            return

        # DBMS_CLOUD_AI.UPDATE_PROFILE does not exist in this ADW version.
        # The only way to change a profile attribute is to drop and recreate it
        # with the full attribute set, merging the existing values with the update.
        # We read all current attributes from user_cloud_ai_profile_attributes,
        # overlay the changed value, then drop + recreate.

        import json as _json

        # Build updated attribute dict from current values
        attr_dict = {}
        for a in attrs:
            k = a["attribute_name"]
            v = a["attribute_value"]
            # object_list is stored as JSON — keep as parsed object, not string
            if k == "object_list":
                try:
                    attr_dict[k] = _json.loads(v)
                except Exception:
                    attr_dict[k] = v
            else:
                # Coerce numeric-looking values back to numbers for valid JSON
                try:
                    attr_dict[k] = int(v)
                except ValueError:
                    try:
                        attr_dict[k] = float(v)
                    except ValueError:
                        attr_dict[k] = v

        # Apply the update
        key_lower = attr_name.lower()
        try:
            attr_dict[key_lower] = int(attr_value)
        except ValueError:
            try:
                attr_dict[key_lower] = float(attr_value)
            except ValueError:
                attr_dict[key_lower] = attr_value

        # Confirm before dropping
        print()
        print(f"  This will DROP and RECREATE {profile_name} with:")
        print(f"    {key_lower} = {attr_value}")
        try:
            confirm = input("  Proceed? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if confirm not in ("", "y", "yes"):
            display.info("Cancelled")
            return

        attrs_json = _json.dumps(attr_dict, indent=2)

        # Drop existing profile
        ok, err = db_module.execute(conn,
            f"BEGIN DBMS_CLOUD_AI.DROP_PROFILE('{profile_name}', force=>TRUE); END;")
        if not ok:
            display.err(f"Drop failed: {err}")
            return

        # Recreate with updated attributes
        # JSON must use single-quote-escaped string for the PL/SQL literal
        attrs_plsql = attrs_json.replace("'", "''")
        ok, err = db_module.execute(conn, f"""
            BEGIN
                DBMS_CLOUD_AI.CREATE_PROFILE(
                    profile_name => '{profile_name}',
                    attributes   => '{attrs_plsql}'
                );
            END;
        """)
        if ok:
            display.ok(f"{profile_name} recreated — {key_lower} = {attr_value}")
        else:
            display.err(f"Recreate failed: {err}")
            display.warn("Profile was dropped — run Rebuild to restore it fully")

    except Exception as ex:
        display.err(f"Error: {ex}")
    finally:
        conn.close()


def _explain_prompt_error(ex: Exception, display) -> None:
    """Translate Oracle agent runtime errors into plain-English guidance."""
    msg = str(ex)

    # ORA-20404 — model endpoint not found (wrong model ID or not in region)
    if "ORA-20404" in msg and "inference.generativeai" in msg:
        import re
        url_match = re.search(r"https://[^\s]+", msg)
        url_hint  = f"\n  URL: {url_match.group()}" if url_match else ""
        display.err("Model not found on OCI GenAI")
        print(
            f"  The LLM model set on the profile does not exist at this endpoint.{url_hint}\n"
            f"  Most likely causes:\n"
            f"    • Wrong model ID (e.g. 'meta.llama-3-70b' instead of 'meta.llama-3.3-70b-instruct')\n"
            f"    • Model not available in your region (us-chicago-1)\n"
            f"  Fix: use Main Menu → 5 (View available models) to get the exact ID\n"
            f"       then Review & Manage → 4 (Update profile attribute) → model"
        )

    # ORA-20053 / ORA-20051 — agent job / task failure wrapper
    elif "ORA-20053" in msg or "ORA-20051" in msg:
        import re
        all_ora = re.findall(r"ORA-\d+:[^\n]+", msg)
        specific = [line for line in all_ora if not line.strip().startswith("ORA-06512")]
        root = specific[0].strip() if specific else msg.strip()[:500]
        display.err("Agent task failed")
        print(f"  {root}")

    # ORA-40441 — JSON syntax error (malformed LLM response or params)
    elif "ORA-40441" in msg:
        display.err("JSON error in agent response")
        print(
            f"  Oracle could not parse a JSON payload in the agent pipeline.\n"
            f"  This usually means the LLM returned a partial/malformed response,\n"
            f"  often caused by the model hitting its token limit mid-generation.\n"
            f"  Fix: increase max_tokens via Review & Manage → 4 (Update profile attribute)"
        )

    # ORA-20050 — conversation ID issues
    elif "ORA-20050" in msg:
        display.err("Conversation ID error")
        print(
            f"  The conversation session was rejected by Oracle.\n"
            f"  The session may have expired or the conversation ID is invalid.\n"
            f"  Fix: exit and restart the test session (type 'exit' then re-enter option 8)"
        )

    # ORA-01400 — NULL not allowed (missing required field)
    elif "ORA-01400" in msg:
        display.err("Required field missing in request")
        print(
            f"  Oracle rejected the call due to a NULL value in a required field.\n"
            f"  This is most likely the conversation_id — a known issue that should\n"
            f"  not occur in this version. Exit and restart the test session."
        )

    # Generic fallback — still show the error but formatted more cleanly
    else:
        display.err("Prompt failed")
        # Strip Oracle stack trace cruft and show only meaningful lines
        lines = [l.strip() for l in msg.splitlines()
                 if l.strip() and "ORA-06512" not in l and "DBMS_CLOUD" not in l]
        for line in lines[:5]:
            print(f"  {line}")
        if len(lines) > 5:
            print(f"  ... ({len(lines) - 5} more lines)")


# ── Menu option 11: Run test prompt ───────────────────────────────────────────

def run_test(cfg, display):
    display.head("TEST — RUN PROMPT AGAINST A TEAM")

    try:
        team_name = input("  Team name: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if not team_name:
        display.warn("Team name required")
        return

    conn = _get_connection(cfg, display)
    if not conn:
        return

    try:
        # Create one conversation for the whole session — all turns share it
        # so the agent maintains multi-turn context. DBMS_CLOUD_AI.CREATE_CONVERSATION
        # (not DBMS_CLOUD_AI_AGENT) returns the hyphenated UUID format RUN_TEAM requires.
        conv_sql = "SELECT DBMS_CLOUD_AI.CREATE_CONVERSATION AS conversation_id FROM dual"
        conv_row = db_module.query_one(conn, conv_sql)
        if not conv_row or not conv_row.get("conversation_id"):
            display.err("Could not create conversation — check EXECUTE privilege on DBMS_CLOUD_AI")
            return
        conv_id = conv_row["conversation_id"]
        display.ok(f"Session started — team: {team_name}  (type 'exit' or 'quit' to end)")
        display.blank()

        while True:
            try:
                prompt = input(f"  {display.C.BOLD}You:{display.C.RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            # input() bypasses the Tee stdout logger, so what's typed never
            # reaches the session log file on its own — echo it explicitly.
            # Same gap found and fixed earlier in modules/conversation.py.
            if prompt:
                import sys as _sys
                if hasattr(_sys.stdout, "write_log_only"):
                    _sys.stdout.write_log_only(f"{prompt}\n")

            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit", "q", "bye", "done"):
                break

            try:
                import json as _json
                display.blank()

                # Use SELECT FROM DUAL with bind variables for user_prompt and
                # params. This avoids:
                #   • CLOB truncation from PL/SQL OUT variable maxSize limits
                #   • ORA-40441 JSON errors (params built by json.dumps, not
                #     string concatenation, so special chars are properly escaped)
                #   • SQL injection (prompt is a bind variable, not inlined)
                params_json = _json.dumps({"conversation_id": conv_id})
                sql = (
                    f"SELECT DBMS_CLOUD_AI_AGENT.RUN_TEAM("
                    f"team_name => '{team_name}', "
                    f"user_prompt => :prompt, "
                    f"params => :params"
                    f") AS response FROM dual"
                )
                row = db_module.query_one(conn, sql,
                                          {"prompt": prompt, "params": params_json})
                response = (row.get("response") or "No response") if row else "No response"

                print(f"  {display.C.CYAN}Agent:{display.C.RESET}")
                print(f"  {'─' * 64}")
                for line in str(response).splitlines():
                    print(f"  {line}")
                print(f"  {'─' * 64}")

                # Tool history — 30-second window (no conversation_id column)
                display.blank()
                hist_sql = (
                    "SELECT tool_name, TO_CHAR(start_date, 'HH24:MI:SS') AS called_at, tool_output "
                    "FROM user_ai_agent_tool_history "
                    "WHERE start_date >= SYSTIMESTAMP - INTERVAL '30' SECOND "
                    "ORDER BY start_date"
                )
                hist = db_module.query_all(conn, hist_sql)
                if hist:
                    print(f"  {display.C.DIM}Tools invoked:{display.C.RESET}")
                    for h in hist:
                        raw_out = h.get("tool_output")
                        if raw_out is None:
                            out_preview = ""
                        elif hasattr(raw_out, "read"):
                            out_preview = (raw_out.read(300) or "").strip()
                        else:
                            out_preview = str(raw_out)[:300].strip()
                        out_preview = out_preview.replace("\n", " ")
                        print(f"  {display.C.DIM}  [{h['called_at']}] {h['tool_name']}{display.C.RESET}")
                        if out_preview:
                            print(f"  {display.C.DIM}  -> {out_preview[:180]}{display.C.RESET}")
                display.blank()

            except Exception as ex:
                _explain_prompt_error(ex, display)
                display.blank()

        display.info("Session ended")

    except Exception as ex:
        display.err(f"Test session failed: {ex}")
    finally:
        conn.close()
