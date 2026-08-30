"""
modules/grant_check.py
DE feature — audit a DS schema's grants and identify what is missing
before they can build Select AI Agents against a target data schema
(e.g. acme_corp).

Called from the ADMIN SETUP submenu.  The DE connects as ADMIN (or their
own DE schema with DBA-level access) and inspects:

  1. The DS schema's EXECUTE grants on required DBMS_ packages
  2. The DS schema's role grants (PYQADMIN, OML_DEVELOPER)
  3. Resource Principal enablement for the DS schema
  4. Network ACE (pyqAppendHostAce) for the DS schema
  5. Vault credential in the DS schema
  6. Agent framework view accessibility
  7. Cross-schema SELECT grants from DS schema → target data schema (acme_corp)
  8. Optional: COMMENT ON privilege on the target data schema

For each missing item the output prints the exact GRANT or EXEC statement
the DE needs to run to fix it.
"""

from __future__ import annotations

import os
from core import config as cfg_module
from core import db as db_module


# ── Grant requirements ────────────────────────────────────────────────────────

# Packages the DS schema needs EXECUTE on
REQUIRED_EXECUTE_GRANTS = [
    "DBMS_CLOUD_AI",
    "DBMS_CLOUD_AI_AGENT",
    "DBMS_CLOUD",
    "DBMS_VECTOR_CHAIN",
]

# Roles the DS schema must have
REQUIRED_ROLES = [
    "PYQADMIN",
    "OML_DEVELOPER",
]

# Agent framework views the DS schema must be able to SELECT
REQUIRED_VIEWS = [
    "USER_AI_AGENT_TOOLS",
    "USER_AI_AGENTS",
    "USER_AI_AGENT_TASKS",
    "USER_AI_AGENT_TEAMS",
    "USER_CLOUD_AI_PROFILES",
]

# ── Result types ──────────────────────────────────────────────────────────────

class GrantItem:
    """A single check result with the fix SQL if it failed."""
    OK   = "ok"
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"

    def __init__(self, status: str, label: str,
                 detail: str = "", fix_sql: str = ""):
        self.status  = status
        self.label   = label
        self.detail  = detail
        self.fix_sql = fix_sql   # exact SQL the DE should run to fix it


class GrantReport:
    def __init__(self, ds_schema: str, data_schema: str,
                 admin_schema: str):
        self.ds_schema    = ds_schema.upper()
        self.data_schema  = data_schema.upper()
        self.admin_schema = admin_schema.upper()
        self.sections: list[tuple[str, list[GrantItem]]] = []

    def add_section(self, title: str, items: list[GrantItem]):
        self.sections.append((title, items))

    @property
    def all_items(self) -> list[GrantItem]:
        return [i for _, items in self.sections for i in items]

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.all_items if i.status == GrantItem.FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for i in self.all_items if i.status == GrantItem.WARN)

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.all_items if i.status == GrantItem.OK)

    def fix_statements(self) -> list[str]:
        """Return all fix_sql statements in run-order."""
        return [i.fix_sql for i in self.all_items
                if i.status == GrantItem.FAIL and i.fix_sql]


# ── DB helpers ────────────────────────────────────────────────────────────────

def _qall(conn, sql: str, params: dict = None) -> list[dict]:
    cur = conn.cursor()
    cur.execute(sql, params or {})
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _qone(conn, sql: str, params: dict = None) -> dict | None:
    rows = _qall(conn, sql, params)
    return rows[0] if rows else None


# ── Individual check functions ────────────────────────────────────────────────

