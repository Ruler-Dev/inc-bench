"""
Storico delle esecuzioni: ogni run viene salvato in data/runs/<id>.json con
modello, backend, parametri, riepilogo e TUTTI gli eventi (per poterlo riaprire
nella web UI esattamente com'era).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path(__file__).parent / "data" / "runs"


def model_info(backend: str, model: str, base_url) -> dict:
    """Che modello e' stato usato davvero. Per LM Studio aggiunge quantizzazione e contesto.
    Piu' modelli separati da virgola (confronto in parallelo) -> {"id": "a + b", "models": [...]}"""
    if "," in (model or ""):
        ms = [model_info(backend, m.strip(), base_url) for m in model.split(",") if m.strip()]
        return {"id": " + ".join(m["id"] for m in ms), "backend": backend, "models": ms}
    if isinstance(base_url, dict):   # modello -> server (piu' server insieme)
        base_url = base_url.get(model, "")
    if backend != "local":
        return {"id": "mock", "backend": "mock"}
    info = {"id": model, "backend": "local", "base_url": base_url}
    try:
        root = base_url.rstrip("/")
        root = root[:-3] if root.endswith("/v1") else root
        with urllib.request.urlopen(f"{root}/api/v0/models/{model}", timeout=3) as r:
            m = json.loads(r.read())
        for k in ("publisher", "arch", "quantization", "state", "loaded_context_length", "max_context_length"):
            if k in m:
                info[k] = m[k]
    except Exception:
        pass  # non e' LM Studio, o il server non risponde: teniamo solo l'id
    return info


