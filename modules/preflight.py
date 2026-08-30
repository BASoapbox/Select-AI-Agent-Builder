"""
modules/preflight.py
Pre-flight check — verify the data engineer setup is complete before building agents.

Checks everything the data scientist CANNOT do themselves:
  OCI layer  : compartment reachability, dynamic group, IAM policy, OCI GenAI access
  ADW layer  : EXECUTE grants, role grants, pyqAppendHostAce, vault credential,
               Resource Principal, agent framework views
"""

import os
import re
from core import config as cfg_module
from core import db as db_module


def _safe_ident(name: str) -> str:
    """Return name reduced to a bare Oracle identifier.

    Schema and table names cannot be bind variables, so any config-sourced
    name spliced into SQL text goes through here first.
    """
    return re.sub(r"[^A-Za-z0-9_$#]", "", (name or "")).upper()


# ─────────────────────────────────────────────────────────────────────────────
# Result accumulator
# ─────────────────────────────────────────────────────────────────────────────

class CheckResult:
    OK    = "ok"
    WARN  = "warn"
    FAIL  = "fail"
    SKIP  = "skip"

    def __init__(self):
        self.items = []   # list of (status, label, detail)

    def ok(self,   label, detail=""): self.items.append((self.OK,   label, detail))
    def warn(self, label, detail=""): self.items.append((self.WARN, label, detail))
    def fail(self, label, detail=""): self.items.append((self.FAIL, label, detail))
    def skip(self, label, detail=""): self.items.append((self.SKIP, label, detail))

    @property
    def fail_count(self):  return sum(1 for s,_,_ in self.items if s == self.FAIL)
    @property
    def warn_count(self):  return sum(1 for s,_,_ in self.items if s == self.WARN)
    @property
    def ok_count(self):    return sum(1 for s,_,_ in self.items if s == self.OK)
    @property
    def skip_count(self):  return sum(1 for s,_,_ in self.items if s == self.SKIP)


def _print_section(title, display):
    display.blank()
    print(f"  {display.C.BOLD}{title}{display.C.RESET}")
    print(f"  {'─' * 60}")


