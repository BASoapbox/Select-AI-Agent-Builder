"""
modules/preflight_schema.py
Pre-flight schema check — target schema configuration diagnostics.

Checks whether the agent-owning schema (e.g. ACME_CORP) is properly
bootstrapped for Select AI Agent — independent of which user is connected.

Checks:
  - Schema exists and account is OPEN
  - Resource Principal enabled (OCI$RESOURCE_PRINCIPAL credential)
  - EXECUTE grants on all DBMS_CLOUD packages
  - PYQADMIN + OML_DEVELOPER roles
  - EPE network ACL (pyqAppendHostAce)
  - Agent framework views accessible
  - NL2SQL comment coverage on source tables
  - OCI GenAI reachability (Resource Principal → OCI)
"""

from __future__ import annotations
import os
from core import config as cfg_module
from core import db as db_module
from modules.preflight_dsde import (
    CheckResult, _print_section_box, _print_summary,
    _qone as _query_one, _qall as _query_all,
    _check_oci_genai, _connect as _connect_proxy
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema-level checks
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    "DBMS_CLOUD_AI",
    "DBMS_CLOUD_AI_AGENT",
    "DBMS_CLOUD",
    "DBMS_CLOUD_ADMIN",
]

REQUIRED_ROLES = [
    "PYQADMIN",
    "OML_DEVELOPER",
]

REQUIRED_VIEWS = [
    "USER_AI_AGENT_TOOLS",
    "USER_AI_AGENTS",
    "USER_AI_AGENT_TASKS",
    "USER_AI_AGENT_TEAMS",
    "USER_CLOUD_AI_PROFILES",
]


def _check_schema_exists(conn, target_schema, r: CheckResult):
    """Check schema exists and is OPEN.

    NOTE: this runs over a proxy connection (db_user[target_schema]), so
    CURRENT_USER already IS target_schema in this session. USER_USERS
    reflects exactly that — no DBA_USERS / catalog access needed, and a
    successful connection already proves the account exists and is usable.
    """
    try:
        row = _query_one(conn, """
            SELECT username, account_status, created
            FROM   USER_USERS
        """)
        if row:
            actual = (row.get("username") or "").upper()
            status = row.get("account_status", "UNKNOWN")
            if actual and actual != target_schema.upper():
                r.warn(f"Session identity mismatch — connected as {actual}, "
                       f"expected {target_schema}")
            if status == "OPEN":
                r.ok(f"Schema {target_schema} exists — status: OPEN")
            else:
                r.fail(f"Schema {target_schema} exists but status is {status}",
                       "Account may be locked — ask DBA to unlock it")
        else:
            r.fail(f"Schema {target_schema} does not exist",
                   "Run Admin Setup → Schema to create it, or run SA_01_ACME_CORP_setup.sql")
    except Exception as ex:
        r.warn("Schema existence check failed", str(ex)[:100])


def _check_resource_principal(conn, target_schema, r: CheckResult):
    """Check Resource Principal access is granted to the target schema.

    NOTE: OCI$RESOURCE_PRINCIPAL is a single credential object always owned
    by ADMIN. DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(username=>X) does
    NOT create a new credential owned by X — it grants X access to ADMIN's
    existing credential. So checking ownership (USER_CREDENTIALS /
    DBA_CREDENTIALS WHERE owner=X) will never find a row; the correct check
    is the grant relationship, per Oracle's own documented verification
    query: ALL_TAB_PRIVS WHERE grantee=X AND table_name='OCI$RESOURCE_PRINCIPAL'
    AND table_schema='ADMIN'. USER_TAB_PRIVS_RECD is the no-special-privilege
    equivalent of ALL_TAB_PRIVS scoped to "grants received by me", which is
    exactly what's needed here since CURRENT_USER is target_schema in this
    proxied session. NOTE: USER_TAB_PRIVS_RECD names that column OWNER, not
    TABLE_SCHEMA like ALL_TAB_PRIVS does.
    """
    try:
        row = _query_one(conn, """
            SELECT table_name, owner
            FROM   USER_TAB_PRIVS_RECD
            WHERE  table_name = 'OCI$RESOURCE_PRINCIPAL'
              AND  owner      = 'ADMIN'
        """)
        if row:
            r.ok(f"Resource Principal — {target_schema} has access to ADMIN's OCI$RESOURCE_PRINCIPAL")
        else:
            r.fail(f"Resource Principal — NOT enabled for {target_schema}",
                   f"Run as ADMIN: EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL("
                   f"username=>'{target_schema}')")
    except Exception as ex:
        r.warn("Resource Principal check failed", str(ex)[:100])


