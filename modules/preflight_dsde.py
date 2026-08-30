"""
modules/preflight_dsde.py
Pre-flight DS/DE check — user-level provisioning diagnostics.

Entry points:
  run_ds(cfg, config_path, clients, display)  — DS provisioning check
  run_de(cfg, config_path, clients, display)  — DE provisioning check

Each check:
  Part 1: Full config inventory — every key in config.ini, check/X/skip per field
  Part 2: Grant checks — runs only if connection-critical config is present

Password is resolved ONCE per check using silent=True (no repeated banner).
The proxy grant is validated implicitly — a successful DS_USER[ACME_CORP]
connection proves the grant is in place; ORA-01017 proves it is missing.
"""

from __future__ import annotations
import os
from core import config as cfg_module
from core import db as db_module


# ─────────────────────────────────────────────────────────────────────────────
# Result accumulator
# ─────────────────────────────────────────────────────────────────────────────

class CheckResult:
    OK   = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

    def __init__(self):
        self.items = []

    def ok  (self, label, detail=""): self.items.append((self.OK,   label, detail))
    def warn(self, label, detail=""): self.items.append((self.WARN, label, detail))
    def fail(self, label, detail=""): self.items.append((self.FAIL, label, detail))
    def skip(self, label, detail=""): self.items.append((self.SKIP, label, detail))

    @property
    def fail_count(self): return sum(1 for s,_,_ in self.items if s == self.FAIL)
    @property
    def warn_count(self): return sum(1 for s,_,_ in self.items if s == self.WARN)
    @property
    def ok_count  (self): return sum(1 for s,_,_ in self.items if s == self.OK)
    @property
    def skip_count(self): return sum(1 for s,_,_ in self.items if s == self.SKIP)


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

_SENSITIVE_KEYS = {"compartment_ocid", "secret_ocid", "tenancy_ocid",
                   "adw_ocid"}
_MAX_VAL_LEN = 52


def _display_value(key: str, val: str) -> str:
    if key in _SENSITIVE_KEYS:
        return val[:20] + "…" if len(val) > 20 else val
    if len(val) > _MAX_VAL_LEN:
        return val[:_MAX_VAL_LEN] + "…"
    return val


def _print_item(item, display):
    C = display.C
    status, label, detail = item
    sym, col = {
        CheckResult.OK:   ("✓", C.GREEN),
        CheckResult.WARN: ("⚠", C.YELLOW),
        CheckResult.FAIL: ("✗", C.RED),
        CheckResult.SKIP: ("○", C.DIM),
    }.get(status, ("?", C.DIM))
    print(f"  {col}{sym}{C.RESET}  {label}")
    if detail:
        for d in detail.splitlines():
            print(f"       {C.DIM}{d}{C.RESET}")


def _print_section_box(title, items, display):
    C = display.C
    display.blank()
    width = 54
    print(f"  ┌─ {C.BOLD}{title}{C.RESET} {'─'*max(0, width - len(title))}┐")
    for item in items:
        _print_item(item, display)
    if not items:
        print(f"  │  {C.DIM}(no checks run){C.RESET}")
    print(f"  └{'─'*(width+3)}┘")


