"""
Provider OpenAI-compatibili configurabili (LM Studio, Ollama, llama.cpp, vLLM, SGLang, servizi cloud).

La configurazione sta in `providers.json` (non versionato, vedi .gitignore). Se manca si usa
`providers.example.json`. Si modifica a mano o dalla web UI (pannello "Provider").

    {"providers": [
      {"name": "lmstudio", "base_url": "http://localhost:1234/v1", "enabled": true},
      {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1",
       "api_key_env": "OPENROUTER_API_KEY", "models": ["vendor/model-id"], "enabled": false}
    ]}

Campi:
  name         nome libero, unico
  base_url     URL dell'API OpenAI-compatibile (termina di solito con /v1)
  enabled      se false il provider viene ignorato
  api_key_env  NOME della variabile d'ambiente che contiene la chiave (mai la chiave nel file)
  models       opzionale: elenco fisso di modelli; se manca si interroga GET <base_url>/models

La chiave viene inviata solo al provider il cui base_url coincide esattamente.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).parent
FILE = ROOT / "providers.json"
EXAMPLE = ROOT / "providers.example.json"
FALLBACK = [{"name": "lmstudio", "base_url": "http://localhost:1234/v1", "enabled": True}]
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/")


def load() -> list[dict]:
    for f in (FILE, EXAMPLE):
        if f.exists():
            try:
                return validate(json.loads(f.read_text(encoding="utf-8")).get("providers", []))
            except (ValueError, json.JSONDecodeError):
                continue
    return [dict(p) for p in FALLBACK]


def validate(items: list) -> list[dict]:
    """Controlla e normalizza l'elenco. Solleva ValueError con un messaggio leggibile."""
    if not isinstance(items, list):
        raise ValueError("'providers' deve essere un elenco")
    out, nomi = [], set()
    for i, p in enumerate(items, 1):
        if not isinstance(p, dict):
            raise ValueError(f"provider {i}: deve essere un oggetto")
        name = str(p.get("name") or "").strip()
        base = _norm(p.get("base_url"))
        if not name or name in nomi:
            raise ValueError(f"provider {i}: 'name' mancante o duplicato")
        if not re.match(r"^https?://[^\s/]+", base):
            raise ValueError(f"provider '{name}': base_url deve iniziare con http:// o https://")
        if "api_key" in p:
            raise ValueError(f"provider '{name}': non scrivere la chiave nel file, usa api_key_env")
        env = str(p.get("api_key_env") or "").strip()
        if env and not ENV_NAME.match(env):
            raise ValueError(f"provider '{name}': api_key_env deve essere un nome di variabile (es. MIO_API_KEY)")
        models = p.get("models")
        if models is not None and (not isinstance(models, list) or not all(isinstance(m, str) for m in models)):
            raise ValueError(f"provider '{name}': 'models' deve essere un elenco di stringhe")
        q = {"name": name, "base_url": base, "enabled": bool(p.get("enabled", True))}
        if env:
            q["api_key_env"] = env
        if models:
            q["models"] = [m.strip() for m in models if m.strip()]
        nomi.add(name)
        out.append(q)
    return out


def save(items: list) -> list[dict]:
    items = validate(items)
    FILE.write_text(json.dumps({"providers": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return items


def enabled() -> list[dict]:
    return [p for p in load() if p["enabled"]]


def enabled_bases() -> str:
    """Gli URL dei provider attivi, separati da virgola (formato del campo in alto nella web UI)."""
    return ", ".join(p["base_url"] for p in enabled()) or FALLBACK[0]["base_url"]


def find(base_url: str) -> dict | None:
    b = _norm(base_url)
    return next((p for p in load() if p["base_url"] == b), None)


def api_key(base_url: str) -> str | None:
    p = find(base_url)
    return os.environ.get(p["api_key_env"]) if p and p.get("api_key_env") else None


def public_view() -> list[dict]:
    """Per la web UI: dice se la variabile della chiave e' impostata, senza mai mostrarne il valore."""
    return [{**p, "api_key_set": bool(p.get("api_key_env") and os.environ.get(p["api_key_env"]))} for p in load()]
