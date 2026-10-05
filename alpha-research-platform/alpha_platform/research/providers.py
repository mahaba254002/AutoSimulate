"""Small provider adapters. Credentials never enter database records or prompts."""
import json
from copy import deepcopy
import re
import threading
import requests

from alpha_platform.config.settings import get_settings
from alpha_platform.db.models import WorkspacePreference
from alpha_platform.db.session import SessionLocal

PROVIDERS = {"openai": "OpenAI", "gemini": "Gemini", "anthropic": "Claude", "groq": "Groq"}
SESSION_KEYS = {}
KEY_LOCK = threading.Lock()


def api_key(provider):
    with KEY_LOCK:
        key = SESSION_KEYS.get(provider)
    return key or getattr(get_settings(), {"openai": "openai_api_key", "gemini": "gemini_api_key",
                                          "anthropic": "anthropic_api_key", "groq": "groq_api_key"}[provider])


def profiles():
    with SessionLocal() as db:
        saved = db.get(WorkspacePreference, "providers")
        prefs = saved.value if saved else {}
    return [{"id": p, "name": name, "model": prefs.get(p, ""), "configured": bool(api_key(p))}
            for p, name in PROVIDERS.items()]


def save_profile(provider, model, key=None):
    if provider not in PROVIDERS or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", model):
        raise ValueError("Choose a supported provider and a valid model identifier.")
    with SessionLocal() as db:
        record = db.get(WorkspacePreference, "providers")
        if record is None:
            record = WorkspacePreference(key="providers", value={})
            db.add(record)
        record.value = {**record.value, provider: model}
        db.commit()
    if key:
        with KEY_LOCK:
            SESSION_KEYS[provider] = key
    return profiles()


# UTF-8 bytes conservatively bound text tokens; reserve space for chat framing
# and up to 2,500 completion tokens under the observed 8,000-token free tier.
GROQ_INPUT_BYTES = 4800
GROQ_OUTPUT_TOKENS = 2500


