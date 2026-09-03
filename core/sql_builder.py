"""
core/sql_builder.py
Deterministic PL/SQL generator for Select AI Agent Builder specs.

The SQL builder is intentionally deterministic: user-captured facts own object
names, object lists, comment metadata, and tool references. The LLM should not
be involved in SQL generation.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from core import config as cfg_module


# ── Helpers ──────────────────────────────────────────────────────────────────

def _q(value) -> str:
    """Escape text for a SQL single-quoted string."""
    return str(value if value is not None else "").replace("'", "''")


def _norm(value: str) -> str:
    """Normalize Oracle object names but preserve already-good values."""
    return str(value or "").strip().upper()


def _name(value: str, default: str = "OBJECT") -> str:
    raw = _norm(value)
    raw = re.sub(r"[^A-Z0-9_$#]", "_", raw).strip("_")
    return raw or default


def _json_sql(payload: dict) -> str:
    """Render JSON as a SQL-safe string literal payload."""
    return _q(json.dumps(payload, indent=6, ensure_ascii=False))


def _block(comment: str, body: str) -> str:
    return f"-- {comment}\nBEGIN\n{body}\nEND;\n/"


def _raw_block(comment: str, body: str) -> str:
    return f"-- {comment}\n{body.strip()}"


def _get_cred(cfg) -> str:
    return cfg_module.get(cfg, "object_storage", "credential_name", fallback="OCI$RESOURCE_PRINCIPAL")


def _get_compartment(cfg) -> str:
    return cfg_module.get(cfg, "compartment", "compartment_ocid", fallback="")


def _get_region(cfg) -> str:
    return cfg_module.get(cfg, "oci", "region", fallback="us-chicago-1")


def _get_location(cfg) -> str:
    return cfg_module.get(cfg, "object_storage", "rag_location_url", fallback="")


def _get_model(cfg) -> str:
    return cfg_module.get(cfg, "llm", "chat_model", fallback="meta.llama-3.3-70b-instruct")


def _get_embed_model(cfg) -> str:
    return cfg_module.get(cfg, "llm", "embed_model", fallback="cohere.embed-multilingual-v3.0")


def _split_owner_name(raw: str, schema: str) -> tuple[str, str]:
    raw = _norm(raw)
    if "." in raw:
        owner, name = raw.split(".", 1)
        return _norm(owner), _norm(name)
    return _norm(schema), raw


def _format_object_list(tables: list, schema: str) -> list[dict]:
    out = []
    for item in tables or []:
        if isinstance(item, dict):
            owner = _norm(item.get("owner") or schema)
            name = _norm(item.get("name") or item.get("table_name") or "")
        else:
            owner, name = _split_owner_name(str(item), schema)
        if owner and name:
            out.append({"owner": owner, "name": name})
    return out


def _selected_table_names(spec: dict) -> list[str]:
    for p in spec.get("profiles", []):
        if str(p.get("type", "")).upper() == "NL2SQL":
            tables = p.get("tables") or p.get("object_list") or []
            names = []
            for t in tables:
                if isinstance(t, dict):
                    names.append(_norm(t.get("name") or t.get("table_name") or ""))
                else:
                    names.append(_norm(str(t).split(".")[-1]))
            return [n for n in names if n]
    return []


def _find_table(spec: dict, suffix: str, fallback: str) -> str:
    suffix = suffix.upper()
    for name in _selected_table_names(spec):
        if name == suffix or name.endswith("_" + suffix):
            return name.lower()
    return fallback.lower()


# ── Cleanup ──────────────────────────────────────────────────────────────────

def _build_cleanup(spec: dict) -> str:
    drops = []
    for team in spec.get("teams", []):
        n = _name(team.get("name"))
        drops.append(f"  BEGIN DBMS_CLOUD_AI_AGENT.DROP_TEAM('{_q(n)}'); EXCEPTION WHEN OTHERS THEN NULL; END;")
        drops.append(f"  BEGIN DBMS_CLOUD_AI.DROP_PROFILE('AGENT${_q(n)}', force=>TRUE); EXCEPTION WHEN OTHERS THEN NULL; END;")
    for task in spec.get("tasks", []):
        n = _name(task.get("name"))
        drops.append(f"  BEGIN DBMS_CLOUD_AI_AGENT.DROP_TASK('{_q(n)}'); EXCEPTION WHEN OTHERS THEN NULL; END;")
    for agent in spec.get("agents", []):
        n = _name(agent.get("name"))
        drops.append(f"  BEGIN DBMS_CLOUD_AI_AGENT.DROP_AGENT('{_q(n)}'); EXCEPTION WHEN OTHERS THEN NULL; END;")
    for tool in spec.get("tools", []):
        n = _name(tool.get("name"))
        drops.append(f"  BEGIN DBMS_CLOUD_AI_AGENT.DROP_TOOL('{_q(n)}'); EXCEPTION WHEN OTHERS THEN NULL; END;")
    for tool in spec.get("tools", []):
        fn = _norm(tool.get("function_name", ""))
        if fn:
            drops.append(f"  BEGIN EXECUTE IMMEDIATE 'DROP FUNCTION {_q(fn)}'; EXCEPTION WHEN OTHERS THEN NULL; END;")
    # Drop vector indexes before profiles so profile drops do not silently fail
    # while still referenced by an index.
    for vi in spec.get("vector_indexes", []):
        n = _name(vi.get("name"))
        drops.append(f"  BEGIN DBMS_CLOUD_AI.DROP_VECTOR_INDEX('{_q(n)}', force=>TRUE); EXCEPTION WHEN OTHERS THEN NULL; END;")
    for profile in spec.get("profiles", []):
        n = _name(profile.get("name"))
        drops.append(f"  BEGIN DBMS_CLOUD_AI.DROP_PROFILE('{_q(n)}', force=>TRUE); EXCEPTION WHEN OTHERS THEN NULL; END;")
    return "-- Drop existing generated objects (safe — errors suppressed)\nBEGIN\n" + "\n".join(drops) + "\nEND;\n/"


# ── Comments ─────────────────────────────────────────────────────────────────

def _build_comments(spec: dict) -> list[str]:
    comments = spec.get("comments") or {}
    if comments.get("mode") == "skipped":
        return []
    # Draft/scanned comments are metadata only. Emit COMMENT ON statements only
    # after the user explicitly approves them. This avoids overwriting existing
    # database comments during exploratory builds.
    if str(comments.get("status", "")).lower() != "approved":
        return []
    objects = comments.get("objects") or {}
    if not objects:
        return []

    lines = [
        "-- ============================================================",
        "-- NL2SQL table and column comments",
        "-- Comments are created before the NL2SQL profile so comments=true can use them.",
        "-- ============================================================",
    ]
    for key in sorted(objects):
        obj = objects[key] or {}
        if "." in key:
            owner, table = key.split(".", 1)
        else:
            owner, table = _norm(spec.get("schema", "")), _norm(key)
        table_comment = str(obj.get("table_comment") or "").strip()
        if table_comment:
            lines.append(f"COMMENT ON TABLE {_norm(owner)}.{_norm(table)} IS '{_q(table_comment)}';")
        for col, comment in sorted((obj.get("columns") or {}).items()):
            comment = str(comment or "").strip()
            if comment:
                lines.append(f"COMMENT ON COLUMN {_norm(owner)}.{_norm(table)}.{_norm(col)} IS '{_q(comment)}';")
        lines.append("")
    return ["\n".join(lines).rstrip()]


# ── Profiles ─────────────────────────────────────────────────────────────────

def _build_profiles(spec: dict, cfg) -> list[str]:
    blocks = []
    compartment = _get_compartment(cfg)
    # NOTE: "region" is deliberately NOT emitted into profile attributes.
    # Setting it to the database's own region makes the database build a
    # malformed inference endpoint — every call fails with
    #   ORA-20404: Object not found - https://inference.generativeai.<region>.oci.my$cloud_domain/...
    # Omitting it lets the database resolve its own endpoint correctly.
    model = _get_model(cfg)
    embed_model = _get_embed_model(cfg)

    for p in spec.get("profiles", []):
        name = _name(p.get("name"))
        ptype = str(p.get("type", "")).upper()
        if ptype == "RAG":
            spec_temp   = p.get("temperature")
            spec_tokens = p.get("max_tokens")
            attrs = {
                "provider": "oci",
                "credential_name": "OCI$RESOURCE_PRINCIPAL",
                "oci_compartment_id": compartment,
                "model": p.get("model") or model,
                "embedding_model": p.get("embed_model") or embed_model,
                "vector_index_name": _name(p.get("vector_index_name")),
                "temperature": float(spec_temp) if spec_temp is not None else float(cfg_module.get(cfg, "llm", "temperature", fallback="0.3")),
                "max_tokens": int(spec_tokens) if spec_tokens is not None else int(cfg_module.get(cfg, "llm", "max_tokens", fallback="4000")),
            }
            body = (
                f"  DBMS_CLOUD_AI.CREATE_PROFILE(\n"
                f"    profile_name => '{_q(name)}',\n"
                f"    attributes   => '{_json_sql(attrs)}'\n"
                f"  );"
            )
            blocks.append(_block(f"Create RAG profile: {name}", body))
        elif ptype == "NL2SQL":
            spec_temp   = p.get("temperature")
            spec_tokens = p.get("max_tokens")
            object_list = _format_object_list(p.get("tables") or p.get("object_list") or [], spec.get("schema", ""))
            comments_enabled = "true" if p.get("comments_enabled", True) else "false"
            attrs = {
                "provider": "oci",
                "credential_name": "OCI$RESOURCE_PRINCIPAL",
                "oci_compartment_id": compartment,
                "model": p.get("model") or model,
                "comments": comments_enabled,
                "constraints": "true",
                "conversation": "true",
                "temperature": float(spec_temp) if spec_temp is not None else float(cfg_module.get(cfg, "llm", "temperature", fallback="0.1")),
                "max_tokens": int(spec_tokens) if spec_tokens is not None else int(cfg_module.get(cfg, "llm", "max_tokens", fallback="4000")),
                "object_list": object_list,
            }
            body = (
                f"  DBMS_CLOUD_AI.CREATE_PROFILE(\n"
                f"    profile_name => '{_q(name)}',\n"
                f"    attributes   => '{_json_sql(attrs)}'\n"
                f"  );"
            )
            blocks.append(_block(f"Create NL2SQL profile: {name}", body))
    return blocks


# ── Vector indexes ───────────────────────────────────────────────────────────

def _build_vector_indexes(spec: dict, cfg) -> list[str]:
    blocks = []
    for vi in spec.get("vector_indexes", []):
        name = _name(vi.get("name"))
        location = str(vi.get("location") or spec.get("rag_location") or spec.get("rag_url") or "").strip() or _get_location(cfg)
        attrs = {
            "vector_db_provider": "oracle",
            "location": location,
            "object_storage_credential_name": "OCI$RESOURCE_PRINCIPAL",
            "profile_name": _name(vi.get("profile_name")),
            "chunk_size": int(vi.get("chunk_size", 1024)),
            "chunk_overlap": int(vi.get("chunk_overlap", 128)),
        }
        body = (
            f"  DBMS_CLOUD_AI.CREATE_VECTOR_INDEX(\n"
            f"    index_name => '{_q(name)}',\n"
            f"    attributes => '{_json_sql(attrs)}'\n"
            f"  );"
        )
        blocks.append(_block(f"Create vector index: {name}", body))
    return blocks


# ── Custom analysis functions ────────────────────────────────────────────────

def _parse_param_spec(raw: str) -> list[dict]:
    """
    Parse a simple pipe- or comma-separated parameter spec into structured
    parts, e.g.:
      "P_DEPARTMENT VARCHAR2 DEFAULT NULL | P_THRESHOLD_PCT NUMBER DEFAULT 20"
    -> [{"name": "P_DEPARTMENT", "type": "VARCHAR2", "default": "NULL"},
        {"name": "P_THRESHOLD_PCT", "type": "NUMBER", "default": "20"}]

    Tolerant of missing DEFAULT clauses and either separator. Returns []
    for empty/unparseable input rather than raising — a malformed spec
    just means no parameters, not a hard failure.
    """
    if not raw or not raw.strip():
        return []
    parts = re.split(r"[|;]", raw) if "|" in raw or ";" in raw else \
            re.split(r",(?![^()]*\))", raw)  # split on commas not inside parens
    params = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(
            r"^(\w+)\s+(VARCHAR2(?:\(\d+\))?|NUMBER|DATE|CLOB|BOOLEAN)"
            r"(?:\s+DEFAULT\s+(.+))?$",
            part, re.IGNORECASE
        )
        if m:
            params.append({
                "name": m.group(1).upper(),
                "type": m.group(2).upper(),
                "default": (m.group(3) or "NULL").strip(),
            })
    return params


def _build_error_log_ddl(error_log: str) -> str:
    """
    Idempotent CREATE TABLE for the shared custom-tool error log — created
    once, safe to re-run. Shared by both the OML4Py generator path
    (_build_oml_python_tool) and the raw-pasted-PL/SQL path
    (_build_custom_tool_functions), so a project gets exactly one error
    log table regardless of which path its custom tools were authored
    through.
    """
    return f"""