def _check_execute_grants(conn, ds_schema: str) -> list[GrantItem]:
    """Check EXECUTE grants on required DBMS packages via DBA_TAB_PRIVS."""
    items = []

    # DBA_TAB_PRIVS is authoritative here — we're connected as ADMIN so
    # we can see all grants. We check both direct grants and grants through
    # DWROLE by also checking DBA_ROLE_PRIVS chain.
    try:
        granted_rows = _qall(conn, """
            SELECT table_name
            FROM   dba_tab_privs
            WHERE  grantee  IN (
                       -- direct grant to the user
                       :schema,
                       -- grants to roles the user holds (resolves DWROLE etc.)
                       SELECT granted_role
                       FROM   dba_role_privs
                       START  WITH grantee = :schema
                       CONNECT BY PRIOR granted_role = grantee
                   )
            AND    privilege = 'EXECUTE'
            AND    table_name IN (
                       'DBMS_CLOUD_AI', 'DBMS_CLOUD_AI_AGENT',
                       'DBMS_CLOUD', 'DBMS_VECTOR_CHAIN'
                   )
        """, {"schema": ds_schema})
        granted = {r["table_name"] for r in granted_rows}
    except Exception:
        # Fallback: DBA_TAB_PRIVS direct only (no role resolution)
        try:
            granted_rows = _qall(conn, """
                SELECT table_name
                FROM   dba_tab_privs
                WHERE  grantee   = :schema
                AND    privilege  = 'EXECUTE'
                AND    table_name IN (
                           'DBMS_CLOUD_AI', 'DBMS_CLOUD_AI_AGENT',
                           'DBMS_CLOUD', 'DBMS_VECTOR_CHAIN'
                       )
            """, {"schema": ds_schema})
            granted = {r["table_name"] for r in granted_rows}
        except Exception as ex:
            items.append(GrantItem(
                GrantItem.WARN,
                "EXECUTE grants — could not query DBA_TAB_PRIVS",
                str(ex)[:120],
            ))
            return items

    for pkg in REQUIRED_EXECUTE_GRANTS:
        if pkg in granted:
            items.append(GrantItem(GrantItem.OK, f"EXECUTE on {pkg}"))
        else:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"EXECUTE on {pkg} — not granted",
                f"Grant EXECUTE on {pkg} to {ds_schema}",
                fix_sql=f"GRANT EXECUTE ON {pkg} TO {ds_schema};",
            ))
    return items


def _check_role_grants(conn, ds_schema: str) -> list[GrantItem]:
    """Check PYQADMIN and OML_DEVELOPER role grants via DBA_ROLE_PRIVS."""
    items = []
    try:
        role_rows = _qall(conn, """
            SELECT granted_role
            FROM   dba_role_privs
            START  WITH grantee = :schema
            CONNECT BY PRIOR granted_role = grantee
        """, {"schema": ds_schema})
        granted_roles = {r["granted_role"] for r in role_rows}
    except Exception:
        try:
            role_rows = _qall(conn, """
                SELECT granted_role FROM dba_role_privs
                WHERE  grantee = :schema
            """, {"schema": ds_schema})
            granted_roles = {r["granted_role"] for r in role_rows}
        except Exception as ex:
            items.append(GrantItem(
                GrantItem.WARN,
                "Role grants — could not query DBA_ROLE_PRIVS",
                str(ex)[:120],
            ))
            return items

    for role in REQUIRED_ROLES:
        if role in granted_roles:
            items.append(GrantItem(GrantItem.OK, f"Role {role}"))
        else:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"Role {role} — not granted",
                f"Grant role {role} to {ds_schema}",
                fix_sql=f"GRANT {role} TO {ds_schema};",
            ))
    return items