def _check_schema_package_grants(conn, target_schema, r: CheckResult):
    """Check EXECUTE grants on DBMS_CLOUD packages for the target schema.

    NOTE: USER_TAB_PRIVS_RECD (object grants received by the current user)
    instead of DBA_TAB_PRIVS — same reasoning as _check_schema_exists.

    NOTE on DBMS_CLOUD specifically: it is a PUBLIC SYNONYM for a versioned
    internal package (e.g. C##CLOUD$SERVICE.DBMS_CLOUD$PDBCS_<build>_0 — the
    suffix changes per ADW patch level). GRANT EXECUTE ON DBMS_CLOUD resolves
    through the synonym and creates a privilege row keyed to that REAL
    package name, not the literal string 'DBMS_CLOUD'. A naive check against
    table_name='DBMS_CLOUD' will therefore always report it as ungranted
    even when the grant succeeded and access genuinely works. We resolve the
    synonym first via USER_SYNONYMS/ALL_SYNONYMS, then check the resolved name.
    """
    # Resolve DBMS_CLOUD's real underlying object name via its synonym.
    # ALL_SYNONYMS is queryable without special privileges for PUBLIC synonyms.
    dbms_cloud_real_name = "DBMS_CLOUD"
    try:
        syn_row = _query_one(conn, """
            SELECT table_name
            FROM   all_synonyms
            WHERE  synonym_name = 'DBMS_CLOUD'
              AND  owner = 'PUBLIC'
        """)
        if syn_row and syn_row.get("table_name"):
            dbms_cloud_real_name = syn_row["table_name"]
    except Exception:
        pass  # fall back to literal 'DBMS_CLOUD' if synonym lookup fails

    check_names = {
        "DBMS_CLOUD_AI": "DBMS_CLOUD_AI",
        "DBMS_CLOUD_AI_AGENT": "DBMS_CLOUD_AI_AGENT",
        "DBMS_CLOUD": dbms_cloud_real_name,
        "DBMS_CLOUD_ADMIN": "DBMS_CLOUD_ADMIN",
    }

    try:
        rows = _query_all(conn, """
            SELECT table_name AS pkg_name
            FROM   USER_TAB_PRIVS_RECD
            WHERE  privilege  = 'EXECUTE'
              AND  table_name IN (:n1, :n2, :n3, :n4)
        """, {
            "n1": check_names["DBMS_CLOUD_AI"],
            "n2": check_names["DBMS_CLOUD_AI_AGENT"],
            "n3": check_names["DBMS_CLOUD"],
            "n4": check_names["DBMS_CLOUD_ADMIN"],
        })
        granted = {row["pkg_name"].upper() for row in rows}
    except Exception:
        granted = set()

    for pkg in REQUIRED_PACKAGES:
        resolved_name = check_names.get(pkg, pkg)
        if resolved_name.upper() in granted:
            r.ok(f"EXECUTE on {pkg} — granted to {target_schema}")
        else:
            r.fail(f"EXECUTE on {pkg} — NOT granted to {target_schema}",
                   f"Run as ADMIN: GRANT EXECUTE ON {pkg} TO {target_schema};")


def _check_schema_roles(conn, target_schema, r: CheckResult):
    """Check PYQADMIN and OML_DEVELOPER roles for the target schema.

    NOTE: USER_ROLE_PRIVS (roles granted to/enabled for the current user)
    instead of DBA_ROLE_PRIVS — same reasoning as _check_schema_exists.
    """
    try:
        rows = _query_all(conn, """
            SELECT granted_role
            FROM   USER_ROLE_PRIVS
        """)
        granted = {row["granted_role"].upper() for row in rows}
    except Exception:
        granted = set()

    for role in REQUIRED_ROLES:
        if role in granted:
            r.ok(f"Role {role} — granted to {target_schema}")
        else:
            r.fail(f"Role {role} — NOT granted to {target_schema}",
                   f"Run as ADMIN: GRANT {role} TO {target_schema};")


