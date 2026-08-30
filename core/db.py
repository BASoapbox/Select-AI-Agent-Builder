"""
core/db.py
ADW database connection and SQL execution for the Agent Builder.

Key fix: sets TNS_ADMIN env var to wallet_dir before connecting so that
oracledb thick mode finds tnsnames.ora in the wallet folder — not in
the Instant Client network/admin directory.
"""

import os
import sys
from pathlib import Path

from core import config as cfg_module

# Session-level password cache — resolved once, reused throughout
_password_cache: dict[str, str] = {}


def _fetch_password_from_vault(cfg, secret_ocid: str = "") -> str:
    """
    Try to retrieve a password from OCI Vault.

    If secret_ocid is not provided, uses [de] secret_ocid from config.
    Returns the password string, or an empty string if not configured/available.
    """
    secret_ocid = (secret_ocid or cfg_module.get(cfg, "de", "secret_ocid", fallback="")).strip()
    if not secret_ocid:
        return ""
    try:
        import oci as _oci
        config_file = os.path.expanduser(
            cfg_module.get(cfg, "oci", "config_file", fallback="~/.oci/config")
        )
        config_profile = cfg_module.get(cfg, "oci", "config_profile", fallback="DEFAULT")
        region = cfg_module.get(cfg, "oci", "region", fallback="")
        oci_cfg = _oci.config.from_file(config_file, config_profile)
        if region:
            oci_cfg["region"] = region
        secrets_client = _oci.secrets.SecretsClient(oci_cfg)
        import base64
        bundle = secrets_client.get_secret_bundle(secret_ocid, stage="CURRENT").data
        encoded = bundle.secret_bundle_content.content
        return base64.b64decode(encoded).decode("utf-8").strip()
    except Exception:
        return ""  # fall through to env var / prompt


def _env_password_keys(db_user: str) -> list[str]:
    """Return environment variable names to check for a user's DB password."""
    clean_user = "".join(ch if ch.isalnum() else "_" for ch in (db_user or "").upper())
    keys = []
    if clean_user:
        keys.append(f"OCI_DB_PASSWORD_{clean_user}")
    keys.append("OCI_DB_PASSWORD")
    return keys


def resolve_password(cfg, db_user: str, *,
                     allow_prompt: bool = True,
                     silent: bool = False) -> str:
    """
    Resolve the database password. silent=True suppresses informational
    output — only the bare getpass prompt is shown.

    Passwords are cached session-wide — the user is never prompted more than
    once per schema per session regardless of how many times this is called.

    Resolution order:
      1. Session cache (if already resolved this session)
      2. OCI Vault secret  ([de] secret_ocid)
      3. OCI_DB_PASSWORD_<USER> environment variable
      4. OCI_DB_PASSWORD environment variable
      5. Interactive getpass prompt (if allow_prompt=True)
    """
    # 1. Session cache — return immediately if already resolved
    cache_key = (db_user or "").strip().upper()
    if cache_key and cache_key in _password_cache:
        return _password_cache[cache_key]

    # 2. Vault
    password = _fetch_password_from_vault(cfg)
    if password:
        if not silent:
            print(f"  Password for {db_user} retrieved from Vault")
        if cache_key:
            _password_cache[cache_key] = password
        return password

    # 2 / 3. Environment variables
    for key in _env_password_keys(db_user):
        password = os.environ.get(key, "").strip()
        if password:
            if not silent:
                print(f"  Password for {db_user} read from environment variable {key}")
            return password

    # 4. Prompt
    if allow_prompt:
        try:
            import getpass
            if not silent:
                print()
                print(f"  │ Connecting to ADW as {db_user}")
                print(f"  │ Password resolution order:")
                print(f"  │   1. OCI Vault secret — set [de] secret_ocid in config")
                specific_key = _env_password_keys(db_user)[0]
                print(f"  │   2. User-specific env — export {specific_key}=\"...\"")
                print(f"  │   3. Generic env       — export OCI_DB_PASSWORD=\"...\"")
                print(f"  │   4. Prompt (current)")
                print()
            password = getpass.getpass(f"  Password for {db_user}: ")
            pwd = (password or "").strip()
            if pwd and cache_key:
                _password_cache[cache_key] = pwd
            return pwd
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("Password input cancelled")

    return ""

def _build_proxy_user(db_user: str, target_schema: str) -> str:
    """
    Build the proxy user string for Oracle proxy authentication.

    When target_schema is set the connection string becomes:
      db_user[target_schema]   e.g. DS_USER[ACME_CORP]

    The session authenticates as db_user but runs with target_schema's
    identity. Objects created land in target_schema's namespace.
    The audit trail shows db_user as the authenticated proxy user.

    If target_schema is blank, returns db_user unchanged (normal connection).
    """
    target = (target_schema or "").strip().upper()
    if target:
        return f"{db_user.upper()}[{target}]"
    return db_user