def _check_resource_principal(conn, ds_schema: str) -> list[GrantItem]:
    """Check Resource Principal enablement via DBA_CLOUD_LINK_CONFIG."""
    items = []
    try:
        row = _qone(conn, """
            SELECT value
            FROM   dba_db_links
            WHERE  ROWNUM = 1
        """)
        # Indirect check — use v$parameter which ADMIN can read
        row = _qone(conn, """
            SELECT COUNT(*) AS cnt
            FROM   dba_cloud_link_config
            WHERE  schema_name = :schema
            AND    config_type = 'RESOURCE_PRINCIPAL'
        """, {"schema": ds_schema})
        if row and int(row.get("cnt", 0)) > 0:
            items.append(GrantItem(
                GrantItem.OK,
                f"Resource Principal enabled for {ds_schema}",
            ))
        else:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"Resource Principal NOT enabled for {ds_schema}",
                "The schema cannot authenticate to OCI services without this.",
                fix_sql=(
                    f"-- Run as ADMIN:\n"
                    f"EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL("
                    f"username => '{ds_schema}');"
                ),
            ))
    except Exception:
        # dba_cloud_link_config may not be available in all ADW versions
        # Fall back to checking sys.dbms_cloud_admin directly
        try:
            row = _qone(conn, """
                SELECT TO_CHAR(attribute_value) AS rp_enabled
                FROM   dba_cloud_ai_config
                WHERE  schema_name = :schema
                AND    attribute_name = 'resource_principal_enabled'
            """, {"schema": ds_schema})
            if row and str(row.get("rp_enabled", "")).upper() == "TRUE":
                items.append(GrantItem(
                    GrantItem.OK,
                    f"Resource Principal enabled for {ds_schema}",
                ))
            else:
                items.append(GrantItem(
                    GrantItem.FAIL,
                    f"Resource Principal NOT enabled for {ds_schema}",
                    "The schema cannot authenticate to OCI services without this.",
                    fix_sql=(
                        f"-- Run as ADMIN:\n"
                        f"EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL("
                        f"username => '{ds_schema}');"
                    ),
                ))
        except Exception as ex2:
            items.append(GrantItem(
                GrantItem.WARN,
                f"Resource Principal — could not verify from ADMIN connection",
                (
                    f"Could not read configuration view: {str(ex2)[:100]}. "
                    f"Verify manually: EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL("
                    f"username => '{ds_schema}');"
                ),
                fix_sql=(
                    f"-- Verify / enable if not already done:\n"
                    f"EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL("
                    f"username => '{ds_schema}');"
                ),
            ))
    return items


def _check_network_ace(conn, ds_schema: str) -> list[GrantItem]:
    """Check pyqAppendHostAce network ACL entries for the DS schema."""
    items = []
    try:
        ace_rows = _qall(conn, """
            SELECT host, lower_port, upper_port, privilege
            FROM   dba_network_acl_privileges
            WHERE  aclid IN (
                       SELECT aclid FROM dba_network_acls
                       WHERE  acl LIKE '%' || :schema || '%'
                   )
            AND    privilege = 'connect'
        """, {"schema": ds_schema.lower()})

        if not ace_rows:
            # Try pyqhostace$ directly
            ace_rows = _qall(conn, """
                SELECT host, lower_port, upper_port
                FROM   sys.pyqhostace$
                WHERE  schema_name = :schema
            """, {"schema": ds_schema})

        if ace_rows:
            hosts = [
                f"{r.get('host','?')}:{r.get('lower_port','?')}-{r.get('upper_port','?')}"
                for r in ace_rows[:5]
            ]
            items.append(GrantItem(
                GrantItem.OK,
                f"Network ACE — {len(ace_rows)} entry/entries for {ds_schema}",
                "\n".join(hosts),
            ))
        else:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"Network ACE — no entries found for {ds_schema}",
                "Required for OML execution and outbound OCI calls.",
                fix_sql=(
                    f"-- Run as ADMIN (replace OML_HOST with your ADW OML base URL):\n"
                    f"BEGIN\n"
                    f"  sys.pyqAppendHostAce(\n"
                    f"    p_host       => '<your-oml-host>.oraclecloudapps.com',\n"
                    f"    p_schema     => '{ds_schema}',\n"
                    f"    p_lower_port => 443,\n"
                    f"    p_upper_port => 443\n"
                    f"  );\n"
                    f"END;\n/"
                ),
            ))
    except Exception as ex:
        items.append(GrantItem(
            GrantItem.WARN,
            "Network ACE — could not query from ADMIN connection",
            str(ex)[:120],
        ))
    return items


