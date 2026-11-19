"""
Web UI di open-harness - solo libreria standard.

    python webui.py            -> http://127.0.0.1:8765

Come funziona:
  POST /api/run          avvia una demo in un thread, ritorna un run_id
  GET  /api/stream/<id>  Server-Sent Events: ogni evento dell'agente arriva al browser in tempo reale
  GET  /api/models       elenco modelli del server locale (LM Studio / Ollama / vLLM ...)
"""
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import history
import providers
import main as demos  # riusa fattura, mock, schemi e run_confronto della CLI
from harness import LocalLLM, MockLLM, Tool, ToolRegistry, Tracer, ask_json

ROOT = demos.ROOT
RUNS: dict[str, queue.Queue] = {}
STOPS: dict[str, threading.Event] = {}  # id nello storico -> evento "Stop premuto"


class WebTracer(Tracer):
    """Invece di stampare, mette ogni evento in una coda letta dallo stream SSE."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def event(self, kind: str, depth: int, **data) -> None:
        self.q.put({"type": kind, "depth": depth, **data})


# piu' server OpenAI-compatibili insieme, separati da virgola: ogni modello sa su quale server sta.
# L'elenco predefinito viene da providers.json (vedi providers.py).
DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"
MODEL_BASE: dict[str, str] = {}   # modello -> server, riempito da list_models


def split_bases(s: str | None) -> list[str]:
    return [b.strip().rstrip("/") for b in (s or providers.enabled_bases()).replace(";", ",").split(",") if b.strip()]


def base_for(p: dict, model: str) -> str:
    """Il server su cui gira 'model' (fra quelli scritti nel campo in alto)."""
    bases = split_bases(p.get("base_url"))
    if model not in MODEL_BASE or MODEL_BASE[model] not in bases:
        list_models(",".join(bases))
    return MODEL_BASE.get(model) if MODEL_BASE.get(model) in bases else bases[0]


def with_stop(p: dict, llm):
    """Collega il modello al pulsante Stop del run."""
    llm.cancel = p.get("_stop")
    return llm


def local_llm(p: dict, model: str, **kw) -> LocalLLM:
    """Client per 'model' sul suo server, con la chiave API del provider se configurata."""
    base = base_for(p, model)
    return with_stop(p, LocalLLM(model, base, api_key=providers.api_key(base), **kw))


def make_llm(p: dict, mock_policy):
    if p.get("backend") == "local":
        return local_llm(p, p.get("model") or DEFAULT_MODEL, max_tokens=int(p.get("max_tokens") or 8192))
    return with_stop(p, MockLLM(mock_policy))


# ------------------------------------------------------------------ runner
def run_agente(p, q):
    demos.assicura_fattura(int(p.get("righe", 40)))
    llm = make_llm(p, demos.mock_agente)
    agent = demos.build_agent(llm, WebTracer(q), max_steps=int(p.get("max_steps", 10)))
    if isinstance(llm, MockLLM):
        q.put({"type": "notice", "text": "Backend mock: le risposte del modello sono preimpostate "
               "(il task che scrivi viene ignorato). Passa a 'local' per un modello vero."})
    agent.run(p.get("task") or demos.TASK_AGENTE)


def run_anatomia(p, q):
    llm = make_llm(p, demos.mock_anatomia)
    reg = ToolRegistry([Tool(
        "calcola_iva", "Calcola l'IVA al 22% di un imponibile in euro.",
        {"type": "object", "properties": {"imponibile": {"type": "number", "minimum": 0}},
         "required": ["imponibile"], "additionalProperties": False},
        lambda imponibile: {"iva": round(imponibile * 0.22, 2)})])
    step = lambda s, **kw: q.put({"type": "anat", "step": s, **kw})

    step("a", json=reg.specs())
    messages = [{"role": "user", "content": p.get("prompt") or "Quanta IVA pago su 1250 euro? Usa il tool."}]
    step("u", json=messages[0])
    msg = llm.chat(messages, tools=reg.specs())
    step("b", json=msg, raw=llm.last_response)
    if not msg.get("tool_calls"):
        step("x", text="Il modello ha risposto senza chiamare il tool (con tool_choice=auto puo' "
                       "succedere). Un harness vero a questo punto ri-chiede.")
        return
    call = msg["tool_calls"][0]
    result, err = reg.execute(call)
    tool_msg = {"role": "tool", "tool_call_id": call["id"], "content": result}
    step("c", json=tool_msg, arguments=call["function"]["arguments"], is_error=err)
    final = llm.chat(messages + [msg, tool_msg], tools=reg.specs())
    step("d", text=final.get("content") or "", raw=llm.last_response)
    bad = []
    for b in ['{"imponibile": "mille"}', '{"imponibile": 100, "sconto": 5}',
              '{imponibile: 1}', '{"importo": 100}', '{"imponibile": -50}']:
        r, _ = reg.execute({"id": "x", "function": {"name": "calcola_iva", "arguments": b}})
        bad.append({"arguments": b, "result": r})
    step("e", rows=bad)


def run_json(p, q):
    llm = make_llm(p, demos.mock_json)
    try:
        schema = json.loads(p["schema"]) if p.get("schema") else demos.CONTATTO_SCHEMA
    except json.JSONDecodeError as e:
        raise ValueError(f"Lo schema non e' JSON valido: {e}")
    testo = p.get("text") or demos.TESTO
    for mode in p.get("modes") or ["prompt", "constrained"]:
        q.put({"type": "json_start", "mode": mode})
        t0 = time.perf_counter()
        r = ask_json(llm, f"Estrai i dati da questo messaggio:\n{testo}", schema, mode=mode)
        q.put({"type": "json_result", "mode": mode, "sec": time.perf_counter() - t0, **r})


def run_confronto(p, q):
    llm = make_llm(p, demos.mock_confronto)
    for ev in demos.run_confronto(llm, int(p.get("righe", 40)), int(p.get("n", 3)),
                                  bool(p.get("solo_b"))):
        q.put(ev)


def run_lunghi(p, q):
    import pipeline as P
    if p.get("backend") == "local":
        make_factory = lambda truth: (lambda: make_llm(p, None))
    else:
        make_factory = P.mock_factory
    P.run_benchmark(make_factory, righe=int(p.get("righe", 40)), n=int(p.get("n", 1)),
                    metodi=[m for m in "ABC" if m in (p.get("metodi") or "ABC")],
                    chunk=int(p.get("chunk", 10)), judge=bool(p.get("judge", True)),
                    judge_ab=bool(p.get("judge_ab")), emit=q.put)


def run_incidenti(p, q):
    import incidenti as I
    modelli = p.get("modelli") or [p.get("model") or DEFAULT_MODEL]
    if p.get("backend") == "local":
        mt = int(p.get("max_tokens") or 24000)
        to = int(p.get("timeout") or 1800)  # prompt enormi su un PC remoto possono superare i 15 minuti
        make = lambda truth, model: (lambda: local_llm(p, model, max_tokens=mt, timeout=to))
    else:
        modelli = [f"mock-{i + 1}" for i in range(len(modelli))]
        delay = float(p.get("mock_delay") or 0)  # latenza finta, per provare la UI "dal vivo"

        def make(truth, model):
            base = I.mock_factory(truth, model)

            def f():
                llm = base()
                llm.delay = delay
                return with_stop(p, llm)
            return f
    I.run_benchmark(make, scala=p.get("scala", "medio"), n=int(p.get("n", 1)),
                    metodi=[m for m in "SABC" if m in (p.get("metodi") or "SABC")],
                    judge=bool(p.get("judge", True)), emit=q.put, modelli=modelli, stop=p.get("_stop"),
                    campione=int(p["campione"]) if str(p.get("campione") or "").strip() else None,
                    dataset=p.get("dataset") or None)


RUNNERS = {"agente": run_agente, "anatomia": run_anatomia, "json": run_json,
           "confronto": run_confronto, "lunghi": run_lunghi, "incidenti": run_incidenti}


# --------------------------------------------------------- codice e prompt
SOURCE_GLOBS = ["*.py", "harness/*.py", "skills/*/SKILL.md", "skills/*/scripts/*.py", "webui/index.html"]


def source_index() -> list[dict]:
    import ast
    out = []
    for g in SOURCE_GLOBS:
        for f in sorted(ROOT.glob(g)):
            rel = f.relative_to(ROOT).as_posix()
            text = f.read_text(encoding="utf-8")
            item = {"path": rel, "righe": text.count("\n") + 1, "kb": round(len(text.encode()) / 1024, 1),
                    "doc": "", "simboli": []}
            if f.suffix == ".py":
                try:
                    tree = ast.parse(text)
                    item["doc"] = (ast.get_docstring(tree) or "").strip().split("\n\n")[0]
                    for n in tree.body:
                        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                            item["simboli"].append({"nome": n.name, "tipo": "class" if isinstance(n, ast.ClassDef) else "def",
                                                    "riga": n.lineno, "doc": (ast.get_docstring(n) or "").split("\n")[0]})
                except SyntaxError:
                    pass
            out.append(item)
    return out


def source_file(path: str) -> str | None:
    allowed = {i["path"] for i in source_index()}
    return (ROOT / path).read_text(encoding="utf-8") if path in allowed else None


_PROMPTS: list | None = None
RUOLI = {  # nome dello schema -> (benchmark, metodo, ruolo, funzione che costruisce il prompt)
    "incidenti_completo": ("Incidenti SOC", "A", "LLM unico (tutto il corpus)", "incidenti.py · metodo_A"),
    "incidente_singolo": ("Incidenti SOC", "B", "LLM per incidente", "incidenti.py · metodo_B"),
    "classifica": ("Incidenti SOC", "C", "Classificatore", "incidenti.py · classifica"),
    "arbitro": ("Incidenti SOC", "C", "Arbitro (classificatori in disaccordo)", "incidenti.py · classifica(proposte=...)"),
    "giudice_incidenti": ("Incidenti SOC", "C", "Giudice LLM-as-judge", "incidenti.py · giudica"),
    "ordine_completo": ("JSON lunghi", "A", "LLM unico", "pipeline.py · metodo_A"),
    "estrazione": ("JSON lunghi", "B", "Estrattore unico", "pipeline.py · metodo_B"),
    "estrazione_blocco": ("JSON lunghi", "C", "Estrattore di blocco", "pipeline.py · estrai"),
    "correzione_riga": ("JSON lunghi", "C", "Correttore", "pipeline.py · correggi"),
    "giudice": ("JSON lunghi", "C", "Giudice LLM-as-judge", "pipeline.py · giudica"),
    "tools": ("Agente", "-", "Agente / sub-agente (tool calling)", "harness/agent.py · Agent.run"),
}


def agent_prompts() -> list[dict]:
    """Esegue i benchmark col modello FINTO e cattura il primo prompt reale di ogni ruolo:
    sono esattamente i testi che ricevono i sub-agenti (con dati di esempio)."""
    global _PROMPTS
    if _PROMPTS is not None:
        return _PROMPTS
    import incidenti as I
    import pipeline as P
    seen, out = set(), []

    def grab(ev):
        if ev.get("type") != "llm_call":
            return
        variante = ("correzione dopo validatori" if "RESPINTA" in (ev.get("prompt") or "") else
                    "seconda opinione" if "seconda opinione" in (ev.get("system") or "") else
                    "sub-agente" if (ev.get("system") or "").startswith("Sei un sub-agente") else "")
        key = (ev["name"], variante)
        if key in seen:
            return
        seen.add(key)
        b, m, ruolo, fn = RUOLI.get(ev["name"], ("?", "?", ev["name"], "?"))
        out.append({"benchmark": b, "metodo": m, "ruolo": ruolo + (f" · {variante}" if variante else ""),
                    "funzione": fn, "schema_name": ev["name"], "system": ev.get("system"),
                    "prompt": ev.get("prompt"), "messages": ev.get("messages"), "schema": ev.get("schema"),
                    "tools": ev.get("tools"), "esempio_risposta": ev.get("response")})

    I.run_benchmark(I.mock_factory, scala="piccolo", emit=grab, metodi=["A", "B", "C"])
    P.run_benchmark(P.mock_factory, righe=12, n=2, emit=grab)
    llm = MockLLM(demos.mock_agente)
    llm.on_call = grab
    silenzioso = type("Silenzioso", (Tracer,), {"event": lambda *a, **k: None})()
    demos.build_agent(llm, silenzioso).run(demos.TASK_AGENTE)
    out.sort(key=lambda x: (x["benchmark"], x["metodo"]))
    _PROMPTS = out
    return out


class RecordingQueue(queue.Queue):
    """Coda che, oltre a passare gli eventi al browser, li registra per lo storico."""

    def __init__(self, recorder: history.Recorder):
        super().__init__()
        self.recorder = recorder

    def put(self, item, *a, **kw):
        self.recorder.add(item)
        super().put(item, *a, **kw)


def start_run(p: dict) -> str:
    run_id = uuid.uuid4().hex[:10]
    backend = p.get("backend") or "mock"
    model = ",".join(p["modelli"]) if p.get("modelli") and backend == "local" else (p.get("model") or "")
    base = ({m: base_for(p, m) for m in model.split(",") if m} if backend == "local" and model else "")
    rec = history.Recorder(p["demo"], backend, model, base, p)
    stop = p["_stop"] = STOPS[rec.rec["id"]] = threading.Event()   # dopo il Recorder: non va nei parametri salvati
    q = RecordingQueue(rec)
    RUNS[run_id] = q
    q.put({"type": "run_info", "history_id": rec.rec["id"], "model": rec.rec["model"],
           "started_at": rec.rec["started_at"]})

    def worker():
        try:
            RUNNERS[p["demo"]](p, q)
        except Exception as e:  # mostriamo l'errore nella UI invece di perderlo
            if not stop.is_set():
                q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if stop.is_set():
                q.put({"type": "notice", "text": "Run fermato dall'utente: i risultati già completati restano nello storico."})
            STOPS.pop(rec.rec["id"], None)
            try:
                rec.save("stopped" if stop.is_set() else "done")
            except OSError as e:
                q.put({"type": "error", "message": f"Storico non salvato: {e}"})
            q.put({"type": "end"})

    threading.Thread(target=worker, daemon=True).start()
    return run_id


def _get(url: str, base: str):
    key = providers.api_key(base)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _models_one(base: str) -> dict:
    prov = providers.find(base) or {}
    if prov.get("models"):   # elenco fisso nel file: niente richiesta al server (utile per i servizi cloud)
        for i in prov["models"]:
            MODEL_BASE[i] = base
        return {"ok": True, "models": [{"id": i, "server": base, "provider": prov["name"], "loaded": True,
                                        "ctx": None, "quant": None} for i in prov["models"]]}
    try:
        data = [m for m in _get(f"{base}/models", base).get("data", []) if "embed" not in m["id"]]
    except Exception as e:
        return {"ok": False, "error": f"{base} non raggiungibile ({e})", "models": []}
    loaded: dict[str, dict] | None = None
    try:  # API specifica di LM Studio: quali modelli sono caricati, con che contesto e quantizzazione
        root = base[:-3] if base.endswith("/v1") else base
        loaded = {m["id"]: m for m in _get(f"{root}/api/v0/models", base)["data"] if m.get("state") == "loaded"}
    except Exception:
        pass  # non e' LM Studio (vLLM, llama.cpp, ...): i modelli elencati sono gia' serviti
    models = []
    for m in data:
        i, meta = m["id"], m.get("meta") or {}
        lm = (loaded or {}).get(i) or {}
        models.append({"id": i, "server": base, "provider": prov.get("name"), "loaded": True if loaded is None else i in loaded,
                       "ctx": lm.get("loaded_context_length") or meta.get("n_ctx") or m.get("max_model_len"),
                       "quant": lm.get("quantization") or meta.get("ftype")})
        MODEL_BASE[i] = base
    return {"ok": True, "models": models}


def list_models(base_urls: str) -> dict:
    """Modelli di TUTTI i server indicati (separati da virgola)."""
    res = [_models_one(b) for b in split_bases(base_urls)]
    models = [m for r in res for m in r["models"]]
    models.sort(key=lambda m: not m["loaded"])
    errs = [r["error"] for r in res if not r["ok"]]
    return {"ok": any(r["ok"] for r in res), "models": models, "servers": len(res),
            "servers_ok": sum(r["ok"] for r in res), "error": " · ".join(errs)}


# -------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silenzia il log di ogni richiesta
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = (ROOT / "webui" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/defaults":
            self._json({"task": demos.TASK_AGENTE, "text": demos.TESTO,
                        "schema": json.dumps(demos.CONTATTO_SCHEMA, indent=2, ensure_ascii=False),
                        "model": DEFAULT_MODEL, "base_url": providers.enabled_bases()})
        elif url.path == "/api/models":
            qs = parse_qs(url.query)
            self._json(list_models(qs.get("base_url", [providers.enabled_bases()])[0]))
        elif url.path == "/api/providers":
            self._json({"providers": providers.public_view(), "file": providers.FILE.name,
                        "salvato": providers.FILE.exists()})
        elif url.path == "/api/datasets":
            import incidenti as I
            self._json({"datasets": I.elenco_dataset()})
        elif url.path == "/api/fattura":
            n = int(parse_qs(url.query).get("righe", ["40"])[0])
            f = demos.assicura_fattura(n)
            self._json({"fattura": f, "verita": demos.calcola_fattura.calcola(f)})
        elif url.path == "/api/source":
            f = parse_qs(url.query).get("file", [None])[0]
            if f:
                txt = source_file(f)
                self._json({"path": f, "text": txt} if txt is not None else {"error": "file non consentito"},
                           200 if txt is not None else 404)
            else:
                self._json({"files": source_index()})
        elif url.path == "/api/prompts":
            self._json({"prompts": agent_prompts()})
        elif url.path == "/api/runs":
            self._json({"runs": history.list_runs()})
        elif url.path.startswith("/api/runs/"):
            since = int(parse_qs(url.query).get("from", ["0"])[0] or 0)
            r = history.load_run(url.path.rsplit("/", 1)[-1], since)
            self._json(r if r else {"error": "run non trovato"}, 200 if r else 404)
        elif url.path.startswith("/api/stream/"):
            self._stream(url.path.rsplit("/", 1)[-1])
        else:
            self.send_error(404)

    def _same_origin(self) -> bool:
        """Difesa da richieste di altri siti verso localhost: solo JSON e solo dalla pagina servita qui."""
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == f"http://{self.headers.get('Host')}"

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "richiesta rifiutata (origine o Content-Type non ammessi)"}, 403)
        if self.path == "/api/providers":
            try:
                saved = providers.save(self._body().get("providers"))
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            MODEL_BASE.clear()
            return self._json({"ok": True, "providers": providers.public_view(), "n": len(saved)})
        if self.path == "/api/datasets":   # {"nome": ..., "files": {"siem_export.log": "...", ...}, "sovrascrivi": false}
            import incidenti as I
            try:
                b = self._body()
                ds = I.salva_dataset(str(b.get("nome") or ""), dict(b.get("files") or {}), bool(b.get("sovrascrivi")))
            except (ValueError, json.JSONDecodeError, OSError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "dataset": ds, "datasets": I.elenco_dataset()})
        if self.path == "/api/stop":   # pulsante Stop: {"id": <id nello storico>}
            p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            ev = STOPS.get(p.get("id") or "")
            if ev is None:
                return self._json({"ok": False, "error": "run non in corso su questo server"}, 404)
            ev.set()
            return self._json({"ok": True})
        if self.path != "/api/run":
            return self.send_error(404)
        p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if p.get("demo") not in RUNNERS:
            return self._json({"error": "demo sconosciuta"}, 400)
        self._json({"run_id": start_run(p)})

    def _stream(self, run_id: str):
        q = RUNS.get(run_id)
        if q is None:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # il modello sta ancora pensando
                    self.wfile.flush()
                    continue
                data = json.dumps(ev, ensure_ascii=False, default=str)
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
                if ev["type"] == "end":
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            RUNS.pop(run_id, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"open-harness web UI su {url}  (Ctrl+C per uscire)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
