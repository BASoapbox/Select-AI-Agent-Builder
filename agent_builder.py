#!/usr/bin/env python3
"""

agent_builder.py
================
Select AI Agent Builder — interactive tool for data scientists to build,
manage, and test Oracle Select AI Agent configurations.

Usage:
  python agent_builder.py
  python agent_builder.py --config /path/to/config.ini
  python agent_builder.py --option 1    # run specific option directly (skips menu)

Requires:
  pip install oci oracledb
"""

import argparse
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import oci
except ImportError:
    print("ERROR: oci not installed.  Run: pip install oci")
    sys.exit(1)

from core import config as cfg_module
from core import db as db_module
from core import oci_clients

# ── DE: extra imports (used only when [de] section is present) ────────────────
import getpass as _getpass



# ─────────────────────────────────────────────────────────────────────────────
# Display helper
# ─────────────────────────────────────────────────────────────────────────────

class Display:
    class C:
        RED    = "\033[91m"
        GREEN  = "\033[92m"
        YELLOW = "\033[93m"
        CYAN   = "\033[96m"
        BLUE   = "\033[94m"
        BOLD   = "\033[1m"
        DIM    = "\033[2m"
        RESET  = "\033[0m"

    def ok(self, msg):   print(f"  {self.C.GREEN}✓{self.C.RESET}  {msg}")
    def warn(self, msg): print(f"  {self.C.YELLOW}⚠{self.C.RESET}  {msg}")
    def err(self, msg):  print(f"  {self.C.RED}✗{self.C.RESET}  {msg}")
    def info(self, msg): print(f"  {self.C.CYAN}→{self.C.RESET}  {msg}")
    def blank(self):     print()
    def head(self, msg): print(f"\n{self.C.BOLD}{msg}{self.C.RESET}\n" + "─" * 64)


display = Display()
C = Display.C


# ─────────────────────────────────────────────────────────────────────────────
# Menu structure
# ─────────────────────────────────────────────────────────────────────────────

MENU = [
    # (key, label, group)
    ("preflight_menu", "Pre-flight check ▸",                              "SETUP"),
    ("new_project",    "Start new agent project",                         "BUILD"),
    ("resume",         "Resume existing project",                         "BUILD"),
    ("review_menu",    "Review & manage ▸",                               "REVIEW & MANAGE"),
    ("list_models",    "View available OCI GenAI models and update LLM",  "TOOLS"),
    ("storage",        "Create bucket / upload documents to Object Storage (RAG)", "TOOLS"),
    ("debug",          "Debug & diagnostics",                             "DEBUG"),
    ("quit",           "Quit",                                            ""),
]