def _check_vault_credential(conn, ds_schema: str, cred_name: str) -> list[GrantItem]:
    """Check that the Vault credential exists in the DS schema."""
    items = []
    try:
        row = _qone(conn, """
            SELECT credential_name, username, enabled
            FROM   dba_credentials
            WHERE  owner           = :schema
            AND    credential_name = :cred
        """, {"schema": ds_schema, "cred": cred_name.upper()})

        if row:
            enabled = str(row.get("enabled", "")).upper()
            status  = GrantItem.OK if enabled in ("TRUE", "YES", "1") else GrantItem.WARN
            items.append(GrantItem(
                status,
                f"Vault credential '{cred_name}' in {ds_schema} — "
                f"{'enabled' if status == GrantItem.OK else 'exists but may be disabled'}",
            ))
        else:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"Vault credential '{cred_name}' not found in {ds_schema}",
                "Required for OML token refresh and OCI service calls.",
                fix_sql=(
                    f"-- Run as ADMIN or as {ds_schema}:\n"
                    f"BEGIN\n"
                    f"  DBMS_CLOUD.CREATE_CREDENTIAL(\n"
                    f"    credential_name => '{cred_name}',\n"
                    f"    username        => '{ds_schema}',\n"
                    f"    password        => '<vault-secret-ocid>'\n"
                    f"  );\n"
                    f"END;\n/"
                ),
            ))
    except Exception as ex:
        items.append(GrantItem(
            GrantItem.WARN,
            "Vault credential — could not query DBA_CREDENTIALS",
            str(ex)[:120],
        ))
    return items


def _check_agent_views(conn, ds_schema: str) -> list[GrantItem]:
    """Verify agent framework views are accessible (implies grants are correct)."""
    items = []
    for view in REQUIRED_VIEWS:
        try:
            _qone(conn, f"SELECT COUNT(*) FROM {view}")
            items.append(GrantItem(
                GrantItem.OK,
                f"View {view} accessible",
            ))
        except Exception as ex:
            ex_str = str(ex).lower()
            if "does not exist" in ex_str or "942" in ex_str:
                items.append(GrantItem(
                    GrantItem.FAIL,
                    f"View {view} — does not exist",
                    "ADW version may not support Select AI Agent framework.",
                ))
            else:
                items.append(GrantItem(
                    GrantItem.WARN,
                    f"View {view} — query error",
                    str(ex)[:100],
                ))
    return items