def compact_schema(value):
    if isinstance(value, dict):
        return {key: compact_schema(item) for key, item in value.items() if key not in {"title", "default", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern"}}
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    return value


def fit_groq_context(system, context):
    result = deepcopy(context)
    if "output_schema" in result:
        result["output_schema"] = compact_schema(result["output_schema"])
    result.pop("prior_research", None)
    if "operators" in result:
        result["operators"] = [{"name": o["name"], "signature": o["signature"]} for o in result["operators"]]
    if "fields" in result:
        result["fields"] = [{k: f[k] for k in ("id", "type", "description", "dataset", "units", "frequency") if k in f} for f in result["fields"]]
    def size():
        return len(system.encode("utf-8")) + len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 64
    # Drop least-relevant entries already sorted by planning; never truncate evidence,
    # the user's objective, source expression, or schema.
    while size() > GROQ_INPUT_BYTES:
        fields = result.get("fields", [])
        if len(fields) > 1:
            originals = set(result.get("required_field_ids", []))
            index = next((i for i in range(len(fields)-1, -1, -1) if fields[i]["id"] not in originals), None)
            if index is not None:
                fields.pop(index)
                continue
        catalogue = result.get("catalogue", [])
        if catalogue:
            last = catalogue[-1]
            if len(last["datasets"]) > 1:
                last["datasets"].pop()
                continue
            if len(catalogue) > 1:
                catalogue.pop()
                continue
        raise ValueError("This research objective and required evidence exceed Groq's free-tier request budget. Shorten the objective or use another provider; no API request was sent.")
    return result


def provider_error(response, provider, key):
    guidance = {
        400: "The provider rejected the request format or parameters.",
        401: "The API key was rejected. Check the key in LLM Integration.",
        403: "The API account lacks permission for this request.",
        413: "The request exceeds this provider account's size or token limit. Reduce research context before retrying.",
        503: "The provider is temporarily busy. Wait briefly, then retry this same project.",
        404: "Check the model identifier and your account's model access.",
        429: "Check API billing, available quota, and rate limits.",
    }.get(response.status_code, "The provider could not complete the request.")
    detail = ""
    try:
        error = response.json().get("error", {})
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                message = message.replace(key, "[redacted]")
                message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", message)
                message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted]", message)
                detail = " ".join(message.split())[:500]
    except (ValueError, TypeError, AttributeError):
        pass
    return f"{PROVIDERS[provider]} returned HTTP {response.status_code}. {guidance}" + (" Provider detail: " + detail if detail else "")


def generate_json(provider, model, system, context):
    if provider not in PROVIDERS or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", model):
        raise ValueError("Configure a supported research provider and model first.")
    key = api_key(provider)
    if not key:
        raise ValueError(f"Configure a {PROVIDERS[provider]} API key in LLM Integration first.")
    context = fit_groq_context(system, context) if provider == "groq" else context
    prompt = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    headers = {"Content-Type": "application/json"}
    if provider == "openai":
        url = "https://api.openai.com/v1/responses"
        headers["Authorization"] = "Bearer " + key
        payload = {"model": model, "instructions": system, "input": "Return a JSON object matching the supplied output schema.\n" + prompt, "store": False,
                   "max_output_tokens": 12000, "text": {"format": {"type": "json_object"}}}
    elif provider == "groq":
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers["Authorization"] = "Bearer " + key
        payload = {"model": model, "messages": [{"role": "system", "content": system + "\nReturn JSON only."},
                   {"role": "user", "content": prompt}], "max_completion_tokens": GROQ_OUTPUT_TOKENS,
                   "response_format": {"type": "json_object"}}
    elif provider == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers["x-goog-api-key"] = key
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 12000}}
    else:
        url = "https://api.anthropic.com/v1/messages"
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        payload = {"model": model, "system": system, "max_tokens": 12000,
                   "messages": [{"role": "user", "content": prompt}]}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=(10, 180))
        if response.status_code != 200:
            raise ValueError(provider_error(response, provider, key))
        data = response.json()
        if provider == "openai":
            output = "".join(c.get("text", "") for item in data.get("output", [])
                             for c in item.get("content", []) if c.get("type") == "output_text")
            usage = data.get("usage", {})
        elif provider == "groq":
            output = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
        elif provider == "gemini":
            output = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            usage = data.get("usageMetadata", {})
        else:
            output = "".join(c.get("text", "") for c in data["content"] if c.get("type") == "text")
            usage = data.get("usage", {})
        if output.strip().startswith("```"):
            output = re.sub(r"^```(?:json)?\s*|\s*```$", "", output.strip())
        return json.loads(output), usage
    except (KeyError, IndexError, json.JSONDecodeError):
        # requests.JSONDecodeError is also a RequestException; handle it first.
        raise ValueError(f"{PROVIDERS[provider]} returned an incomplete or invalid JSON research response. No automatic retry was attempted.") from None
    except requests.exceptions.ConnectTimeout:
        raise ValueError(f"{PROVIDERS[provider]} connection timed out before a response. Check your connection and retry.") from None
    except requests.exceptions.ReadTimeout:
        raise ValueError(f"{PROVIDERS[provider]} did not respond within 180 seconds. The request may still have been processed; no automatic retry was attempted.") from None
    except requests.exceptions.SSLError:
        raise ValueError(f"{PROVIDERS[provider]} TLS certificate verification failed. Check your computer clock and network certificate configuration.") from None
    except requests.exceptions.ProxyError:
        raise ValueError(f"{PROVIDERS[provider]} could not connect through the configured proxy. Check your proxy connection.") from None
    except requests.exceptions.ConnectionError:
        raise ValueError(f"{PROVIDERS[provider]} connection was interrupted or could not be established. Check network, DNS and firewall access. No automatic retry was attempted.") from None
    except requests.RequestException:
        raise ValueError(f"{PROVIDERS[provider]} request failed during network transport. No automatic retry was attempted.") from None
