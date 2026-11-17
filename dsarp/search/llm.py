"""Language-model clients.

The pipeline never depends on a particular provider. A client is any object
with ``complete(system, prompt) -> str``. Built in:

* ``none``: no model; the search uses learned affinity only and explanations
  are generated from templates.
* ``openai``: any server with the OpenAI chat-completions API, e.g. **vLLM**
  serving an open model on a Noctua 2 GPU node
  (``vllm serve <model> --port 8000``), or a hosted API. Configured with
  ``DSARP_LLM_URL`` (e.g. ``http://localhost:8000/v1``, or the University of
  Paderborn AI gateway ``https://ai-gateway.uni-paderborn.de/v1``),
  ``DSARP_LLM_MODEL`` and ``DSARP_LLM_KEY`` (read from the environment only,
  never written to a file). ``dsarp llm-models`` lists the models of the endpoint.

Every answer is treated as a suggestion: operations proposed by the model are
validated against the candidate operations, and explanations are checked so
that they only name classes and packages that are part of the plan.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    pass


@dataclass
class NoLLM:
    name: str = "none"

    def complete(self, system: str, prompt: str) -> str:
        raise LLMError("no language model configured")


@dataclass
class OpenAICompatible:
    url: str
    model: str
    key: str = ""
    temperature: float = 0.2
    max_tokens: int = 1500
    timeout: float = 120.0
    name: str = "openai"
    calls: int = field(default=0)

    def _body(self, system: str, prompt: str) -> bytes:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if getattr(self, "_newer_api", False):
            # newer models (reasoning models): no temperature, max_completion_tokens instead of max_tokens,
            # with room for the tokens the model spends on reasoning before it answers
            return json.dumps({"model": self.model, "max_completion_tokens": max(self.max_tokens, 1500) * 4,
                               "messages": msgs}).encode()
        return json.dumps({"model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens,
                           "messages": msgs}).encode()

    def complete(self, system: str, prompt: str) -> str:
        data = None
        switched = False
        attempt = 0
        while True:
            req = urllib.request.Request(self.url.rstrip("/") + "/chat/completions", data=self._body(system, prompt),
                                         headers={"Content-Type": "application/json",
                                                  **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                detail = _error_text(e)
                if e.code == 400 and not switched and not getattr(self, "_newer_api", False) and re.search(
                        r"temperature|max_tokens|max_completion_tokens|unsupported|not supported", detail, re.I):
                    self._newer_api, switched = True, True       # remembered for all further requests
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    attempt += 1
                    time.sleep(5 * attempt)                       # rate limit or busy server: wait and retry
                    continue
                raise LLMError(f"language model request failed: HTTP {e.code} {e.reason}"
                               + (f": {detail}" if detail else "")) from e
            except Exception as e:  # network or JSON errors
                raise LLMError(f"language model request failed: {e}") from e
        self.calls += 1
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected answer: {str(data)[:200]}") from e
        if not text:
            raise LLMError("empty answer (the model used its whole token budget; try another model)")
        return text


def _error_text(e) -> str:
    """The error message an OpenAI-compatible server sends with an HTTP error (shortened)."""
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        d = json.loads(raw)
        msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d.get("error") or d.get("message")
        return str(msg or raw)[:300]
    except ValueError:
        return raw.strip()[:300]


@dataclass
class Scripted:
    """Returns fixed answers in order (tests and offline experiments)."""
    answers: list[str]
    name: str = "scripted"
    calls: int = 0

    def complete(self, system: str, prompt: str) -> str:
        self.calls += 1
        if not self.answers:
            raise LLMError("no more scripted answers")
        return self.answers.pop(0)


def list_models(url: str | None = None, key: str | None = None) -> list[str]:
    """Model ids offered by an OpenAI-compatible endpoint (``GET /models``)."""
    url = url or os.environ.get("DSARP_LLM_URL")
    key = key if key is not None else os.environ.get("DSARP_LLM_KEY", "")
    if not url:
        raise LLMError("set DSARP_LLM_URL")
    req = urllib.request.Request(url.rstrip("/") + "/models",
                                 headers={**({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise LLMError(f"listing models failed: {e}") from e
    return sorted(m.get("id", "") for m in data.get("data", []) if isinstance(m, dict))


def from_env(kind: str | None = None, temperature: float | None = None):
    kind = kind or os.environ.get("DSARP_LLM", "none")
    if kind == "none":
        return NoLLM()
    if kind == "openai":
        url = os.environ.get("DSARP_LLM_URL")
        model = os.environ.get("DSARP_LLM_MODEL")
        if not url or not model:
            raise LLMError("set DSARP_LLM_URL and DSARP_LLM_MODEL for the openai client")
        c = OpenAICompatible(url, model, os.environ.get("DSARP_LLM_KEY", ""))
        if temperature is not None:
            c.temperature = temperature
        return c
    raise LLMError(f"unknown language model client {kind!r} (use none or openai)")


def extract_json(text: str):
    """First JSON object or array in a model answer (code fences allowed)."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    pairs = sorted((("[", "]"), ("{", "}")), key=lambda p: text.find(p[0]) if p[0] in text else len(text))
    for opener, closer in pairs:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("no JSON in the answer")