SUBMENUS = {
    "preflight_menu": {
        "title": "PRE-FLIGHT CHECK",
        "label": "DS / DE / Schema diagnostics",
        "items": [
            ("preflight_ds",     "DS check      — user-level provisioning"),
            ("preflight_de",     "DE check      — user-level provisioning"),
            ("preflight_schema", "Schema check  — target schema configuration"),
            ("back",             "← Back"),
        ],
    },
    "review_menu": {
        "title": "REVIEW & MANAGE",
        "label": "list / view / update / delete / rebuild / test",
        "items": [
            ("list",           "List existing tools / agents / tasks / teams"),
            ("view",           "View object detail and tool invocation history"),
            ("update",         "Update tool instruction or agent role or task instruction"),
            ("update_profile", "Update profile attribute (max_tokens, temperature, model…)"),
            ("comments",       "Manage NL2SQL comments (project or schema-direct)"),
            ("delete",         "Delete tool / agent / task / team"),
            ("rebuild",        "Rebuild agent stack (drop + recreate)"),
            ("test",           "Run test prompt against a team"),
            ("back",           "← Back"),
            ("quit",           "Quit"),
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# DE: feature detection and menu extension
# ─────────────────────────────────────────────────────────────────────────────

DE_EXTRA_FIELDS = {
    "de": [
        "tenancy_ocid",       # for IAM Dynamic Group / Policy (home region)
        "home_region",        # IAM must be created in home region
        "adw_ocid",           # Dynamic Group matching rule
        "admin_dsn",          # ADMIN connection (low service)
        "target_schema",      # schema to create and grant to
        "dynamic_group_name", # name given to the Dynamic Group
        "policy_name",        # name given to the IAM Policy
        "oml_credential_name",# DB credential name; defaults to <SCHEMA>_OML_CRED if blank
        "oml_base_url",       # for pyqAppendHostAce
        # secret_ocid: OCID of an OCI Vault secret containing the schema
        # password. This tool does not create Vaults or Secrets — DE
        # supplies this OCID directly, from a secret they've already
        # created and manage themselves.
        "secret_ocid",
    ]
}

DE_SUBMENUS_EXTRA = {
    "admin_setup_menu": {
        "title": "ADMIN SETUP",
        "label": "IAM / Vault / Schema / DS user / DE user",
        "items": [
            ("de_iam_menu",    "IAM     ▸  dynamic group + policy"),
            ("de_schema_menu", "Schema  ▸  enable RP / grants / EPE ACL / create schema"),
            ("de_ds_user",     "DS user ▸  proxy grant"),
            ("de_de_user",     "DE user ▸  proxy grant + DE-specific grants"),
            ("back",           "← Back"),
        ],
    },
    "de_iam_menu": {
        "title": "IAM",
        "label": "dynamic group + policy — display CLI commands + Console instructions",
        "items": [
            ("de_iam_dg",     "Generate Dynamic Group instructions + CLI command"),
            ("de_iam_policy", "Generate IAM Policy instructions + statements"),
            ("back",          "← Back"),
        ],
    },
    "de_schema_menu": {
        "title": "SCHEMA",
        "label": "enable RP / grants / EPE ACL / optionally create schema (show + confirm → execute)",
        "items": [
            ("de_adw_schema", "Create target schema (optional — for testing)"),
            ("de_adw_rp",     "Enable Resource Principal for target schema"),
            ("de_adw_grants", "Grant DBMS_CLOUD packages + PYQADMIN + OML_DEVELOPER"),
            ("de_adw_ace",    "pyqAppendHostAce — EPE network ACL for OML endpoint"),
            ("de_adw_cred",   "Create Vault credential (DBMS_CLOUD.CREATE_CREDENTIAL)"),
            ("back",          "← Back"),
        ],
    },
    "de_ds_user": {
        "title": "DS USER",
        "label": "proxy grant (show + confirm → execute)",
        "items": [
            ("de_ds_proxy",  "Grant proxy connect for DS schema → target schema"),
            ("back",         "← Back"),
        ],
    },
    "de_de_user": {
        "title": "DE USER",
        "label": "proxy grant + DE-specific grants (show + confirm → execute)",
        "items": [
            ("de_de_proxy",  "Grant proxy connect + DE grants for DE schema"),
            ("back",         "← Back"),
        ],
    },
}


def _de_present(cfg, role: str = None) -> bool:
    """Return True if the user has DE capability.

    Two gates must both be true:
      1. Config gate  — [de] section present in config file
      2. Privilege gate — schema has DE-level DB privileges (role='de')

    If role is None (not yet determined), falls back to config gate only.
    This allows check_config to run before the DB connection is established.
    """
    config_gate = cfg is not None and cfg.has_section("de")
    if not config_gate:
        return False
    if role is None:
        return True   # config says DE, privilege not yet checked
    return role == "de"


def _build_menu(cfg, role: str = None) -> list:
    """Return the full menu list, including Admin Setup section for DEs."""
    base = MENU[:-1]   # everything except quit
    if _de_present(cfg, role):
        base = base + [
            ("admin_setup_menu", "Admin Setup ▸", "ADMIN SETUP"),
        ]
    return base + [("quit", "Quit", "")]


def _build_submenus(cfg, role: str = None) -> dict:
    """Return the full submenu dict, including DE submenus when appropriate."""
    if _de_present(cfg, role):
        return {**SUBMENUS, **DE_SUBMENUS_EXTRA}
    return SUBMENUS


def print_banner(config_path: str, dry_run: bool = False, de_mode: bool = False):
    print(f"\n{C.BOLD}{'═' * 64}{C.RESET}")
    print(f"{C.BOLD}  Select AI Agent Builder{C.RESET}")
    print(f"{'═' * 64}")
    print(f"  Config : {C.CYAN}{config_path}{C.RESET}")
    if dry_run:
        print(f"  Mode   : {C.YELLOW}DRY RUN — no changes will be made{C.RESET}")
    print(f"{'─' * 64}")


def _render_items(items, show_groups=True):
    current_group = None
    # Number only non-navigation items
    numbered = [e for e in items if e[0] not in ("quit", "back")]
    num_map  = {e[0]: i for i, e in enumerate(numbered, 1)}

    for entry in items:
        key   = entry[0]
        label = entry[1]
        group = entry[2] if len(entry) > 2 else ""
        if show_groups and group and group != current_group:
            print(f"\n  {C.BOLD}{C.DIM}{group}{C.RESET}")
            current_group = group
        if key == "quit":
            print(f"  {C.DIM} q  {label}{C.RESET}")
            continue
        if key == "back":
            print(f"  {C.DIM} b  {label}{C.RESET}")
            continue
        num = f"{C.BOLD}{num_map[key]:2}.{C.RESET}"
        if key == "check_config":
            print(f"  {num}  {C.BLUE}{label}{C.RESET}")
        elif key == "preflight":
            print(f"  {num}  {C.GREEN}{label}{C.RESET}")
        elif key in ("new_project", "resume"):
            print(f"  {num}  {C.YELLOW}{label}{C.RESET}")
        elif key == "de_run_all":
            print(f"  {num}  {C.RED}{C.BOLD}{label}{C.RESET}")
        elif key.endswith("_menu"):
            all_subs = {**SUBMENUS, **DE_SUBMENUS_EXTRA}
            sub_hint = all_subs.get(key, {}).get("label", "")
            print(f"  {num}  {C.CYAN}{label}  {C.DIM}{sub_hint}{C.RESET}")
        else:
            print(f"  {num}  {label}")


def print_menu(cfg=None, dry_run=False, role: str = None):
    print()
    _render_items(_build_menu(cfg, role))
    print()


def print_submenu(menu_key: str):
    sm = SUBMENUS[menu_key]
    print(f"\n  {C.BOLD}{sm['title']}{C.RESET}\n")
    _render_items(sm["items"], show_groups=False)
    print()


def _pick(items) -> str:
    # Navigation items use q/b shortcuts — numbered items are everything else
    keys      = [e[0] for e in items]
    has_back  = "back" in keys
    has_quit  = "quit" in keys
    numbered  = [e for e in items if e[0] not in ("quit", "back")]
    hint_parts = ["number"]
    if has_back: hint_parts.append("b=back")
    if has_quit: hint_parts.append("q=quit")
    hint = " / ".join(hint_parts)

    while True:
        try:
            raw = input(f"  {C.BOLD}[{hint}]:{C.RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        if raw == "q":
            return "quit"
        if raw == "b" and has_back:
            return "back"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(numbered):
                return numbered[idx][0]
            display.warn(f"Please enter a number between 1 and {len(numbered)}")
        except ValueError:
            display.warn(f"Enter a number{', b' if has_back else ''}, or q")


def get_menu_choice(cfg=None, role: str = None) -> str:
    return _pick(_build_menu(cfg, role))


def get_submenu_choice(menu_key: str) -> str:
    return _pick(SUBMENUS[menu_key]["items"])


# Sentinels — dispatch returns these to control the main loop
_SILENT = "__SILENT__"   # skip the "Press Enter" pause
_QUIT   = "__QUIT__"     # break the main loop entirely (quit from sub-menu)


def _unpack(result, current_clients):
    """
    Unpack dispatch result. Always returns (clients, skip_pause, do_quit).

    Handles every shape dispatch can return:
      None                → (current_clients, False, False) — show pause
      plain clients value → (clients, False, False)         — show pause
      (clients, _SILENT)  → (clients, True, False)          — skip pause
      (clients, _QUIT)    → (clients, True, True)           — skip + quit
    """
    if result is None:
        return current_clients, False, False
    if not isinstance(result, tuple):
        # Plain return value (clients object or anything else)
        return result, False, False
    if len(result) == 2:
        clients, flag = result
        if flag == _QUIT:
            return clients, True, True
        if flag == _SILENT:
            return clients, True, False
        # Unknown 2-tuple — treat as plain
        return clients, False, False
    # Unexpected shape — return current clients, show pause
    return current_clients, False, False


def run_submenu(menu_key: str, cfg, config_path: str, clients,
                all_submenus: dict = None, role: str = None):
    """Loop a sub-menu until Back or Quit. Returns (clients, quit_flag)."""
    if all_submenus is None:
        all_submenus = _build_submenus(cfg, role)
    do_quit = False
    while True:
        de = _de_present(cfg)
        print_banner(config_path, de_mode=de)
        # Print and pick from the dynamic submenu
        sm = all_submenus[menu_key]
        print(f"\n  {C.BOLD}{sm['title']}{C.RESET}\n")
        _render_items(sm["items"], show_groups=False)
        print()
        choice = _pick(sm["items"])
        if choice == "quit":
            do_quit = True
            break
        if choice == "back":
            break
        display.blank()
        skip = False
        try:
            clients, skip, do_quit = _unpack(
                dispatch(choice, cfg, config_path, clients,
                         dry_run=False, _all_submenus=all_submenus, role=role), clients)
            if do_quit:
                break
            if skip:
                continue
        except oci.exceptions.ServiceError as ex:
            display.err(f"OCI API error [{ex.status}]: {ex.message}")
            if ex.code:
                display.err(f"Code: {ex.code}")
        except Exception as ex:
            display.err(f"Error: {ex}")
        display.blank()
        try:
            input(f"  {C.DIM}Press Enter to continue...{C.RESET}")
        except (EOFError, KeyboardInterrupt):
            do_quit = True
            break
    return clients, do_quit


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────────────


# ═════════════════════════════════════════════════════════════════════════════
# DATA ENGINEER FUNCTIONS (only called when [de] section is present in config)
# ═════════════════════════════════════════════════════════════════════════════

def _de_get(cfg, key, fallback=""):
    return cfg_module.get(cfg, "de", key, fallback=fallback)


def do_iam_dynamic_group(cfg, clients, display, dry_run):
    display.head("IAM — CREATE DYNAMIC GROUP")

    tenancy_ocid  = _de_get(cfg, "tenancy_ocid")
    adw_ocid      = _de_get(cfg, "adw_ocid")
    dg_name       = _de_get(cfg, "dynamic_group_name")
    home_region   = _de_get(cfg, "home_region",
                             fallback=cfg_module.get(cfg, "oci", "region"))

    if not all([tenancy_ocid, adw_ocid, dg_name]):
        display.err("Missing [de] fields: tenancy_ocid, adw_ocid, dynamic_group_name")
        return

    matching_rule = f"resource.id = '{adw_ocid}'"
    display.info(f"Dynamic group : {dg_name}")
    display.info(f"Matching rule : {matching_rule}")
    display.info(f"Home region   : {home_region}")

    oci_cfg_home = dict(clients["_cfg"])
    oci_cfg_home["region"] = home_region
    identity = oci.identity.IdentityClient(oci_cfg_home)

    try:
        all_dgs = oci.pagination.list_call_get_all_results(
            identity.list_dynamic_groups, tenancy_ocid).data
        existing = next((dg for dg in all_dgs
                         if dg.name == dg_name and dg.lifecycle_state != "DELETED"), None)
    except Exception as ex:
        display.err(f"Could not list dynamic groups: {ex}")
        return

    if existing:
        display.ok(f"Dynamic group '{dg_name}' already exists ({existing.lifecycle_state})")
        display.info(f"OCID: {existing.id}")
        return

    if dry_run:
        display.warn(f"[DRY RUN] Would create dynamic group '{dg_name}'")
        return

    try:
        dg = identity.create_dynamic_group(
            oci.identity.models.CreateDynamicGroupDetails(
                compartment_id = tenancy_ocid,
                name           = dg_name,
                description    = f"Dynamic group for ADW instance {adw_ocid}",
                matching_rule  = matching_rule,
            )
        ).data
        display.ok(f"Dynamic group '{dg_name}' created (OCID: {dg.id})")
    except Exception as ex:
        display.err(f"Dynamic group creation failed: {ex}")


def do_iam_policy(cfg, clients, display, dry_run):
    display.head("IAM — CREATE POLICY")

    tenancy_ocid = _de_get(cfg, "tenancy_ocid")
    comp_name    = cfg_module.get(cfg, "compartment", "compartment_name")
    dg_name      = _de_get(cfg, "dynamic_group_name")
    policy_name  = _de_get(cfg, "policy_name")
    home_region  = _de_get(cfg, "home_region",
                            fallback=cfg_module.get(cfg, "oci", "region"))

    if not all([tenancy_ocid, comp_name, dg_name, policy_name]):
        display.err("Missing [de] or [compartment] fields — check config")
        return

    comp_ref = f"TeamSpace:{comp_name}" if "TeamSpace" not in comp_name else comp_name

    # Explicit dynamic group approach (scoped to named group — not any-user)
    statements = [
        f"allow dynamic-group {dg_name} to manage genai-agent-family in compartment {comp_ref}",
        f"allow dynamic-group {dg_name} to manage object-family in compartment {comp_ref}",
        f"allow dynamic-group {dg_name} to manage generative-ai-family in compartment {comp_ref}",
        f"allow any-user to manage object-family in compartment {comp_ref} where request.principal.type = 'genaiagent'",
        f"allow any-user to manage generative-ai-family in compartment {comp_ref} where request.principal.type = 'genaiagent'",
    ]

    display.info(f"Policy      : {policy_name}")
    display.info(f"Compartment : {comp_ref}")
    display.info(f"Home region : {home_region}")
    display.blank()
    for i, s in enumerate(statements, 1):
        print(f"    {i}. {s}")

    oci_cfg_home = dict(clients["_cfg"])
    oci_cfg_home["region"] = home_region
    identity = oci.identity.IdentityClient(oci_cfg_home)

    try:
        all_policies = oci.pagination.list_call_get_all_results(
            identity.list_policies, tenancy_ocid).data
        existing = next((p for p in all_policies
                         if p.name == policy_name and p.lifecycle_state != "DELETED"), None)
    except Exception as ex:
        display.err(f"Could not list policies: {ex}")
        return

    if existing:
        display.ok(f"Policy '{policy_name}' already exists — statements NOT modified")
        display.info(f"OCID: {existing.id}")
        return

    if dry_run:
        display.warn(f"[DRY RUN] Would create policy '{policy_name}'")
        return

    try:
        policy = identity.create_policy(
            oci.identity.models.CreatePolicyDetails(
                compartment_id = tenancy_ocid,
                name           = policy_name,
                description    = "Resource Principal policy for Select AI Agent Builder",
                statements     = statements,
            )
        ).data
        display.ok(f"Policy '{policy_name}' created (OCID: {policy.id})")
    except Exception as ex:
        display.err(f"Policy creation failed: {ex}")


def do_adw_resource_principal(cfg, clients, display, dry_run):
    display.head("ADW — ENABLE RESOURCE PRINCIPAL")

    target_schema = _de_get(cfg, "target_schema",
                             fallback=cfg_module.get(cfg, "database", "db_user"))
    display.info(f"Target schema : {target_schema}")

    if dry_run:
        display.warn(f"[DRY RUN] Would enable Resource Principal for {target_schema}")
        return

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return

    try:
        sql = f"""
        BEGIN
            DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL(
                username => '{target_schema.upper()}'
            );
        END;
        """
        ok, err = _exec_admin(conn, sql, ignore_errors=True)
        if ok:
            display.ok(f"Resource Principal enabled for {target_schema}")
        else:
            err_str = str(err) if err else ""
            if "already" in err_str.lower() or "ORA-20001" in err_str:
                display.ok(f"Resource Principal already enabled for {target_schema}")
            else:
                display.err(f"Failed: {err_str[:200]}")
    finally:
        conn.close()


def do_adw_grants(cfg, clients, display, dry_run):
    display.head("ADW — EXECUTE GRANTS AND ROLES")

    target_schema = _de_get(cfg, "target_schema",
                             fallback=cfg_module.get(cfg, "database", "db_user"))

    PACKAGES = ["DBMS_CLOUD_AI", "DBMS_CLOUD_AI_AGENT", "DBMS_CLOUD", "DBMS_VECTOR_CHAIN"]
    ROLES    = ["PYQADMIN", "OML_DEVELOPER"]

    display.info(f"Target schema : {target_schema}")
    display.info(f"Packages      : {', '.join(PACKAGES)}")
    display.info(f"Roles         : {', '.join(ROLES)}")

    if dry_run:
        for pkg in PACKAGES:
            display.warn(f"[DRY RUN] GRANT EXECUTE ON {pkg} TO {target_schema}")
        for role in ROLES:
            display.warn(f"[DRY RUN] GRANT {role} TO {target_schema}")
        return

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return

    try:
        for pkg in PACKAGES:
            _exec_show(conn,
                f"GRANT EXECUTE ON {pkg} TO {target_schema.upper()}",
                f"GRANT EXECUTE ON {pkg} TO {target_schema}",
                display)
        for role in ROLES:
            _exec_show(conn,
                f"GRANT {role} TO {target_schema.upper()}",
                f"GRANT {role} TO {target_schema}",
                display)
    finally:
        conn.close()


def do_adw_pyq_ace(cfg, clients, display, dry_run):
    display.head("ADW — pyqAppendHostAce (EPE NETWORK ACL)")

    from urllib.parse import urlparse

    target_schema = _de_get(cfg, "target_schema",
                             fallback=cfg_module.get(cfg, "database", "db_user"))
    oml_base_url  = _de_get(cfg, "oml_base_url")

    if not oml_base_url:
        display.err("oml_base_url not set in [de] config section")
        return

    try:
        oml_host = urlparse(oml_base_url).hostname or oml_base_url
    except Exception:
        oml_host = oml_base_url

    display.info(f"Target schema : {target_schema}")
    display.info(f"OML host      : {oml_host}")

    if dry_run:
        display.warn(f"[DRY RUN] Would add network ACE for {oml_host}:443 → {target_schema}")
        return

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return

    try:
        # DBMS_NETWORK_ACL_ADMIN — DB-level ACL
        ace_sql = f"""
        BEGIN
            DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(
                host       => '{oml_host}',
                lower_port => 443,
                upper_port => 443,
                ace        => xs$ace_type(
                    privilege_list => xs$name_list('connect','resolve'),
                    principal_name => '{target_schema.upper()}',
                    principal_type => xs_acl.ptype_db
                )
            );
        END;
        """
        _exec_show(conn, ace_sql,
                   f"Network ACE {oml_host}:443 → {target_schema}",
                   display)

        # sys.pyqAppendHostAce — OML4Py-specific ACL
        pyq_sql = f"""
        BEGIN
            sys.pyqAppendHostAce(
                hostname => '{oml_host}',
                schema   => '{target_schema.upper()}'
            );
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
        """
        ok, _ = _exec_admin(conn, pyq_sql, ignore_errors=True)
        if ok:
            display.ok(f"pyqAppendHostAce called for {oml_host}")
        else:
            display.warn("pyqAppendHostAce not available — DBMS_NETWORK_ACL_ADMIN ACE may suffice")
    finally:
        conn.close()


def do_adw_vault_credential(cfg, clients, display, dry_run):
    display.head("ADW — CREATE VAULT CREDENTIAL")

    target_schema = _de_get(cfg, "target_schema",
                             fallback=cfg_module.get(cfg, "database", "db_user"))
    secret_ocid   = _de_get(cfg, "secret_ocid")
    cred_name     = _de_get(cfg, "oml_credential_name",
                             fallback=f"{(target_schema or '').strip().upper()}_OML_CRED")

    if not secret_ocid:
        display.err("secret_ocid not set in [de] config section")
        display.info("Ask your DE team for the Vault secret OCID holding this "
                     "schema's password, then set [de] secret_ocid in config.ini")
        return

    display.info(f"Target schema : {target_schema}")
    display.info(f"Credential    : {cred_name}")
    display.info(f"Secret OCID   : {secret_ocid[:60]}...")

    if dry_run:
        display.warn(f"[DRY RUN] Would create credential '{cred_name}' in {target_schema}")
        return

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return

    try:
        # Drop first (idempotent)
        drop_sql = f"""
        BEGIN
            DBMS_CLOUD.DROP_CREDENTIAL(
                credential_name => '{cred_name}',
                owner           => '{target_schema.upper()}'
            );
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
        """
        _exec_admin(conn, drop_sql, ignore_errors=True)

        create_sql = f"""
        BEGIN
            DBMS_CLOUD.CREATE_CREDENTIAL(
                credential_name => '{cred_name}',
                params          => JSON_OBJECT(
                    'username'  VALUE '{target_schema.upper()}',
                    'secret_id' VALUE '{secret_ocid}'
                ),
                owner           => '{target_schema.upper()}'
            );
        END;
        """
        ok, err = _exec_admin(conn, create_sql, ignore_errors=True)
        if ok:
            display.ok(f"Credential '{cred_name}' created in schema {target_schema}")
            display.info("Auto-refreshes from Vault every 12 hours")
        else:
            display.err(f"Credential creation failed: {str(err)[:200]}")
    finally:
        conn.close()


def _show_confirm_exec(display, dry_run, sql_lines, title, conn, cfg):
    """Show SQL, ask for confirmation, execute if confirmed. Returns True on success."""
    C = display.C
    display.blank()
    print(f"  {C.BOLD}{title}{C.RESET}")
    print(f"  {'─'*60}")
    for line in sql_lines:
        print(f"  {C.DIM}{line}{C.RESET}" if line.startswith('--') else
              f"  {C.CYAN}{line}{C.RESET}")
    display.blank()
    if dry_run:
        display.warn("[DRY RUN] Would execute the SQL above")
        return True
    try:
        confirm = input(f"  Proceed? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        confirm = "n"
    if confirm not in ("y", "yes"):
        display.warn("Skipped")
        return False
    for line in sql_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        ok, err = _exec_admin(conn, stripped, ignore_errors=False)
        if not ok:
            display.err(f"Failed: {err}")
            return False
    display.ok("Done")
    return True


def do_ds_proxy_grant(cfg, clients, display, dry_run):
    """Grant proxy connect for a DS schema → target schema."""
    display.head("ADMIN SETUP — DS USER: PROXY GRANT")
    display.blank()

    target_schema = cfg_module.get(cfg, "database", "target_schema",
                                   fallback="").strip().upper()
    if not target_schema:
        display.err("target_schema not set in [database] config section")
        display.info("Set target_schema = ACME_CORP (or your agent-owning schema)")
        return

    try:
        ds_schema = input(
            f"  DS schema name to grant proxy access to {target_schema}: "
        ).strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if not ds_schema:
        display.warn("No schema name entered — cancelled")
        return

    sql_lines = [
        f"-- Grant proxy connect: {ds_schema} → {target_schema}",
        f"ALTER USER {target_schema} GRANT CONNECT THROUGH {ds_schema};",
    ]

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return
    try:
        ok = _show_confirm_exec(display, dry_run, sql_lines,
                                f"DS Proxy Grant: {ds_schema} → {target_schema}",
                                conn, cfg)
        if ok and not dry_run:
            display.blank()
            display.info(f"DS {ds_schema} can now connect as: {ds_schema}[{target_schema}]")
            display.info(f"They use their own password — {target_schema} password not needed")
    finally:
        conn.close()


def do_de_proxy_grant(cfg, clients, display, dry_run):
    """Grant proxy connect + DE-specific grants for a DE schema."""
    display.head("ADMIN SETUP — DE USER: PROXY GRANT + DE GRANTS")
    display.blank()

    target_schema = cfg_module.get(cfg, "database", "target_schema",
                                   fallback="").strip().upper()
    if not target_schema:
        display.err("target_schema not set in [database] config section")
        display.info("Set target_schema = ACME_CORP (or your agent-owning schema)")
        return

    try:
        de_schema = input(
            f"  DE schema name to grant proxy + DE access to {target_schema}: "
        ).strip().upper()
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if not de_schema:
        display.warn("No schema name entered — cancelled")
        return

    # Read source tables from config or prompt
    src_tables_raw = cfg_module.get(cfg, "de", "source_tables", fallback="").strip()
    if src_tables_raw:
        src_tables = [t.strip().upper() for t in src_tables_raw.split(",") if t.strip()]
    else:
        display.info("No source_tables set in [de] config — enter tables for direct SELECT grants")
        display.info("Format: SCHEMA.TABLE1, SCHEMA.TABLE2  (or press Enter to skip)")
        try:
            raw = input("  Source tables: ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        src_tables = [t.strip().upper() for t in raw.split(",") if t.strip()] if raw else []

    sql_lines = [
        f"-- Proxy connect: {de_schema} → {target_schema}",
        f"ALTER USER {target_schema} GRANT CONNECT THROUGH {de_schema};",
        "",
        f"-- DBMS_CLOUD package execute grants",
        f"GRANT EXECUTE ON DBMS_CLOUD_AI        TO {de_schema};",
        f"GRANT EXECUTE ON DBMS_CLOUD_AI_AGENT  TO {de_schema};",
        f"GRANT EXECUTE ON DBMS_CLOUD           TO {de_schema};",
        f"GRANT EXECUTE ON DBMS_CLOUD_ADMIN     TO {de_schema};",
        "",
        f"-- DE administration privileges",
        f"GRANT SELECT ANY TABLE       TO {de_schema};",
        f"GRANT SELECT ANY DICTIONARY  TO {de_schema};",
        f"GRANT COMMENT ANY TABLE      TO {de_schema};",
        f"GRANT CREATE SESSION         TO {de_schema};",
        f"GRANT CREATE TABLE           TO {de_schema};",
        f"GRANT CREATE VIEW            TO {de_schema};",
        f"GRANT CREATE PROCEDURE       TO {de_schema};",
        f"GRANT CREATE SEQUENCE        TO {de_schema};",
    ]

    if src_tables:
        sql_lines.append("")
        sql_lines.append(f"-- Direct SELECT grants (required for NL2SQL — role-based grants ignored)")
        for tbl in src_tables:
            sql_lines.append(f"GRANT SELECT ON {tbl} TO {de_schema};")

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return
    try:
        ok = _show_confirm_exec(display, dry_run, sql_lines,
                                f"DE Proxy + Grants: {de_schema} → {target_schema}",
                                conn, cfg)
        if ok and not dry_run:
            display.blank()
            display.info(f"DE {de_schema} can now connect as: {de_schema}[{target_schema}]")
            display.info(f"They use their own password — {target_schema} password not needed")
            display.info("Enable Resource Principal separately via SA_01_ACME_CORP_setup.sql")
    finally:
        conn.close()


def do_create_schema(cfg, clients, display, dry_run):
    """Create a new ADW schema (user) with standard grants."""
    display.head("ADW — CREATE SCHEMA")

    target_schema = _de_get(cfg, "target_schema",
                             fallback=cfg_module.get(cfg, "database", "db_user"))

    display.info(f"Schema to create : {target_schema}")
    display.info("Password will be prompted — it is never stored on disk.")
    display.info("After creation the password is stored in OCI Vault as a secret.")

    if dry_run:
        display.warn(f"[DRY RUN] Would create schema '{target_schema}' with standard grants")
        return

    # Check if schema already exists before prompting for password
    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM dba_users WHERE username = :u",
            {"u": target_schema.upper()}
        )
        exists = cur.fetchone()[0] > 0
    except Exception as ex:
        display.warn(f"Could not check existing users: {ex}")
        exists = False
    finally:
        conn.close()

    if exists:
        display.ok(f"Schema '{target_schema}' already exists — skipping CREATE USER")
        return

    # Prompt for password
    import getpass
    display.blank()
    print(f"  {C.YELLOW}Choose a password for schema {target_schema}.{C.RESET}")
    print(f"  {C.DIM}Min 12 chars, at least one uppercase, number, and special char.{C.RESET}")
    print(f"  {C.DIM}This tool does not store or display it — record it yourself; "
          f"DE will need it to create a Vault secret for the OML credential step.{C.RESET}")
    display.blank()
    try:
        pwd1 = getpass.getpass(f"  New password for {target_schema}: ")
        pwd2 = getpass.getpass(f"  Confirm password          : ")
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    if pwd1 != pwd2:
        display.err("Passwords do not match — schema not created")
        return
    if len(pwd1) < 12:
        display.err("Password too short (minimum 12 characters) — schema not created")
        return

    conn, err = _connect_admin(cfg)
    if not conn:
        display.err(err)
        return

    try:
        sqls = [
            f"CREATE USER {target_schema.upper()} IDENTIFIED BY '"+pwd1+"'"
            f"DEFAULT TABLESPACE DATA TEMPORARY TABLESPACE TEMP QUOTA UNLIMITED ON DATA",
            f"GRANT CREATE SESSION TO {target_schema.upper()}",
            f"GRANT CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, "
            f"CREATE PROCEDURE, CREATE TYPE, CREATE SYNONYM TO {target_schema.upper()}",
            f"GRANT DWROLE TO {target_schema.upper()}",
        ]
        for sql in sqls:
            ok, err = _exec_admin(conn, sql, ignore_errors=True)
            label = sql.strip().split("\n")[0][:70]
            if ok:
                display.ok(label)
            else:
                err_str = str(err) if err else ""
                if "already" in err_str.lower():
                    display.ok(f"{label} — already in place")
                else:
                    display.err(f"{label}: {err_str[:120]}")
    finally:
        conn.close()

    display.ok(f"Schema '{target_schema}' created")
    display.warn("This tool never stored or displayed the password you entered — "
                  "make sure you recorded it. DE will need it to create a Vault "
                  "secret for the OML credential step.")


def do_run_all(cfg, clients, display, dry_run, config_path=None):
    display.head("RUN ALL — DE SETUP END-TO-END")
    display.info("This runs all DE admin steps in the correct order:")
    display.info("  1.  Create schema (prompts for password once)")
    display.info("  2.  IAM Dynamic Group")
    display.info("  3.  IAM Policy")
    display.info("  4.  Enable Resource Principal")
    display.info("  5.  EXECUTE grants + roles")
    display.info("  6.  pyqAppendHostAce")
    display.info("  7.  Create DB credential (DBMS_CLOUD.CREATE_CREDENTIAL)")
    display.blank()
    display.warn("Step 1 will prompt for a schema password.")
    display.warn("Step 7 requires [de] secret_ocid to already be set in config — "
                  "this tool no longer creates Vaults or Secrets. Ask your DE team "
                  "for a Vault secret OCID and set it in config.ini before running.")
    display.blank()

    try:
        input(f"  Press Enter to start, or Ctrl+C to cancel: ")
    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    _cp = config_path or "agent_builder_config.ini"

    steps = [
        ("Create Schema",            do_create_schema),
        ("IAM Dynamic Group",        do_iam_dynamic_group),
        ("IAM Policy",               do_iam_policy),
        ("Resource Principal",       do_adw_resource_principal),
        ("Grants and Roles",         do_adw_grants),
        ("pyqAppendHostAce",         do_adw_pyq_ace),
        ("DB Credential",            do_adw_vault_credential),
    ]

    for label, fn in steps:
        display.blank()
        try:
            fn(cfg, clients, display, dry_run)
        except Exception as ex:
            display.err(f"Step '{label}' error: {ex}")
        print(f"  {'─' * 60}")

    display.blank()
    display.ok("All DE steps complete — run pre-flight check to verify")


# ─────────────────────────────────────────────────────────────────────────────
# Extended dispatcher — handles DE choices, falls through to DS for the rest
# ─────────────────────────────────────────────────────────────────────────────


def check_config_de(cfg, config_path: str):
    """
    Unified config check for data engineers.
    Shows all sections in one view — DS fields, preflight fields, and DE fields —
    with clear labelling of which role needs each section.
    """
    display.head("CONFIG FILE CHECK — DATA ENGINEER (FULL)")
    display.info(f"File: {config_path}")
    display.blank()

    # ── DS sections (required for both DS and DE) ─────────────────────────────
    print(f"  {C.BOLD}{C.DIM}DATA SCIENTIST SECTIONS (required for all users){C.RESET}")
    print(f"  {'─' * 60}")

    ds_sections = cfg_module.REQUIRED_FIELDS  # from core/config.py
    ds_total_issues = 0

    for section, keys in ds_sections.items():
        section_issues = []
        if not cfg.has_section(section):
            section_issues.append("section missing")
        else:
            for key in keys:
                val = cfg_module.get(cfg, section, key)
                if not val:
                    section_issues.append(key)

        if section_issues:
            display.err(f"[{section}]")
            for issue in section_issues:
                print(f"       {C.RED}✗{C.RESET}  {issue} — empty or missing")
            ds_total_issues += len(section_issues)
        else:
            display.ok(f"[{section}]  ({len(keys)} fields set)")

    # ── Preflight section ─────────────────────────────────────────────────────
    display.blank()
    print(f"  {C.BOLD}{C.DIM}PREFLIGHT SECTION (used by pre-flight check){C.RESET}")
    print(f"  {'─' * 60}")

    preflight_fields = [
        "dynamic_group_name", "policy_name", "tenancy_ocid",
    ]
    pf_issues = []
    if not cfg.has_section("preflight"):
        pf_issues.append("section missing")
    else:
        for key in preflight_fields:
            val = cfg_module.get(cfg, "preflight", key)
            if not val:
                pf_issues.append(key)

    if pf_issues:
        display.warn(f"[preflight]  — {len(pf_issues)} empty field(s):")
        for issue in pf_issues:
            print(f"       {C.YELLOW}⚠{C.RESET}  {issue}")
    else:
        display.ok(f"[preflight]  ({len(preflight_fields)} fields set)")

    # ── DE section ────────────────────────────────────────────────────────────
    display.blank()
    print(f"  {C.BOLD}{C.RED}DATA ENGINEER SECTION (DE tool only){C.RESET}")
    print(f"  {'─' * 60}")

    # secret_ocid is optional at check time — DE may not have it yet
    auto_written = {"secret_ocid"}
    # Fields with defaults
    has_defaults = {"oml_credential_name", "admin_user"}

    de_fields = [f for f in DE_EXTRA_FIELDS["de"] if not f.startswith("#")]
    de_issues = []
    if not cfg.has_section("de"):
        de_issues.append("[de] section missing — add it to config file")
    else:
        for key in de_fields:
            val = cfg_module.get(cfg, "de", key)
            if not val and key in auto_written:
                display.warn(f"[de] {key} — empty (will be written after Vault creation)")
            elif not val and key not in has_defaults:
                de_issues.append(key)
            elif not val:
                display.warn(f"[de] {key} — using default value")

    if de_issues:
        display.err(f"[de]  — {len(de_issues)} empty field(s):")
        for issue in de_issues:
            print(f"       {C.RED}✗{C.RESET}  {issue} — empty or missing")
    else:
        display.ok(f"[de]  ({len(de_fields)} fields set)")

    # ── Password env vars ─────────────────────────────────────────────────────
    display.blank()
    print(f"  {C.BOLD}{C.DIM}PASSWORD SOURCES{C.RESET}")
    print(f"  {'─' * 60}")

    ds_pwd    = os.environ.get("OCI_DB_PASSWORD",    "").strip()
    admin_pwd = os.environ.get("OCI_ADMIN_PASSWORD", "").strip()

    if ds_pwd:
        display.ok("OCI_DB_PASSWORD set  (DS schema connection)")
    else:
        display.warn("OCI_DB_PASSWORD not set — will prompt at DS connection time")

    if admin_pwd:
        display.ok("OCI_ADMIN_PASSWORD set  (ADMIN connection)")
    else:
        display.warn("OCI_ADMIN_PASSWORD not set — will prompt at ADMIN connection time")

    # ── Wallet check ──────────────────────────────────────────────────────────
    display.blank()
    print(f"  {C.BOLD}{C.DIM}WALLET & OCI CONFIG{C.RESET}")
    print(f"  {'─' * 60}")

    wallet_dir = cfg_module.get(cfg, "database", "wallet_dir")
    if wallet_dir:
        wp = Path(wallet_dir).expanduser()
        if not wp.exists():
            display.err(f"Wallet directory not found: {wp}")
            ds_total_issues += 1
        else:
            required_files = ["tnsnames.ora", "sqlnet.ora", "cwallet.sso"]
            missing_files  = [f for f in required_files if not (wp / f).exists()]
            if missing_files:
                display.err(f"Wallet missing files: {', '.join(missing_files)}")
                ds_total_issues += 1
            else:
                display.ok(f"Wallet OK: {wp}")
    else:
        display.warn("wallet_dir not set in [database]")

    config_file    = cfg_module.get(cfg, "oci", "config_file", fallback="~/.oci/config")
    config_profile = cfg_module.get(cfg, "oci", "config_profile", fallback="DEFAULT")
    expanded       = Path(config_file).expanduser()
    if not expanded.exists():
        display.err(f"OCI config file not found: {config_file}")
        ds_total_issues += 1
    else:
        try:
            import oci as oci_mod
            oci_cfg = oci_mod.config.from_file(str(expanded), config_profile)
            oci_mod.config.validate_config(oci_cfg)
            display.ok(f"OCI config valid (profile: {config_profile})")
        except Exception as ex:
            display.err(f"OCI config invalid: {ex}")
            ds_total_issues += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    display.blank()
    total_issues = ds_total_issues + len(de_issues)
    print(f"  {'─' * 60}")
    if total_issues == 0 and not pf_issues:
        print(f"  {C.GREEN}{C.BOLD}All sections complete — ready for full DE setup{C.RESET}")
    else:
        if ds_total_issues:
            print(f"  {C.RED}{C.BOLD}{ds_total_issues} DS field(s) missing "
                  f"— data scientists cannot use the tool yet{C.RESET}")
        if de_issues:
            print(f"  {C.RED}{C.BOLD}{len(de_issues)} DE field(s) missing "
                  f"— admin setup steps will fail{C.RESET}")
        if pf_issues:
            print(f"  {C.YELLOW}{C.BOLD}{len(pf_issues)} preflight field(s) empty "
                  f"— pre-flight check will skip IAM checks{C.RESET}")
    print(f"  {'─' * 60}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def _run_grant_check(cfg, clients: dict, display) -> None:
    """Thin wrapper so grant_check.run() fits the DE action signature."""
    from modules.grant_check import run as gc_run
    gc_run(cfg, clients, display)


def dispatch(choice: str, cfg, config_path: str,
             clients=None, dry_run: bool = False,
             _all_submenus: dict = None, role: str = None):
    """Route a menu choice to the correct handler.

    DS options are always available. DE options require both config gate
    ([de] section present) and privilege gate (role='de' from DB check).
    """
    all_submenus = _all_submenus if _all_submenus is not None else _build_submenus(cfg, role)

    # ── Sub-menus (DS and DE) ──────────────────────────────────────────────────
    if choice in all_submenus:
        clients, do_quit = run_submenu(choice, cfg, config_path, clients,
                                       all_submenus=all_submenus)
        return clients, (_QUIT if do_quit else _SILENT)

    # ── DE-only choices ────────────────────────────────────────────────────────
    de_actions = {
        "de_iam_dg":       do_iam_dynamic_group,
        "de_iam_policy":   do_iam_policy,
        "de_adw_schema":   do_create_schema,
        "de_adw_rp":       do_adw_resource_principal,
        "de_adw_grants":   do_adw_grants,
        "de_adw_ace":      do_adw_pyq_ace,
        "de_adw_cred":     do_adw_vault_credential,
        "de_ds_proxy":     do_ds_proxy_grant,
        "de_de_proxy":     do_de_proxy_grant,
    }

    if choice in de_actions:
        if clients is None:
            # Shouldn't happen — clients are initialised at startup.
            # Attempt recovery rather than crashing.
            display.warn("OCI clients unavailable — attempting reconnect...")
            try:
                clients = oci_clients.init(cfg)
            except Exception as ex:
                display.err(f"OCI reconnect failed: {ex}")
                display.info("Restart the tool to re-establish the connection.")
                return clients
        de_actions[choice](cfg, clients, display, dry_run)
        return clients

    # ── DS choices ────────────────────────────────────────────────────────
    # ── PRE-FLIGHT ────────────────────────────────────────────────────────────
    if choice == "preflight_ds":
        from modules.preflight_dsde import run_ds as run_ds_check
        run_ds_check(cfg, config_path, clients, display)
        return clients, _SILENT

    if choice == "preflight_de":
        from modules.preflight_dsde import run_de as run_de_check
        run_de_check(cfg, config_path, clients, display)
        return clients, _SILENT

    if choice == "preflight_schema":
        from modules.preflight_schema import run as run_schema
        run_schema(cfg, config_path, clients, display)
        return clients, _SILENT

    # OCI clients are initialised at startup — this is a safety net only
    if clients is None:
        display.warn("OCI clients unavailable — attempting reconnect...")
        try:
            clients = oci_clients.init(cfg)
        except Exception as ex:
            display.err(f"OCI reconnect failed: {ex}")
            display.info("Restart the tool to re-establish the connection.")
            return clients

    # ── SETUP ─────────────────────────────────────────────────────────────────
    if choice == "list_models":
        from modules.list_models import run
        run(cfg, config_path, clients, display)
        return clients, _SILENT

    # ── OBJECT STORAGE ────────────────────────────────────────────────────────
    elif choice == "storage":
        from modules.object_storage import run
        run(cfg, clients, display, config_path)
        return clients, _SILENT

    # ── BUILD ─────────────────────────────────────────────────────────────────
    elif choice == "new_project":
        from modules.conversation import run_new
        run_new(cfg, clients, display)
        return clients, _SILENT

    elif choice == "resume":
        from modules.conversation import run_resume
        run_resume(cfg, clients, display)
        return clients, _SILENT

    # ── REVIEW & MANAGE (sub-menu items) ──────────────────────────────────────
    elif choice == "list":
        from modules.review import run_list
        run_list(cfg, display)
        return clients, _SILENT

    elif choice == "view":
        from modules.review import run_view
        run_view(cfg, display)
        return clients, _SILENT

    elif choice == "update":
        from modules.review import run_update
        run_update(cfg, display)
        return clients, _SILENT

    elif choice == "update_profile":
        from modules.review import run_update_profile
        run_update_profile(cfg, display)
        return clients, _SILENT
        from modules.comments import run as run_comments
        run_comments(cfg, display, clients)
        return clients, _SILENT

    elif choice == "delete":
        from modules.review import run_delete
        run_delete(cfg, display)
        return clients, _SILENT

    elif choice == "rebuild":
        from modules.review import run_rebuild
        run_rebuild(cfg, display)
        return clients, _SILENT

    elif choice == "test":
        from modules.review import run_test
        run_test(cfg, display)
        return clients, _SILENT

    elif choice == "debug":
        from modules.debug_menu import run as debug_run
        debug_run(cfg, config_path, clients, display)
        return clients, _SILENT

    return clients


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Select AI Agent Builder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="agent_builder_config.ini",
        help="Path to config file (default: agent_builder_config.ini)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DE actions: show what would run without making changes"
    )
    parser.add_argument(
        "--option", default=None, type=int,
        help="Run a specific menu option number directly (skips menu)"
    )
    args = parser.parse_args()

    # Runtime overrides (model picks, RAG upload location, debug toggle) are
    # session-scoped only — clear them once, here, before the first load.
    # Do NOT call this inside the menu loop's repeated cfg_module.load()
    # calls below; that would wipe in-session selections on every redraw.
    cfg_module.clear_runtime(args.config)

    try:
        cfg = cfg_module.load(args.config)
    except FileNotFoundError as ex:
        print(f"\n  {C.RED}✗{C.RESET}  {ex}")
        print(f"  Create {args.config} from the template and fill in your values.\n")
        sys.exit(1)

    # ── Open session-level runtime log ────────────────────────────────────────
    from core import state as state_module
    from datetime import datetime
    from pathlib import Path
    _log_dir = cfg_module.get(cfg, "builder", "log_dir", fallback="./logs")
    _log_path = Path(_log_dir)
    _log_path.mkdir(parents=True, exist_ok=True)
    _session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _runtime_log = _log_path / f"agent_builder_{_session_ts}.log"
    try:
        with open(_runtime_log, "w", encoding="utf-8") as _f:
            _f.write(f"Select AI Agent Builder — Session Log\n")
            _f.write(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            _f.write(f"Config  : {args.config}\n")
            _f.write("=" * 60 + "\n\n")
    except Exception:
        pass  # Non-fatal if log dir is not writable
    # Redirect stdout to tee (screen + clean log file)
    import re as _re
    import sys as _sys

    _ANSI_ESCAPE = _re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJSTuhl]|\x1b\][^\x07]*\x07|\x1b[()][AB012]')
    _SPINNER_CLEAN = _re.compile(r'\s*\(thinking\.\.\.\)\s*')
    _BLANK_LINES = _re.compile(r'\n{3,}')

    def _clean_for_log(data: str) -> str:
        """Strip ANSI codes, spinner artifacts, and excess blank lines for the log file."""
        s = _ANSI_ESCAPE.sub('', data)      # strip all ANSI escape codes
        s = s.replace('\r', '')              # remove carriage returns (spinner overwrites)
        s = _SPINNER_CLEAN.sub('', s)       # remove thinking... residue
        s = s.replace('\x00', '')           # strip null bytes
        # Collapse 3+ consecutive blank lines to 2
        s = _BLANK_LINES.sub('\n\n', s)
        return s

    class _Tee:
        def __init__(self, terminal, logfile):
            self._terminal = terminal
            self._logfile  = logfile
            self._buf      = ""   # buffer for log to handle partial lines

        def write(self, data):
            # Terminal gets the original (with colours)
            try:
                self._terminal.write(data)
            except Exception:
                pass
            # Log file gets the cleaned version
            try:
                cleaned = _clean_for_log(data)
                if cleaned:
                    self._logfile.write(cleaned)
            except Exception:
                pass

        def flush(self):
            try: self._terminal.flush()
            except Exception: pass
            try: self._logfile.flush()
            except Exception: pass

        def write_log_only(self, data):
            """Write only to the clean session log. Used for input() echoes."""
            try:
                cleaned = _clean_for_log(data)
                if cleaned:
                    self._logfile.write(cleaned)
                    self._logfile.flush()
            except Exception:
                pass

        def isatty(self):
            return hasattr(self._terminal, 'isatty') and self._terminal.isatty()

    try:
        _log_fh = open(_runtime_log, "a", encoding="utf-8")
        _sys.stdout = _Tee(_sys.__stdout__, _log_fh)
        print(f"  Session log: {_runtime_log}")
    except Exception:
        pass

    # ── Startup: establish identity before showing any menu ──────────────────
    # check_config is allowed without DB — everything else needs a connection.
    # We connect once here, determine the role (DS or DE), then pass both
    # through the session so the menu and dispatch always know who the user is.

    db_user = cfg_module.get(cfg, "database", "db_user", fallback="")
    role    = None    # determined after DB connect
    clients = None    # OCI clients, initialised below

    def _startup_connect(allow_check_config_only: bool = False):
        """Connect to ADW + OCI at startup. Returns (conn, clients, role).
        Prints a clear startup banner. On failure: if allow_check_config_only
        is True returns (None, None, 'ds') so the user can still fix their config.
        """
        nonlocal role, clients

        target_schema = cfg_module.get(cfg, "database", "target_schema",
                                       fallback="").strip()
        if target_schema:
            connect_label = f"{db_user}[{target_schema}]  (proxy → {target_schema})"
        else:
            connect_label = db_user
        print()
        display.info(f"Starting up — connecting as {connect_label}...")

        # ── ADW connection ────────────────────────────────────────────────────
        try:
            conn = db_module.connect(cfg)
        except Exception as ex:
            display.err(f"Database connection failed: {ex}")
            if allow_check_config_only:
                display.warn("You can still use 'Check config' to diagnose the issue.")
                return None, None, "ds"
            sys.exit(1)

        # ── Privilege check → role ────────────────────────────────────────────
        role = db_module.check_schema_role(conn, cfg)
        conn.close()   # connection held per-action, not across the session

        # ── OCI clients ───────────────────────────────────────────────────────
        try:
            clients = oci_clients.init(cfg)
        except Exception as ex:
            display.err(f"OCI connection failed: {ex}")
            if allow_check_config_only:
                display.warn("OCI unavailable — some features will not work.")
                clients = None
            else:
                sys.exit(1)

        mode_label = (
            ""
        )
        display.ok(f"Connected")
        return conn, clients, role

    _startup_connect(allow_check_config_only=True)

    # Direct option — no menu loop
    if args.option is not None:
        active_menu = _build_menu(cfg, role)
        numbered = [e for e in active_menu if e[0] not in ("quit", "back")]
        idx = args.option - 1
        if 0 <= idx < len(numbered):
            de = _de_present(cfg, role)
            print_banner(args.config, dry_run=args.dry_run, de_mode=de)
            choice = numbered[idx][0]
            if choice == "check_config":
                if de:
                    check_config_de(cfg, args.config)
                else:
                    from modules.check_config import run
                    run(cfg, args.config, display)
            else:
                dispatch(choice, cfg, args.config, clients,
                         dry_run=args.dry_run, role=role)
        else:
            display.err(f"Option must be 1–{len(numbered)}")
        return

    # Interactive menu loop
    while True:
        try:
            cfg = cfg_module.load(args.config)
        except FileNotFoundError:
            pass

        de = _de_present(cfg, role)
        print_banner(args.config, dry_run=args.dry_run, de_mode=de)
        print_menu(cfg, dry_run=args.dry_run, role=role)

        choice = get_menu_choice(cfg, role)

        if choice == "quit":
            print(f"\n  {C.DIM}Goodbye.{C.RESET}\n")
            break

        display.blank()
        skip_pause = False
        do_quit    = False
        try:
            clients, skip_pause, do_quit = _unpack(
                dispatch(choice, cfg, args.config, clients,
                         dry_run=getattr(args, "dry_run", False),
                         role=role), clients)
        except oci.exceptions.ServiceError as ex:
            display.err(f"OCI API error [{ex.status}]: {ex.message}")
            if ex.code:
                display.err(f"Code: {ex.code}")
        except Exception as ex:
            display.err(f"Error: {ex}")

        if do_quit:
            print(f"\n  {C.DIM}Goodbye.{C.RESET}\n")
            break

        if not skip_pause:
            display.blank()
            try:
                input(f"  {C.DIM}Press Enter to return to menu...{C.RESET}")
            except (EOFError, KeyboardInterrupt):
                break


if __name__ == "__main__":
    main()
