"""ChatGPT (OpenAI), DeepSeek, Kimi (Moonshot) and Mistral: they all speak the same "chat completions" API,
so one connection covers them. Same structured answers as Claude and Gemini: the JSON schema goes in
`response_format`; a service that doesn't take a schema gets plain JSON mode with the schema in the
instructions, and every answer is checked against the schema either way."""
import json
import logging
import time

import requests
from pydantic import ValidationError

log = logging.getLogger("tips.openai_compat")

# value: (name people see, API address, where to get a key, key starts with, models to prefer, in order)
PROVIDERS = {
    "openai": ("ChatGPT", "https://api.openai.com/v1", "https://platform.openai.com/api-keys", "sk-",
               ["gpt-5.4-mini", "gpt-5-mini", "gpt-4.1-mini", "gpt-4o-mini"]),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", "https://platform.deepseek.com/api_keys", "sk-",
                 ["deepseek-v4-flash", "deepseek-flash", "deepseek-chat"]),
    "kimi": ("Kimi", "https://api.moonshot.ai/v1", "https://platform.moonshot.ai/console/api-keys", "sk-",
             ["kimi-k2.6", "kimi-k2.5", "kimi-k2-turbo-preview", "kimi-latest", "moonshot-v1-32k"]),
    "mistral": ("Mistral", "https://api.mistral.ai/v1", "https://console.mistral.ai/api-keys", "",
                ["mistral-small-latest", "mistral-medium-latest", "mistral-large-latest"]),
}
BUSY_WAIT = 2


def _error():
    from .ai import AIError
    return AIError


def name(provider):
    return PROVIDERS[provider][0]


