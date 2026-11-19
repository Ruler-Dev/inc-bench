"""
L'AGENT LOOP - il cuore di ogni harness (Claude Code, Codex, Cline... sono questo
ciclo + molti accessori).

    messages = [system, user]
    ripeti:
        risposta = LLM(messages, tools)
        se risposta non contiene tool_calls  -> FINE, e' la risposta finale
        per ogni tool_call:
            risultato = esegui_tool(tool_call)       # codice Python deterministico
            messages += [{"role": "tool", ...}]
    (con un limite di passi, altrimenti un modello confuso gira all'infinito)

SUB-AGENTE = lo stesso loop, avviato da un tool, con un contesto NUOVO e vuoto
e (di solito) meno tool. Al padre torna solo il testo finale, non tutta la
conversazione: e' cosi' che si tiene pulito il contesto dell'agente principale.
"""
from __future__ import annotations

import json

from .tools import Tool, ToolRegistry

C = {"dim": "\033[2m", "cyan": "\033[36m", "yel": "\033[33m", "grn": "\033[32m",
     "red": "\033[31m", "mag": "\033[35m", "end": "\033[0m"}


class Tracer:
    """Riceve gli EVENTI dell'agente (dict strutturati). Questa versione li stampa
    nel terminale; la web UI usa una sottoclasse che li manda al browser."""

    def __init__(self, raw: bool = False):
        self.raw = raw

    def event(self, kind: str, depth: int, **data) -> None:
        a = data.get("agent", "")
        if kind == "task":
            self.log(depth, "mag", f"[{a}] task:", data["text"])
        elif kind == "llm":
            if self.raw:
                self.log(depth, "dim", f"--- risposta grezza del server (passo {data['step']}) ---",
                         json.dumps(data["response"], ensure_ascii=False, indent=2)[:4000])
            if data.get("finish_reason") == "length":
                self.log(depth, "red", f"[{a}] ATTENZIONE: risposta troncata da max_tokens")
            if data["message"].get("content"):
                self.log(depth, "cyan", f"[{a}] modello:", data["message"]["content"])
        elif kind == "tool_call":
            self.log(depth, "yel", f"[{a}] tool_call -> {data['name']}", data["arguments"])
        elif kind == "tool_result":
            self.log(depth, "red" if data["is_error"] else "dim", "  risultato:", data["result"][:1500])
        elif kind == "final":
            self.log(depth, "grn", f"[{a}] FINE dopo {data['steps']} passi")
        elif kind == "stop":
            self.log(depth, "red", f"[{a}] STOP: raggiunto max_steps={data['max_steps']}")
        elif kind == "info":
            self.log(depth, "dim", data["label"], data["text"])

    def log(self, depth: int, color: str, label: str, text: str = "") -> None:
        pad = "    " * depth
        body = ("\n" + pad + "  ").join(str(text).splitlines()) if text else ""
        print(f"{pad}{C[color]}{label}{C['end']} {body}")


class Agent:
    def __init__(self, llm, name: str, system: str, tools: ToolRegistry,
                 max_steps: int = 10, tracer: Tracer | None = None, depth: int = 0):
        self.llm, self.name, self.system = llm, name, system
        self.tools, self.max_steps = tools, max_steps
        self.tracer, self.depth = tracer or Tracer(), depth

    def run(self, task: str) -> str:
        emit = lambda kind, **kw: self.tracer.event(kind, self.depth, agent=self.name, **kw)
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": task}]
        emit("task", text=task, system=self.system, tools=self.tools.specs())
        for step in range(1, self.max_steps + 1):
            msg = self.llm.chat(messages, tools=self.tools.specs())
            emit("llm", step=step, message=msg, response=self.llm.last_response,
                 finish_reason=getattr(self.llm, "last_finish", None))
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                emit("final", steps=step, text=msg.get("content") or "")
                return msg.get("content") or ""
            for call in calls:
                f = call["function"]
                emit("tool_call", id=call["id"], name=f["name"], arguments=f.get("arguments"))
                result, is_err = self.tools.execute(call)
                emit("tool_result", id=call["id"], name=f["name"], result=result, is_error=is_err)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": result})
        emit("stop", max_steps=self.max_steps)
        return "[interrotto: troppi passi]"


def subagent_tool(llm, tools: ToolRegistry, tracer: Tracer, parent_depth: int = 0,
                  system: str = "Sei un sub-agente. Esegui SOLO il compito ricevuto "
                                "e rispondi in modo conciso.") -> Tool:
    """Crea il tool 'delega': ogni chiamata avvia un Agent nuovo (contesto vuoto)."""

    def delega(compito: str) -> str:
        sub = Agent(llm, "sub-agente", system, tools, max_steps=6,
                    tracer=tracer, depth=parent_depth + 1)
        return sub.run(compito)

    return Tool(
        name="delega",
        description="Affida un sotto-compito autonomo a un sub-agente con contesto "
                    "pulito. Ricevi indietro solo il suo risultato finale.",
        parameters={"type": "object", "properties": {
            "compito": {"type": "string", "description": "Istruzioni complete"}},
            "required": ["compito"], "additionalProperties": False},
        fn=delega)