BEGIN
    EXECUTE IMMEDIATE '
        CREATE TABLE {error_log} (
            log_id        NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            error_date    TIMESTAMP DEFAULT SYSTIMESTAMP,
            error_source  VARCHAR2(200),
            error_msg     VARCHAR2(4000),
            error_detail  CLOB
        )';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -955 THEN  -- -955 = name already used by an existing object
            RAISE;
        END IF;
END;
/
"""


def _build_oml_python_tool(tool: dict, spec: dict, cfg) -> list[str]:
    """
    Generate a complete OML4Py (Embedded Python Execution) custom tool from
    a small, schema-agnostic set of inputs: the Python script body, the SQL
    query that builds its JSON data payload, and a parameter spec. This is
    a generalised version of the auto-refreshing-token pattern — the same
    *behaviour* (fetch a fresh OML token on every call so the ~60-minute
    token expiry never has to be handled manually), reworked to avoid
    hardcoding anything environment- or schema-specific:

      - Token endpoint comes from config [de] oml_base_url, not a literal
        hostname baked into the function body.
      - The OAuth POST body uses the PL/SQL USER built-in for the username,
        not a hardcoded schema name — works unchanged in any schema.
      - The credential name defaults to "<SCHEMA>_OML_CRED" but can be
        overridden per tool; credential CREATION is intentionally NOT
        generated here (a real password has no business sitting in
        generated SQL) — it's a one-time Admin Setup → Vault Credential
        step the builder already supports elsewhere.
      - The data/parameter payload is built with JSON_OBJECT(...) rather
        than manual string concatenation + REPLACE-based quote escaping.
        JSON_OBJECT lets Oracle handle escaping correctly and is the more
        robust, modern (12c+/ADW) approach — the original hand-rolled
        concatenation is a real correctness risk for any data containing
        special characters.
      - Failures are logged to a generic, auto-created <PREFIX>_ERROR_LOG
        table (created once, idempotently) rather than assuming a
        project-specific log table already exists.

    What CANNOT be generalised, and must be supplied per tool:
      - The Python script logic itself (python_script)
      - The SQL query that assembles the data payload (data_query)
      - The parameter list specific to this tool (params)
    These are genuinely tool-specific; everything else here is boilerplate
    this function exists to eliminate.
    """
    name          = tool.get("name", "CUSTOM_TOOL")
    function_name = _name(tool.get("function_name", ""), f"{name}_FN")
    fn_l          = function_name.lower()
    pyqscript     = (tool.get("pyqscript_name") or fn_l + "_script").strip().lower()
    python_script = (tool.get("python_script") or "").strip()
    data_query    = (tool.get("data_query") or "").strip()
    schema        = (spec.get("schema") or "").strip().upper() or "<SCHEMA>"
    cred_name     = (tool.get("credential_name") or f"{schema}_OML_CRED").strip().upper()
    # Explicit override (project-level, set once for all custom tools in
    # this project) takes priority over the schema-derived default — lets
    # a generated tool share an error log table that already exists from
    # hand-written code pasted in via another tool's PL/SQL Body field.
    error_log = (spec.get("error_log_table") or f"{schema}_ERROR_LOG").strip().upper()

    oml_base_url = ""
    if cfg is not None:
        oml_base_url = cfg_module.get(cfg, "de", "oml_base_url", fallback="").strip()
    token_url_literal = (
        f"'{oml_base_url.rstrip('/')}/omlusers/api/oauth2/v1/token'"
        if oml_base_url else
        "'<SET [de] oml_base_url IN CONFIG AND REGENERATE>'"
    )

    params = tool.get("params") or _parse_param_spec(tool.get("param_spec", ""))
    sig_lines = [f"    {p['name']} IN {p['type']} DEFAULT {p['default']}" for p in params]
    sig = ("\n" + ",\n".join(sig_lines)) if sig_lines else ""

    json_obj_pairs = ["'oml_service_level' VALUE 'LOW'"]
    for p in params:
        json_obj_pairs.append(f"'{p['name'].lower()}' VALUE {p['name']}")
    json_obj_pairs.append("'data_json' VALUE v_data")
    json_obj_sql = ",\n        ".join(json_obj_pairs)

    blocks = []

    # Error log table — created once, idempotently, shared by all custom
    # tools in this project rather than one log table per tool.
    error_log_ddl = _build_error_log_ddl(error_log)
    blocks.append(_raw_block(f"Error log table for custom tools (shared, created once)", error_log_ddl))

    # Register/refresh the Python script in the OML4Py repository.
    if python_script:
        script_ddl = f"""
