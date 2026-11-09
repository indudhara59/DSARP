"""Language-model clients.

The pipeline never depends on a particular provider. A client is any object
with ``complete(system, prompt) -> str``. Built in:

* ``none``: no model; the search uses learned affinity only and explanations
  are generated from templates.
* ``openai``: any server with the OpenAI chat-completions API, e.g. **vLLM**
  serving an open model on a Noctua 2 GPU node
  (``vllm serve <model> --port 8000``), or a hosted API. Configured with
  ``DSARP_LLM_URL`` (e.g. ``http://localhost:8000/v1``), ``DSARP_LLM_MODEL``
  and optionally ``DSARP_LLM_KEY``.

Every answer is treated as a suggestion: operations proposed by the model are
validated against the candidate operations, and explanations are checked so
that they only name classes and packages that are part of the plan.
"""

from __future__ import annotations

import json
import os
import re
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

    def complete(self, system: str, prompt: str) -> str:
        body = json.dumps({"model": self.model, "temperature": self.temperature, "max_tokens": self.max_tokens,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(self.url.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.key}"} if self.key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except Exception as e:  # network, HTTP or JSON errors
            raise LLMError(f"language model request failed: {e}") from e
        self.calls += 1
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected answer: {str(data)[:200]}") from e


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


def from_env(kind: str | None = None):
    kind = kind or os.environ.get("DSARP_LLM", "none")
    if kind == "none":
        return NoLLM()
    if kind == "openai":
        url = os.environ.get("DSARP_LLM_URL")
        model = os.environ.get("DSARP_LLM_MODEL")
        if not url or not model:
            raise LLMError("set DSARP_LLM_URL and DSARP_LLM_MODEL for the openai client")
        return OpenAICompatible(url, model, os.environ.get("DSARP_LLM_KEY", ""))
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
