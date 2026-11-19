"""
SKILL = cartella con istruzioni (SKILL.md) + script deterministici.

    skills/fatturazione/
        SKILL.md            <- frontmatter (name, description) + istruzioni
        scripts/calcola_fattura.py

"Progressive disclosure": nel system prompt va SOLO nome + descrizione di ogni
skill (poche righe). Il modello carica il corpo completo con `carica_skill`
quando gli serve, ed esegue gli script con `esegui_script`. Cosi' il contesto
resta piccolo anche con decine di skill.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .tools import Tool

NAME_OK = re.compile(r"^[a-z0-9_\-]+$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path


def load_skills(root: Path) -> dict[str, Skill]:
    skills = {}
    for md in sorted(root.glob("*/SKILL.md")):
        text = md.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M)) if m else {}
        s = Skill(meta.get("name", md.parent.name), meta.get("description", ""),
                  m.group(2) if m else text, md.parent)
        skills[s.name] = s
    return skills


def skills_index(skills: dict[str, Skill]) -> str:
    """Il pezzetto che va nel system prompt."""
    lines = [f"- {s.name}: {s.description}" for s in skills.values()]
    return "Skill disponibili (usa carica_skill per leggerne le istruzioni):\n" + "\n".join(lines)


def skill_tools(skills: dict[str, Skill]) -> list[Tool]:
    def carica_skill(nome: str) -> str:
        if nome not in skills:
            return f"ERRORE: skill '{nome}' inesistente. Disponibili: {list(skills)}"
        s = skills[nome]
        scripts = [p.stem for p in (s.path / "scripts").glob("*.py")]
        return f"{s.body}\n\nScript disponibili: {scripts}"

    def esegui_script(skill: str, script: str, input: dict) -> str:
        # Sicurezza: niente path arbitrari, solo script dentro la cartella della skill
        if skill not in skills or not NAME_OK.match(script):
            return "ERRORE: skill o script non validi"
        path = skills[skill].path / "scripts" / f"{script}.py"
        if not path.is_file():
            return f"ERRORE: script '{script}' non trovato nella skill '{skill}'"
        r = subprocess.run([sys.executable, "-I", str(path)], input=json.dumps(input),
                           capture_output=True, text=True, timeout=60,
                           cwd=str(skills[skill].path.parent.parent))
        return r.stdout if r.returncode == 0 else f"ERRORE script (exit {r.returncode}):\n{r.stderr}"

    return [
        Tool("carica_skill", "Legge le istruzioni complete di una skill.",
             {"type": "object", "properties": {"nome": {"type": "string"}},
              "required": ["nome"], "additionalProperties": False}, carica_skill),
        Tool("esegui_script",
             "Esegue uno script Python deterministico di una skill. "
             "'input' e' un oggetto JSON passato allo script su stdin.",
             {"type": "object", "properties": {
                 "skill": {"type": "string"}, "script": {"type": "string"},
                 "input": {"type": "object"}},
              "required": ["skill", "script", "input"], "additionalProperties": False},
             esegui_script),
    ]
