"""
Backend LLM.

Un harness non "contiene" il modello: gli parla via HTTP. Quasi tutti i server
locali (LM Studio, Ollama, llama.cpp, vLLM, SGLang) espongono la stessa API
"OpenAI-compatibile":  POST {base_url}/chat/completions

Richiesta (semplificata):
    {
      "model": "...",
      "messages": [ {"role": "system"|"user"|"assistant"|"tool", ...}, ... ],
      "tools": [ {"type": "function", "function": {name, description, parameters}} ],
      "response_format": {...}          # opzionale: forza uno schema JSON
    }

Risposta: choices[0].message =
    {"role": "assistant", "content": "...", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "somma", "arguments": "{\"a\": 1, \"b\": 2}"}}  # <- STRINGA JSON
    ]}

Nota: "arguments" e' una STRINGA generata dal modello. Puo' essere JSON rotto.
L'harness deve sempre fare json.loads + validazione.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

# Alcuni modelli (Qwen, Hermes...) scrivono le tool call come testo nel loro
# chat template; il server di solito le converte in "tool_calls", ma non sempre.
TEXT_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
THINK = re.compile(r"<think>.*?</think>", re.S)


def _normalize(msg: dict) -> dict:
    """Pulisce la risposta: toglie il ragionamento <think> e recupera eventuali
    tool call scritte come testo invece che nel campo strutturato."""
    content = THINK.sub("", msg.get("content") or "").strip()
    calls = msg.get("tool_calls") or []
    if not calls:
        for m in TEXT_TOOL_CALL.finditer(content):
            try:
                obj = json.loads(m.group(1))
                calls.append({
                    "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": obj.get("name"),
                                 "arguments": json.dumps(obj.get("arguments", {}))},
                })
            except json.JSONDecodeError:
                pass  # lasciamo il testo com'e': l'harness vedra' "nessuna tool call"
        content = TEXT_TOOL_CALL.sub("", content).strip()
    out = {"role": "assistant", "content": content or None}
    if calls:
        out["tool_calls"] = calls
    return out


class Cancelled(RuntimeError):
    """Run fermato dall'utente (pulsante Stop)."""


def _check(llm) -> None:
    ev = getattr(llm, "cancel", None)
    if ev is not None and ev.is_set():
        raise Cancelled("Run fermato dall'utente")


def _cancellable(llm, fn):
    """Esegue fn() (la richiesta HTTP) ma smette di aspettarla se l'utente preme Stop.
    La richiesta abbandonata finisce da sola sul server; il run invece si ferma subito."""
    ev = getattr(llm, "cancel", None)
    if ev is None:
        return fn()
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 - la rilanciamo nel thread chiamante
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)
        if ev.is_set() and t.is_alive():
            raise Cancelled("Run fermato dall'utente (chiamata in corso abbandonata)")
    if "e" in box:
        raise box["e"]
    return box["v"]


class LocalLLM:
    """Client minimale (solo libreria standard) per server OpenAI-compatibili."""

    def __init__(self, model: str, base_url: str = "http://localhost:1234/v1",
                 temperature: float = 0.2, timeout: int = 900, max_tokens: int | None = 8192,
                 api_key: str | None = None):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.api_key = api_key  # solo per provider che la richiedono (Authorization: Bearer)
        self.temperature, self.timeout = temperature, timeout
        # max_tokens: SEMPRE impostarlo. Un modello con "reasoning" puo' entrare in un
        # loop di ragionamento e generare decine di migliaia di token (visto davvero:
        # 28k token per scrivere una email di 3 righe). L'harness deve mettere un tetto.
        self.max_tokens = max_tokens
        self.last_request: dict | None = None
        self.last_response: dict | None = None
        self.last_finish: str | None = None
        self.cancel: threading.Event | None = None  # impostato dalla web UI: pulsante Stop

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             response_format: dict | None = None) -> dict:
        _check(self)
        body = {"model": self.model, "messages": messages,
                "temperature": self.temperature}
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        self.last_request = body
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(), headers=headers)
        t0 = time.perf_counter()
        try:
            def fetch():
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            data = _cancellable(self, fetch)
        except Cancelled as e:
            _report(self, messages, response_format, tools, None, time.perf_counter() - t0, str(e))
            raise
        except urllib.error.HTTPError as e:
            err = f"Il server ha risposto {e.code}: {e.read().decode(errors='replace')[:1000]}"
            _report(self, messages, response_format, tools, None, time.perf_counter() - t0, err)
            raise RuntimeError(err) from None
        except urllib.error.URLError as e:
            err = f"Server non raggiungibile su {self.base_url} ({e.reason}). LM Studio/Ollama e' avviato?"
            _report(self, messages, response_format, tools, None, time.perf_counter() - t0, err)
            raise RuntimeError(err) from None
        except (TimeoutError, OSError) as e:  # timeout in lettura, connessione chiusa dal server...
            dt = time.perf_counter() - t0
            err = (f"Timeout: nessuna risposta dopo {dt:.0f}s (limite {self.timeout}s; aumentalo con --timeout)"
                   if isinstance(e, TimeoutError) else f"Connessione interrotta dopo {dt:.0f}s: {e}")
            _report(self, messages, response_format, tools, None, dt, err)
            raise RuntimeError(err) from None
        self.last_response = data
        # "length" = troncato da max_tokens: la risposta e' incompleta (JSON rotto, tool call persa...)
        self.last_finish = data["choices"][0].get("finish_reason")
        _report(self, messages, response_format, tools, data, time.perf_counter() - t0)
        return _normalize(data["choices"][0]["message"])