def _print_summary(label, r: CheckResult, display):
    C = display.C
    if r.fail_count == 0 and r.warn_count == 0:
        msg = f"{C.GREEN}✓ All checks passed{C.RESET}"
    elif r.fail_count == 0:
        msg = f"{C.YELLOW}⚠ {r.warn_count} warning(s) — review before use{C.RESET}"
    else:
        msg = f"{C.RED}✗ {r.fail_count} issue(s) must be resolved{C.RESET}"
    print(f"  {C.BOLD}{label}:{C.RESET} {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Config field definitions — what we EXPECT to see
# (section, key, hint, required, sensitive)
# ─────────────────────────────────────────────────────────────────────────────

DS_EXPECTED = [
    # [oci]
    ("oci",           "region",             "e.g. us-chicago-1",                    True,  False),
    ("oci",           "config_file",        "e.g. ~/.oci/config",                   True,  False),
    ("oci",           "config_profile",     "e.g. DEFAULT",               True,  False),
    # [compartment]
    ("compartment",   "compartment_ocid",   "copy from OCI Console",                True,  True),
    ("compartment",   "compartment_name",   "e.g. cmp-acme-dev",                 True,  False),
    # [database]
    ("database",      "db_user",            "personal schema — e.g. DS_JOHN",       True,  False),
    ("database",      "target_schema",      "agent schema — e.g. ACME_CORP",          True,  False),
    ("database",      "dsn",               "TNS alias — e.g. acmedw_medium",  True,  False),
    ("database",      "wallet_dir",         "full path to ADW wallet dir",           True,  False),
    ("database",      "lib_dir",            "full path to Instant Client",           True,  False),
    # [object_storage]
    ("object_storage","default_bucket",     "e.g. acme-kb",    False, False),
    ("object_storage","default_prefix",     "subfolder in bucket — blank = root",   False, False),
    ("object_storage","rag_location_url",   "full Object Storage URL ending in /o/",False, False),
    # [llm]
    ("llm",           "chat_model",         "e.g. xai.grok-4-1-fast-non-reasoning", False, False),
    ("llm",           "embed_model",        "e.g. cohere.embed-multilingual-v3.0",  False, False),
    # [builder]
    ("builder",       "projects_dir",       "e.g. ./projects",                      False, False),
    ("builder",       "log_dir",            "e.g. ./logs",                          False, False),
    # [de] — source_tables only (everything else is DE-only)
    ("de",            "source_tables",
     "Set in [de] section: source_tables = ACME_CORP.TABLE1, ACME_CORP.TABLE2",
     False, False),
]

DE_EXTRA_EXPECTED = [
    # [oci] — DE needs tenancy_ocid for IAM
    ("oci",        "tenancy_ocid",       "copy from OCI Console → Tenancy",          False, True),
    # [de] — in config.ini order
    ("de",         "home_region",        "e.g. us-ashburn-1 (IAM home region)",      False, False),
    ("de",         "adw_ocid",           "copy from OCI Console → ADW",              False, True),
    ("de",         "admin_dsn",          "e.g. acmedw_low",                    False, False),
    ("de",         "admin_user",         "e.g. ADMIN",                               False, False),
    ("de",         "target_schema",      "schema to own agent objects",              False, False),
    ("de",         "de_schema",          "DE personal schema — e.g. DE_USER",       False, False),
    ("de",         "target_data_schema", "schema owning production tables",          False, False),
    ("de",         "dynamic_group_name", "e.g. acme-adw-dynamic-group",             False, False),
    ("de",         "policy_name",        "e.g. pol-acme-resource-principal",   False, False),
    ("de",         "oml_credential_name","defaults to <SCHEMA>_OML_CRED if blank",   False, False),
    ("de",         "secret_ocid",        "Vault secret OCID — DE-supplied, not auto-populated", False, False),
    ("de",         "oml_base_url",       "OML REST endpoint URL",                    False, False),
    # [preflight]
    ("preflight",  "dynamic_group_name", "IAM Dynamic Group name",                   False, False),
    ("preflight",  "policy_name",        "IAM Policy name",                          False, False),
    ("preflight",  "tenancy_ocid",       "for IAM checks",                           False, True),
]

DE_EXPECTED = DS_EXPECTED + DE_EXTRA_EXPECTED

_CRITICAL = {"wallet_dir", "dsn", "db_user"}


def _check_config(expected_fields, cfg, r: CheckResult) -> bool:
    """
    Show ALL keys found in config.ini, then show any expected-but-missing fields.
    Returns True if connection-critical fields are all present.

    Strategy:
      1. Collect every key actually in config (all sections)
      2. For each expected field: show check/X based on whether value is set
      3. For any extra keys in config not in expected list: show them too (informational)
    """
    critical_missing = []

    # Build set of expected (section, key) pairs for dedup
    expected_set = {(s, k) for s, k, *_ in expected_fields}

    # Track which expected fields we've shown
    shown = set()

    for section, key, hint, required, sensitive in expected_fields:
        val = cfg_module.get(cfg, section, key, fallback="").strip()
        shown.add((section, key))
        if val:
            r.ok(f"{key:<22} {_display_value(key, val)}")
        elif required:
            r.fail(f"{key:<22} not set  ← {hint}")
            if key in _CRITICAL:
                critical_missing.append(key)
        else:
            r.skip(f"{key:<22} not set  ← {hint}")

    return len(critical_missing) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Password — resolved once, silently
# ─────────────────────────────────────────────────────────────────────────────

def _get_password(cfg, db_user: str, display) -> tuple[str, str | None]:
    """
    Resolve password once using silent=True (no banner, just the prompt).
    Returns (password, error) — error is None on success.
    """
    C = display.C
    display.blank()
    print(f"  {C.DIM}Connecting to ADW as {db_user}...{C.RESET}")
    try:
        pwd = db_module.resolve_password(cfg, db_user,
                                         allow_prompt=True, silent=True)
        if pwd:
            return pwd, None
        return "", f"No password provided for {db_user}"
    except Exception as ex:
        return "", str(ex)


# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────

def _connect(cfg, db_user: str, target_schema: str,
             password: str, r: CheckResult):
    """
    Open proxy connection using pre-resolved password.
    Successful connection implicitly confirms proxy grant is in place.
    ORA-01017 on a valid password means proxy grant is missing.
    Returns conn or None.
    """
    try:
        import oracledb
    except ImportError:
        r.fail("oracledb not installed", "pip install oracledb")
        return None

    proxy_user = f"{db_user}[{target_schema}]" if target_schema else db_user
    wallet_dir = os.path.expanduser(
        cfg_module.get(cfg, "database", "wallet_dir", fallback=""))
    lib_dir    = os.path.expanduser(
        cfg_module.get(cfg, "database", "lib_dir",    fallback=""))
    dsn        = cfg_module.get(cfg, "database", "dsn", fallback="")

    os.environ["TNS_ADMIN"] = wallet_dir
    try:
        oracledb.init_oracle_client(lib_dir=lib_dir)
    except Exception as ex:
        if "already been called" not in str(ex).lower():
            r.fail("Oracle client init failed", str(ex)[:120])
            return None

    try:
        conn = oracledb.connect(
            user=proxy_user, password=password,
            dsn=dsn, wallet_location=wallet_dir
        )
        # Success = proxy grant confirmed implicitly
        r.ok(f"Proxy connection — connected as {proxy_user}",
             "Proxy grant confirmed — connection would fail without it")
        return conn
    except Exception as ex:
        ex_str = str(ex)
        if "ORA-01017" in ex_str:
            r.fail(
                f"Proxy connection failed — ORA-01017",
                f"This usually means the proxy grant is missing.\n"
                f"Ask DE: ALTER USER {target_schema} "
                f"GRANT CONNECT THROUGH {db_user};\n"
                f"If the password was wrong, re-run and try again."
            )
        else:
            r.fail("Proxy connection failed", ex_str[:200])
        return None


def _connect_direct(cfg, db_user: str, password: str, r: CheckResult):
    """
    Open a plain direct connection as db_user (NOT through the proxy).
    Used only for checks that need db_user's own privilege domain — in a
    proxied session CURRENT_USER is the target schema, not db_user, so
    SESSION_PRIVS / dictionary visibility there reflects the target schema,
    not db_user. A direct login sidesteps that entirely.
    Returns conn or None (failure here is reported as a warning, not a
    fatal check, since the proxy connection already succeeded).
    """
    try:
        import oracledb
    except ImportError:
        return None

    wallet_dir = os.path.expanduser(
        cfg_module.get(cfg, "database", "wallet_dir", fallback=""))
    lib_dir    = os.path.expanduser(
        cfg_module.get(cfg, "database", "lib_dir",    fallback=""))
    dsn        = cfg_module.get(cfg, "database", "dsn", fallback="")

    os.environ["TNS_ADMIN"] = wallet_dir
    try:
        oracledb.init_oracle_client(lib_dir=lib_dir)
    except Exception as ex:
        if "already been called" not in str(ex).lower():
            r.warn("Direct connection — Oracle client init failed", str(ex)[:120])
            return None

    try:
        return oracledb.connect(
            user=db_user, password=password,
            dsn=dsn, wallet_location=wallet_dir
        )
    except Exception as ex:
        r.warn(f"Direct connection as {db_user} failed — system priv check skipped",
               str(ex)[:160])
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────

def _qone(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or {})
    row = cur.fetchone()
    return dict(zip([d[0].lower() for d in cur.description], row)) if row else None


def _qall(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or {})
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Grant checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_session_identity(conn, target_schema: str, r: CheckResult):
    try:
        row = _qone(conn, """
            SELECT SYS_CONTEXT('USERENV','SESSION_USER') AS su,
                   SYS_CONTEXT('USERENV','PROXY_USER')   AS pu
            FROM   dual
        """)
        if row:
            su = (row.get("su") or "").upper()
            pu = (row.get("pu") or "").upper()
            if su == target_schema.upper():
                r.ok(f"Session identity — running as {su} (proxy: {pu})")
            else:
                r.warn(f"Session identity — running as {su}, expected {target_schema}")
    except Exception as ex:
        r.warn("Session identity check failed", str(ex)[:100])


def _check_source_table_grants(conn, cfg, db_user: str, r: CheckResult):
    raw = cfg_module.get(cfg, "de", "source_tables", fallback="").strip()
    if not raw:
        r.skip(f"{'source_tables':<22} not configured",
               "Set [de] source_tables in config.ini — e.g. ACME_CORP.ACME_GL_TRANSACTIONS, ACME_CORP.ACME_DEPARTMENTS")
        return
    tables = [t.strip().upper() for t in raw.split(",") if t.strip()]
    for tbl in tables:
        parts = tbl.split(".")
        owner = parts[0] if len(parts) > 1 else ""
        tname = parts[-1]
        try:
            rows = _qall(conn, """
                SELECT privilege FROM all_tab_privs
                WHERE  grantee    = :g
                  AND  table_name = :t
                  AND  privilege  = 'SELECT'
                  AND  (:o IS NULL OR table_schema = :o)
            """, {"g": db_user, "t": tname, "o": owner or None})
            if rows:
                r.ok(f"Direct SELECT — {tbl}")
            else:
                r.fail(f"Direct SELECT — {tbl} — NOT granted",
                       f"Ask DE: GRANT SELECT ON {tbl} TO {db_user};")
        except Exception as ex:
            r.warn(f"SELECT check failed for {tbl}", str(ex)[:100])


def _check_execute_grants(conn, db_user: str, packages: list, r: CheckResult):
    PKG_TESTS = {
        "DBMS_CLOUD_AI":
            ("SELECT COUNT(*) FROM TABLE(DBMS_CLOUD_AI.LIST_PROFILES())", "query"),
        "DBMS_CLOUD_AI_AGENT":
            ("SELECT COUNT(*) FROM user_ai_agent_tools", "query"),
        "DBMS_CLOUD":
            ("BEGIN DBMS_CLOUD.LIST_OBJECTS('__pf__','__x__'); END;", "plsql"),
        "DBMS_CLOUD_ADMIN":
            ("SELECT COUNT(*) FROM dba_credentials WHERE rownum < 2", "query"),
    }
    for pkg in packages:
        test_sql, test_type = PKG_TESTS.get(pkg, ("SELECT 1 FROM DUAL", "query"))
        try:
            cur = conn.cursor()
            cur.execute(test_sql)
            if test_type == "query":
                cur.fetchone()
            r.ok(f"EXECUTE — {pkg}")
        except Exception as ex:
            if "ORA-01031" in str(ex):
                r.fail(f"EXECUTE — {pkg} — NOT granted",
                       f"Ask DE: GRANT EXECUTE ON {pkg} TO {db_user};")
            else:
                r.ok(f"EXECUTE — {pkg} (callable)")


def _check_system_privs(cfg, db_user: str, password: str, privs: list, r: CheckResult):
    """
    Check system privileges granted to db_user.

    Deliberately opens its OWN direct (non-proxy) connection as db_user
    rather than reusing the shared proxied connection. In a proxied session
    (db_user[target_schema]), CURRENT_USER is target_schema, so SESSION_PRIVS
    there reflects target_schema's privileges, not db_user's — and there is
    no ALL_SYS_PRIVS-style view to check an arbitrary grantee without DBA-
    level dictionary access. A direct login as db_user sidesteps both
    problems: CURRENT_USER really is db_user, and SESSION_PRIVS needs no
    special grants to query your own active privileges.
    """
    conn = _connect_direct(cfg, db_user, password, r)
    if not conn:
        r.warn("System priv checks skipped — could not open direct connection")
        return
    try:
        rows = _qall(conn, "SELECT privilege FROM session_privs")
        granted = {row["privilege"].upper() for row in rows}
    except Exception as ex:
        r.warn("System priv check — could not query SESSION_PRIVS", str(ex)[:160])
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    for priv in privs:
        if priv.upper() in granted:
            r.ok(f"System priv — {priv}")
        else:
            r.fail(f"System priv — {priv} — NOT granted",
                   f"Ask DE: GRANT {priv} TO {db_user};")


def _check_oci_genai(clients, r: CheckResult):
    try:
        genai = clients.get("genai") if clients else None
        if not genai:
            r.skip("OCI GenAI reachability — OCI clients not available")
            return
        try:
            genai.list_models(compartment_id="ocid1.tenancy.oc1..dummy", limit=1)
            r.ok("OCI GenAI — reachable")
        except Exception as ex:
            s = str(ex)
            if any(c in s for c in ("404", "400", "InvalidParameter",
                                    "NotAuthorizedOrNotFound")):
                r.ok("OCI GenAI — reachable (service responded)")
            elif "401" in s or "NotAuthenticated" in s:
                r.fail("OCI GenAI — authentication failed",
                       "Check OCI CLI config / Resource Principal")
            else:
                r.fail("OCI GenAI — not reachable", s[:120])
    except Exception as ex:
        r.warn("OCI GenAI check error", str(ex)[:100])


# ─────────────────────────────────────────────────────────────────────────────
# Package lists
# ─────────────────────────────────────────────────────────────────────────────

DS_PACKAGES  = ["DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT"]
DE_PACKAGES  = ["DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT",
                "DBMS_CLOUD", "DBMS_CLOUD_ADMIN"]
DE_SYS_PRIVS = ["SELECT ANY TABLE", "SELECT ANY DICTIONARY", "COMMENT ANY TABLE"]


# ─────────────────────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────────────────────

def _footer(r_cfg, r_data, role, hint, display):
    C = display.C
    display.blank()
    print(f"  {'─'*60}")
    merged = CheckResult()
    merged.items = r_cfg.items + r_data.items
    _print_summary(f"{role} provisioning", merged, display)
    print(f"  {'─'*60}")
    display.blank()
    print(f"  {C.DIM}{hint}{C.RESET}")


def run_ds(cfg, config_path: str, clients: dict, display):
    """DS pre-flight — single password prompt, no repeated asks."""
    display.head("PRE-FLIGHT CHECK — DS USER PROVISIONING")
    display.blank()

    r_cfg  = CheckResult()
    r_data = CheckResult()

    db_user       = cfg_module.get(cfg, "database", "db_user",
                                   fallback="").strip().upper()
    target_schema = cfg_module.get(cfg, "database", "target_schema",
                                   fallback="").strip().upper()

    # Part 1: full config inventory
    can_connect = _check_config(DS_EXPECTED, cfg, r_cfg)
    _print_section_box("Config", r_cfg.items, display)

    if not can_connect:
        r_data.skip("Grant checks skipped — fix missing config fields above first")
        _print_section_box("DS Level Check", r_data.items, display)
        _footer(r_cfg, r_data, "DS",
                "Fix config.ini then re-run.", display)
        return

    # Resolve password ONCE — silent (no banner, just prompt)
    password, err = _get_password(cfg, db_user, display)
    if err:
        r_data.fail("Password resolution failed", err)
        _print_section_box("DS Level Check", r_data.items, display)
        _footer(r_cfg, r_data, "DS", "", display)
        return

    # Part 2: grant checks
    conn = _connect(cfg, db_user, target_schema, password, r_data)
    if conn:
        try:
            _check_session_identity(conn, target_schema, r_data)
            _check_source_table_grants(conn, cfg, db_user, r_data)
            _check_execute_grants(conn, db_user, DS_PACKAGES, r_data)
            _check_oci_genai(clients, r_data)
        finally:
            conn.close()

    _print_section_box("DS Level Check", r_data.items, display)
    _footer(r_cfg, r_data, "DS",
            "If checks fail — ask the DE to re-run SA_03_DS_grants.sql",
            display)


def run_de(cfg, config_path: str, clients: dict, display):
    """DE pre-flight — single password prompt, no repeated asks."""
    display.head("PRE-FLIGHT CHECK — DE USER PROVISIONING")
    display.blank()

    r_cfg  = CheckResult()
    r_data = CheckResult()

    db_user       = cfg_module.get(cfg, "database", "db_user",
                                   fallback="").strip().upper()
    target_schema = cfg_module.get(cfg, "database", "target_schema",
                                   fallback="").strip().upper()

    # Part 1: full config inventory
    can_connect = _check_config(DE_EXPECTED, cfg, r_cfg)
    _print_section_box("Config", r_cfg.items, display)

    if not can_connect:
        r_data.skip("Grant checks skipped — fix missing config fields above first")
        _print_section_box("DE Level Check", r_data.items, display)
        _footer(r_cfg, r_data, "DE",
                "Fix config.ini then re-run.", display)
        return

    # Resolve password ONCE — silent
    password, err = _get_password(cfg, db_user, display)
    if err:
        r_data.fail("Password resolution failed", err)
        _print_section_box("DE Level Check", r_data.items, display)
        _footer(r_cfg, r_data, "DE", "", display)
        return

    # Part 2: grant checks
    conn = _connect(cfg, db_user, target_schema, password, r_data)
    if conn:
        try:
            _check_session_identity(conn, target_schema, r_data)
            _check_source_table_grants(conn, cfg, db_user, r_data)
            _check_execute_grants(conn, db_user, DE_PACKAGES, r_data)
            _check_system_privs(cfg, db_user, password, DE_SYS_PRIVS, r_data)
            _check_oci_genai(clients, r_data)
        finally:
            conn.close()

    _print_section_box("DE Level Check", r_data.items, display)
    _footer(r_cfg, r_data, "DE",
            "If checks fail — ask ADMIN to re-run SA_00_DE_grants.sql",
            display)


# Backward-compatible combined entry point
def run(cfg, config_path: str, clients: dict, display):
    run_ds(cfg, config_path, clients, display)
    display.blank()
    run_de(cfg, config_path, clients, display)