BEGIN
    BEGIN
        sys.pyqScriptDrop('{pyqscript}', TRUE);
    EXCEPTION WHEN OTHERS THEN NULL;
    END;

    sys.pyqScriptCreate(
        '{pyqscript}',
'{python_script}'
    );
END;
/
"""
        blocks.append(_raw_block(f"Register OML4Py script: {pyqscript}", script_ddl))

    # The wrapper function: auto-refresh token -> build data payload ->
    # call pyqEval -> return result. Falls back gracefully if token
    # refresh fails (logs and continues with whatever token is set).
    wrapper = f"""
CREATE OR REPLACE FUNCTION {fn_l}({sig}
) RETURN CLOB IS
    v_result   CLOB;
    v_data     VARCHAR2(32767);
    v_par_lst  VARCHAR2(32767);
    v_token    VARCHAR2(32767);
    v_resp     CLOB;
    v_pwd      VARCHAR2(500);
BEGIN
    -- Step 1: auto-refresh OML token (handles the ~60-minute token expiry
    -- without requiring a manual pyqSetAuthToken call before every use)
    BEGIN
        SELECT cred_attribute_value
        INTO   v_pwd
        FROM   user_cloud_credential_attrs
        WHERE  credential_name     = '{cred_name}'
        AND    cred_attribute_name = 'PASSWORD';

        v_resp := DBMS_CLOUD.SEND_REQUEST(
            credential_name => 'OCI$RESOURCE_PRINCIPAL',
            uri             => {token_url_literal},
            method          => 'POST',
            headers         => '{{"Content-Type":"application/json"}}',
            body            => UTL_RAW.CAST_TO_RAW(
                                   JSON_OBJECT(
                                       'grant_type' VALUE 'password',
                                       'username'   VALUE USER,
                                       'password'   VALUE v_pwd
                                   )
                               )
        ).text;

        v_token := JSON_VALUE(v_resp, '$.accessToken');

        IF v_token IS NOT NULL THEN
            pyqSetAuthToken(v_token);
        ELSE
            INSERT INTO {error_log}(error_source, error_msg, error_detail)
            VALUES('{function_name}', 'OML token refresh returned null token',
                   SUBSTR(v_resp, 1, 4000));
            COMMIT;
        END IF;
    EXCEPTION
        WHEN OTHERS THEN
            INSERT INTO {error_log}(error_source, error_msg)
            VALUES('{function_name} — token refresh', SQLERRM);
            COMMIT;
    END;

    -- Step 2: build the data payload (tool-specific query, supplied as-is)
{data_query if data_query else "    v_data := NULL; -- no data_query supplied"}

    -- Step 3: build par_lst via JSON_OBJECT (safer than manual string
    -- concatenation — Oracle handles all escaping automatically)
    v_par_lst := JSON_OBJECT(
        {json_obj_sql}
        RETURNING VARCHAR2(32767)
    );

    SELECT TO_CLOB(result)
    INTO   v_result
    FROM   TABLE(
               pyqEval(
                   par_lst  => v_par_lst,
                   out_fmt  => '{{"RESULT": "VARCHAR2(32767)"}}',
                   scr_name => '{pyqscript}'
               )
           );

    RETURN v_result;