def _check_cross_schema_select(conn, ds_schema: str,
                                data_schema: str) -> list[GrantItem]:
    """Check SELECT grants from ds_schema → data_schema tables.

    We check:
      a) Schema-level grant: SELECT ANY TABLE ON SCHEMA data_schema TO ds_schema
      b) Per-table grants for each table in data_schema owned by ds_schema
         (via DBA_TAB_PRIVS)

    If neither a) nor b) covers all tables, we list which tables are missing.
    """
    items = []

    # -- Schema-level grant check (ADW 23ai+) ----------------------------------
    schema_grant = False
    try:
        row = _qone(conn, """
            SELECT COUNT(*) AS cnt
            FROM   dba_sys_privs
            WHERE  grantee    = :ds
            AND    privilege  LIKE '%ANY TABLE%'
            AND    privilege  LIKE '%SELECT%'
        """, {"ds": ds_schema})
        if row and int(row.get("cnt", 0)) > 0:
            schema_grant = True
    except Exception:
        pass

    # -- Per-table SELECT grants -----------------------------------------------
    try:
        table_rows = _qall(conn, """
            SELECT table_name FROM dba_tables
            WHERE  owner = :data_schema
            ORDER  BY table_name
        """, {"data_schema": data_schema})
        all_tables = [r["table_name"] for r in table_rows]
    except Exception as ex:
        items.append(GrantItem(
            GrantItem.WARN,
            f"Cross-schema tables — could not list {data_schema} tables",
            str(ex)[:120],
        ))
        return items

    if not all_tables:
        items.append(GrantItem(
            GrantItem.INFO,
            f"No tables found in {data_schema}",
            "Schema may be empty or ADMIN cannot see it.",
        ))
        return items

    if schema_grant:
        items.append(GrantItem(
            GrantItem.OK,
            f"Schema-level SELECT ANY TABLE ON SCHEMA {data_schema} → {ds_schema}",
            f"Covers all {len(all_tables)} current and future tables automatically.",
        ))
        return items

    # Per-table grant check
    try:
        grant_rows = _qall(conn, """
            SELECT table_name FROM dba_tab_privs
            WHERE  owner     = :data_schema
            AND    grantee   = :ds
            AND    privilege = 'SELECT'
        """, {"data_schema": data_schema, "ds": ds_schema})
        granted_tables = {r["table_name"] for r in grant_rows}
    except Exception as ex:
        items.append(GrantItem(
            GrantItem.WARN,
            f"Cross-schema SELECT grants — could not query DBA_TAB_PRIVS",
            str(ex)[:120],
        ))
        return items

    missing_tables = [t for t in all_tables if t not in granted_tables]
    covered_tables = [t for t in all_tables if t in granted_tables]

    if not missing_tables:
        items.append(GrantItem(
            GrantItem.OK,
            f"SELECT grants: {ds_schema} → all {len(all_tables)} tables in {data_schema}",
        ))
    else:
        # Recommend schema-level grant as the better fix
        schema_fix = (
            f"-- Recommended: schema-level grant (covers all current and future tables)\n"
            f"GRANT SELECT ANY TABLE ON SCHEMA {data_schema} TO {ds_schema};\n\n"
            f"-- Alternative: per-table grants\n"
        ) + "\n".join(
            f"GRANT SELECT ON {data_schema}.{t} TO {ds_schema};"
            for t in missing_tables
        )

        if covered_tables:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"SELECT grants: {ds_schema} → {data_schema}  "
                f"({len(covered_tables)}/{len(all_tables)} tables covered, "
                f"{len(missing_tables)} missing)",
                "Missing tables:\n" + "\n".join(f"  {data_schema}.{t}" for t in missing_tables[:20])
                + (f"\n  ... and {len(missing_tables)-20} more" if len(missing_tables) > 20 else ""),
                fix_sql=schema_fix,
            ))
        else:
            items.append(GrantItem(
                GrantItem.FAIL,
                f"SELECT grants: {ds_schema} has NO SELECT access to any table in {data_schema}",
                f"{len(all_tables)} tables exist in {data_schema}, none accessible.",
                fix_sql=schema_fix,
            ))

    return items


def _check_comment_privilege(conn, ds_schema: str,
                              data_schema: str) -> list[GrantItem]:
    """Check whether ds_schema can COMMENT ON tables in data_schema."""
    items = []
    try:
        # Check for schema-level COMMENT privilege
        row = _qone(conn, """
            SELECT COUNT(*) AS cnt
            FROM   dba_sys_privs
            WHERE  grantee   = :ds
            AND    privilege LIKE '%COMMENT%'
        """, {"ds": ds_schema})
        schema_comment = row and int(row.get("cnt", 0)) > 0

        # Check per-table COMMENT grants
        table_rows = _qall(conn, """
            SELECT COUNT(*) AS cnt
            FROM   dba_tab_privs
            WHERE  grantee   = :ds
            AND    owner     = :data_schema
            AND    privilege = 'COMMENT'
        """, {"ds": ds_schema, "data_schema": data_schema})
        per_table_comment = table_rows and int(table_rows[0].get("cnt", 0)) > 0

        if schema_comment:
            items.append(GrantItem(
                GrantItem.OK,
                f"COMMENT ANY TABLE ON SCHEMA {data_schema} → {ds_schema} — granted",
                "DS can push COMMENT ON statements to production tables.",
            ))
        elif per_table_comment:
            items.append(GrantItem(
                GrantItem.OK,
                f"Per-table COMMENT privilege on {data_schema} → {ds_schema} — granted",
            ))
        else:
            items.append(GrantItem(
                GrantItem.INFO,
                f"COMMENT privilege on {data_schema} — not granted to {ds_schema}",
                (
                    "This is optional. Without it, DS can generate COMMENT ON SQL files "
                    "but cannot push them to the database directly. "
                    "Omit this grant to prevent DS from modifying production metadata."
                ),
                fix_sql=(
                    f"-- Optional: grant only if DS should push comments to production tables\n"
                    f"-- Schema-level (all current and future tables):\n"
                    f"GRANT COMMENT ANY TABLE ON SCHEMA {data_schema} TO {ds_schema};\n\n"
                    f"-- Or selectively per table:\n"
                    f"GRANT COMMENT ON {data_schema}.<table_name> TO {ds_schema};"
                ),
            ))
    except Exception as ex:
        items.append(GrantItem(
            GrantItem.WARN,
            "COMMENT privilege — could not verify",
            str(ex)[:120],
        ))
    return items