def _check_epe_acl(conn, target_schema, r: CheckResult):
    """Check pyqAppendHostAce network ACL for OML endpoint.

    NOTE: there is no USER_-scoped equivalent of DBA_NETWORK_ACL_PRIVILEGES,
    so this still needs catalog access this proxied session won't have —
    that query is expected to fail here. The bug fixed below: previously the
    pyqGetHostAce fallback only ran when the DBA_* query *succeeded* but
    returned zero rows, not when it *raised* (which it always does without
    catalog access) — so the fallback never actually executed. Restructured
    so any failure of the primary path — exception or empty result — falls
    through to the function-based fallback.
    """
    rows = None
    try:
        rows = _query_all(conn, """
            SELECT host, lower_port, upper_port
            FROM   dba_network_acl_privileges
            WHERE  principal = :schema
              AND  privilege  = 'connect'
        """, {"schema": target_schema.upper()})
    except Exception:
        rows = None  # fall through to the function-based fallback below

    if rows:
        hosts = [f"{a['host']}" for a in rows]
        r.ok(f"EPE network ACL — {len(rows)} entry/entries for {target_schema}",
             "\n".join(hosts[:5]))
        return

    # Fallback: ask pyqGetHostAce directly — works without catalog access,
    # since it's a function call against the current (target) schema.
    try:
        row = _query_one(conn,
            f"SELECT pyqGetHostAce('{target_schema}') AS ace FROM dual")
        if row and row.get("ace"):
            r.ok(f"EPE network ACL — confirmed via pyqGetHostAce")
        else:
            r.fail(f"EPE network ACL — no entries found for {target_schema}",
                   f"Run as ADMIN: EXEC pyqAppendHostAce('{target_schema}', "
                   f"'adb.<region>.oraclecloudapps.com');")
    except Exception as ex:
        r.warn("EPE ACL check failed", str(ex)[:100])


def _check_agent_views(conn, r: CheckResult):
    """Check agent framework views are accessible."""
    for view in REQUIRED_VIEWS:
        try:
            _query_one(conn, f"SELECT COUNT(*) FROM {view}")
            r.ok(f"View {view} — accessible")
        except Exception as ex:
            ex_str = str(ex)
            if "table or view does not exist" in ex_str.lower():
                r.fail(f"View {view} — does not exist",
                       "ADW version may not support Select AI Agent framework")
            else:
                r.warn(f"View {view} — query error", ex_str[:100])