EXCEPTION
    WHEN OTHERS THEN
        RETURN '{{"error": "Python EPE failed: ' || REPLACE(SQLERRM, '"', '''') || '"}}';
END {fn_l};
/
"""
    blocks.append(_raw_block(f"OML4Py wrapper function: {function_name}", wrapper))
    return blocks


def _build_custom_tool_functions(spec: dict, cfg=None) -> list[str]:
    blocks = []
    gl_table = _find_table(spec, "ACME_GL_TRANSACTIONS", "acme_gl_transactions")
    dept_table = _find_table(spec, "ACME_DEPARTMENTS", "acme_departments")

    # Raw-pasted tools (Word doc "Custom Tool N PL/SQL Body" field) are
    # emitted verbatim and may reference an error log table by name
    # without ever creating it themselves — the pasted body is whatever
    # was captured in the doc, nothing more. If any tool in this project
    # uses that path, create the table up front, once, idempotently —
    # same guarded DDL the generator path already uses, so a project no
    # longer depends on a separate admin setup script having been run
    # first. Skipped if no raw_plsql tool is present, so a project with
    # error_log_table set but no custom tools yet doesn't get an unused
    # table.
    has_raw_plsql_tool = any(
        str(t.get("type", "")).upper() not in ("SQL", "RAG") and t.get("raw_plsql")
        for t in spec.get("tools", [])
    )
    if has_raw_plsql_tool:
        schema = (spec.get("schema") or "").strip().upper() or "<SCHEMA>"
        error_log = (spec.get("error_log_table") or f"{schema}_ERROR_LOG").strip().upper()
        blocks.append(_raw_block(
            "Error log table for custom tools (shared, created once)",
            _build_error_log_ddl(error_log)
        ))

    for tool in spec.get("tools", []):
        typ = str(tool.get("type", "")).upper()
        fn = _name(tool.get("function_name", ""), "")
        if typ in ("SQL", "RAG"):
            continue

        # Generalised OML4Py custom tool: builder-generated token-refresh
        # wrapper + script registration, from just the Python script body,
        # the data query, and a parameter spec. Checked before raw_plsql —
        # a tool can supply python_script/data_query without ever writing
        # PL/SQL by hand at all.
        if tool.get("python_script") or tool.get("data_query"):
            blocks.extend(_build_oml_python_tool(tool, spec, cfg))
            continue

        # Imported custom tools (Word doc "Custom Tool N PL/SQL Body" field,
        # or CSV equivalent) carry their own already-tested implementation —
        # emit it verbatim instead of running it through the hardcoded
        # TREND/FORECAST/ANOMALY templates below, which are demo templates
        # tied to a specific schema (ACME_GL_TRANSACTIONS/ACME_DEPARTMENTS)
        # and were never meant to be a general-purpose code generator.
        raw_plsql = tool.get("raw_plsql")
        if raw_plsql:
            tool_label = tool.get("name") or fn or "CUSTOM_TOOL"
            blocks.append(_raw_block(f"Create custom function for tool: {tool_label}", raw_plsql))
            continue

        if not fn:
            continue
        fn_l = fn.lower()
        if typ == "TREND":
            ddl = f"""
CREATE OR REPLACE FUNCTION {fn_l}(
    p_department IN VARCHAR2 DEFAULT NULL,
    p_periods    IN NUMBER   DEFAULT 6
) RETURN CLOB IS
    v_result CLOB := '';
    v_sep    VARCHAR2(2) := '';
BEGIN
    v_result := '{{"trend_analysis": {{"department": "' || NVL(p_department,'ALL') || '", "periods": [';
    FOR r IN (
        SELECT t.period_name,
               d.department_name,
               SUM(t.debit_amount) AS total_expense,
               LAG(SUM(t.debit_amount)) OVER (PARTITION BY d.department_name ORDER BY t.period_name) AS prior_expense,
               ROUND((SUM(t.debit_amount) - LAG(SUM(t.debit_amount)) OVER (PARTITION BY d.department_name ORDER BY t.period_name))
                     / NULLIF(LAG(SUM(t.debit_amount)) OVER (PARTITION BY d.department_name ORDER BY t.period_name),0) * 100, 2) AS growth_pct
        FROM   {gl_table} t
        JOIN   {dept_table} d ON d.department_code = t.department_code
        WHERE  t.account_type = 'EXPENSE'
        AND    (p_department IS NULL OR UPPER(d.department_name) LIKE UPPER('%'||p_department||'%'))
        GROUP  BY t.period_name, d.department_name
        ORDER  BY d.department_name, t.period_name DESC
        FETCH  FIRST p_periods ROWS ONLY
    ) LOOP
        v_result := v_result || v_sep ||
            '{{"period":"' || r.period_name || '","department":"' || r.department_name ||
            '","expense":' || ROUND(r.total_expense,2) || ',"prior_expense":' ||
            NVL(TO_CHAR(ROUND(r.prior_expense,2)),'null') || ',"growth_pct":' ||
            NVL(TO_CHAR(r.growth_pct),'null') || '}}';
        v_sep := ',';
    END LOOP;
    v_result := v_result || ']}}}}';
    RETURN v_result;
END {fn_l};
/
"""
            blocks.append(_raw_block(f"Create custom function: {fn}", ddl))
        elif typ == "FORECAST":
            ddl = f"""
CREATE OR REPLACE FUNCTION {fn_l}(
    p_department     IN VARCHAR2 DEFAULT NULL,
    p_future_periods IN NUMBER   DEFAULT 3
) RETURN CLOB IS
    v_result     CLOB;
    v_n          NUMBER := 0;
    v_sum_x      NUMBER := 0;
    v_sum_y      NUMBER := 0;
    v_sum_xy     NUMBER := 0;
    v_sum_x2     NUMBER := 0;
    v_slope      NUMBER;
    v_intercept  NUMBER;
    v_sep        VARCHAR2(2) := '';
BEGIN
    SELECT COUNT(*), SUM(rn), SUM(total), SUM(rn*total), SUM(rn*rn)
    INTO   v_n, v_sum_x, v_sum_y, v_sum_xy, v_sum_x2
    FROM (
        SELECT ROW_NUMBER() OVER (ORDER BY period_name) AS rn, SUM(debit_amount) AS total
        FROM   {gl_table} t
        JOIN   {dept_table} d ON d.department_code = t.department_code
        WHERE  t.account_type = 'EXPENSE'
        AND    (p_department IS NULL OR UPPER(d.department_name) LIKE UPPER('%'||p_department||'%'))
        GROUP  BY period_name
    );
    IF v_n < 2 THEN
        RETURN '{{"error": "Insufficient data. Need at least 2 periods."}}';
    END IF;
    v_slope := (v_n * v_sum_xy - v_sum_x * v_sum_y) / NULLIF((v_n * v_sum_x2 - v_sum_x * v_sum_x), 0);
    v_intercept := (v_sum_y - v_slope * v_sum_x) / v_n;
    v_result := '{{"forecast": {{"department": "' || NVL(p_department,'ALL') || '","model": "linear_regression",' ||
                '"slope": ' || ROUND(v_slope,2) || ',"intercept": ' || ROUND(v_intercept,2) ||
                ',"historical_periods": ' || v_n || ',"projections": [';
    FOR i IN 1..p_future_periods LOOP
        v_result := v_result || v_sep || '{{"period_offset": ' || i || ',"label": "FORECAST+' || i ||
                    '","predicted_expense": ' || ROUND(v_slope*(v_n+i)+v_intercept,2) || '}}';
        v_sep := ',';
    END LOOP;
    v_result := v_result || ']}}}}';
    RETURN v_result;
END {fn_l};
/
"""
            blocks.append(_raw_block(f"Create custom function: {fn}", ddl))
        elif typ == "ANOMALY":
            ddl = f"""
CREATE OR REPLACE FUNCTION {fn_l}(
    p_threshold_pct IN NUMBER DEFAULT 20
) RETURN CLOB IS
    v_result CLOB;
    v_sep    VARCHAR2(2) := '';
    v_count  NUMBER := 0;
BEGIN
    v_result := '{{"anomaly_detection": {{"threshold_pct": ' || p_threshold_pct || ',"anomalies": [';
    FOR r IN (
        WITH dept_stats AS (
            SELECT d.department_name, t.period_name,
                   SUM(t.debit_amount) AS period_expense,
                   AVG(SUM(t.debit_amount)) OVER (PARTITION BY d.department_name) AS avg_expense,
                   STDDEV(SUM(t.debit_amount)) OVER (PARTITION BY d.department_name) AS stddev_expense
            FROM   {gl_table} t
            JOIN   {dept_table} d ON d.department_code = t.department_code
            WHERE  t.account_type = 'EXPENSE'
            GROUP  BY d.department_name, t.period_name
        )
        SELECT department_name, period_name,
               ROUND(period_expense,2) AS expense,
               ROUND(avg_expense,2) AS avg_expense,
               ROUND(stddev_expense,2) AS stddev_expense,
               ROUND((period_expense-avg_expense)/NULLIF(avg_expense,0)*100,2) AS deviation_pct
        FROM   dept_stats
        WHERE  ABS((period_expense-avg_expense)/NULLIF(avg_expense,0)*100) > p_threshold_pct
        ORDER  BY ABS(deviation_pct) DESC
    ) LOOP
        v_result := v_result || v_sep || '{{"department": "' || r.department_name || '","period": "' || r.period_name ||
                    '","expense": ' || r.expense || ',"avg_expense": ' || r.avg_expense ||
                    ',"deviation_pct": ' || r.deviation_pct || ',"flagged": true}}';
        v_sep := ',';
        v_count := v_count + 1;
    END LOOP;
    IF v_count = 0 THEN
        v_result := v_result || '{{"message": "No anomalies above ' || p_threshold_pct || '% threshold"}}';
    END IF;
    v_result := v_result || '], "total_anomalies": ' || v_count || '}}}}';
    RETURN v_result;
END {fn_l};
/
"""
            blocks.append(_raw_block(f"Create custom function: {fn}", ddl))
        else:
            ddl = f"""
CREATE OR REPLACE FUNCTION {fn_l}(
    p_request IN VARCHAR2 DEFAULT NULL
) RETURN CLOB IS
BEGIN
    RETURN '{{"tool":"{fn}","status":"stub","message":"Custom function template created. Replace implementation before production use.","request":"' || REPLACE(NVL(p_request,''),'"','''') || '"}}';
END {fn_l};
/
"""
            blocks.append(_raw_block(f"Create custom function: {fn}", ddl))
    return blocks


# ── Tools ────────────────────────────────────────────────────────────────────

def _build_tools(spec: dict) -> list[str]:
    blocks = []
    for t in spec.get("tools", []):
        name = _name(t.get("name"))
        ttype = str(t.get("type", "")).upper()
        profile = _name(t.get("profile_name", ""), "")
        if ttype == "RAG":
            attrs = {"tool_type": "RAG", "tool_params": {"profile_name": profile}}
        elif ttype == "SQL":
            attrs = {"tool_type": "SQL", "tool_params": {"profile_name": profile, "action": "runsql"}}
        else:
            attrs = {
                "function": _name(t.get("function_name", name + "_FN")),
                "instruction": t.get("instruction", ""),
            }
            if t.get("inputs"):
                attrs["tool_inputs"] = [
                    {"name": _norm(i.get("name", "")), "description": i.get("description", "")}
                    for i in t.get("inputs", [])
                ]
        body = (
            f"  DBMS_CLOUD_AI_AGENT.CREATE_TOOL(\n"
            f"    tool_name  => '{_q(name)}',\n"
            f"    attributes => '{_json_sql(attrs)}'\n"
            f"  );"
        )
        blocks.append(_block(f"Create {ttype} tool: {name}", body))
    return blocks


# ── Agents / Tasks / Teams ───────────────────────────────────────────────────

def _build_agents(spec: dict) -> list[str]:
    blocks = []
    for a in spec.get("agents", []):
        name = _name(a.get("name"))
        attrs = {
            "role": a.get("role", ""),
            "enable_human_tool": "False",
            "tools": [_name(t) for t in a.get("tools", [])],
        }
        profile = _norm(a.get("profile_name", ""))
        if profile:
            attrs = {"profile_name": profile, **attrs}
        body = (
            f"  DBMS_CLOUD_AI_AGENT.CREATE_AGENT(\n"
            f"    agent_name  => '{_q(name)}',\n"
            f"    attributes  => '{_json_sql(attrs)}',\n"
            f"    description => '{_q(name)} generated by Select AI Agent Builder'\n"
            f"  );"
        )
        blocks.append(_block(f"Create agent: {name}", body))
    return blocks


def _build_tasks(spec: dict) -> list[str]:
    blocks = []
    for t in spec.get("tasks", []):
        name = _name(t.get("name"))
        attrs = {"instruction": t.get("instruction", "")}
        tools = [_name(x) for x in t.get("tools", []) if _name(x, "")]
        if tools:
            attrs["tools"] = tools
        body = (
            f"  DBMS_CLOUD_AI_AGENT.CREATE_TASK(\n"
            f"    task_name   => '{_q(name)}',\n"
            f"    attributes  => '{_json_sql(attrs)}',\n"
            f"    description => '{_q(t.get('description') or name + ' task')}'\n"
            f"  );"
        )
        blocks.append(_block(f"Create task: {name}", body))
    return blocks


def _build_teams(spec: dict) -> list[str]:
    blocks = []
    for team in spec.get("teams", []):
        name = _name(team.get("name"))
        attrs = {
            "agents": [
                {"name": _name(a.get("name")), "task": _name(a.get("task"))}
                for a in team.get("agents", [])
            ],
            "process": "sequential",
        }
        body = (
            f"  DBMS_CLOUD_AI_AGENT.CREATE_TEAM(\n"
            f"    team_name   => '{_q(name)}',\n"
            f"    attributes  => '{_json_sql(attrs)}',\n"
            f"    description => '{_q(team.get('description') or name + ' agent team')}'\n"
            f"  );"
        )
        blocks.append(_block(f"Create team: {name}", body))
    return blocks


# ── Verification ─────────────────────────────────────────────────────────────

def _build_verification(spec: dict) -> list[str]:
    profile_names = [_name(p.get("name")) for p in spec.get("profiles", [])]
    vector_names = [_name(v.get("name")) for v in spec.get("vector_indexes", [])]
    tool_names = [_name(t.get("name")) for t in spec.get("tools", [])]
    agent_names = [_name(a.get("name")) for a in spec.get("agents", [])]
    task_names = [_name(t.get("name")) for t in spec.get("tasks", [])]
    team_names = [_name(t.get("name")) for t in spec.get("teams", [])]

    def in_list(names: Iterable[str]) -> str:
        return ", ".join(f"'{_q(n)}'" for n in names) or "''"

    lines = [
        "-- ============================================================",
        "-- Verification queries",
        "-- ============================================================",
    ]
    if profile_names:
        lines.append(f"SELECT profile_name, status FROM user_cloud_ai_profiles WHERE profile_name IN ({in_list(profile_names)}) ORDER BY profile_name;")
    if vector_names:
        lines.append(f"SELECT index_name, status FROM user_cloud_vector_indexes WHERE index_name IN ({in_list(vector_names)}) ORDER BY index_name;")
    if tool_names:
        lines.append(f"SELECT tool_name, status FROM user_ai_agent_tools WHERE tool_name IN ({in_list(tool_names)}) ORDER BY tool_name;")
    if agent_names:
        lines.append(f"SELECT agent_name, status FROM user_ai_agents WHERE agent_name IN ({in_list(agent_names)}) ORDER BY agent_name;")
    if task_names:
        lines.append(f"SELECT task_name, status FROM user_ai_agent_tasks WHERE task_name IN ({in_list(task_names)}) ORDER BY task_name;")
    if team_names:
        lines.append(f"SELECT agent_team_name, status FROM user_ai_agent_teams WHERE agent_team_name IN ({in_list(team_names)}) ORDER BY agent_team_name;")
        team = team_names[0]
        lines.append("")
        lines.append("-- Smoke tests (run manually after verifying objects are ENABLED)")
        # conversation_id must be the hyphenated 36-char UUID returned by
        # DBMS_CLOUD_AI.CREATE_CONVERSATION. SYS_GUID() returns 32 hex chars
        # with no hyphens and is rejected with ORA-20050.
        for _prompt in ("List the available tools and what each should be used for.",
                        "Answer a simple question using the correct tool. If no data is available, say so clearly."):
            lines.append("DECLARE")
            lines.append("  l_conv VARCHAR2(36);")
            lines.append("  l_resp CLOB;")
            lines.append("BEGIN")
            lines.append("  l_conv := DBMS_CLOUD_AI.CREATE_CONVERSATION();")
            lines.append(f"  l_resp := DBMS_CLOUD_AI_AGENT.RUN_TEAM(team_name => '{_q(team)}',")
            lines.append(f"              user_prompt => '{_q(_prompt)}',")
            lines.append("              params      => '{\"conversation_id\":\"' || l_conv || '\"}');")
            lines.append("  DBMS_OUTPUT.PUT_LINE(SUBSTR(l_resp, 1, 3000));")
            lines.append("END;")
            lines.append("/")
    return ["\n".join(lines)]


# ── Public entry point ───────────────────────────────────────────────────────

def build_full_sql(spec: dict, cfg) -> str:
    blocks = []
    blocks.append("-- ============================================================")
    blocks.append(f"-- Select AI Agent Stack: {spec.get('project_name', 'Agent')}")
    blocks.append(f"-- Schema: {spec.get('schema', '')}")
    blocks.append("-- Generated by agent_builder_v5.1.6 sql_builder")
    blocks.append("-- ============================================================")
    blocks.append("")
    blocks.append(_build_cleanup(spec))
    blocks.append("")

    for b in _build_comments(spec):
        blocks.append(b)
        blocks.append("")
    for b in _build_profiles(spec, cfg):
        blocks.append(b)
        blocks.append("")
    for b in _build_vector_indexes(spec, cfg):
        blocks.append(b)
        blocks.append("")
    for b in _build_custom_tool_functions(spec, cfg):
        blocks.append(b)
        blocks.append("")
    for b in _build_tools(spec):
        blocks.append(b)
        blocks.append("")
    for b in _build_agents(spec):
        blocks.append(b)
        blocks.append("")
    for b in _build_tasks(spec):
        blocks.append(b)
        blocks.append("")
    for b in _build_teams(spec):
        blocks.append(b)
        blocks.append("")
    for b in _build_verification(spec):
        blocks.append(b)
        blocks.append("")

    return "\n".join(blocks).strip() + "\n"
