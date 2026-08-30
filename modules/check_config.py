"""
modules/check_config.py
Menu option 1 — check config file for missing or empty values.
"""

import os
from pathlib import Path

from core import config as cfg_module


# Section-level explanations — what each section enables
SECTION_HELP = {
    "oci": (
        "OCI credentials",
        "Required for ALL options — used to connect to Oracle Cloud services "
        "(GenAI models, Object Storage, Vault)."
    ),
    "compartment": (
        "OCI compartment",
        "Required for ALL options — identifies which OCI compartment your "
        "resources live in."
    ),
    "database": (
        "ADW connection",
        "Required to build agents — connects to your Autonomous Database to "
        "create and manage tools, agents, tasks, and teams."
    ),
    "object_storage": (
        "Object Storage / RAG",
        "Required ONLY for RAG tools — upload documents (PDF, Word, etc.) to Object Storage for "
        "vector search. Skip if you only need SQL or Python tools."
    ),
    "llm": (
        "LLM preference",
        "Required for agent building — sets which OCI GenAI model drives the "
        "builder conversation and which embedding model is used for RAG."
    ),
    "builder": (
        "Builder settings",
        "Optional — sets where project specs are saved between sessions. "
        "Defaults to ./projects in the current directory."
    ),
}


def run(cfg, config_path: str, display):
    C = display.C
    display.head("CONFIG FILE CHECK")
    display.info(f"File: {config_path}")
    display.blank()

    # Print section legend
    print(f"  {C.BOLD}Section overview{C.RESET}  (what each section enables):")
    for section, (short, desc) in SECTION_HELP.items():
        print(f"  {C.DIM}[{section}]{C.RESET}  {C.BOLD}{short}{C.RESET}")
        print(f"         {C.DIM}{desc}{C.RESET}")
    display.blank()
    print(f"  {'─' * 60}")
    display.blank()

    issues   = cfg_module.validate(cfg)
    sections = {}
    for section, key, problem in issues:
        sections.setdefault(section, []).append((key, problem))

    all_sections = list(cfg_module.REQUIRED_FIELDS.keys())
    total_issues = 0

    for section in all_sections:
        section_issues = sections.get(section, [])
        required_count = len(cfg_module.REQUIRED_FIELDS.get(section, []))
        optional_keys  = cfg_module.OPTIONAL_FIELDS.get(section, [])
        optional_set   = [k for k in optional_keys
                          if not cfg_module.is_empty(
                              cfg.get(section, k, fallback=""))]
        optional_empty = [k for k in optional_keys if k not in optional_set]

        if section_issues:
            display.err(f"[{section}]")
            for key, problem in section_issues:
                display.err(f"       {key} — {problem}")
            total_issues += len(section_issues)
        else:
            suffix = f"  ({required_count} required"
            if optional_keys:
                suffix += f", {len(optional_set)}/{len(optional_keys)} optional set"
                if optional_empty:
                    suffix += f" — {', '.join(optional_empty)} not set"
            suffix += ")"
            display.ok(f"[{section}]{suffix}")

    display.blank()

    # ── Wallet directory check ────────────────────────────────────────────────
    wallet_dir = cfg_module.get(cfg, "database", "wallet_dir")
    display.info("Wallet directory:")
    if not wallet_dir:
        display.warn("wallet_dir not set in [database]")
    else:
        wallet_path = Path(wallet_dir).expanduser()
        if not wallet_path.exists():
            display.err(f"Wallet directory not found: {wallet_path}")
            total_issues += 1
        else:
            required = ["tnsnames.ora", "sqlnet.ora", "cwallet.sso"]
            missing  = [f for f in required if not (wallet_path / f).exists()]
            if missing:
                display.err(f"Missing wallet files: {', '.join(missing)}")
                total_issues += 1
            else:
                display.ok(f"{wallet_path}  (tnsnames.ora, sqlnet.ora, cwallet.sso ✓)")

    display.blank()

    # ── OCI config check ──────────────────────────────────────────────────────
    display.info("OCI config file:")
    config_file    = cfg_module.get(cfg, "oci", "config_file", fallback="~/.oci/config")
    config_profile = cfg_module.get(cfg, "oci", "config_profile", fallback="DEFAULT")
    expanded       = Path(config_file).expanduser()
    if not expanded.exists():
        display.err(f"OCI config file not found: {config_file}")
        total_issues += 1
    else:
        try:
            import oci
            oci_cfg = oci.config.from_file(str(expanded), config_profile)
            oci.config.validate_config(oci_cfg)
            display.ok(f"{config_file}  (profile: {config_profile} ✓)")
        except Exception as ex:
            display.err(f"OCI config invalid: {ex}")
            total_issues += 1

    display.blank()

    # ── Password source ───────────────────────────────────────────────────────
    display.info("Password source:")
    db_pwd = os.environ.get("OCI_DB_PASSWORD", "").strip()
    if db_pwd:
        display.ok("OCI_DB_PASSWORD env var is set")
    else:
        display.warn("OCI_DB_PASSWORD not set — will prompt at connection time")
        print(f"       {display.C.DIM}Tip: export OCI_DB_PASSWORD=\"your_password\"{display.C.RESET}")

    display.blank()

    # ── Projects directory ────────────────────────────────────────────────────
    display.info("Projects directory:")
    projects_dir = cfg_module.get(cfg, "builder", "projects_dir", fallback="./projects")
    p = Path(projects_dir)
    if p.exists():
        count = len(list(p.glob("*.json")))
        display.ok(f"{p.resolve()}  ({count} saved project(s))")
    else:
        display.ok(f"{p.resolve()}  (will be created on first save)")

    display.blank()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"  {'─' * 60}")
    if total_issues == 0:
        print(f"  {C.GREEN}{C.BOLD}All checks passed — ready to run{C.RESET}")
    else:
        print(f"  {C.RED}{C.BOLD}{total_issues} issue(s) found{C.RESET}")
        display.blank()
        print(f"  {C.BOLD}Reminder — what each section is needed for:{C.RESET}")
        for section in all_sections:
            if section in sections:
                short, desc = SECTION_HELP.get(section, (section, ""))
                print(f"  {C.YELLOW}[{section}]{C.RESET}  {short} — {desc}")
    print(f"  {'─' * 60}")

    display.blank()