def _messages(system, content, schema=None):
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image":
            source = block["source"]
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"}})
    if schema is not None:  # JSON mode only: the shape goes in the instructions
        system += ("\n\nAnswer with ONE JSON object only (no other text), matching this JSON schema:\n"
                   + json.dumps(schema))
    only_text = all(p["type"] == "text" for p in parts)
    user = "\n\n".join(p["text"] for p in parts) if only_text else parts
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _post(base, key, body, timeout=20):
    return requests.post(f"{base}/chat/completions", json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {key}", "content-type": "application/json"})


def parse(provider, system, content, output_model):
    """Ask the service; return a validated `output_model`. Raises AIError with a sentence a person can act on."""
    from . import gemini
    from .platform import other_ai_key, other_ai_model
    AIError = _error()
    label, base = PROVIDERS[provider][0], PROVIDERS[provider][1]
    key, model = other_ai_key(provider), other_ai_model(provider)
    if not key:
        raise AIError(f"{label} isn't connected yet (Platform settings → AI).")
    schema = gemini.inline_schema(output_model.model_json_schema())
    tries = [
        {"model": model, "messages": _messages(system, content),
         "response_format": {"type": "json_schema",
                             "json_schema": {"name": output_model.__name__, "schema": schema, "strict": False}}},
        {"model": model, "messages": _messages(system, content, schema), "response_format": {"type": "json_object"}},
    ]
    from .ai import TIME_BUDGET
    end = time.monotonic() + TIME_BUDGET

    def send(body):  # never past the time budget
        left = end - time.monotonic()
        if left < 2:
            raise AIError(f"{label} took too long to answer.")
        return _post(base, key, body, timeout=(5, left))

    for attempt in range(2):  # an empty or unreadable answer gets one more try, time allowing
        response, body = _ask(send, tries, end, label, AIError)
        text = _text_of(response, label, AIError)
        out = _read(text, output_model)
        if out is not None:
            break
        log.warning("%s answer didn't fit %s (try %s): %r", label, output_model.__name__, attempt + 1, text[:300])
        tries = [body]
        if end - time.monotonic() < 8:
            break
    if out is None:
        raise AIError(f"{label}'s answer could not be read. Please try again.")
    data = response.json()
    usage = (data.get("usage") if isinstance(data, dict) else None) or {}
    log.info("%s usage: in=%s out=%s", label, usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return out


def _ask(send, tries, end, label, AIError):
    """Send the request (JSON schema first, plain JSON mode if the service refuses schemas).
    Returns (the response, the body that got it); raises AIError for errors a person should see."""
    try:
        for i, body in enumerate(tries):
            response = send(body)
            if response.status_code in (500, 502, 503) and end - time.monotonic() > BUSY_WAIT + 5:
                time.sleep(BUSY_WAIT)  # busy for a moment: once more
                response = send(body)
            # a service that doesn't take a JSON schema gets plain JSON mode
            if response.status_code in (400, 422) and i == 0 and _about_format(response):
                continue
            break
    except requests.Timeout as exc:
        raise AIError(f"{label} took too long to answer.") from exc
    except requests.RequestException as exc:
        raise AIError(f"Could not reach {label} (network).") from exc
    if response.status_code in (401, 403):
        raise AIError(f"{label} didn't accept the key. Check it in Platform settings → AI.")
    if response.status_code == 402:
        raise AIError(f"The {label} account has no credit left.")
    if response.status_code == 429:
        raise AIError(f"{label}'s limit is reached for the moment.")
    if response.status_code >= 500:
        raise AIError(f"{label} is very busy right now.")
    if response.status_code >= 400:
        log.warning("%s error %s: %s", label, response.status_code, response.text[:300])
        raise AIError(f"{label} API error ({response.status_code}).")
    return response, body


def _text_of(response, label, AIError):
    """The answer's text ("" when the service sent none)."""
    try:
        data = response.json()
    except ValueError as exc:
        raise AIError(f"{label}'s answer could not be read. Please try again.") from exc
    choices = (data.get("choices") if isinstance(data, dict) else None) or []
    if not choices:
        raise AIError(f"{label} declined this request.")
    message = (choices[0] if isinstance(choices[0], dict) else {}).get("message")
    text = (message if isinstance(message, dict) else {}).get("content") or ""
    text = text.strip() if isinstance(text, str) else ""
    if text.startswith("```"):  # some services wrap JSON in a code block
        text = text.strip("`").removeprefix("json").strip()
    return text


def _read(text, output_model):
    """The answer as `output_model`, or None. A part that is optional and doesn't fit (say, a half-filled
    action on a plain question) is left out rather than losing the whole answer; required parts must fit."""
    if not text:
        return None
    try:
        return output_model.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(exc, ValidationError):
            return None
        optional = {name for name, field in output_model.model_fields.items() if not field.is_required()}
        broken = {e["loc"][0] for e in exc.errors() if e.get("loc")}
        if not broken or not broken <= optional:
            return None
        try:
            return output_model.model_validate({k: v for k, v in data.items() if k not in broken})
        except ValidationError:
            return None


def _about_format(response):
    words = response.text.lower()
    return any(w in words for w in ("response_format", "json_schema", "json schema", "not supported"))


def best_model(provider, key, wanted=""):
    """Check the key and pick a model: the one typed (if the service has it), else the preferred one.
    Returns (model, error sentence)."""
    label, base, _, _, preferred = PROVIDERS[provider]
    try:
        response = requests.get(f"{base}/models", timeout=20, headers={"Authorization": f"Bearer {key}"})
    except requests.RequestException:
        return None, f"Couldn't reach {label} just now. Try again in a minute."
    if response.status_code in (401, 403):
        return None, f"{label} didn't accept that key. Copy it again from {PROVIDERS[provider][2]}."
    if response.status_code >= 400:
        return None, f"{label} answered with an error ({response.status_code}). Try again in a minute."
    try:
        available = [m.get("id", "") for m in response.json().get("data", [])]
    except ValueError:
        available = []
    if wanted:
        if wanted in available or not available:
            return wanted, ""
        return None, f"{label} doesn't offer a model called {wanted}. Leave the model empty to use the recommended one."
    for model in preferred:
        if model in available:
            return model, ""
    if available:
        return sorted(available)[0], ""
    return preferred[0], ""


def check(provider, key, model):
    """One tiny real request, so a wrong model or an empty account shows up now. Returns an error or ""."""
    label, base = PROVIDERS[provider][0], PROVIDERS[provider][1]
    try:
        response = _post(base, key, {"model": model, "max_tokens": 5,
                                     "messages": [{"role": "user", "content": "Reply with OK."}]})
    except requests.RequestException:
        return f"Couldn't reach {label} just now. Try again in a minute."
    if response.status_code in (429, 500, 502, 503):
        return ""  # the key works; the service is only busy right now
    if response.status_code == 402:
        return f"The key works, but the {label} account has no credit. Add a few dollars of credit first."
    if response.status_code >= 400:
        try:
            said = str((response.json().get("error") or {}).get("message", ""))[:200]
        except (ValueError, AttributeError):
            said = ""
        return f"{label} answered with an error ({response.status_code}){': ' + said if said else ''}."
    return ""