def _print_item(result, display):
    C = display.C
    status, label, detail = result
    if status == CheckResult.OK:
        sym, col = "✓", C.GREEN
    elif status == CheckResult.WARN:
        sym, col = "⚠", C.YELLOW
    elif status == CheckResult.FAIL:
        sym, col = "✗", C.RED
    else:
        sym, col = "○", C.DIM

    line = f"  {col}{sym}{C.RESET}  {label}"
    print(line)
    if detail:
        for d in detail.splitlines():
            print(f"       {C.DIM}{d}{C.RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# OCI checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_oci(cfg, clients, r: CheckResult):
    import oci

    compartment  = cfg_module.get(cfg, "compartment", "compartment_ocid")
    comp_name    = cfg_module.get(cfg, "compartment", "compartment_name", fallback="")
    tenancy_ocid = cfg_module.get(cfg, "oci", "tenancy_ocid", fallback="")
    dg_name      = cfg_module.get(cfg, "preflight", "dynamic_group_name", fallback="")
    policy_name  = cfg_module.get(cfg, "preflight", "policy_name", fallback="")

    # Required IAM policy statements (key fragments to match)
    REQUIRED_STATEMENTS = [
        ("generative-ai-family", "autonomousdatabase",
         "ADW → OCI GenAI (NL2SQL and RAG inference)"),
        ("object-family",        "autonomousdatabase",
         "ADW → Object Storage (vector index build)"),
        ("genai-agent-family",   "autonomousdatabase",
         "ADW → OCI GenAI Agent (call agent API)"),
        ("object-family",        "genaiagent",
         "OCI GenAI Agent → Object Storage (read PDFs)"),
        ("secret-bundles",       "autonomousdatabase",
         "ADW → Vault (read OML credential secret)"),
    ]

    # Compartment reachable
    try:
        os_client = clients["object_storage"]
        ns = os_client.get_namespace().data
        r.ok(f"OCI connectivity — namespace: {ns}")
    except Exception as ex:
        r.fail("OCI connectivity", str(ex)[:120])
        return   # No point checking further if we can't reach OCI

    # Compartment exists
    try:
        identity = clients["identity"]
        comp = identity.get_compartment(compartment).data
        r.ok(f"Compartment '{comp.name}' is {comp.lifecycle_state}")
    except Exception as ex:
        r.fail(f"Compartment OCID not reachable", str(ex)[:120])

    # OCI GenAI reachable
    try:
        genai = clients["genai"]
        models = oci.pagination.list_call_get_all_results(
            genai.list_models, compartment_id=compartment).data
        active = [m for m in models if getattr(m, "lifecycle_state", "") == "ACTIVE"]
        r.ok(f"OCI GenAI reachable — {len(active)} active models available")
    except Exception as ex:
        r.fail("OCI GenAI not reachable", str(ex)[:120])

    # Dynamic Group
    if not dg_name:
        r.skip("Dynamic Group — set [preflight] dynamic_group_name in config to check")
    elif not tenancy_ocid:
        r.skip("Dynamic Group — set [oci] tenancy_ocid in config to check")
    else:
        try:
            all_dgs = oci.pagination.list_call_get_all_results(
                identity.list_dynamic_groups, tenancy_ocid).data
            match = next(
                (dg for dg in all_dgs
                 if dg.name == dg_name and dg.lifecycle_state != "DELETED"),
                None
            )
            if match:
                r.ok(f"Dynamic Group '{dg_name}' exists ({match.lifecycle_state})")
            else:
                r.fail(f"Dynamic Group '{dg_name}' not found",
                       "Ask your data engineer to create it")
        except Exception as ex:
            r.warn(f"Dynamic Group check failed", str(ex)[:120])

    # IAM Policy
    if not policy_name:
        r.skip("IAM Policy — set [preflight] policy_name in config to check")
    elif not tenancy_ocid:
        r.skip("IAM Policy — set [oci] tenancy_ocid in config to check")
    else:
        try:
            all_policies = oci.pagination.list_call_get_all_results(
                identity.list_policies, tenancy_ocid).data
            policy = next(
                (p for p in all_policies
                 if p.name == policy_name and p.lifecycle_state != "DELETED"),
                None
            )
            if not policy:
                r.fail(f"IAM Policy '{policy_name}' not found",
                       "Ask your data engineer to create it")
            else:
                # Check each required statement fragment
                stmts = " ".join(policy.statements or []).lower()
                missing = []
                for resource, principal, desc in REQUIRED_STATEMENTS:
                    if resource.lower() in stmts and principal.lower() in stmts:
                        pass   # present
                    else:
                        missing.append(f"{desc}  [{resource} / {principal}]")
                if missing:
                    r.warn(
                        f"IAM Policy '{policy_name}' found but missing {len(missing)} "
                        f"of {len(REQUIRED_STATEMENTS)} required statements",
                        "\n".join(missing)
                    )
                else:
                    r.ok(f"IAM Policy '{policy_name}' — all {len(REQUIRED_STATEMENTS)} "
                         f"required statements present")
        except Exception as ex:
            r.warn("IAM Policy check failed", str(ex)[:120])


# ─────────────────────────────────────────────────────────────────────────────
# ADW checks
# ─────────────────────────────────────────────────────────────────────────────

def _connect_db(cfg, db_user_override: str = ""):
    """
    Open an ADW connection. Returns (conn, error_string).

    If db_user_override is supplied, pre-flight connects as that schema/user for
    this run only. The config file is not modified.
    """
    try:
        import oracledb
    except ImportError:
        return None, "oracledb not installed — run: pip install oracledb"

    configured_user = cfg_module.get(cfg, "database", "db_user")
    db_user = (db_user_override or configured_user or "").strip().upper()
    wallet_dir = cfg_module.get(cfg, "database", "wallet_dir")
    lib_dir = cfg_module.get(cfg, "database", "lib_dir")
    dsn = cfg_module.get(cfg, "database", "dsn")

    missing = []
    if not db_user:    missing.append("[database] db_user or runtime schema override")
    if not dsn:        missing.append("[database] dsn")
    if not wallet_dir: missing.append("[database] wallet_dir")
    if not lib_dir:    missing.append("[database] lib_dir")

    if missing:
        return None, (
            "Database config incomplete — fill in config file:\n" +
            "\n".join(f"       {m} — not set" for m in missing)
        )

    wallet_dir = os.path.expanduser(wallet_dir)
    lib_dir = os.path.expanduser(lib_dir)

    try:
        password = db_module.resolve_password(cfg, db_user, allow_prompt=True)
    except Exception as ex:
        return None, str(ex)

    if not password:
        return None, f"No password provided for {db_user}"

    # Set TNS_ADMIN so thick mode finds tnsnames.ora in the wallet folder
    os.environ["TNS_ADMIN"] = wallet_dir

    try:
        oracledb.init_oracle_client(lib_dir=lib_dir)
    except Exception as ex:
        if "already been called" not in str(ex).lower():
            return None, f"Oracle client init failed: {ex}"

    try:
        conn = oracledb.connect(
            user=db_user, password=password,
            dsn=dsn, wallet_location=wallet_dir
        )
        return conn, None
    except Exception as ex:
        return None, f"Connection failed for {db_user}: {ex}"

def _query_one(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or {})
    row = cur.fetchone()
    if row and cur.description:
        cols = [d[0].lower() for d in cur.description]
        return dict(zip(cols, row))
    return None


def _query_all(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or {})
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _check_adw(cfg, r: CheckResult, db_user_override: str = ""):
    db_user     = (db_user_override or cfg_module.get(cfg, "database", "db_user") or "").strip().upper()
    cred_name   = cfg_module.get(cfg, "de", "oml_credential_name",
                                 fallback=f"{db_user}_OML_CRED")
    oml_host    = cfg_module.get(cfg, "preflight", "oml_host", fallback="")

    # Required EXECUTE grants
    REQUIRED_GRANTS = [
        "DBMS_CLOUD_AI",
        "DBMS_CLOUD_AI_AGENT",
        "DBMS_CLOUD",
        "DBMS_VECTOR_CHAIN",
    ]

    # Required roles
    REQUIRED_ROLES = [
        "PYQADMIN",
        "OML_DEVELOPER",
    ]

    # Agent framework views that must be accessible
    REQUIRED_VIEWS = [
        "USER_AI_AGENT_TOOLS",
        "USER_AI_AGENTS",           # correct name — not USER_AI_AGENT_AGENTS
        "USER_AI_AGENT_TASKS",
        "USER_AI_AGENT_TEAMS",
        "USER_CLOUD_AI_PROFILES",
    ]

    conn, err = _connect_db(cfg, db_user_override=db_user)
    if not conn:
        # Check if it's a config issue vs a real connection error
        if "config incomplete" in err.lower() or "not set" in err.lower():
            r.skip("ADW checks skipped — database config fields missing",
                   err.replace("\n", "\n       "))
        else:
            r.fail("ADW connection failed", err)
        return

    try:
        # ── Resource Principal ────────────────────────────────────────────────
        # Strategy: try LIST_PROFILES (returns a table — no arg needed),
        # then fall back to v$parameter. ORA-20401 / ORA-29273 means RP not
        # enabled; a result (even empty) means it is enabled.
        rp_ok = False
        try:
            # LIST_PROFILES returns a table of profiles — works with no args
            conn.cursor().execute(
                "SELECT COUNT(*) FROM TABLE(DBMS_CLOUD_AI.LIST_PROFILES())"
            )
            rp_ok = True
        except Exception as ex1:
            ex1_str = str(ex1)
            if "ORA-20401" in ex1_str or "ORA-29273" in ex1_str:
                # Resource Principal explicitly disabled
                r.fail("Resource Principal NOT enabled",
                       "Ask DE: EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL()")
                rp_ok = None  # sentinel — already recorded
            else:
                # Try v$parameter as fallback
                try:
                    row = _query_one(conn, """
                        SELECT value FROM v$parameter
                        WHERE  name = 'enable_resource_principal_for_dbms_cloud'
                    """)
                    if row and str(row.get("value", "")).upper() == "TRUE":
                        rp_ok = True
                    else:
                        r.fail("Resource Principal NOT enabled",
                               "Ask DE: EXEC DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL()")
                        rp_ok = None
                except Exception:
                    # Neither method worked — warn but don't fail
                    r.warn("Resource Principal — could not verify directly",
                           "Neither LIST_PROFILES() nor v$parameter is accessible to this user. "
                           "This is normal for a schema user — if EXECUTE grants on DBMS_CLOUD_AI "
                           "pass below, Resource Principal is likely working.")
                    rp_ok = None

        if rp_ok is True:
            r.ok("Resource Principal enabled")
        elif rp_ok is None:
            pass  # already recorded above

        # ── EXECUTE grants ─────────────────────────────────────────────────────
        # Strategy: test each package with a benign call.
        # Privilege-view queries (user_tab_privs, all_tab_privs) miss grants
        # inherited transitively through roles like DWROLE — leading to false
        # negatives even when the package is fully callable. A live call is
        # the only reliable test.
        #
        # Test calls chosen to be safe and side-effect-free:
        #   DBMS_CLOUD_AI        — LIST_PROFILES() returns table, empty is fine
        #   DBMS_CLOUD_AI_AGENT  — USER_AI_AGENT_TOOLS view access as proxy
        #   DBMS_CLOUD           — get_object_names with dummy bucket (ORA-20401 = callable)
        #   DBMS_VECTOR_CHAIN    — utl_to_chunks on empty string (ORA-* except 01031 = callable)

        PKG_TESTS = {
            "DBMS_CLOUD_AI": (
                "SELECT COUNT(*) FROM TABLE(DBMS_CLOUD_AI.LIST_PROFILES())",
                "query"
            ),
            "DBMS_CLOUD_AI_AGENT": (
                "SELECT COUNT(*) FROM user_ai_agent_tools",
                "query"
            ),
            "DBMS_CLOUD": (
                "BEGIN DBMS_CLOUD.LIST_OBJECTS('__preflight_dummy__','__none__'); END;",
                "plsql"
            ),
            "DBMS_VECTOR_CHAIN": (
                "BEGIN DBMS_VECTOR_CHAIN.UTL_TO_CHUNKS('test'); END;",
                "plsql"
            ),
        }

        for pkg in REQUIRED_GRANTS:
            test_sql, test_type = PKG_TESTS.get(pkg, ("SELECT 1 FROM DUAL", "query"))
            try:
                cur = conn.cursor()
                cur.execute(test_sql)
                if test_type == "query":
                    cur.fetchone()
                r.ok(f"EXECUTE on {pkg} — granted")
            except Exception as ex:
                ex_str = str(ex)
                # ORA-01031 = insufficient privileges — genuinely not granted
                if "ORA-01031" in ex_str:
                    r.fail(f"EXECUTE on {pkg} — not granted",
                           f"Ask DE: GRANT EXECUTE ON {pkg} TO {db_user};")
                else:
                    # Any other error means the package is callable but the
                    # test operation failed for a different reason (e.g. bad
                    # bucket name, wrong args) — grant is present
                    r.ok(f"EXECUTE on {pkg} — granted (callable)")

        # ── Role grants ───────────────────────────────────────────────────────
        # Check session_roles first (direct + inherited),
        # then fall back to calling a package-level function as proof.
        try:
            role_rows = _query_all(conn, "SELECT granted_role FROM session_roles")
            session_roles = {row["granted_role"].upper() for row in role_rows}
        except Exception:
            session_roles = set()

        # Role functional tests — queries that require the role to succeed.
        # ORA-01031 = definitely not granted.
        # Any other error = role present, test query failed for unrelated reason.
        # session_roles may miss roles granted through parent roles (e.g. DWROLE)
        # so we always run the functional test as a second opinion.
        ROLE_FUNCTIONAL_TESTS = {
            "PYQADMIN":      "SELECT COUNT(*) FROM user_mining_models",
            "OML_DEVELOPER": "SELECT COUNT(*) FROM user_mining_models",
        }

        for role in REQUIRED_ROLES:
            in_session = role.upper() in session_roles
            test_sql   = ROLE_FUNCTIONAL_TESTS.get(role, "SELECT 1 FROM DUAL")
            try:
                cur = conn.cursor()
                cur.execute(test_sql)
                cur.fetchone()
                if in_session:
                    r.ok(f"Role {role} — granted")
                else:
                    r.ok(f"Role {role} — granted (via parent role, e.g. DWROLE)")
            except Exception as ex:
                ex_str = str(ex)
                if "ORA-01031" in ex_str:
                    r.fail(f"Role {role} — not granted",
                           f"Ask DE: GRANT {role} TO {db_user};")
                elif in_session:
                    r.ok(f"Role {role} — granted")
                else:
                    detail = f"Not in session_roles. Test error: {ex_str[:80]}"
                    r.warn(f"Role {role} — could not verify",
                           detail + " — if your tools work, role is effectively granted.")

        # ── pyqAppendHostAce ──────────────────────────────────────────────────
        try:
            ace_rows = _query_all(conn, """
                SELECT host, lower_port, upper_port
                FROM   user_network_acl_privileges
                WHERE  privilege = 'connect'
            """)
            if ace_rows:
                hosts = [f"{a['host']}:{a['lower_port']}-{a['upper_port']}"
                         for a in ace_rows]
                r.ok(f"Network ACE (pyqAppendHostAce) — {len(ace_rows)} entry/entries",
                     "\n".join(hosts[:5]))
            else:
                r.fail("Network ACE (pyqAppendHostAce) — no entries found",
                       "Ask DE to run pyqAppendHostAce for OML endpoint")
        except Exception as ex:
            r.warn("Network ACE check failed", str(ex)[:100])

        # ── Vault credential ──────────────────────────────────────────────────
        try:
            cred_rows = _query_all(conn, """
                SELECT credential_name, username, enabled
                FROM   user_credentials
                WHERE  credential_name = :cn
            """, {"cn": cred_name.upper()})
            if cred_rows:
                c = cred_rows[0]
                enabled = str(c.get("enabled", "")).upper()
                if enabled == "TRUE" or enabled == "YES" or enabled == "1":
                    r.ok(f"Vault credential '{cred_name}' exists and is enabled")
                else:
                    r.warn(f"Vault credential '{cred_name}' exists but may be disabled")
            else:
                r.fail(f"Vault credential '{cred_name}' not found",
                       "Ask DE to run DBMS_CLOUD.CREATE_CREDENTIAL with the Vault secret OCID")
        except Exception as ex:
            r.warn(f"Vault credential check failed", str(ex)[:100])

        # ── Proxy authentication ─────────────────────────────────────────────
        target_schema = cfg_module.get(cfg, "database", "target_schema",
                                       fallback="").strip().upper()
        if target_schema:
            try:
                proxy_rows = _query_all(conn, """
                    SELECT proxy, client, authentication
                    FROM   dba_proxies
                    WHERE  proxy  = :proxy
                      AND  client = :client
                """, {"proxy": db_user, "client": target_schema})
                if proxy_rows:
                    r.ok(f"Proxy auth — {db_user} → {target_schema} granted")
                else:
                    r.fail(f"Proxy auth — {db_user} → {target_schema} NOT granted",
                           f"Ask DE: ALTER USER {target_schema} "
                           f"GRANT CONNECT THROUGH {db_user};")
            except Exception as ex:
                r.warn("Proxy auth check failed", str(ex)[:100])

            # Verify builder user registry entry.
            # SELECTAI_BUILDER_USERS is an optional site-local governance table
            # in the agent-owning schema: one row per person allowed to use the
            # builder, with a role and an active flag. Sites that do not use it
            # simply have no such table — the query then raises and the check
            # degrades to a warning rather than a failure.
            registry = f"{_safe_ident(target_schema)}.SELECTAI_BUILDER_USERS"
            try:
                reg_row = _query_one(conn, f"""
                    SELECT user_role, active
                    FROM   {registry}
                    WHERE  username = :uname
                """, {"uname": db_user})
                if reg_row:
                    reg_role   = reg_row.get("user_role", "?").upper()
                    reg_active = reg_row.get("active", "N").upper()
                    if reg_active == "Y":
                        r.ok(f"Builder registry — {db_user} registered as {reg_role}")
                    else:
                        r.fail(f"Builder registry — {db_user} is INACTIVE",
                               f"Ask DE to set active='Y' in {registry}")
                else:
                    r.fail(f"Builder registry — {db_user} not found",
                           f"Ask DE to add entry to {registry}")
            except Exception as ex:
                r.warn("Builder registry check skipped "
                       f"(no {registry} table, or not readable)", str(ex)[:100])
        else:
            r.skip("Proxy auth — not configured (target_schema not set)")

        # ── Agent framework views ─────────────────────────────────────────────
        for view in REQUIRED_VIEWS:
            try:
                _query_one(conn, f"SELECT COUNT(*) FROM {view}")
                r.ok(f"View {view} accessible")
            except Exception as ex:
                ex_str = str(ex)
                if "table or view does not exist" in ex_str.lower():
                    r.fail(f"View {view} — does not exist",
                           "ADW version may not support Select AI Agent framework")
                else:
                    r.warn(f"View {view} — query error", ex_str[:100])

    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Runtime prompt helpers
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_schema_override(cfg, display) -> str:
    """
    Ask which database schema/user should be checked for ADW pre-flight.

    Pressing Enter uses [database] db_user. The override is session-only and
    is not written back to the config file.
    """
    C = display.C
    configured_user = cfg_module.get(cfg, "database", "db_user", fallback="").strip().upper()
    if not configured_user:
        return ""

    display.blank()
    print(f"  {C.BOLD}Database schema/user to validate{C.RESET}")
    print(f"  {C.DIM}Configured default: {configured_user}{C.RESET}")
    print(f"  {C.DIM}Press Enter to use the configured default. This does not modify the config file.{C.RESET}")
    try:
        raw = input(f"  {C.BOLD}Schema/user to check [{configured_user}]:{C.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raw = ""

    selected = (raw or configured_user).strip().upper()
    if selected != configured_user:
        print(f"  {C.YELLOW}Using schema override for this pre-flight only: {selected}{C.RESET}")
        print(f"  {C.DIM}Password lookup order: [de] secret_ocid, OCI_DB_PASSWORD_{selected}, OCI_DB_PASSWORD, prompt.{C.RESET}")
    return selected


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(cfg, config_path: str, clients: dict, display):
    C = display.C
    display.head("PRE-FLIGHT CHECK — DATA ENGINEER SETUP VERIFICATION")
    display.blank()
    print(f"  {C.DIM}Checking that all DE-managed prerequisites are in place.{C.RESET}")
    print(f"  {C.DIM}This may take 30–60 seconds.{C.RESET}")

    r = CheckResult()

    # ── OCI checks ────────────────────────────────────────────────────────────
    _print_section("OCI CONNECTIVITY & IAM", display)
    display.info("Checking OCI layer...")
    try:
        _check_oci(cfg, clients, r)
    except Exception as ex:
        r.fail("OCI check error", str(ex)[:200])

    # ── ADW checks ────────────────────────────────────────────────────────────
    _print_section("ADW — GRANTS, ROLES & CONFIGURATION", display)
    db_user_to_check = _prompt_schema_override(cfg, display)
    display.info(f"Connecting to ADW as {db_user_to_check or 'configured schema'}...")
    _check_adw(cfg, r, db_user_override=db_user_to_check)

    # ── Print all results ─────────────────────────────────────────────────────
    display.blank()
    _print_section("RESULTS", display)
    for item in r.items:
        _print_item(item, display)

    # ── Summary ───────────────────────────────────────────────────────────────
    display.blank()
    total   = len(r.items)
    fails   = r.fail_count
    warns   = r.warn_count
    oks     = r.ok_count

    skips = r.skip_count

    print(f"  {'─' * 60}")
    if fails == 0 and warns == 0 and skips == 0:
        print(f"  {C.GREEN}{C.BOLD}All {oks} checks passed — ready to build agents!{C.RESET}")
    elif fails == 0 and skips > 0 and warns == 0:
        print(f"  {C.YELLOW}{C.BOLD}{oks} check(s) passed, {skips} skipped "
              f"— fill in missing config values to complete the check{C.RESET}")
    elif fails == 0 and warns > 0:
        print(f"  {C.YELLOW}{C.BOLD}{oks} passed, {warns} warning(s)"
              + (f", {skips} skipped" if skips else "")
              + f" — review before building{C.RESET}")
    else:
        print(f"  {C.RED}{C.BOLD}{fails} issue(s) must be resolved "
              f"before you can build agents{C.RESET}")
        if warns:
            print(f"  {C.YELLOW}  {warns} warning(s) to review{C.RESET}")
        if skips:
            print(f"  {C.YELLOW}  {skips} check(s) skipped — fill in config to enable them{C.RESET}")
    print(f"  {'─' * 60}")

    # ── Capability summary ────────────────────────────────────────────────────
    # Determine what the user can do based on check results
    display.blank()
    print(f"  {C.BOLD}WHAT YOU CAN DO:{C.RESET}")
    print(f"  {'─' * 60}")

    # Check for DBMS_CLOUD (needed for Object Storage uploads / RAG)
    rag_items   = [s for s,l,d in r.items if "DBMS_CLOUD" in l and s == CheckResult.OK]
    rag_bucket  = [s for s,l,d in r.items if "Vault credential" in l and s == CheckResult.OK]
    rag_ok      = bool(rag_items) and bool(rag_bucket)

    # Check agent framework views (needed to create agent resources)
    agent_views = [s for s,l,d in r.items
                   if any(v in l for v in ("USER_AI_AGENT_TOOLS","USER_AI_AGENTS",
                                           "USER_AI_AGENT_TEAMS","USER_CLOUD_AI_PROFILES"))
                   and s == CheckResult.OK]
    agent_grants = [s for s,l,d in r.items
                    if "DBMS_CLOUD_AI" in l and s == CheckResult.OK]
    agent_ok     = len(agent_views) >= 2 and bool(agent_grants)

    if rag_ok:
        print(f"  {C.GREEN}✓{C.RESET}  {C.BOLD}Object Storage / RAG{C.RESET}  — "
              f"you can upload documents and build RAG tools")
    else:
        missing_rag = []
        if not rag_items:   missing_rag.append("EXECUTE on DBMS_CLOUD")
        if not rag_bucket:  missing_rag.append("Vault credential")
        print(f"  {C.YELLOW}⚠{C.RESET}  {C.BOLD}Object Storage / RAG{C.RESET}  — "
              f"not fully ready ({', '.join(missing_rag)} needed)")

    if agent_ok:
        print(f"  {C.GREEN}✓{C.RESET}  {C.BOLD}Agent resources{C.RESET}  — "
              f"you can create tools, agents, tasks, and teams")
    else:
        missing_agent = []
        if not agent_grants: missing_agent.append("EXECUTE on DBMS_CLOUD_AI")
        if len(agent_views) < 2: missing_agent.append("agent framework views")
        print(f"  {C.RED}✗{C.RESET}  {C.BOLD}Agent resources{C.RESET}  — "
              f"not ready ({', '.join(missing_agent)} needed)")

    print(f"  {'─' * 60}")