def connect(cfg):
    """
    Open and return an oracledb connection to ADW using wallet auth.

    Proxy authentication:
      If [database] target_schema is set, connects as db_user[target_schema].
      The session runs with target_schema's identity and privileges.
      db_user's own password is used — target_schema's password is never needed.
      Example: DS_USER[ACME_CORP] — authenticates as DS_USER, runs as ACME_CORP.

    Password resolution order:
      1. OCI Vault secret (if [de] secret_ocid is set in config)
      2. User-specific environment variable, e.g. OCI_DB_PASSWORD_DS_USER
      3. OCI_DB_PASSWORD environment variable
      4. Interactive prompt

    Sets TNS_ADMIN to wallet_dir so tnsnames.ora is found in the wallet.
    """
    try:
        import oracledb
    except ImportError:
        print("ERROR: oracledb not installed. Run: pip install oracledb")
        sys.exit(1)

    wallet_dir    = os.path.expanduser(cfg_module.get(cfg, "database", "wallet_dir"))
    lib_dir       = os.path.expanduser(cfg_module.get(cfg, "database", "lib_dir"))
    db_user       = cfg_module.get(cfg, "database", "db_user")
    target_schema = cfg_module.get(cfg, "database", "target_schema", fallback="").strip()
    dsn           = cfg_module.get(cfg, "database", "dsn")

    # Build proxy user string — e.g. DS_USER[ACME_CORP] or just DS_USER
    proxy_user = _build_proxy_user(db_user, target_schema)

    # Always resolve password for the personal login user (db_user),
    # not the target_schema — the DS/DE own password authenticates them.
    password = resolve_password(cfg, db_user, allow_prompt=True)
    if not password:
        raise RuntimeError(f"No password provided for {db_user}")

    # Set TNS_ADMIN so thick mode finds tnsnames.ora in the wallet directory
    os.environ["TNS_ADMIN"] = wallet_dir

    # Init Oracle thick client — safe to call multiple times
    try:
        oracledb.init_oracle_client(lib_dir=lib_dir)
    except Exception as ex:
        if "already been called" not in str(ex).lower():
            raise RuntimeError(f"Oracle client init failed: {ex}")

    try:
        conn = oracledb.connect(
            user            = proxy_user,
            password        = password,
            dsn             = dsn,
            wallet_location = wallet_dir,
        )
        return conn
    except Exception as ex:
        raise RuntimeError(f"Database connection failed for {proxy_user}: {ex}")

def check_schema_role(conn, cfg) -> str:
    """Determine whether the connected user has DE or DS capability.

    Uses COMMENT ANY TABLE as the DE indicator — it is granted to DEs via
    SA_00_DE_grants.sql and never granted to DS users. No registry table needed.

    Returns 'de' if the user has COMMENT ANY TABLE in their session privileges,
    'ds' otherwise. Fails safe to 'ds' on any error.
    Caches result on the connection object so it runs at most once per session.
    """
    cached = getattr(conn, "_ab_schema_role", None)
    if cached is not None:
        return cached

    try:
        rows = query_all(
            conn,
            "SELECT privilege FROM session_privs "
            "WHERE privilege = 'COMMENT ANY TABLE'"
        )
        role = "de" if rows else "ds"
    except Exception:
        role = "ds"  # fail safe

    try:
        conn._ab_schema_role = role
    except Exception:
        pass

    return role



    """
    Execute a single SQL or PL/SQL statement.
    Returns (success: bool, error: str or None).
    """
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        conn.commit()
        return True, None
    except Exception as ex:
        if ignore_errors:
            return False, str(ex)
        raise