# ── Main check function ───────────────────────────────────────────────────────

def run_grant_check(cfg, ds_schema: str, data_schema: str,
                    cred_name: str, conn) -> GrantReport:
    """Run all grant checks and return a GrantReport.

    conn must be an ADMIN-level or DE-level connection with access to
    DBA_TAB_PRIVS, DBA_ROLE_PRIVS, DBA_SYS_PRIVS, DBA_TABLES etc.
    """
    admin_schema = cfg_module.get(cfg, "database", "db_user", fallback="ADMIN")
    report = GrantReport(ds_schema, data_schema, admin_schema)

    report.add_section(
        "EXECUTE Grants (DBMS packages)",
        _check_execute_grants(conn, ds_schema)
    )
    report.add_section(
        "Role Grants",
        _check_role_grants(conn, ds_schema)
    )
    report.add_section(
        "Resource Principal",
        _check_resource_principal(conn, ds_schema)
    )
    report.add_section(
        "Network ACE (pyqAppendHostAce)",
        _check_network_ace(conn, ds_schema)
    )
    report.add_section(
        f"Vault Credential in {ds_schema}",
        _check_vault_credential(conn, ds_schema, cred_name)
    )
    report.add_section(
        "Agent Framework Views",
        _check_agent_views(conn, ds_schema)
    )
    report.add_section(
        f"Cross-Schema SELECT: {ds_schema} → {data_schema}",
        _check_cross_schema_select(conn, ds_schema, data_schema)
    )
    report.add_section(
        f"COMMENT Privilege: {ds_schema} → {data_schema} (optional)",
        _check_comment_privilege(conn, ds_schema, data_schema)
    )

    return report


# ── Display ───────────────────────────────────────────────────────────────────