class Recorder:
    """Raccoglie gli eventi di un run e li salva su disco MENTRE il run e' in corso
    (status='running', ogni pochi secondi) e alla fine (status='done').
    Cosi' un run non si perde se la pagina viene ricaricata, e si puo' seguire
    dal vivo anche un run lanciato da riga di comando."""

    FLUSH_SEC = 3.0

    def __init__(self, demo: str, backend: str, model: str, base_url: str, params: dict,
                 source: str = "webui"):
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.last_flush = 0.0
        self.rec = {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            "demo": demo, "source": source, "status": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "model": model_info(backend, model, base_url),
            "params": {k: v for k, v in params.items()
                       if k not in ("demo", "backend", "model", "base_url")},
            "events": [],
        }
        self._flush()  # il run compare subito nello storico come "in corso"

    def add(self, ev: dict) -> None:
        if ev.get("type") in ("end",):
            return
        with self.lock:
            self.rec["events"].append(ev)
            if time.time() - self.last_flush > self.FLUSH_SEC:
                self._flush()
            elif not getattr(self, "_timer", None):
                # eventi arrivati a raffica: li scriviamo comunque entro FLUSH_SEC,
                # anche se poi per minuti non arriva nulla (una chiamata LLM lunga)
                self._timer = threading.Timer(self.FLUSH_SEC, self._flush_pending)
                self._timer.daemon = True
                self._timer.start()

    def _flush_pending(self) -> None:
        with self.lock:
            self._timer = None
            if self.rec["status"] == "running":
                self._flush()

    def _flush(self) -> None:
        r = self.rec
        r["duration_sec"] = round(time.time() - self.t0, 1)
        r["updated_at"] = datetime.now().isoformat(timespec="seconds")
        r["error"] = next((e["message"] for e in r["events"] if e.get("type") == "error"), None)
        r["summary"] = summarize(r["demo"], r["events"])
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = RUNS_DIR / f".{r['id']}.tmp"
        tmp.write_text(json.dumps(r, ensure_ascii=False, default=str), encoding="utf-8")
        for _ in range(5):  # su Windows il replace fallisce se un lettore ha il file aperto
            try:
                tmp.replace(RUNS_DIR / f"{r['id']}.json")
                break
            except PermissionError:
                time.sleep(0.05)
        self.last_flush = time.time()

    def save(self, status: str = "done") -> str:
        with self.lock:
            self.rec["status"] = status   # "done" oppure "stopped" (pulsante Stop)
            self.rec["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._flush()
        return self.rec["id"]


def _avg(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 2) if xs else None


def summarize(demo: str, events: list[dict]) -> dict:
    """Riepilogo compatto, mostrato nella lista dello storico."""
    if demo == "lunghi":
        truth = next((e for e in events if e["type"] == "truth"), {})
        out = {"righe": truth.get("righe"), "metodi": {}}
        for m in "ABC":
            rs = [e for e in events if e["type"] == "result" and e["method"] == m]
            if rs:
                out["metodi"][m] = {"prove": len(rs), "esatti": sum(r["esatto"] for r in rs),
                                    "campi_pct": _avg(r["pct"] for r in rs),
                                    "sec": _avg(r["sec"] for r in rs), "tok": _avg(r["tok"] for r in rs)}
        return out
    if demo == "incidenti":
        truth = next((e for e in events if e["type"] == "truth"), {})
        out = {"scala": truth.get("scala"), "incidenti": truth.get("incidenti"),
               "token_corpus": (truth.get("stats") or {}).get("_totale", {}).get("token_stimati"), "risultati": []}
        for r in events:
            if r["type"] == "result":
                out["risultati"].append({"model": r.get("model"), "method": r["method"], "esatto": r["esatto"],
                                         "det": r["pct_det"], "sem": r["pct_sem"], "der": r["pct_der"],
                                         "sec": round(r["sec"], 1), "tok": r["tok"],
                                         "lat_media": (r.get("latenza") or {}).get("media"), "error": bool(r.get("error"))})
        return out
    if demo == "confronto":
        truth = next((e for e in events if e["type"] == "truth"), {})
        out = {"righe": truth.get("righe"), "metodi": {}}
        for m in "AB":
            rs = [e for e in events if e["type"] == "row" and e["method"] == m]
            if rs:
                out["metodi"][m] = {"prove": len(rs), "esatti": sum(r["ok"] for r in rs),
                                    "sec": _avg(r["sec"] for r in rs), "tok": _avg(r["tok"] for r in rs)}
        return out
    if demo == "agente":
        llm = [e for e in events if e["type"] == "llm"]
        final = next((e for e in events if e["type"] == "final" and e.get("depth") == 0), None)
        return {"passi": len(llm), "tool_call": sum(e["type"] == "tool_call" for e in events),
                "sub_agenti": sum(e["type"] == "task" and e.get("depth", 0) > 0 for e in events),
                "token": sum(((e.get("response") or {}).get("usage") or {}).get("completion_tokens", 0) for e in llm),
                "completato": final is not None,
                "risposta": (final or {}).get("text", "")[:200]}
    if demo == "json":
        return {"modi": {e["mode"]: {"valido": e["data"] is not None, "tentativi": e["attempts"]}
                         for e in events if e["type"] == "json_result"}}
    if demo == "anatomia":
        return {"tool_chiamato": any(e.get("step") == "c" for e in events)}
    return {}


STALE_SEC = 20 * 60  # un run "running" non aggiornato da 20 minuti e' stato interrotto


def _status(r: dict, p: Path) -> str:
    s = r.get("status", "done")  # i run salvati prima di questa versione non hanno status
    if s == "running" and time.time() - p.stat().st_mtime > STALE_SEC:
        return "interrupted"
    return s


def _read(p: Path) -> dict | None:
    for _ in range(3):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05)  # file in riscrittura: riprova
    return None


def list_runs() -> list[dict]:
    """Elenco (senza eventi), dal piu' recente."""
    out = []
    for p in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        r = _read(p)
        if not r:
            continue
        r["status"] = _status(r, p)
        r["n_events"] = len(r.pop("events", []))
        out.append(r)
    return out


def load_run(run_id: str, since: int = 0) -> dict | None:
    """Run completo; con since=N restituisce solo gli eventi dal N-esimo (per seguire un run in corso)."""
    p = RUNS_DIR / f"{run_id}.json"
    if not run_id.replace("-", "").isalnum() or not p.is_file():
        return None
    r = _read(p)
    if r is None:
        return None
    r["status"] = _status(r, p)
    r["n_events"] = len(r["events"])
    if since:
        r["events"] = r["events"][since:]
    r["since"] = since
    return r
