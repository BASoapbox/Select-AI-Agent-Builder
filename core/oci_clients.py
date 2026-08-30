"""
core/oci_clients.py
OCI client initialisation for the Agent Builder.
"""

import os
import sys

try:
    import oci
except ImportError:
    print("ERROR: oci not installed. Run: pip install oci")
    sys.exit(1)

from core import config as cfg_module


def init(cfg) -> dict:
    """
    Initialise all OCI service clients.
    Returns a dict of named clients plus the raw oci_cfg.
    """
    config_file    = os.path.expanduser(cfg_module.get(cfg, "oci", "config_file",
                                                        fallback="~/.oci/config"))
    config_profile = cfg_module.get(cfg, "oci", "config_profile", fallback="DEFAULT")
    region         = cfg_module.get(cfg, "oci", "region")

    try:
        oci_cfg = oci.config.from_file(config_file, config_profile)
        if region:
            oci_cfg["region"] = region
        oci.config.validate_config(oci_cfg)
    except Exception as ex:
        raise RuntimeError(f"OCI config error: {ex}")

    clients = {
        "_cfg":           oci_cfg,
        "identity":       oci.identity.IdentityClient(oci_cfg),
        "object_storage": oci.object_storage.ObjectStorageClient(oci_cfg),
        "genai":          oci.generative_ai.GenerativeAiClient(oci_cfg),
        "genai_inference":oci.generative_ai_inference.GenerativeAiInferenceClient(oci_cfg),
    }
    # genai_mgmt is the same management client — aliased so list_models.py
    # can call list_chat_models(clients, compartment) without passing cfg
    clients["genai_mgmt"] = clients["genai"]
    return clients


# Capability strings OCI uses for chat/text generation models
_CHAT_CAPS = {"CHAT", "TEXT_GENERATION"}

# Capability strings OCI uses for embedding models
# OCI has used different strings across SDK versions — catch all variants
_EMBED_CAPS = {"EMBEDDING", "EMBED", "TEXT_EMBEDDINGS", "EMBEDDINGS"}


def _fetch_all_models(clients: dict, compartment_ocid: str) -> list:
    """Fetch all ACTIVE models from OCI GenAI."""
    genai = clients["genai"]
    try:
        all_models = oci.pagination.list_call_get_all_results(
            genai.list_models,
            compartment_id=compartment_ocid,
        ).data
    except Exception as ex:
        raise RuntimeError(f"Failed to list models: {ex}")
    return [m for m in all_models
            if getattr(m, "lifecycle_state", "") == "ACTIVE"]


def _model_record(m) -> dict:
    caps = getattr(m, "capabilities", []) or []
    # Normalise to uppercase strings — OCI SDK returns enum objects in some versions
    caps = [str(c).upper() if not isinstance(c, str) else c.upper() for c in caps]
    return {
        "id":           m.id,
        "display_name": getattr(m, "display_name", m.id),
        "vendor":       getattr(m, "vendor", ""),
        "version":      getattr(m, "version", ""),
        "capabilities": caps,
    }


def list_chat_models(clients: dict, compartment_ocid: str) -> list:
    """Return list of active chat/text-generation models."""
    result = []
    for m in _fetch_all_models(clients, compartment_ocid):
        rec  = _model_record(m)
        caps = set(rec["capabilities"])
        if caps & _CHAT_CAPS:
            result.append(rec)
    return sorted(result, key=lambda x: (x["vendor"], x["display_name"]))


def list_embed_models(clients: dict, compartment_ocid: str) -> list:
    """Return list of active embedding models."""
    result = []
    for m in _fetch_all_models(clients, compartment_ocid):
        rec  = _model_record(m)
        caps = set(rec["capabilities"])
        if caps & _EMBED_CAPS:
            result.append(rec)
    return sorted(result, key=lambda x: (x["vendor"], x["display_name"]))


def list_all_models_debug(clients: dict, compartment_ocid: str) -> list:
    """
    Return ALL active models with their raw capability strings.
    Used by the debug option in list_models.py to see exactly what OCI returns.
    """
    result = []
    for m in _fetch_all_models(clients, compartment_ocid):
        result.append(_model_record(m))
    return sorted(result, key=lambda x: (x["vendor"], x["display_name"]))


# ── Model listing (added in V3 — from V2) ────────────────────────────────────

from oci.generative_ai import GenerativeAiClient as _GenAiMgmt


def _ensure_mgmt_client(clients: dict, cfg):
    """Lazily init the GenAI management client (for listing models)."""
    if "genai_mgmt" not in clients:
        import oci as _oci
        from core import config as _cfg
        config_file = _cfg.get(cfg, "oci", "config_file")
        profile     = _cfg.get(cfg, "oci", "config_profile")
        region      = _cfg.get(cfg, "oci", "region")
        oci_cfg     = _oci.config.from_file(config_file, profile)
        oci_cfg["region"] = region
        endpoint = f"https://generativeai.{region}.oci.oraclecloud.com"
        clients["genai_mgmt"] = _GenAiMgmt(
            config=oci_cfg, service_endpoint=endpoint
        )
    return clients["genai_mgmt"]


def _model_to_dict(m) -> dict:
    caps = []
    if hasattr(m, "capabilities") and m.capabilities:
        caps = [str(c).lower() for c in m.capabilities]
    return {
        "display_name":     m.display_name or m.id,
        "vendor":           getattr(m, "vendor", "unknown") or "unknown",
        "capabilities":     caps,
        "lifecycle_state":  getattr(m, "lifecycle_state", ""),
    }


def list_chat_models(clients: dict, compartment_id: str, cfg=None) -> list:
    """Return active chat / text-generation models."""
    mgmt = _ensure_mgmt_client(clients, cfg) if cfg else clients["genai_mgmt"]
    raw  = mgmt.list_models(compartment_id=compartment_id).data.items or []
    result = []
    for m in raw:
        d = _model_to_dict(m)
        if d["lifecycle_state"] not in ("", "ACTIVE") and d["lifecycle_state"]:
            continue
        if any("chat" in c or "text_generation" in c or "generation" in c
               for c in d["capabilities"]):
            result.append(d)
    return result


def list_embed_models(clients: dict, compartment_id: str, cfg=None) -> list:
    """Return active embedding models."""
    mgmt = _ensure_mgmt_client(clients, cfg) if cfg else clients["genai_mgmt"]
    raw  = mgmt.list_models(compartment_id=compartment_id).data.items or []
    result = []
    for m in raw:
        d = _model_to_dict(m)
        if d["lifecycle_state"] not in ("", "ACTIVE") and d["lifecycle_state"]:
            continue
        if any("embed" in c for c in d["capabilities"]):
            result.append(d)
    return result


def list_all_models_debug(clients: dict, compartment_id: str, cfg=None) -> list:
    """Return all models with raw capabilities — for diagnostics."""
    mgmt = _ensure_mgmt_client(clients, cfg) if cfg else clients["genai_mgmt"]
    raw  = mgmt.list_models(compartment_id=compartment_id).data.items or []
    return [_model_to_dict(m) for m in raw]
