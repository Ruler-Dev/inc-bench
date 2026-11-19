"""
Tool = funzione Python + descrizione + JSON Schema dei parametri.

Il modello NON esegue niente: vede solo nome, descrizione e schema, e "chiede"
di chiamare un tool scrivendo JSON. E' l'harness che:
  1. fa il parse degli argomenti (possono essere JSON rotto)
  2. li valida contro lo schema (possono avere campi inventati / tipi sbagliati)
  3. esegue la funzione VERA (deterministica)
  4. rimanda il risultato al modello come messaggio role="tool"
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .schema import validate


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema degli argomenti
    fn: Callable[..., object]

    def spec(self) -> dict:
        """Formato inviato al server (OpenAI-compatible 'tools')."""
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self.tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def specs(self) -> list[dict]:
        return [t.spec() for t in self.tools.values()]

    def execute(self, call: dict) -> tuple[str, bool]:
        """Esegue una tool call. Ritorna (testo_risultato, is_error).
        Gli errori NON fanno crashare l'agente: tornano al modello come testo,
        cosi' puo' correggersi al giro successivo."""
        name = call["function"]["name"]
        raw = call["function"].get("arguments") or "{}"
        if name not in self.tools:
            return f"ERRORE: il tool '{name}' non esiste. Tool disponibili: {list(self.tools)}", True
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            return f"ERRORE: argomenti non sono JSON valido ({e}): {raw!r}", True
        errors = validate(args, self.tools[name].parameters)
        if errors:
            return "ERRORE di validazione argomenti:\n- " + "\n- ".join(errors), True
        try:
            result = self.tools[name].fn(**args)
        except Exception as e:  # il tool stesso ha fallito
            return f"ERRORE durante l'esecuzione: {type(e).__name__}: {e}", True
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False), False
