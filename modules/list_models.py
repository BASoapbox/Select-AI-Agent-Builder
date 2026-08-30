"""
modules/list_models.py
Menu option 3 — view available OCI GenAI models and update LLM preference.

Displays display_name (e.g. meta.llama-3.3-70b-instruct) — the value that
goes in the config and is passed to the OCI GenAI inference API.
"""

from core import config as cfg_module
from core import oci_clients


def _show_table(models, label, current):
    print(f"\n  {f' {label} ':─^64}")
    if not models:
        print(f"  (none found)")
        return
    print(f"  {'#':<5}  {'Display Name (use this in config)':<44}  {'Vendor':<8}  Capabilities")
    print(f"  {'─'*5}  {'─'*44}  {'─'*8}  {'─'*24}")
    for i, m in enumerate(models, 1):
        marker = f"  ◀ current" if m["display_name"] == current else ""
        caps   = ", ".join(m["capabilities"])
        print(f"  {i:<5}  {m['display_name']:<44}  {m['vendor']:<8}  {caps}{marker}")


def run(cfg, config_path: str, clients: dict, display):
    display.head("OCI GENAI — AVAILABLE MODELS")

    compartment   = cfg_module.get(cfg, "compartment", "compartment_ocid")
    current_chat  = cfg_module.get(cfg, "llm", "chat_model")
    current_embed = cfg_module.get(cfg, "llm", "embed_model")

    display.info(f"Current chat model  : {current_chat  or 'not set'}")
    display.info(f"Current embed model : {current_embed or 'not set'}")
    display.blank()

    display.info("Fetching models from OCI GenAI...")
    try:
        chat_models  = oci_clients.list_chat_models(clients, compartment)
        embed_models = oci_clients.list_embed_models(clients, compartment)
    except Exception as ex:
        display.err(f"Failed to list models: {ex}")
        return

    _show_table(chat_models,  "CHAT / TEXT GENERATION MODELS", current_chat)
    _show_table(embed_models, "EMBEDDING MODELS",               current_embed)

    # ── If no embed models found, show a debug dump of all capabilities ───────
    if not embed_models:
        display.blank()
        display.warn("No embedding models found — showing all model capabilities for diagnosis:")
        try:
            all_models = oci_clients.list_all_models_debug(clients, compartment)
            unique_caps = sorted({cap for m in all_models for cap in m["capabilities"]})
            display.info(f"All capability strings returned by OCI: {unique_caps}")
            display.blank()
            print(f"  {'Display Name':<44}  {'Vendor':<8}  Capabilities")
            print(f"  {'─'*44}  {'─'*8}  {'─'*30}")
            for m in all_models:
                caps = ", ".join(m["capabilities"])
                print(f"  {m['display_name']:<44}  {m['vendor']:<8}  {caps}")
        except Exception as ex:
            display.err(f"Debug dump failed: {ex}")

    display.blank()
    print(f"  Note: chat model is used for agent reasoning, NL2SQL/RAG responses, and conversation prompts; "
          f"embed model is used to create/query vector embeddings for RAG search.")
    print(f"  Note: If updating the config.ini file ([llm] chat_model / embed_model) use the Display Name above.")
    display.blank()
    print(f"  {display.C.DIM}Enter a number to update, Enter to skip, "
          f"b to go back, q to quit.{display.C.RESET}")
    display.blank()

    # ── Update chat model ─────────────────────────────────────────────────────
    try:
        answer = input(
            f"  Update chat model [number / Enter=skip / b=back / q=quit]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if answer in ("q", "quit"):
        return
    if answer in ("b", "back"):
        return

    if answer:
        try:
            idx = int(answer) - 1
            if 0 <= idx < len(chat_models):
                chosen = chat_models[idx]["display_name"]
                confirm = input(f"  Update chat model to: {chosen}? [y/N]: ").strip().lower()
                if confirm == "y":
                    cfg_module.update_value(config_path, "llm", "chat_model", chosen)
                    display.ok(f"Chat model updated to: {chosen}")
                else:
                    display.warn("Cancelled — no change made")
            else:
                display.warn("Invalid selection — no change made")
        except ValueError:
            display.warn("Not a number — no change made")

    # ── Update embed model ────────────────────────────────────────────────────
    if embed_models:
        try:
            answer2 = input(
                f"  Update embed model [number / Enter=skip / b=back / q=quit]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if answer2 in ("q", "quit", "b", "back"):
            return

        if answer2:
            try:
                idx = int(answer2) - 1
                if 0 <= idx < len(embed_models):
                    chosen = embed_models[idx]["display_name"]
                    confirm = input(f"  Update embed model to: {chosen}? [y/N]: ").strip().lower()
                    if confirm == "y":
                        cfg_module.update_value(config_path, "llm", "embed_model", chosen)
                        display.ok(f"Embed model updated to: {chosen}")
                    else:
                        display.warn("Cancelled — no change made")
                else:
                    display.warn("Invalid selection — no change made")
            except ValueError:
                display.warn("Not a number — no change made")
    else:
        display.blank()
        display.warn("No embedding models found — set embed_model manually in config:")
        print(f"  embed_model = cohere.embed-multilingual-v3.0")
        display.info("Common embed model names:")
        print(f"  cohere.embed-multilingual-v3.0")
        print(f"  cohere.embed-english-v3.0")
        print(f"  cohere.embed-multilingual-light-v3.0")

    # ── Temperature ───────────────────────────────────────────────────────────
    display.blank()
    current_temp = cfg_module.get(cfg, "llm", "temperature", fallback="")
    print(f"  {display.C.BOLD}Temperature{display.C.RESET}  (current: {current_temp or 'not set'})")
    print(f"  Controls how creative vs precise the LLM is during the builder conversation:")
    print(f"    0.0 = fully deterministic — always picks the most likely word")
    print(f"    0.3 = structured, consistent — recommended for agent building")
    print(f"    0.7 = more varied responses")
    print(f"    1.0 = very creative, unpredictable")
    display.blank()
    try:
        t_ans = input(f"  Set temperature [Enter=skip / b=back / q=quit, recommended: 0.3]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if t_ans in ("q", "quit", "b", "back"):
        return
    if t_ans:
        try:
            t_val = float(t_ans)
            if 0.0 <= t_val <= 1.0:
                confirm = input(f"  Set temperature to {t_val}? [y/N]: ").strip().lower()
                if confirm == "y":
                    cfg_module.update_value(config_path, "llm", "temperature", str(t_val))
                    display.ok(f"Temperature set to: {t_val}")
                else:
                    display.warn("Cancelled — no change made")
            else:
                display.warn("Value must be between 0.0 and 1.0 — no change made")
        except ValueError:
            display.warn("Not a number — no change made")

    # ── Max tokens ────────────────────────────────────────────────────────────
    display.blank()
    current_tokens = cfg_module.get(cfg, "llm", "max_tokens", fallback="")
    print(f"  {display.C.BOLD}Max tokens{display.C.RESET}  (current: {current_tokens or 'not set'})")
    print(f"  Maximum length of each LLM response in the builder conversation:")
    print(f"    1000  = short responses, may truncate complex answers")
    print(f"    2000  = minimum for reliable PL/SQL code generation")
    print(f"    4000  = recommended — handles full agent stack generation")
    print(f"    8000  = use if generated PL/SQL is being cut off")
    display.blank()
    try:
        tok_ans = input(f"  Set max_tokens [Enter=skip / b=back / q=quit, recommended: 4000]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if tok_ans in ("q", "quit", "b", "back"):
        return
    if tok_ans:
        try:
            tok_val = int(tok_ans)
            if tok_val >= 100:
                confirm = input(f"  Set max_tokens to {tok_val}? [y/N]: ").strip().lower()
                if confirm == "y":
                    cfg_module.update_value(config_path, "llm", "max_tokens", str(tok_val))
                    display.ok(f"max_tokens set to: {tok_val}")
                else:
                    display.warn("Cancelled — no change made")
            else:
                display.warn("Value must be at least 100 — no change made")
        except ValueError:
            display.warn("Not a number — no change made")
