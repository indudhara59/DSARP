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
    timeout: float = 300.0
    name: str = "openai"
    calls: int = field(default=0)

    reasoning: str = ""                 # reasoning effort for reasoning models: low, medium, high ("" = server default)
    max_budget: int = 32000             # largest completion budget tried after an answer was cut off

    def _body(self, system: str, prompt: str, budget: int | None = None, effort: str = "") -> bytes:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if getattr(self, "_newer_api", False):
            # newer models (reasoning models): no temperature, max_completion_tokens instead of max_tokens,
            # with room for the tokens the model spends on reasoning before it answers
            body = {"model": self.model, "max_completion_tokens": budget or max(self.max_tokens, 1500) * 4,
                    "messages": msgs}
            effort = effort or self.reasoning
            if effort and not getattr(self, "_no_effort", False):
                body["reasoning_effort"] = effort
            return json.dumps(body).encode()
        return json.dumps({"model": self.model, "temperature": self.temperature,
                           "max_tokens": budget or self.max_tokens, "messages": msgs}).encode()

    def _post(self, body: bytes) -> dict:
        switched = False
        attempt = 0
        while True:
            req = urllib.request.Request(self.url.rstrip("/") + "/chat/completions", data=body,
                                         headers={"Content-Type": "application/json",
                                                  **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = _error_text(e)
                if e.code == 400 and "reasoning_effort" in body.decode("utf-8", "replace") and re.search(
                        r"reasoning", detail, re.I) and not getattr(self, "_no_effort", False):
                    self._no_effort = True                       # the model takes no reasoning effort
                    body = json.dumps({k: v for k, v in json.loads(body).items() if k != "reasoning_effort"}).encode()
                    continue
                if e.code == 400 and not switched and not getattr(self, "_newer_api", False) and re.search(
                        r"temperature|max_tokens|max_completion_tokens|unsupported|not supported", detail, re.I):
                    self._newer_api, switched = True, True       # remembered for all further requests
                    raise _Switch() from e
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    attempt += 1
                    time.sleep(5 * attempt)                       # rate limit or busy server: wait and retry
                    continue
                raise LLMError(f"language model request failed: HTTP {e.code} {e.reason}"
                               + (f": {detail}" if detail else "")) from e
            except Exception as e:  # network or JSON errors
                raise LLMError(f"language model request failed: {e}") from e

    def complete(self, system: str, prompt: str) -> str:
        budget, effort, used, finish = None, "", 0, None
        for _ in range(4):
            try:
                data = self._post(self._body(system, prompt, budget, effort))
            except _Switch:
                continue                                          # retry in the newer API format
            self.calls += 1
            try:
                choice = data["choices"][0]
                text = choice["message"].get("content") or ""
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                raise LLMError(f"unexpected answer: {str(data)[:200]}") from e
            text = _THINK.sub("", text).strip()                   # open models that write their reasoning inline
            if text:
                return text
            used = budget or (max(self.max_tokens, 1500) * 4 if getattr(self, "_newer_api", False) else self.max_tokens)
            finish = choice.get("finish_reason")
            if finish != "length" or used >= self.max_budget:
                break
            # the answer was cut off (usually by the model's reasoning): more room and less reasoning
            budget, effort = min(used * 4, self.max_budget), "low"
        if finish != "length":
            raise LLMError(f"empty answer from {self.model} (finish reason {finish!r})")
        raise LLMError(f"empty answer: the model {self.model} used its whole token budget ({used} tokens) "
                       "before answering; set DSARP_LLM_MAX_TOKENS higher, DSARP_LLM_REASONING=low, "
                       "or use another model")


class _Switch(Exception):
    """Internal: retry the request in the newer API format."""


_THINK = re.compile(r"<think>.*?</think>", re.S)


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
        if os.environ.get("DSARP_LLM_MAX_TOKENS"):
            c.max_tokens = int(os.environ["DSARP_LLM_MAX_TOKENS"])
            c.max_budget = max(c.max_budget, c.max_tokens)
        c.reasoning = os.environ.get("DSARP_LLM_REASONING", "").strip().lower()
        if os.environ.get("DSARP_LLM_TIMEOUT"):
            c.timeout = float(os.environ["DSARP_LLM_TIMEOUT"])
        if os.environ.get("DSARP_LLM_NEW_API", "").lower() in ("1", "true", "yes"):
            c._newer_api = True
        return c
    raise LLMError(f"unknown language model client {kind!r} (use none or openai)")


def parallel(fn, items: list, workers: int | None = None):
    """``fn`` applied to every item, ``workers`` at a time (default ``DSARP_LLM_WORKERS``, else 1), in order."""
    if workers is None:
        workers = int(os.environ.get("DSARP_LLM_WORKERS", "1") or 1)
    if workers <= 1 or len(items) <= 1:
        for x in items:
            yield fn(x)
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        yield from ex.map(fn, items)


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