def _check_nl2sql_comments(conn, cfg, r: CheckResult):
    """Check NL2SQL comment coverage on source tables."""
    raw = cfg_module.get(cfg, "de", "source_tables", fallback="").strip()
    if not raw:
        r.skip("NL2SQL comment coverage — no source_tables set in [de] config",
               "Set [de] source_tables = SCHEMA.TABLE1, SCHEMA.TABLE2 to enable")
        return

    tables = [t.strip().upper() for t in raw.split(",") if t.strip()]
    for tbl in tables:
        parts  = tbl.split(".")
        owner  = parts[0] if len(parts) > 1 else ""
        tname  = parts[-1]

        # Table-level comment
        try:
            tab_row = _query_one(conn, """
                SELECT comments
                FROM   all_tab_comments
                WHERE  owner      = :owner
                  AND  table_name = :tname
                  AND  comments IS NOT NULL
            """, {"owner": owner, "tname": tname})
            if tab_row:
                r.ok(f"Table comment — {tbl} ✓")
            else:
                r.warn(f"Table comment — {tbl} — MISSING",
                       "Run SA_02_ACME_CORP_config.sql or Admin Setup → NL2SQL comments")
        except Exception as ex:
            r.warn(f"Table comment check failed for {tbl}", str(ex)[:80])

        # Column comment count
        try:
            rows = _query_all(conn, """
                SELECT COUNT(*) AS cnt
                FROM   all_col_comments
                WHERE  owner      = :owner
                  AND  table_name = :tname
                  AND  comments IS NOT NULL
            """, {"owner": owner, "tname": tname})
            cnt = rows[0]["cnt"] if rows else 0
            if cnt > 0:
                r.ok(f"Column comments — {tbl} ({cnt} columns)")
            else:
                r.warn(f"Column comments — {tbl} — NONE SET",
                       "Run SA_02_ACME_CORP_config.sql to set column-level NL2SQL metadata")
        except Exception as ex:
            r.warn(f"Column comment check failed for {tbl}", str(ex)[:80])


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(cfg, config_path: str, clients: dict, display):
    C = display.C
    target_schema = cfg_module.get(cfg, "database", "target_schema",
                                   fallback="").strip().upper()
    db_user_fallback = cfg_module.get(cfg, "database", "db_user",
                                      fallback="").strip().upper()

    # target_schema blank means direct-login mode (no proxy) — db_user IS
    # the schema being checked in that case. Only fail if BOTH are blank.
    if not target_schema:
        if not db_user_fallback:
            display.err("Neither target_schema nor db_user is set in [database] config section")
            display.info("Set target_schema = ACME_CORP (proxy mode), or db_user = ACME_CORP with target_schema blank (direct mode)")
            return
        target_schema = db_user_fallback
        display.info(f"target_schema not set — using db_user as direct-login schema: {target_schema}")

    display.head(f"PRE-FLIGHT CHECK — SCHEMA: {target_schema}")
    display.blank()
    print(f"  {C.DIM}Checks whether {target_schema} is properly bootstrapped for Select AI Agent.{C.RESET}")
    print(f"  {C.DIM}This may take 15–30 seconds.{C.RESET}")

    r_cfg  = CheckResult()
    r_data = CheckResult()

    # ── Config check ──────────────────────────────────────────────────────────
    schema_config_fields = [
        ("oci",      "region",        "OCI region",                    True,  False),
        ("database", "db_user",       "personal schema username",       True,  False),
        ("database", "target_schema", "agent-owning schema name (blank = direct-login mode)", False, False),
        ("database", "dsn",           "TNS alias from tnsnames.ora",    True,  False),
        ("database", "wallet_dir",    "full path to ADW wallet dir",    True,  False),
        ("database", "lib_dir",       "full path to Instant Client",    True,  False),
    ]
    from modules.preflight_dsde import _check_config as _check_config_section
    can_connect = _check_config_section(schema_config_fields, cfg, r_cfg)

    _print_section_box(f"Config — fields for schema check", r_cfg.items, display)

    if not can_connect:
        r_data.skip("Schema checks skipped — critical config fields missing")
        _print_section_box(f"Schema: {target_schema}", r_data.items, display)
        return

    # ── Connect and check ─────────────────────────────────────────────────────
    display.blank()
    display.info(f"Connecting to check {target_schema} configuration...")

    db_user_s     = cfg_module.get(cfg, "database", "db_user", fallback="").strip().upper()
    # target_schema_s uses the already-resolved value (falls back to db_user
    # in direct-login mode) — NOT a fresh cfg read, which would be blank again.
    target_schema_s = target_schema if target_schema != db_user_s else ""
    from modules.preflight_dsde import _get_password
    password_s, pwd_err = _get_password(cfg, db_user_s, display)
    if pwd_err:
        r_data.fail("Password resolution failed", pwd_err)
        _print_section_box(f"Schema: {target_schema}", r_data.items, display)
        return
    conn = _connect_proxy(cfg, db_user_s, target_schema_s, password_s, r_data)
    proxy_user = f"{db_user_s}[{target_schema_s}]" if target_schema_s else db_user_s
    if conn is None:
        _print_section_box(f"Schema: {target_schema}", r_data.items, display)
        return

    r_data.ok(f"Connected as {proxy_user}")

    try:
        _check_schema_exists       (conn, target_schema, r_data)
        _check_resource_principal  (conn, target_schema, r_data)
        _check_schema_package_grants(conn, target_schema, r_data)
        _check_schema_roles        (conn, target_schema, r_data)
        _check_epe_acl             (conn, target_schema, r_data)
        _check_agent_views         (conn, r_data)
        _check_nl2sql_comments     (conn, cfg, r_data)
        _check_oci_genai           (clients, r_data)
    finally:
        conn.close()

    _print_section_box(f"Schema: {target_schema}", r_data.items, display)

    # ── Summary ───────────────────────────────────────────────────────────────
    display.blank()
    print(f"  {'─'*60}")
    merged = CheckResult()
    merged.items = r_cfg.items + r_data.items
    _print_summary(f"Schema {target_schema}", merged, display)
    print(f"  {'─'*60}")
    display.blank()
    if merged.fail_count > 0:
        print(f"  {C.DIM}To fix schema issues — ask ADMIN to run SA_01_ACME_CORP_setup.sql{C.RESET}")
        print(f"  {C.DIM}For NL2SQL comments — run SA_02_ACME_CORP_config.sql{C.RESET}")
    else:
        print(f"  {C.GREEN}Schema {target_schema} is properly configured for Select AI Agent.{C.RESET}")
