"""
core/spec_builder.py
Canonical facts and deterministic spec generation for the Select AI Agent Builder.

The LLM is useful for conversation, but it should not be the source of truth for
object names, table lists, or final object references. This module keeps those
values in project["facts"] and builds a spec from them deterministically.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from core import config as cfg_module


_VALID_SOURCES = {"documents", "tables", "both"}


def db_name(value: str, default: str = "OBJECT") -> str:
    """Return a safe uppercase Oracle object name."""
    value = (value or "").strip().upper()
    value = re.sub(r"[^A-Z0-9_]+", "_", value).strip("_")
    return value or default


def project_prefix(project: dict) -> str:
    raw = project.get("display_name") or project.get("project_name") or "PROJECT"
    return db_name(raw, "PROJECT")


def parse_table_list(text: str) -> list[str]:
    """Parse user-entered table/object names while preserving canonical names."""
    if not text:
        return []
    # Prefer comma/newline/semicolon separated lists, but also tolerate spaces.
    normalized = text.replace("\n", ",").replace(";", ",")
    parts = []
    for chunk in normalized.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # If a chunk contains multiple obvious DB object tokens, keep each.
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*)?", chunk)
        if len(tokens) > 1 and " " in chunk:
            parts.extend(tokens)
        else:
            parts.append(chunk)

    cleaned = []
    seen = set()
    for p in parts:
        # Remove common prose if a full sentence slipped in.
        p = p.strip().strip("`'\"")
        if not p:
            continue
        # Keep OWNER.TABLE if supplied, but canonicalize each part.
        if "." in p:
            pieces = [db_name(x) for x in p.split(".") if x.strip()]
            name = ".".join(pieces)
        else:
            name = db_name(p)
        if name and name not in seen:
            cleaned.append(name)
            seen.add(name)
    return cleaned


def parse_file_types(text: str) -> list[str]:
    if not text:
        return []
    tokens = re.split(r"[,/;\s]+", text.strip())
    out = []
    seen = set()
    for t in tokens:
        t = re.sub(r"[^A-Za-z0-9]+", "", t).upper()
        if t and t not in seen:
            out.append(t)
            seen.add(t)
    return out


def detect_data_source(text: str) -> str | None:
    """Detect data source from user free text.

    Ordering matters: check 'both' first so phrases like 'SQL and RAG' or
    'tables and documents' don't accidentally match the single-source branches.
    Single-source keywords use word-boundary matching where possible to avoid
    false positives (e.g. 'database-backed system' when the user means documents).
    """
    import re as _re
    t = (text or "").lower()
    # Both — must check before individual sources
    if any(w in t for w in ("both", "sql and rag", "rag and sql",
                             "documents and tables", "tables and documents",
                             "nl2sql and rag")):
        return "both"
    # Tables / SQL — word-boundary match to reduce false positives
    if _re.search(r"\b(tables?|nl2sql|database queries|db queries)\b", t):
        return "tables"
    # Documents / RAG
    if _re.search(r"\b(documents?|rag|pdfs?|files?|knowledge base|object storage)\b", t):
        return "documents"
    # Broader fallback — lower priority
    if any(w in t for w in ("sql", "db")):
        return "tables"
    return None


def ensure_facts(project: dict) -> dict:
    """Ensure project["facts"] exists and has all expected sub-sections."""
    # Backward compatibility with earlier private key if present.
    if "facts" not in project and "_facts" in project:
        project["facts"] = project.get("_facts") or {}

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


def default_names(project: dict, data_source: str | None = None) -> dict[str, Any]:
    prefix = project_prefix(project)
    data_source = data_source or ensure_facts(project).get("data_source")
    is_rag = data_source in (None, "documents", "both")
    is_sql = data_source in ("tables", "both")
    names: dict[str, Any] = {}
    if is_rag:
        names.update({
            "rag_profile": f"{prefix}_RAG_PROFILE",
            "vector_index": f"{prefix}_VECTOR_IDX",
            "chunk_size": 1024,
            "chunk_overlap": 128,
            "rag_tool": f"{prefix}_RAG_TOOL",
        })
    if is_sql:
        names.update({
            "nl2sql_profile": f"{prefix}_NL2SQL_PROFILE",
            "sql_tool": f"{prefix}_SQL_TOOL",
        })
    names.update({
        "agent_name": f"{prefix}_AGENT",
        "task_name": f"{prefix}_TASK",
        "team_name": f"{prefix}_TEAM",
    })
    return names


def facts_summary(project: dict) -> str:
    facts = ensure_facts(project)
    return (
        "CURRENT_CANONICAL_FACTS\n"
        f"data_source={facts.get('data_source')}\n"
        f"rag.subject={facts['rag'].get('subject')}\n"
        f"rag.file_types={facts['rag'].get('file_types')}\n"
        f"rag.object_storage_url={facts['rag'].get('object_storage_url')}\n"
        f"sql.tables={facts['sql'].get('tables')}\n"
        f"sql.question_types={facts['sql'].get('question_types')}\n"
        f"names={facts.get('names')}\n"
        f"agent_role={facts.get('agent_role')}\n"
        f"task={facts.get('task')}\n"
    )


def record_answer(project: dict, step: int, user_input: str, last_assistant: str = "") -> dict:
    """Capture important user answers into canonical project facts.

    Steps handled here: 2 (data source), 3 (RAG/SQL details), 5 (analysis tools),
    6 (agent role).

    Step 4 (object names, chunk size, chunk overlap) is intentionally NOT handled
    here — those values are captured directly in conversation.py _run_step4() and
    written into facts["names"] by the application, bypassing the LLM.  Calling
    record_answer for Step 4 input is a no-op by design.

    Step 7 (task instruction, task name, team name) is handled entirely by
    _run_step7_finalize() in conversation.py and written directly into
    facts["task"].  Same rationale: application owns Step 7 to prevent LLM drift.
    """
    facts = ensure_facts(project)
    answer = (user_input or "").strip()
    prompt = (last_assistant or "").lower()

    if not answer:
        return facts

    if step == 2:
        detected = detect_data_source(answer)
        if detected:
            facts["data_source"] = detected
        return facts

    if step == 3:
        # Prompt-based capture is more reliable than turn-counting because Step 3
        # differs for documents/tables/both and can be resumed.
        if "subject matter" in prompt or "subject" in prompt:
            facts["rag"]["subject"] = answer
        elif "file type" in prompt or "file types" in prompt:
            facts["rag"]["file_types"] = parse_file_types(answer)
        elif "object storage" in prompt or "storage url" in prompt or "where the documents" in prompt or "url" in prompt:
            facts["rag"]["object_storage_url"] = answer
            project["rag_url"] = answer
        elif "which tables" in prompt or "agent query" in prompt or "table names" in prompt:
            tables = parse_table_list(answer)
            if tables:
                facts["sql"]["tables"] = tables
        elif "what kinds of questions" in prompt or "questions will users ask" in prompt or "questions" in prompt:
            facts["sql"]["question_types"] = answer
        else:
            # Fallback: if the answer looks like a comma-separated table list, keep it.
            tables = parse_table_list(answer)
            if len(tables) >= 2 or re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", answer):
                facts["sql"]["tables"] = tables
        return facts

    if step == 5:
        low = answer.lower()
        if low in {"no", "n", "none", "skip", "nope", "not needed"}:
            facts["analysis_tools"] = []
        else:
            facts["analysis_tools"] = [{"type": "CUSTOM", "description": answer}]
        return facts

    if step == 6:
        facts["agent_role"] = answer
        return facts

    return facts


def build_spec_from_project(project: dict, cfg=None) -> dict:
    """Build a deterministic Select AI Agent spec from canonical project facts."""
    facts = ensure_facts(project)
    data_source = facts.get("data_source") or "documents"
    if data_source not in _VALID_SOURCES:
        data_source = "documents"

    schema = project.get("schema") or (cfg_module.get(cfg, "database", "db_user", fallback="") if cfg is not None else "")
    names = default_names(project, data_source)
    names.update({k: v for k, v in (facts.get("names") or {}).items() if v not in (None, "")})
    task_facts = facts.get("task") or {}
    agent_name = db_name(task_facts.get("agent_name") or names.get("agent_name"), names["agent_name"])
    task_name = db_name(task_facts.get("task_name") or names.get("task_name"), names["task_name"])
    team_name = db_name(task_facts.get("team_name") or names.get("team_name"), names["team_name"])

    is_rag = data_source in ("documents", "both")
    is_sql = data_source in ("tables", "both")

    spec: dict[str, Any] = {
        "project_name": project.get("display_name") or project.get("project_name") or "Agent Project",
        "schema": schema,
        "data_source": data_source,
        "comments": facts.get("sql", {}).get("comments", {}),
        # Optional override for the shared error log table used by builder-
        # generated OML4Py custom tools. Falls back to "<SCHEMA>_ERROR_LOG"
        # in sql_builder if not set — only needs setting when a project
        # wants to match an already-existing table name (e.g. one created
        # by hand-written code pasted in via a tool's PL/SQL Body field).
        "error_log_table": facts.get("error_log_table", ""),
        "profiles": [],
        "vector_indexes": [],
        "tools": [],
        "agents": [],
        "tasks": [],
        "teams": [],
    }

    tool_names: list[str] = []

    if is_rag:
        rag_profile = db_name(names.get("rag_profile"), f"{project_prefix(project)}_RAG_PROFILE")
        vector_index = db_name(names.get("vector_index"), f"{project_prefix(project)}_VECTOR_IDX")
        rag_tool = db_name(names.get("rag_tool"), f"{project_prefix(project)}_RAG_TOOL")
        location = (
            facts.get("rag", {}).get("object_storage_url")
            or project.get("rag_url")
            or (cfg_module.get(cfg, "object_storage", "rag_location_url", fallback="") if cfg is not None else "")
        )
        spec["profiles"].append({
            "name": rag_profile,
            "type": "RAG",
            "vector_index_name": vector_index,
            "embed_model": (
                facts.get("llm", {}).get("embed_model")
                or (cfg_module.get(cfg, "llm", "embed_model", fallback="") if cfg is not None else "")
            ),
            "temperature": facts.get("llm", {}).get("temperature"),
            "max_tokens":  facts.get("llm", {}).get("max_tokens"),
        })
        spec["vector_indexes"].append({
            "name": vector_index,
            "location": location,
            "profile_name": rag_profile,
            "chunk_size": int(names.get("chunk_size") or 1024),
            "chunk_overlap": int(names.get("chunk_overlap") or 128),
        })
        spec["tools"].append({
            "name": rag_tool,
            "type": "RAG",
            "profile_name": rag_profile,
            "instruction": "Use this tool to retrieve and summarize relevant document knowledge.",
            "inputs": [],
        })
        tool_names.append(rag_tool)

    if is_sql:
        nl2sql_profile = db_name(names.get("nl2sql_profile"), f"{project_prefix(project)}_NL2SQL_PROFILE")
        sql_tool = db_name(names.get("sql_tool"), f"{project_prefix(project)}_SQL_TOOL")
        tables = facts.get("sql", {}).get("tables") or []
        spec["profiles"].append({
            "name": nl2sql_profile,
            "type": "NL2SQL",
            "model": (
                facts.get("llm", {}).get("chat_model")
                or (cfg_module.get(cfg, "llm", "chat_model", fallback="") if cfg is not None else "")
            ),
            "temperature": facts.get("llm", {}).get("temperature"),
            "max_tokens":  facts.get("llm", {}).get("max_tokens"),
            "tables": tables,
            "object_list": tables,
            "comments_enabled": facts.get("sql", {}).get("comments", {}).get("mode") != "skipped",
        })
        spec["tools"].append({
            "name": sql_tool,
            "type": "SQL",
            "profile_name": nl2sql_profile,
            "instruction": facts.get("sql", {}).get("question_types") or "Answer questions using the approved database tables.",
            "inputs": [],
        })
        tool_names.append(sql_tool)

    # Deterministic optional analysis tools collected by the conversation flow.
    for tool in facts.get("analysis_tools", []) or []:
        spec["tools"].append(deepcopy(tool))
        if tool.get("name"):
            tool_names.append(tool["name"])

    role = facts.get("agent_role") or (
        "Answer user questions using only the configured Select AI tools. "
        "Be concise, cite the data source when useful, and do not invent facts."
    )
    task_instruction = task_facts.get("instruction") or "Answer each user request using the configured tools."

    primary_profile = ""
    if is_sql:
        primary_profile = db_name(names.get("nl2sql_profile"), f"{project_prefix(project)}_NL2SQL_PROFILE")
    elif is_rag:
        primary_profile = db_name(names.get("rag_profile"), f"{project_prefix(project)}_RAG_PROFILE")

    spec["agents"].append({
        "name": agent_name,
        "profile_name": primary_profile,
        "role": role,
        "tools": tool_names,
    })
    spec["tasks"].append({
        "name": task_name,
        "agent_name": agent_name,
        "instruction": task_instruction,
        "tools": tool_names,   # mirrors agent tool list so CREATE_TASK attributes are complete
    })
    spec["teams"].append({
        "name": team_name,
        "agents": [{"name": agent_name, "task": task_name}],
        "process": "sequential",
    })
    return spec


def canonicalize_spec(spec: dict | None, project: dict, cfg=None) -> dict:
    """
    Return a spec corrected with canonical project facts. If enough facts exist,
    deterministic generation wins over LLM-generated values.
    """
    facts = ensure_facts(project)
    has_task_names = any((facts.get("task") or {}).get(k) for k in ("agent_name", "task_name", "team_name"))
    has_data = bool(facts.get("data_source"))
    has_names = bool(facts.get("names"))

    if has_data and (has_names or has_task_names):
        return build_spec_from_project(project, cfg)

    # Fallback for legacy projects: lightly patch provided spec from facts.
    out = deepcopy(spec or {})
    if not out:
        return build_spec_from_project(project, cfg)

    out["comments"] = facts.get("sql", {}).get("comments", out.get("comments", {}))
    if facts.get("sql", {}).get("tables"):
        for p in out.get("profiles", []):
            if str(p.get("type", "")).upper() == "NL2SQL":
                p["tables"] = facts["sql"]["tables"]
                p["object_list"] = facts["sql"]["tables"]
                p["comments_enabled"] = facts.get("sql", {}).get("comments", {}).get("mode") != "skipped"
    return out