class MockLLM:
    """Finto modello per far girare le demo senza GPU.
    `policy(messages, tools, response_format) -> messaggio assistant` decide cosa
    "risponde". Utile anche per i test: un harness si testa con un modello finto."""

    def __init__(self, policy: Callable[..., dict], delay: float = 0.0):
        self.policy = policy
        self.model = "mock"
        self.delay = delay  # secondi di "latenza" finta per chiamata (per provare la UI dal vivo)
        self.last_request = self.last_response = self.last_finish = None
        self.cancel: threading.Event | None = None

    def chat(self, messages, tools=None, response_format=None) -> dict:
        _check(self)
        self.last_request = {"model": "mock", "messages": messages,
                             "tools": tools, "response_format": response_format}
        t0 = time.perf_counter()
        if self.delay:
            if self.cancel is not None:
                if self.cancel.wait(self.delay):
                    raise Cancelled("Run fermato dall'utente")
            else:
                time.sleep(self.delay)
        try:
            msg = self.policy(messages, tools, response_format)
        except Exception as e:
            _report(self, messages, response_format, tools, None, time.perf_counter() - t0, str(e))
            raise
        self.last_response = {"choices": [{"message": msg}]}
        _report(self, messages, response_format, tools, self.last_response, time.perf_counter() - t0)
        return _normalize(dict(msg))


def _cut(s, n=30000):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n\n[... troncato: altri {len(s) - n} caratteri ...]"


def _report(llm, messages, rf, tools, data, latency, error=None):
    """Se l'LLM ha un `on_call`, gli passa un record completo della chiamata:
    prompt, ragionamento, risposta, token, LATENZA e token/secondo."""
    cb = getattr(llm, "on_call", None)
    if not cb:
        return
    msg = (data or {}).get("choices", [{}])[0].get("message", {}) if data else {}
    usage = (data or {}).get("usage") or {}
    comp = usage.get("completion_tokens") or 0
    cb({"type": "llm_call", "model": llm.model,
        "name": ((rf or {}).get("json_schema") or {}).get("name") or ("tools" if tools else "chat"),
        "schema": ((rf or {}).get("json_schema") or {}).get("schema"),
        "tools": [t["function"]["name"] for t in tools or []],
        "system": next((m["content"] for m in messages if m["role"] == "system"), None),
        "messages": [{"role": m["role"], "content": _cut(m.get("content") or "", 8000)} for m in messages[-6:]
                     if m["role"] != "system"][:-1],
        "prompt": _cut(next((m.get("content") or "" for m in reversed(messages) if m["role"] == "user"), "")),
        "response": msg.get("content"), "reasoning": _cut(msg.get("reasoning_content") or "", 20000),
        "tool_calls": msg.get("tool_calls"), "finish_reason": (data or {}).get("choices", [{}])[0].get("finish_reason"),
        "latency": round(latency, 3), "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": comp,
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "tok_s": round(comp / latency, 1) if comp and latency > 0 else None, "error": error,
        "ts": time.strftime("%H:%M:%S")})


def tool_call(name: str, **arguments) -> dict:
    """Helper per i mock: costruisce un messaggio assistant con una tool call."""
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}
