"""AI providers behind one small interface, with automatic fallback.

Order comes from AI_PROVIDERS (default "groq,gemini"); a provider is used only if its key is set.
If one provider is busy, out of quota or down, the next one is tried - so no single AI service
can stop an invoice from being read.
"""
import base64
import io
import json
import os
import time
from typing import Type

import httpx
from pydantic import BaseModel, ValidationError

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _models(env: str, defaults: list[str]) -> list[str]:
    return list(dict.fromkeys(m for m in [os.environ.get(env)] + defaults if m))


GROQ_TEXT_MODELS = _models("GROQ_TEXT_MODEL", ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"])
GROQ_VISION_MODELS = _models("GROQ_VISION_MODEL", ["qwen/qwen3.8-27b", "meta-llama/llama-4-scout-17b-16e-instruct"])
GROQ_EXPLAIN_MODELS = _models("GROQ_EXPLAIN_MODEL", ["openai/gpt-oss-20b", "openai/gpt-oss-120b",
                                                      "llama-3.3-70b-versatile"])
GEMINI_MODELS = _models("GEMINI_MODEL", ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash"])
TIMEOUT_S = int(os.environ.get("AI_TIMEOUT_S", "90"))


class AIError(Exception):
    pass


def groq_key():
    return os.environ.get("GROQ_API_KEY")


def gemini_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def providers(scanned: bool = False) -> list[str]:
    """Digital PDFs: Groq first (fast, reliable text model). Scanned PDFs: Gemini first, because it
    reads document images more accurately; Groq's vision model is the backup."""
    default = "gemini,groq" if scanned else "groq,gemini"
    env = "AI_PROVIDERS_SCANNED" if scanned else "AI_PROVIDERS"
    order = [p.strip() for p in os.environ.get(env, default).split(",") if p.strip()]
    have = {"groq": bool(groq_key()), "gemini": bool(gemini_key())}
    return [p for p in order if have.get(p)]


def render_pages(pdf: bytes, max_pages: int = 3, dpi: int = 200) -> list[str]:
    """PDF pages -> base64 JPEGs, for vision models that take images rather than PDFs."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    out = []
    for i in range(min(len(doc), max_pages)):
        img = doc[i].render(scale=dpi / 72).to_pil().convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        out.append(base64.b64encode(buf.getvalue()).decode())
    return out


# ---------------------------------------------------------------- Groq (OpenAI-compatible)
def groq_json(models: list[str], messages: list[dict], schema: Type[BaseModel],
              temperature: float = 0, timeout_s: int = TIMEOUT_S) -> tuple[BaseModel, dict]:
    errors, attempts, t0 = {}, 0, time.time()
    headers = {"Authorization": f"Bearer {groq_key()}", "Content-Type": "application/json"}
    with httpx.Client(timeout=timeout_s) as client:
        for model in models:
            msgs = list(messages)
            for attempt in range(3):
                attempts += 1
                body = {"model": model, "messages": msgs, "temperature": temperature,
                        "response_format": {"type": "json_object"}}
                if model.startswith("openai/gpt-oss"):
                    body["reasoning_effort"] = "low"
                elif model.startswith("qwen/"):
                    body["reasoning_effort"] = "none"  # skip long thinking; reading needs care, not reasoning
                try:
                    r = client.post(GROQ_URL, headers=headers, json=body)
                except httpx.HTTPError as e:
                    errors[model] = f"network: {e}"
                    continue
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"]
                    try:
                        return schema.model_validate_json(text), {
                            "provider": "groq", "model": model, "attempts": attempts,
                            "ms": int((time.time() - t0) * 1000)}
                    except ValidationError as e:
                        # tell the model exactly what was wrong and let it fix its answer once
                        errors[model] = f"invalid JSON shape: {str(e)[:200]}"
                        msgs = messages + [{"role": "assistant", "content": text},
                                           {"role": "user", "content": f"That JSON did not match the required shape: {str(e)[:800]}. Return the corrected JSON only."}]
                        continue
                err = (r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {})
                errors[model] = f"{r.status_code} {err.get('code') or ''} {err.get('message') or r.text[:200]}".strip()
                if r.status_code == 429:
                    wait = float(r.headers.get("retry-after", "0") or 0)
                    if 0 < wait <= 30 and attempt < 2:
                        time.sleep(wait + 0.5)
                        continue
                    break  # long wait or daily quota -> next model
                if r.status_code == 400 and "reasoning" in r.text.lower() and attempt < 2:
                    body.pop("reasoning_effort", None)
                    r2 = client.post(GROQ_URL, headers=headers, json=body)
                    attempts += 1
                    if r2.status_code == 200:
                        try:
                            return schema.model_validate_json(r2.json()["choices"][0]["message"]["content"]), {
                                "provider": "groq", "model": model, "attempts": attempts,
                                "ms": int((time.time() - t0) * 1000)}
                        except ValidationError as e:
                            errors[model] = f"invalid JSON shape: {str(e)[:200]}"
                    else:
                        errors[model] = f"{r2.status_code} {r2.text[:200]}"
                    break
                if r.status_code >= 500 and attempt < 1:
                    time.sleep(2)
                    continue
                break  # 4xx (model gone, bad request) or repeated 5xx -> next model
    raise AIError("Groq: " + " | ".join(f"{m}: {e}" for m, e in errors.items()))


# ---------------------------------------------------------------- Gemini
def gemini_json(contents: list, schema: Type[BaseModel], temperature: float = 0,
                timeout_s: int = TIMEOUT_S) -> tuple[BaseModel, dict]:
    from google import genai
    from google.genai import errors as gerr, types

    client = genai.Client(api_key=gemini_key(), http_options=types.HttpOptions(
        timeout=timeout_s * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
    errors, attempts, t0 = {}, 0, time.time()
    for model in GEMINI_MODELS:
        thinking = True
        for attempt in range(3):
            attempts += 1
            kw = dict(temperature=temperature, response_mime_type="application/json", response_schema=schema)
            if thinking:
                kw["thinking_config"] = types.ThinkingConfig(thinking_level="low")
            try:
                resp = client.models.generate_content(model=model, contents=contents,
                                                      config=types.GenerateContentConfig(**kw))
                parsed = resp.parsed or schema.model_validate_json(resp.text)
                return parsed, {"provider": "gemini", "model": model, "attempts": attempts,
                                "ms": int((time.time() - t0) * 1000)}
            except gerr.ClientError as e:
                errors[model] = str(e)[:250]
                code = getattr(e, "code", None)
                if code == 400 and "thinking" in str(e).lower() and thinking:
                    thinking = False
                    continue
                break  # quota / bad request: don't wait - the next model or provider takes over
            except gerr.ServerError as e:
                # busy (503) or timed out (504): hand over immediately; a backup is faster than a retry
                errors[model] = str(e)[:250]
                break
            except Exception as e:
                errors[model] = f"{type(e).__name__}: {str(e)[:200]}"
                break
    raise AIError("Gemini: " + " | ".join(f"{m}: {e}" for m, e in errors.items()))
