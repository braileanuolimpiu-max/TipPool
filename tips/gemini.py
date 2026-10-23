"""Google's Gemini API, as a free alternative to Claude: the same structured answers (rules, explanations,
checks, Ask), through Gemini's REST API with a JSON schema.

Gemini's free tier has daily limits; when they're used up the call fails and TipPool's built-in assistant
answers instead, as it does whenever the AI can't be reached."""
import base64  # noqa: F401  (images arrive already base64-encoded)
import copy
import json
import logging
import time

import requests
from pydantic import ValidationError

log = logging.getLogger("tips.gemini")
API = "https://generativelanguage.googleapis.com/v1beta"
BUSY_WAIT = 2  # seconds before trying a busy model once more
MAX_RETRY_WAIT = 12  # a per-minute limit that clears this soon is waited for; a daily one isn't


def _error():
    from .ai import AIError
    return AIError


def inline_schema(schema):
    """Pydantic's JSON schema with every $ref replaced by its definition (Gemini wants one plain schema)."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].split("/")[-1]]
                merged = {**walk(copy.deepcopy(target)), **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            return {k: walk(v) for k, v in node.items() if k not in ("default",)}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def _parts(content):
    parts = []
    for block in content:
        if block.get("type") == "text":
            parts.append({"text": block["text"]})
        elif block.get("type") == "image":
            source = block["source"]
            parts.append({"inline_data": {"mime_type": source["media_type"], "data": source["data"]}})
    return parts


def retry_after(response):
    """Seconds Google asks us to wait (RetryInfo "retryDelay": "7s"), or None."""
    try:
        details = response.json().get("error", {}).get("details", [])
    except ValueError:
        return None
    for d in details:
        delay = str(d.get("retryDelay", ""))
        if delay.endswith("s"):
            try:
                return max(0.0, float(delay[:-1]))
            except ValueError:
                return None
    return None


def _post(model, key, body, timeout=20):
    return requests.post(f"{API}/models/{model}:generateContent", json=body, timeout=timeout,
                         headers={"x-goog-api-key": key, "content-type": "application/json"})


def parse(system, content, output_model):
    """Ask Gemini; return a validated `output_model`. Raises AIError with a sentence a person can act on."""
    from .platform import gemini_key, gemini_model
    AIError = _error()
    key, model = gemini_key(), gemini_model()
    if not key:
        raise AIError("Gemini isn't connected yet (Platform settings → AI).")
    schema = inline_schema(output_model.model_json_schema())
    base = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": _parts(content)}]}
    # the newer request shape first; the long-standing one if this API version doesn't know it
    shapes = [{"responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": schema}}},
              {"responseMimeType": "application/json", "responseJsonSchema": schema}]
    from .ai import TIME_BUDGET
    end = time.monotonic() + TIME_BUDGET

    think = {"thinkingConfig": {"thinkingLevel": "low"}}  # short answers to clear questions: little thinking

    def send(config):  # never past the time budget
        left = end - time.monotonic()
        if left < 2:
            raise AIError("Gemini took too long to answer.")
        response = _post(model, key, {**base, "generationConfig": {**config, **think}}, timeout=(5, left))
        if response.status_code == 400 and think and "thinking" in response.text.lower():
            think.clear()  # a model without that setting: ask again without it
            return send(config)
        return response

    try:
        for i, config in enumerate(shapes):
            response = send(config)
            if response.status_code in (500, 503) and end - time.monotonic() > BUSY_WAIT + 5:
                time.sleep(BUSY_WAIT)  # busy for a moment (free tier at peak times): once more
                response = send(config)
            elif response.status_code == 429:  # the free tier's per-minute limit: wait as long as Google asks
                wait = retry_after(response)
                if wait is not None and wait <= MAX_RETRY_WAIT and end - time.monotonic() > wait + 5:
                    time.sleep(wait)
                    response = send(config)
            if response.status_code == 400 and i == 0 and "responseFormat" in response.text:
                continue
            break
    except requests.Timeout as exc:
        raise AIError("Gemini took too long to answer.") from exc
    except requests.RequestException as exc:
        raise AIError("Could not reach Gemini (network).") from exc
    if response.status_code in (500, 503):
        raise AIError("Gemini is very busy right now (Google's free tier at a peak time).")
    if response.status_code in (401, 403):
        raise AIError("Gemini didn't accept the key. Check it in Platform settings → AI.")
    if response.status_code == 429:
        raise AIError("Gemini's free limit is reached for the moment.")
    if response.status_code >= 400:
        log.warning("Gemini error %s: %s", response.status_code, response.text[:300])
        raise AIError(f"Gemini API error ({response.status_code}).")
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise AIError("Gemini declined this request.")
    text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))
    try:
        out = output_model.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        raise AIError("Gemini's answer could not be read. Please try again.") from exc
    usage = data.get("usageMetadata", {})
    log.info("Gemini usage: in=%s out=%s", usage.get("promptTokenCount"), usage.get("candidatesTokenCount"))
    return out


def best_model(key):
    """Check the key and pick the newest stable "Flash" model it can use. Returns (model, error)."""
    try:
        response = requests.get(f"{API}/models", params={"pageSize": 200}, timeout=20,
                                headers={"x-goog-api-key": key})
    except requests.RequestException:
        return None, "Couldn't reach Google just now. Try again in a minute."
    if response.status_code in (400, 401, 403):
        return None, "Google didn't accept that key. Copy it again from aistudio.google.com → Get API key."
    if response.status_code >= 400:
        return None, f"Google answered with an error ({response.status_code}). Try again in a minute."
    names = []
    for m in response.json().get("models", []):
        name = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" in name and not any(x in name for x in ("lite", "image", "tts", "live", "audio", "thinking",
                                                               "exp", "preview", "8b")):
            names.append(name)
    if "gemini-flash-latest" in names:
        return "gemini-flash-latest", ""

    def version(n):
        digits = [int(p) if p.isdigit() else 0 for p in n.replace("gemini-", "").split("-")[0].split(".")]
        return digits
    names.sort(key=version, reverse=True)
    if not names:
        return None, "That key works, but Google offers it no Flash model. Pick another Google project."
    return names[0], ""


def check(key, model):
    """One tiny real request, so a wrong model or key shows up now. Returns an error sentence or ""."""
    try:
        response = _post(model, key, {"contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}]})
    except requests.RequestException:
        return "Couldn't reach Gemini just now. Try again in a minute."
    if response.status_code in (429, 500, 503):
        return ""  # the key works; Google is only busy, or today's free limit is used up
    if response.status_code >= 400:
        try:
            said = json.loads(response.text).get("error", {}).get("message", "")[:200]
        except ValueError:
            said = ""
        return f"Gemini answered with an error ({response.status_code}){': ' + said if said else ''}."
    return ""