def print_report(report: GrantReport, display) -> None:
    C = display.C

    display.blank()
    print(f"  {C.BOLD}{'═' * 60}{C.RESET}")
    print(f"  {C.BOLD}GRANT AUDIT — {report.ds_schema}{C.RESET}")
    print(f"  {'─' * 60}")
    print(f"  Checked by  : {report.admin_schema}")
    print(f"  DS schema   : {report.ds_schema}")
    print(f"  Data schema : {report.data_schema}")
    print(f"  {'═' * 60}")

    for section_title, items in report.sections:
        display.blank()
        print(f"  {C.BOLD}{section_title}{C.RESET}")
        print(f"  {'─' * 60}")
        for item in items:
            if item.status == GrantItem.OK:
                sym, col = "✓", C.GREEN
            elif item.status == GrantItem.FAIL:
                sym, col = "✗", C.RED
            elif item.status == GrantItem.WARN:
                sym, col = "⚠", C.YELLOW
            else:
                sym, col = "ℹ", C.CYAN

            print(f"  {col}{sym}{C.RESET}  {item.label}")
            if item.detail:
                for line in item.detail.splitlines():
                    print(f"       {C.DIM}{line}{C.RESET}")

    # ── Summary ───────────────────────────────────────────────────────────────
    display.blank()
    print(f"  {'─' * 60}")
    fails = report.fail_count
    warns = report.warn_count
    oks   = report.ok_count
    if fails == 0 and warns == 0:
        print(f"  {C.GREEN}{C.BOLD}All checks passed — {report.ds_schema} is ready "
              f"to build agents against {report.data_schema}.{C.RESET}")
    elif fails == 0:
        print(f"  {C.YELLOW}{C.BOLD}{oks} passed, {warns} warning(s) — "
              f"review warnings before proceeding.{C.RESET}")
    else:
        print(f"  {C.RED}{C.BOLD}{fails} missing grant(s) must be applied "
              f"before {report.ds_schema} can build agents.{C.RESET}")
        if warns:
            print(f"  {C.YELLOW}  {warns} additional warning(s) to review.{C.RESET}")
    print(f"  {'─' * 60}")

    # ── Fix statements ────────────────────────────────────────────────────────
    fix_stmts = report.fix_statements()
    if fix_stmts:
        display.blank()
        print(f"  {C.BOLD}GRANTS TO APPLY — run as ADMIN or a privileged DE schema:{C.RESET}")
        print(f"  {'─' * 60}")
        for sql in fix_stmts:
            for line in sql.splitlines():
                print(f"    {C.YELLOW}{line}{C.RESET}")
            print()

        # Offer to save the fix script
        display.blank()
        try:
            save_path = input(
                f"  Save fix script to file? "
                f"[Enter path or press Enter to skip]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            save_path = ""

        if save_path:
            try:
                from pathlib import Path
                out = Path(save_path).expanduser().resolve()
                out.parent.mkdir(parents=True, exist_ok=True)
                script = (
                    f"-- Grant fix script for {report.ds_schema}\n"
                    f"-- Generated by Select AI Agent Builder\n"
                    f"-- DS schema  : {report.ds_schema}\n"
                    f"-- Data schema: {report.data_schema}\n"
                    f"-- Run as     : ADMIN or DE schema with DBA_* view access\n"
                    f"-- {'─' * 55}\n\n"
                    + "\n\n".join(fix_stmts)
                    + "\n"
                )
                out.write_text(script, encoding="utf-8")
                display.ok(f"Fix script saved: {out}")
            except Exception as ex:
                display.err(f"Could not save fix script: {ex}")


# ── Interactive entry point ───────────────────────────────────────────────────

def run(cfg, clients: dict, display) -> None:
    """Interactive DE-facing grant audit. Called from ADMIN SETUP menu."""
    C = display.C
    display.head("DS SCHEMA GRANT AUDIT")
    display.blank()
    print(f"  {C.DIM}Check what grants a DS schema has and what it still needs{C.RESET}")
    print(f"  {C.DIM}to build Select AI Agents against a target data schema.{C.RESET}")
    display.blank()

    # ── Collect inputs ────────────────────────────────────────────────────────
    try:
        ds_schema = input(
            f"  {C.BOLD}DS schema to audit{C.RESET} "
            f"{C.DIM}(e.g. JANE_DS, or q to cancel){C.RESET}: "
        ).strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return
    if not ds_schema or ds_schema in ("Q", "QUIT", "EXIT", "B", "BACK"):
        display.warn("Cancelled")
        return

    default_data = cfg_module.get(cfg, "de", "target_data_schema", fallback="ACME_CORP")
    try:
        data_schema = input(
            f"  {C.BOLD}Data schema to check SELECT access against{C.RESET} "
            f"{C.DIM}[{default_data}]{C.RESET}: "
        ).strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return
    if data_schema in ("Q", "QUIT", "EXIT", "B", "BACK"):
        display.warn("Cancelled")
        return
    if not data_schema:
        data_schema = default_data

    cred_name = cfg_module.get(
        cfg, "de", "oml_credential_name", fallback=f"{data_schema}_OML_CRED"
    )

    # ── Connect for the audit ─────────────────────────────────────────────────
    # Preference: ADMIN connection (full DBA_* view access, most accurate).
    # Fallback: DE user's own schema (the one that passed the startup privilege
    # check — NOT [database] db_user which belongs to the DS / app schema).
    display.blank()
    use_admin  = False
    admin_dsn  = cfg_module.get(cfg, "de", "admin_dsn",  fallback="")
    admin_user = cfg_module.get(cfg, "de", "admin_user", fallback="ADMIN")

    # Determine the DE user's own schema.  [de] de_schema is the explicit key;
    # fall back to [database] db_user only when nothing else is configured.
    de_schema = (
        cfg_module.get(cfg, "de", "de_schema", fallback="")
        or cfg_module.get(cfg, "database", "db_user", fallback="")
    ).strip().upper()

    if admin_dsn:
        try:
            ans = input(
                f"  Connect as {admin_user} for full DBA_* view access? [Y/n]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "y"
        use_admin = ans not in ("n", "no")
    else:
        display.warn(
            "admin_dsn is not set in [de] config — cannot offer ADMIN connection. "
            f"Set [de] admin_dsn to enable full grant auditing. "
            f"Connecting as {de_schema or 'DE schema'} instead."
        )

    conn = None

    if use_admin and admin_dsn:
        display.info(f"Connecting as {admin_user}...")
        try:
            import oracledb
            wallet_dir = os.path.expanduser(cfg_module.get(cfg, "database", "wallet_dir"))
            lib_dir    = os.path.expanduser(cfg_module.get(cfg, "database", "lib_dir"))
            os.environ["TNS_ADMIN"] = wallet_dir
            try:
                oracledb.init_oracle_client(lib_dir=lib_dir)
            except Exception as ex:
                if "already been called" not in str(ex).lower():
                    raise
            admin_pass = db_module.resolve_password(cfg, admin_user, allow_prompt=True)
            conn = oracledb.connect(
                user=admin_user, password=admin_pass,
                dsn=admin_dsn, wallet_location=wallet_dir
            )
            display.ok(f"Connected as {admin_user}")
        except Exception as ex:
            display.err(f"ADMIN connection failed: {ex}")
            display.info(f"Falling back to {de_schema} connection...")
            use_admin = False

    if not use_admin:
        if not de_schema:
            display.err(
                "Cannot determine DE schema identity. "
                "Set [de] de_schema or [database] db_user in config."
            )
            return
        display.info(
            f"Connecting as {de_schema} "
            f"(some DBA_* views may be limited without ADMIN access)..."
        )
        try:
            import oracledb
            wallet_dir = os.path.expanduser(cfg_module.get(cfg, "database", "wallet_dir"))
            lib_dir    = os.path.expanduser(cfg_module.get(cfg, "database", "lib_dir"))
            dsn        = cfg_module.get(cfg, "database", "dsn", fallback="")
            os.environ["TNS_ADMIN"] = wallet_dir
            try:
                oracledb.init_oracle_client(lib_dir=lib_dir)
            except Exception as ex:
                if "already been called" not in str(ex).lower():
                    raise
            de_pass = db_module.resolve_password(cfg, de_schema, allow_prompt=True)
            conn = oracledb.connect(
                user=de_schema, password=de_pass,
                dsn=dsn, wallet_location=wallet_dir
            )
            display.ok(f"Connected as {de_schema}")
        except Exception as ex:
            display.err(f"Connection failed as {de_schema}: {ex}")
            return

    # ── Run checks ────────────────────────────────────────────────────────────
    display.blank()
    display.info(f"Auditing {ds_schema} → {data_schema}...")
    try:
        report = run_grant_check(cfg, ds_schema, data_schema, cred_name, conn)
        print_report(report, display)
    except Exception as ex:
        display.err(f"Grant audit failed: {ex}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
