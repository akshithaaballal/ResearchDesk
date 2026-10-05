"""Single Gemini pipeline. Both agents import generate_research() from here.

generate_research() either returns a clean, fully-populated dict or raises, so
callers never have to deal with half-empty results:

    {"title": str, "overview": str, "key_concepts": [str], "key_findings": [str],
     "exploration_questions": [str]}
"""
import json
import os
import re
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
# Tried (once each) if the primary model is unavailable / overloaded.
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-flash-latest"
    ).split(",")
    if m.strip()
]
HTTP_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "60")) * 1000
RETRYABLE_CODES = {429, 500, 502, 503, 504}

_client = None


def _get_client():
    global _client
    if _client is None:
        key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set (check backend/.env).")
        _client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_MS),
        )
    return _client


PROMPT = """You are a research assistant.

Analyze the following research topic:

{topic}

Return ONLY valid JSON in this exact structure (every value must be plain text;
the lists must be lists of plain strings, not objects):

{{
    "title": "A suitable research title",
    "overview": "A clear, informative overview of 3-5 sentences",
    "key_concepts": ["4-6 key concepts, each as 'Name: one-sentence explanation'"],
    "key_findings": ["4-6 key findings, each one clear sentence"],
    "exploration_questions": ["3-4 good follow-up questions worth exploring"]
}}
"""


# --------------------------------------------------------------------------
# Parsing / normalising model output
# --------------------------------------------------------------------------
def _parse_json(text: str):
    text = (text or "").strip()
    # Gemini sometimes wraps JSON in ```json fences.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: pull out the outermost {...} if there is chatter around it.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


_NAME_KEYS = ("concept", "name", "title", "finding", "question", "term", "heading")
_DESC_KEYS = ("description", "explanation", "detail", "details", "summary",
              "text", "content", "answer")


def _first_str(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _to_text(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, (int, float, bool)):
        return str(x)
    if isinstance(x, dict):
        name, desc = _first_str(x, _NAME_KEYS), _first_str(x, _DESC_KEYS)
        if name and desc:
            return f"{name}: {desc}"
        if name or desc:
            return name or desc
        return "; ".join(t for t in (_to_text(v) for v in x.values()) if t)
    if isinstance(x, (list, tuple)):
        return "; ".join(t for t in (_to_text(v) for v in x) if t)
    return str(x)


def _to_list(x) -> list:
    if x is None:
        return []
    if isinstance(x, str):
        parts = [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", p).strip()
                 for p in x.splitlines()]
        return [p for p in parts if p]
    if isinstance(x, dict):
        if any(k in x for k in _NAME_KEYS + _DESC_KEYS):
            t = _to_text(x)
            return [t] if t else []
        return [t for t in (_to_text(v) for v in x.values()) if t]
    if isinstance(x, (list, tuple)):
        return [t for t in (_to_text(v) for v in x) if t]
    return [str(x)]


def normalize_result(raw, topic: str = "") -> dict:
    """Coerce model output into the flat all-strings shape the React app renders.
    Raises ValueError when there is no usable content (so the UI never shows an
    empty card)."""
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        raw = raw[0]
    if not isinstance(raw, dict):
        raise ValueError("Model output was not a JSON object")
    # e.g. {"research": {...}}
    if "overview" not in raw and len(raw) == 1:
        only = next(iter(raw.values()))
        if isinstance(only, dict):
            raw = only

    result = {
        "title": _to_text(raw.get("title")) or (topic or "").strip()[:120] or "Research Result",
        "overview": _to_text(raw.get("overview") or raw.get("summary")),
        "key_concepts": _to_list(raw.get("key_concepts")),
        "key_findings": _to_list(raw.get("key_findings")),
        "exploration_questions": _to_list(raw.get("exploration_questions")),
    }
    if not (result["overview"] or result["key_concepts"] or result["key_findings"]):
        raise ValueError("Model returned no usable research content")
    return result


def _response_text(resp) -> str:
    text = getattr(resp, "text", None)
    if text and text.strip():
        return text
    # .text can be empty for some candidates; collect non-thought text parts.
    chunks = []
    for cand in (getattr(resp, "candidates", None) or []):
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            if getattr(part, "text", None) and not getattr(part, "thought", False):
                chunks.append(part.text)
    text = "".join(chunks).strip()
    if not text:
        raise ValueError("Gemini returned an empty response")
    return text


def _describe(err) -> str:
    msg = str(err) if err else "unknown error"
    return msg if len(msg) <= 300 else msg[:300] + "..."


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def generate_research(topic: str) -> dict:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Empty research topic")

    client = _get_client()                      # raises clearly if no API key
    prompt = PROMPT.format(topic=topic)
    config = types.GenerateContentConfig(response_mime_type="application/json")
    models = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]

    last_err = None
    for model in models:
        attempts = 3 if model == MODEL else 2
        for attempt in range(attempts):
            more = attempt + 1 < attempts
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config)
                return normalize_result(_parse_json(_response_text(resp)), topic)
            except genai_errors.APIError as e:
                last_err = e
                print(f"[gemini] {model} -> API error {getattr(e, 'code', '?')}: {_describe(e)}")
                if getattr(e, "code", None) in RETRYABLE_CODES and more:
                    time.sleep(2)
                    continue
                break                            # try the next model
            except ValueError as e:              # bad / empty JSON (JSONDecodeError too)
                last_err = e
                print(f"[gemini] {model} -> unusable output: {_describe(e)}")
                if more:
                    continue
                break
            except Exception as e:               # network etc.
                last_err = e
                print(f"[gemini] {model} -> {type(e).__name__}: {_describe(e)}")
                if more:
                    time.sleep(2)
                    continue
                break

    raise RuntimeError(f"Gemini request failed: {_describe(last_err)}")
