"""
core/config.py
Config loading and validation for the Agent Builder.

Two-file strategy:
  agent_builder_config.ini         — user-edited file, preserved with comments
  agent_builder_config.runtime.ini — machine-written file (model selections,
                                     RAG upload location, debug toggle, etc.)
                                     — never shown to users

The runtime file is loaded on top of the user file so machine values
take precedence when set, but fall back to the user file otherwise.
Runtime filename = user filename with .ini replaced by .runtime.ini

The runtime file is entirely session-scoped: clear_runtime() wipes it once,
at the very start of the process (see agent_builder.py main()), before the
first load(). Nothing written to it survives past that point — any value a
person selects interactively (model, RAG location, debug mode) applies for
the current run only. To make a change permanent, edit agent_builder_config.ini
directly. This is deliberate: a machine-written override that outlives the
session it was set in is exactly the kind of silent-staleness bug this file
used to cause (e.g. a chat_model pick from weeks earlier silently winning
over a later, deliberate config.ini change).
"""

import configparser
import os
from pathlib import Path


# Fields required per section — checked by config checker
REQUIRED_FIELDS = {
    "oci":            ["region", "config_file", "config_profile"],
    "compartment":    ["compartment_ocid", "compartment_name"],
    "database":       ["db_user", "dsn", "wallet_dir", "lib_dir"],
    "object_storage": ["default_bucket", "default_prefix"],
    "llm":            ["chat_model", "embed_model"],
    "builder":        ["projects_dir"],
}

# Optional fields — shown separately in config checker
OPTIONAL_FIELDS = {
    "llm":            ["temperature", "max_tokens"],
    "object_storage": ["doc_directory"],
    # target_schema: blank = direct connect; set = proxy auth to that schema
    "database":       ["target_schema"],
}


def _runtime_path(user_path: str) -> Path:
    """Derive the runtime config path from the user config path."""
    p = Path(user_path)
    return p.parent / (p.stem + ".runtime.ini")


def clear_runtime(path: str) -> None:
    """
    Delete the runtime config file, if present. Call exactly once, at
    process startup, before the first load() — not on every load() call,
    since load() is re-called throughout a session (e.g. every menu-loop
    iteration) to pick up in-session changes written via update_value().
    Calling this mid-session would wipe those in-session changes too.
    """
    runtime = _runtime_path(path)
    try:
        runtime.unlink(missing_ok=True)
    except TypeError:
        # missing_ok not available before Python 3.8
        if runtime.exists():
            runtime.unlink()


def load(path: str) -> configparser.ConfigParser:
    """
    Load config. Reads user file first, then overlays runtime file.
    Returns a merged ConfigParser — the user file is never modified.
    """
    if not Path(path).exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    cfg = configparser.ConfigParser()
    cfg.read(path)                        # user file (comments preserved on disk)

    runtime = _runtime_path(path)
    if runtime.exists():
        cfg.read(str(runtime))            # machine values overlay user values

    return cfg


def is_empty(value: str) -> bool:
    return not (value or "").strip()


def validate(cfg: configparser.ConfigParser) -> list:
    """
    Return list of (section, key, issue) tuples for missing required fields.
    """
    issues = []
    for section, keys in REQUIRED_FIELDS.items():
        if not cfg.has_section(section):
            issues.append((section, "*", "section missing"))
            continue
        for key in keys:
            val = cfg.get(section, key, fallback="").strip()
            if is_empty(val):
                issues.append((section, key, "empty"))
    return issues


def get(cfg: configparser.ConfigParser, section: str,
        key: str, fallback: str = "") -> str:
    return cfg.get(section, key, fallback=fallback).strip()


def get_int(cfg: configparser.ConfigParser, section: str,
            key: str, fallback: int = 0) -> int:
    try:
        return int(cfg.get(section, key, fallback=str(fallback)).strip())
    except ValueError:
        return fallback


def get_float(cfg: configparser.ConfigParser, section: str,
              key: str, fallback: float = 0.0) -> float:
    try:
        return float(cfg.get(section, key, fallback=str(fallback)).strip())
    except ValueError:
        return fallback


def update_value(config_path: str, section: str, key: str, value: str):
    """
    Write a machine-generated value to the RUNTIME config file.
    The user's config file is never touched.
    """
    runtime_path = _runtime_path(config_path)
    cfg = configparser.ConfigParser()
    if runtime_path.exists():
        cfg.read(str(runtime_path))
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, key, value)
    with open(runtime_path, "w") as f:
        cfg.write(f)