def execute(conn, sql: str, ignore_errors: bool = False) -> tuple[bool, str]:
    """
    Execute a single SQL statement.

    Returns (True, "") on success, or (False, error_message) on failure.
    When ignore_errors=True a failed statement still returns (False, msg)
    so the caller can decide whether to surface it, but no exception is raised.

    review.py calls this for individual DROP / PL/SQL EXECUTE IMMEDIATE
    statements where execute_many's list interface is heavier than needed.
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        conn.commit()
        return True, ""
    except Exception as ex:
        ex_str = str(ex)
        if ignore_errors:
            return False, ex_str
        raise


def execute_many(conn, statements: list,
                 ignore_errors: bool = False,
                 label: str = "statements") -> dict:
    """
    Execute a list of SQL statements.
    Returns dict with success/skipped/failed counts and error details.
    """
    results = {"success": 0, "skipped": 0, "failed": 0, "errors": []}
    cursor  = conn.cursor()

    for i, stmt in enumerate(statements, 1):
        if not stmt or not stmt.strip():
            continue
        try:
            cursor.execute(stmt)
            conn.commit()
            results["success"] += 1
        except Exception as ex:
            ex_str = str(ex)
            already_exists_codes = (
                "ORA-00955", "ORA-01430", "ORA-04068", "ORA-00001",
            )
            if any(code in ex_str for code in already_exists_codes):
                results["skipped"] += 1
            elif ignore_errors:
                results["skipped"] += 1
                results["errors"].append({"stmt": i, "error": ex_str[:200]})
            else:
                results["failed"] += 1
                results["errors"].append({"stmt": i, "sql": stmt[:200],
                                          "error": ex_str[:400]})
                break

    return results


def _read_val(val):
    """Read a value that may be a CLOB/LOB object into a plain Python string."""
    if val is None:
        return None
    if hasattr(val, "read"):          # oracledb.LOB / cx_Oracle.LOB
        return val.read()
    return val


def query_one(conn, sql: str, params: dict = None):
    """Execute a SELECT and return the first row as a dict, or None."""
    cursor = conn.cursor()
    cursor.execute(sql, params or {})
    cols = [d[0].lower() for d in cursor.description]
    row  = cursor.fetchone()
    if row is None:
        return None
    return {k: _read_val(v) for k, v in zip(cols, row)}


def query_all(conn, sql: str, params: dict = None) -> list:
    """Execute a SELECT and return all rows as a list of dicts."""
    cursor = conn.cursor()
    cursor.execute(sql, params or {})
    cols = [d[0].lower() for d in cursor.description]
    return [{k: _read_val(v) for k, v in zip(cols, row)}
            for row in cursor.fetchall()]


def parse_sql_content(content: str) -> list:
    """
    Split a SQL file string into individual executable statements.

    Rules:
    - Markdown code fences (```sql / ``` etc.) are stripped first.
    - PL/SQL blocks (BEGIN...END) collected until a bare / on its own line.
    - Nested BEGIN/END tracked by depth counter.
    - Plain SQL ends at a bare semicolon.
    - Bare SELECT statements outside BEGIN blocks skipped.
    - Comment lines (-- and /* ... */) ignored.
    - Non-SQL prose lines (LLM postamble starting with ** etc.) skipped.
    """
    import re as _re

    # Strip markdown code fences — handles ```sql, ```plsql, ``` etc.
    content = _re.sub(r"^```[a-zA-Z]*\s*$", "", content, flags=_re.MULTILINE)
    content = _re.sub(r"^```\s*$",          "", content, flags=_re.MULTILINE)

    statements    = []
    current       = []
    plsql_depth   = 0
    in_ml_comment = False

    PLSQL_STARTERS = (
        "BEGIN", "DECLARE",
        "CREATE OR REPLACE FUNCTION",
        "CREATE OR REPLACE PROCEDURE",
        "CREATE OR REPLACE PACKAGE",
        "CREATE OR REPLACE TRIGGER",
    )

    for line in content.splitlines():
        stripped = line.strip()

        # Multi-line comment
        if "/*" in stripped:
            in_ml_comment = True
        if "*/" in stripped:
            in_ml_comment = False
            continue
        if in_ml_comment:
            continue

        # Blank / single-line comment / non-SQL prose (LLM **Note:** lines)
        if not stripped or stripped.startswith("--") or stripped.startswith("**"):
            continue

        upper = stripped.upper()

        # ── PL/SQL block start ────────────────────────────────────────────────
        if plsql_depth == 0 and any(upper.startswith(kw) for kw in PLSQL_STARTERS):
            plsql_depth = 1
            current.append(line)
            continue

        if plsql_depth > 0:
            current.append(line)

            # Bare / terminates the block
            if stripped == "/":
                stmt = "\n".join(current[:-1]).strip()
                if stmt:
                    statements.append(stmt)
                current     = []
                plsql_depth = 0
                continue

            # Track nesting — strip inline comments first
            clean = upper.split("--")[0]
            plsql_depth += len(_re.findall(r"\bBEGIN\b", clean))
            plsql_depth -= len(_re.findall(r"\bEND\b", clean))
            plsql_depth  = max(plsql_depth, 1)  # never drop below 1 until /

        else:
            # ── Plain SQL ─────────────────────────────────────────────────────
            # Skip bare SELECT statements (verification queries, not executable here)
            if upper.startswith("SELECT") and not current:
                continue

            current.append(line)

            if stripped.endswith(";"):
                stmt = "\n".join(current).strip().rstrip(";")
                if stmt and not stmt.upper().startswith("SELECT AI"):
                    statements.append(stmt)
                current = []

    return statements
