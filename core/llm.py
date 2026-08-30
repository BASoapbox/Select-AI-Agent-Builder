"""
core/llm.py
LLM interaction for the Agent Builder conversational flow.
Uses OCI GenAI inference API directly for the builder conversation.
"""

import json
import sys

try:
    import oci
except ImportError:
    print("ERROR: oci not installed. Run: pip install oci")
    sys.exit(1)

from core import config as cfg_module


def generate(clients: dict, cfg, prompt: str,
             system_prompt: str = None,
             max_tokens: int = None,
             temperature: float = None) -> str:
    """
    Call OCI GenAI inference directly (single-turn).
    Returns the text response string.
    """
    model_id    = cfg_module.get(cfg, "llm", "chat_model",
                                 fallback="meta.llama-3.3-70b-instruct")
    compartment = cfg_module.get(cfg, "compartment", "compartment_ocid")
    temp        = temperature if temperature is not None else \
                  cfg_module.get_float(cfg, "llm", "temperature", fallback=0.3)
    max_tok     = max_tokens if max_tokens is not None else \
                  cfg_module.get_int(cfg, "llm", "max_tokens", fallback=4000)

    inference_client = clients["genai_inference"]  # GenerativeAiInferenceClient — has the .chat() method

    messages = []
    if system_prompt:
        messages.append(
            oci.generative_ai_inference.models.SystemMessage(
                role    = "SYSTEM",
                content = [oci.generative_ai_inference.models.TextContent(
                    type = "TEXT", text = system_prompt
                )]
            )
        )
    messages.append(
        oci.generative_ai_inference.models.UserMessage(
            role    = "USER",
            content = [oci.generative_ai_inference.models.TextContent(
                type = "TEXT", text = prompt
            )]
        )
    )

    details = oci.generative_ai_inference.models.ChatDetails(
        compartment_id          = compartment,
        serving_mode            = oci.generative_ai_inference.models.OnDemandServingMode(
            model_id            = model_id
        ),
        chat_request            = oci.generative_ai_inference.models.GenericChatRequest(
            messages            = messages,
            max_tokens          = max_tok,
            temperature         = temp,
            is_stream           = False,
        )
    )

    try:
        response = inference_client.chat(details)
        content  = response.data.chat_response.choices[0].message.content
        if isinstance(content, list):
            return "".join(
                c.text for c in content
                if hasattr(c, "text")
            )
        return str(content)
    except Exception as ex:
        raise RuntimeError(f"LLM inference error: {ex}")


def chat_turn(clients: dict, cfg, history: list,
              user_message: str,
              system_prompt: str = None,
              temperature: float = None) -> str:
    """
    Multi-turn chat. history is a list of {"role": "USER"|"ASSISTANT", "text": "..."}.
    Returns assistant response text.
    """
    model_id    = cfg_module.get(cfg, "llm", "chat_model",
                                 fallback="meta.llama-3.3-70b-instruct")
    compartment = cfg_module.get(cfg, "compartment", "compartment_ocid")
    temp        = temperature if temperature is not None else \
                  cfg_module.get_float(cfg, "llm", "temperature", fallback=0.3)
    max_tok     = cfg_module.get_int(cfg, "llm", "max_tokens", fallback=4000)

    inference_client = clients["genai_inference"]

    messages = []
    if system_prompt:
        messages.append(
            oci.generative_ai_inference.models.SystemMessage(
                role    = "SYSTEM",
                content = [oci.generative_ai_inference.models.TextContent(
                    type = "TEXT", text = system_prompt
                )]
            )
        )

    for turn in history:
        role = turn.get("role", "USER").upper()
        text = turn.get("text", "")
        if role == "USER":
            messages.append(
                oci.generative_ai_inference.models.UserMessage(
                    role    = "USER",
                    content = [oci.generative_ai_inference.models.TextContent(
                        type = "TEXT", text = text
                    )]
                )
            )
        else:
            messages.append(
                oci.generative_ai_inference.models.AssistantMessage(
                    role    = "ASSISTANT",
                    content = [oci.generative_ai_inference.models.TextContent(
                        type = "TEXT", text = text
                    )]
                )
            )

    messages.append(
        oci.generative_ai_inference.models.UserMessage(
            role    = "USER",
            content = [oci.generative_ai_inference.models.TextContent(
                type = "TEXT", text = user_message
            )]
        )
    )

    details = oci.generative_ai_inference.models.ChatDetails(
        compartment_id = compartment,
        serving_mode   = oci.generative_ai_inference.models.OnDemandServingMode(
            model_id   = model_id
        ),
        chat_request   = oci.generative_ai_inference.models.GenericChatRequest(
            messages   = messages,
            max_tokens = max_tok,
            temperature= temp,
            is_stream  = False,
        )
    )

    try:
        response = inference_client.chat(details)
        content  = response.data.chat_response.choices[0].message.content
        if isinstance(content, list):
            return "".join(c.text for c in content if hasattr(c, "text"))
        return str(content)
    except Exception as ex:
        raise RuntimeError(f"LLM inference error: {ex}")


def extract_json(text: str) -> dict:
    """
    Extract the first JSON object from an LLM response.
    The LLM often wraps JSON in markdown fences.
    """
    import re
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in LLM response")
    return json.loads(match.group())


def call_with_validation(clients: dict, cfg, prompt: str, retries: int = 3):
    """
    Generate a spec via LLM with validation retry loop.
    Parses the <SPEC> block and validates it — retries with error feedback
    if validation fails. Raises RuntimeError after exhausting retries.
    """
    from core.spec_parser import parse_spec
    from core.spec_validator import validate_spec, SpecValidationError

    current_prompt = prompt
    for attempt in range(retries):
        raw = generate(clients, cfg, current_prompt)
        try:
            spec = parse_spec(raw)
            validate_spec(spec)
            return spec
        except SpecValidationError as e:
            current_prompt = (
                f"The spec you generated has a validation error: {e}\n"
                f"Fix ONLY the SPEC JSON and output it again wrapped in <SPEC> tags "
                f"followed by SPEC_COMPLETE. Do not ask any questions."
            )
        except Exception as e:
            current_prompt = (
                f"Could not parse your SPEC JSON: {e}\n"
                f"Output ONLY valid JSON wrapped in <SPEC> tags followed by SPEC_COMPLETE."
            )
    raise RuntimeError(f"SPEC invalid after {retries} attempts")

