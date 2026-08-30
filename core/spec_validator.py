"""
core/spec_validator.py
Validation for the current Select AI Agent Builder spec shape.

The validator intentionally treats Python-captured discovery facts as the source
of truth. This prevents the LLM from silently dropping table names, renaming
objects, or changing team/task references during final spec generation.
"""

class SpecValidationError(Exception):
    pass


def _names(items):
    return [i.get("name") for i in items if isinstance(i, dict) and i.get("name")]


def _types(items):
    return [str(i.get("type", "")).upper() for i in items if isinstance(i, dict)]


def validate_spec(spec, facts=None):
    facts = facts or {}
    required_sections = [
        "project_name", "schema", "profiles", "tools", "agents", "tasks", "teams"
    ]
    for sec in required_sections:
        if sec not in spec:
            raise SpecValidationError(f"Missing section: {sec}")

    for sec in ["profiles", "tools", "agents", "tasks", "teams"]:
        if not isinstance(spec.get(sec), list):
            raise SpecValidationError(f"Section must be a list: {sec}")

    if not spec.get("agents"):
        raise SpecValidationError("At least one agent is required")
    if not spec.get("tasks"):
        raise SpecValidationError("At least one task is required")
    if not spec.get("teams"):
        raise SpecValidationError("At least one team is required")

    profile_types = _types(spec.get("profiles", []))
    tool_types = _types(spec.get("tools", []))
    data_source = facts.get("data_source")

    if data_source in ("documents", "both"):
        if "RAG" not in profile_types:
            raise SpecValidationError("RAG data source requires a RAG profile")
        if "RAG" not in tool_types:
            raise SpecValidationError("RAG data source requires a RAG tool")
        if not spec.get("vector_indexes"):
            raise SpecValidationError("RAG data source requires vector_indexes")

    if data_source in ("tables", "both"):
        if "NL2SQL" not in profile_types:
            raise SpecValidationError("SQL data source requires an NL2SQL profile")
        if "SQL" not in tool_types:
            raise SpecValidationError("SQL data source requires a SQL tool")
        expected_tables = facts.get("sql", {}).get("tables", [])
        if not expected_tables:
            raise SpecValidationError("SQL data source selected but no table list was captured")
        nl2sql = [p for p in spec.get("profiles", []) if str(p.get("type", "")).upper() == "NL2SQL"]
        if not nl2sql:
            raise SpecValidationError("Expected NL2SQL profile with object list")
        for p in nl2sql:
            tables = p.get("tables") or p.get("object_list") or []
            if tables != expected_tables:
                raise SpecValidationError(
                    "NL2SQL table/object list changed. "
                    f"Expected {expected_tables}, found {tables}"
                )


    # Optional comments metadata must not introduce unknown table names.
    comments = facts.get("sql", {}).get("comments", {}) or {}
    selected_tables = set()
    for t in facts.get("sql", {}).get("tables", []) or []:
        selected_tables.add(str(t).upper().split(".")[-1])
    for key in (comments.get("objects") or {}).keys():
        table = str(key).upper().split(".")[-1]
        if selected_tables and table not in selected_tables:
            raise SpecValidationError(
                f"Comment metadata references table {key}, which is not in the selected SQL object list"
            )

    # Cross-reference validation.
    profile_names = set(_names(spec.get("profiles", [])))
    tool_names = set(_names(spec.get("tools", [])))
    agent_names = set(_names(spec.get("agents", [])))
    task_names = set(_names(spec.get("tasks", [])))

    for tool in spec.get("tools", []):
        ttype = str(tool.get("type", "")).upper()
        if ttype in ("RAG", "SQL") and tool.get("profile_name") not in profile_names:
            raise SpecValidationError(
                f"Tool {tool.get('name')} references missing profile {tool.get('profile_name')}"
            )

    nl2sql_profile_names = {p.get("name") for p in spec.get("profiles", []) if str(p.get("type", "")).upper() == "NL2SQL"}

    for agent in spec.get("agents", []):
        for tool_name in agent.get("tools", []):
            if tool_name not in tool_names:
                raise SpecValidationError(
                    f"Agent {agent.get('name')} references missing tool {tool_name}"
                )
        if data_source in ("tables", "both") and nl2sql_profile_names:
            if agent.get("profile_name") not in nl2sql_profile_names:
                raise SpecValidationError(
                    f"Agent {agent.get('name')} must reference the NL2SQL profile in profile_name"
                )
        role = agent.get("role", "")
        if len(str(role).split()) < 3:
            raise SpecValidationError(f"Agent {agent.get('name')} role is too short")

    for task in spec.get("tasks", []):
        if task.get("agent_name") not in agent_names:
            raise SpecValidationError(
                f"Task {task.get('name')} references missing agent {task.get('agent_name')}"
            )
        if not task.get("instruction"):
            raise SpecValidationError(f"Task {task.get('name')} missing instruction")
        for tool_name in task.get("tools", []):
            if tool_name not in tool_names:
                raise SpecValidationError(
                    f"Task {task.get('name')} references missing tool {tool_name}"
                )

    for team in spec.get("teams", []):
        if str(team.get("process", "sequential")).lower() != "sequential":
            raise SpecValidationError("Team process must be sequential")
        for member in team.get("agents", []):
            if member.get("name") not in agent_names:
                raise SpecValidationError(
                    f"Team {team.get('name')} references missing agent {member.get('name')}"
                )
            if member.get("task") not in task_names:
                raise SpecValidationError(
                    f"Team {team.get('name')} references missing task {member.get('task')}"
                )

    # Canonical Step 6/7 names must not be renamed by the LLM.
    task_facts = facts.get("task", {})
    if task_facts:
        if task_facts.get("agent_name") and task_facts["agent_name"] not in agent_names:
            raise SpecValidationError("Agent name differs from Step 6 canonical value")
        if task_facts.get("task_name") and task_facts["task_name"] not in task_names:
            raise SpecValidationError("Task name differs from Step 7 canonical value")
        if task_facts.get("team_name") and task_facts["team_name"] not in set(_names(spec.get("teams", []))):
            raise SpecValidationError("Team name differs from Step 7 canonical value")

    return True
