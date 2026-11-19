"""
inc-bench - demo didattiche.

    python main.py anatomia   # com'e' fatta una tool call, byte per byte
    python main.py agente     # loop + skill + script + sub-agente
    python main.py json       # forzare uno schema: prompt vs constrained decoding
    python main.py confronto  # LLM che fa i conti in JSON vs Python deterministico

Opzioni: --backend mock|local  --model <id>  --base-url <url>  --raw  -n <prove>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import time
from pathlib import Path

from harness import (Agent, LocalLLM, MockLLM, Tool, ToolRegistry, Tracer,
                     ask_json, load_skills, skill_tools, skills_index,
                     subagent_tool, tool_call)
from harness.agent import C

ROOT = Path(__file__).parent
os.chdir(ROOT)
DATA = ROOT / "data"
FATTURA = "data/fattura.json"

# Lo script della skill e' anche la nostra "verita'" per verificare i risultati.
_spec = importlib.util.spec_from_file_location(
    "calcola_fattura", ROOT / "skills/fatturazione/scripts/calcola_fattura.py")
calcola_fattura = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calcola_fattura)


def genera_fattura(n_righe: int = 40, seed: int = 7) -> dict:
    rng = random.Random(seed)
    prodotti = ["Vite M6", "Staffa inox", "Cavo 3x1.5", "Sensore PT100", "Relè 24V",
                "Quadro IP65", "Morsetto", "PLC modulo I/O", "Guaina 20mm", "Fusibile 10A"]
    righe = [{"codice": f"ART-{i:03d}", "descrizione": rng.choice(prodotti),
              "quantita": rng.randint(1, 48),
              "prezzo_unitario": round(rng.uniform(0.35, 480), 2),
              "sconto_pct": rng.choice([0, 0, 0, 5, 10, 12.5, 15])}
             for i in range(1, n_righe + 1)]
    return {"numero": "2026/0815", "cliente": "Officine Rossi S.r.l.",
            "iva_pct": 22, "righe": righe}


def assicura_fattura(n_righe: int = 40) -> dict:
    """Crea data/fattura.json (rigenerandola se il numero di righe e' diverso)."""
    DATA.mkdir(exist_ok=True)
    p = ROOT / FATTURA
    if p.exists():
        f = json.loads(p.read_text(encoding="utf-8"))
        if len(f["righe"]) == n_righe:
            return f
    f = genera_fattura(n_righe)
    p.write_text(json.dumps(f, indent=1, ensure_ascii=False), encoding="utf-8")
    return f


def titolo(t: str) -> None:
    print(f"\n{C['mag']}{'=' * 70}\n {t}\n{'=' * 70}{C['end']}")


# ----------------------------------------------------------------- 1. ANATOMIA
def demo_anatomia(llm, args):
    titolo("1. ANATOMIA DI UNA TOOL CALL")
    reg = ToolRegistry([Tool(
        "calcola_iva", "Calcola l'IVA al 22% di un imponibile in euro.",
        {"type": "object", "properties": {"imponibile": {"type": "number", "minimum": 0}},
         "required": ["imponibile"], "additionalProperties": False},
        lambda imponibile: {"iva": round(imponibile * 0.22, 2)})])

    print(f"{C['yel']}(a) Cosa vede il modello del tool: SOLO questo JSON Schema{C['end']}")
    print(json.dumps(reg.specs(), indent=2, ensure_ascii=False))

    messages = [{"role": "user", "content": "Quanta IVA pago su 1250 euro? Usa il tool."}]
    msg = llm.chat(messages, tools=reg.specs())
    print(f"\n{C['yel']}(b) Il modello risponde con una RICHIESTA di tool call (non la esegue!){C['end']}")
    print(json.dumps(msg, indent=2, ensure_ascii=False))
    if not msg.get("tool_calls"):
        print(f"{C['red']}Il modello non ha chiamato il tool: con tool_choice=auto puo' succedere. "
              f"L'harness dovrebbe ri-chiedere.{C['end']}")
        return
    call = msg["tool_calls"][0]
    print(f"\n{C['dim']}Nota: 'arguments' e' una STRINGA: {call['function']['arguments']!r}"
          f" -> l'harness fa json.loads e valida.{C['end']}")

    result, err = reg.execute(call)
    tool_msg = {"role": "tool", "tool_call_id": call["id"], "content": result}
    print(f"\n{C['yel']}(c) L'harness esegue il codice Python e rimanda il risultato:{C['end']}")
    print(json.dumps(tool_msg, indent=2, ensure_ascii=False))

    final = llm.chat(messages + [msg, tool_msg], tools=reg.specs())
    print(f"\n{C['yel']}(d) Il modello legge il risultato e risponde:{C['end']}\n{final.get('content')}")

    print(f"\n{C['yel']}(e) Bonus: cosa succede con argomenti sbagliati (es. allucinati):{C['end']}")
    for bad in ['{"imponibile": "mille"}', '{"imponibile": 100, "sconto": 5}', '{imponibile: 1}']:
        r, _ = reg.execute({"id": "x", "function": {"name": "calcola_iva", "arguments": bad}})
        print(f"  {bad:40} -> {r.splitlines()[-1]}")


def mock_anatomia(messages, tools, rf):
    if not any(m["role"] == "tool" for m in messages):
        return tool_call("calcola_iva", imponibile=1250)
    res = json.loads(messages[-1]["content"])
    return {"role": "assistant", "content": f"Su 1250 € di imponibile l'IVA al 22% è {res['iva']} €."}


# ------------------------------------------------------------------- 2. AGENTE
TASK_AGENTE = (f"Calcola imponibile, IVA e totale della fattura in {FATTURA}. Poi delega a un "
               "sub-agente la stesura di una email di 3 righe al cliente con il totale da pagare.")


def recorder(args, demo: str, params: dict):
    """Registra il run nello storico (data/runs/), come fa la web UI."""
    import history
    return history.Recorder(demo, args.backend, args.model, args.base_url, params, source="cli")


def salvato(rec) -> None:
    print(f"{C['dim']}Run salvato nello storico: data/runs/{rec.save()}.json "
          f"(modello: {rec.rec['model'].get('id')}){C['end']}")


class RecordingTracer(Tracer):
    def __init__(self, rec, raw=False):
        super().__init__(raw)
        self.rec = rec

    def event(self, kind, depth, **data):
        self.rec.add({"type": kind, "depth": depth, **data})
        super().event(kind, depth, **data)


def demo_agente(llm, args):
    titolo("2. AGENT LOOP + SKILL + SCRIPT + SUB-AGENTE")
    assicura_fattura(args.righe)
    rec = recorder(args, "agente", {"task": TASK_AGENTE, "righe": args.righe, "max_tokens": args.max_tokens})
    tracer = RecordingTracer(rec, raw=args.raw)
    agent = build_agent(llm, tracer)
    tracer.log(0, "dim", "System prompt:", agent.system)
    risposta = agent.run(TASK_AGENTE)
    titolo("RISPOSTA FINALE")
    print(risposta)
    salvato(rec)


def build_agent(llm, tracer, max_steps: int = 10) -> Agent:
    """Agente principale: leggi_file + skill + delega a sub-agente."""
    skills = load_skills(ROOT / "skills")

    def leggi_file(percorso: str) -> str:
        p = (ROOT / percorso).resolve()
        if DATA.resolve() not in p.parents:
            return "ERRORE: posso leggere solo file in data/"
        return p.read_text(encoding="utf-8")[:3000]

    base_tools = [Tool("leggi_file", "Legge un file di testo dalla cartella data/.",
                       {"type": "object", "properties": {"percorso": {"type": "string"}},
                        "required": ["percorso"], "additionalProperties": False}, leggi_file)]
    # il sub-agente ha meno tool del padre (niente skill, niente delega): principio del minimo privilegio
    sub_reg = ToolRegistry(base_tools)
    reg = ToolRegistry(base_tools + skill_tools(skills) + [subagent_tool(llm, sub_reg, tracer)])

    system = ("Sei un agente amministrativo. Usa i tool, non inventare dati.\n"
              + skills_index(skills))
    return Agent(llm, "agente", system, reg, max_steps=max_steps, tracer=tracer)


def mock_agente(messages, tools, rf):
    if messages[0]["content"].startswith("Sei un sub-agente"):
        return {"role": "assistant", "content":
                "Gentile Officine Rossi,\nin allegato la fattura 2026/0815: il totale da pagare è "
                + messages[1]["content"].split("totale ")[-1].split(" ")[0] +
                " €.\nCordiali saluti."}
    done = [m for m in messages if m["role"] == "tool"]
    if len(done) == 0:
        return tool_call("carica_skill", nome="fatturazione")
    if len(done) == 1:
        return tool_call("esegui_script", skill="fatturazione", script="calcola_fattura",
                         input={"file": FATTURA})
    if len(done) == 2:
        tot = json.loads(done[1]["content"])
        return tool_call("delega", compito=f"Scrivi una email di 3 righe a Officine Rossi "
                                           f"per la fattura 2026/0815, totale {tot['totale']} euro.")
    tot = json.loads(done[1]["content"])
    return {"role": "assistant", "content":
            f"Totali (calcolati dallo script): {json.dumps(tot)}\n\nBozza email:\n{done[2]['content']}"}


# --------------------------------------------------------------------- 3. JSON
CONTATTO_SCHEMA = {"type": "object", "properties": {
    "nome": {"type": "string"}, "email": {"type": "string"},
    "piano": {"type": "string", "enum": ["Base", "Pro", "Enterprise"]},
    "vuole_demo": {"type": "boolean"}},
    "required": ["nome", "email", "piano", "vuole_demo"], "additionalProperties": False}
TESTO = ("Ciao, sono Giulia Bianchi (giulia.bianchi@example.com). Stiamo valutando il "
         "piano Pro, ma prima ci piacerebbe vedere una demo dal vivo.")


def demo_json(llm, args):
    titolo("3. FORZARE UNO SCHEMA JSON")
    print(f"Testo: {TESTO}\nSchema: {json.dumps(CONTATTO_SCHEMA)}\n")
    for mode in ("prompt", "constrained"):
        r = ask_json(llm, f"Estrai i dati da questo messaggio:\n{TESTO}", CONTATTO_SCHEMA, mode=mode)
        col = "grn" if r["data"] else "red"
        print(f"{C['yel']}modo={mode:12}{C['end']} tentativi={r['attempts']}  "
              f"{C[col]}{'OK' if r['data'] else 'FALLITO'}{C['end']}")
        for e in r["errors"]:
            print(f"   {C['red']}{e}{C['end']}")
        print(f"   output: {json.dumps(r['data'], ensure_ascii=False) if r['data'] else r['raw'][:300]}")
    print(f"\n{C['cyan']}Lezione: 'constrained' garantisce la FORMA (lo fa il server, es. XGrammar/GBNF)."
          f"\nMa se il testo dicesse 'piano Pro' e il modello scrivesse 'Enterprise', lo schema "
          f"sarebbe rispettato lo stesso: la forma non e' la verita'.{C['end']}")


def mock_json(messages, tools, rf):
    good = {"nome": "Giulia Bianchi", "email": "giulia.bianchi@example.com",
            "piano": "Pro", "vuole_demo": True}
    if rf:  # constrained decoding: per costruzione e' valido
        return {"role": "assistant", "content": json.dumps(good)}
    if len(messages) == 1:  # primo tentativo "solo prompt": tipico output sporco
        bad = dict(good, vuole_demo="si", telefono="+39 333 1234567")
        return {"role": "assistant", "content": "Ecco i dati estratti:\n```json\n"
                + json.dumps(bad, ensure_ascii=False) + "\n```"}
    return {"role": "assistant", "content": json.dumps(good)}


# ---------------------------------------------------------------- 4. CONFRONTO
TOTALI_SCHEMA = {"type": "object", "properties": {
    "numero_righe": {"type": "integer"}, "imponibile": {"type": "number"},
    "iva": {"type": "number"}, "totale": {"type": "number"}},
    "required": ["numero_righe", "imponibile", "iva", "totale"], "additionalProperties": False}


def run_confronto(llm, righe: int, n: int, solo_b: bool = False):
    """Generatore di eventi: usato sia dalla CLI (demo_confronto) sia dalla web UI."""
    fattura = assicura_fattura(righe)
    verita = calcola_fattura.calcola(fattura)
    yield {"type": "truth", "righe": len(fattura["righe"]), "values": verita,
           "simulated": isinstance(llm, MockLLM)}

    def corretto(d):
        return d is not None and all(abs(d[k] - verita[k]) < 0.005 for k in verita)

    def costo():  # token generati (incluso il ragionamento) dall'ultima chiamata
        return ((llm.last_response or {}).get("usage") or {}).get("completion_tokens", 0)

    # A) il modello calcola e risponde con JSON (schema FORZATO dal server)
    prompt_a = ("Calcola numero_righe, imponibile, iva e totale di questa fattura. "
                "Ogni riga = quantita*prezzo_unitario*(1-sconto_pct/100) arrotondata al centesimo.\n"
                + json.dumps(fattura, ensure_ascii=False))
    for i in range(0 if solo_b else n):
        t0 = time.perf_counter()
        r = ask_json(llm, prompt_a, TOTALI_SCHEMA, mode="constrained", max_retries=0)
        yield {"type": "row", "method": "A", "i": i + 1, "data": r["data"],
               "ok": corretto(r["data"]), "valid": r["data"] is not None,
               "sec": time.perf_counter() - t0, "tok": costo()}

    # B) il modello sceglie SOLO il tool; i numeri passano dallo script all'output
    #    senza essere riscritti dal modello (cosi' non puo' sbagliare a ricopiarli).
    #    E' un mini agent loop: eseguiamo qualunque tool chieda (es. prima carica_skill),
    #    finche' non arriva il risultato di esegui_script. Se risponde SENZA tool call,
    #    l'HARNESS insiste: e' il codice, non il modello, a garantire che il calcolo passi da Python.
    reg = ToolRegistry(skill_tools(load_skills(ROOT / "skills")))
    for i in range(n):
        messages = [{"role": "system", "content": "Per i conti usa SEMPRE il tool esegui_script "
                     "con skill='fatturazione', script='calcola_fattura'."},
                    {"role": "user", "content": f"Calcola i totali della fattura {FATTURA}."}]
        t0, tok, turni, reprompt = time.perf_counter(), 0, 0, 0
        data, errore, chiamata = None, None, None
        while data is None and turni < 4:
            turni += 1
            msg = llm.chat(messages, tools=reg.specs())
            tok += costo()
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                reprompt += 1
                messages.append({"role": "user", "content": "Non hai chiamato il tool. "
                                 "Chiama esegui_script, non calcolare a mente."})
                continue
            for c in calls:
                res, err = reg.execute(c)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": res})
                if c["function"]["name"] == "esegui_script":
                    chiamata = c["function"]["arguments"]
                    try:
                        data = None if err else json.loads(res)
                    except json.JSONDecodeError:
                        err = True
                    errore = f"chiamata: {chiamata} -> {res[:300]!r}" if err else None
        yield {"type": "row", "method": "B", "i": i + 1, "data": data, "ok": corretto(data),
               "tool": chiamata is not None, "turni": turni, "reprompt": reprompt,
               "error": errore, "call": chiamata,
               "sec": time.perf_counter() - t0, "tok": tok}


def demo_confronto(llm, args):
    titolo("4. CONFRONTO: il modello fa i conti in JSON  vs  il modello chiama Python")
    a = {"validi": 0, "giusti": 0, "tok": 0, "sec": 0.0}
    b = {"giusti": 0, "tool": 0, "retry": 0, "tok": 0, "sec": 0.0}
    rec = recorder(args, "confronto", {"righe": args.righe, "n": args.n, "solo_b": args.solo_b,
                                       "max_tokens": args.max_tokens})
    for ev in run_confronto(llm, args.righe, args.n, args.solo_b):
        rec.add(ev)
        if ev["type"] == "truth":
            print(f"Fattura con {ev['righe']} righe. Valori corretti: {ev['values']}\n")
            if ev["simulated"]:
                print(f"{C['red']}ATTENZIONE: backend mock -> gli errori del metodo A sono SIMULATI. "
                      f"Usa --backend local per misurarli su un modello vero.{C['end']}\n")
            continue
        esito = f"{C['grn'] + 'GIUSTO' if ev['ok'] else C['red'] + 'SBAGLIATO'}{C['end']}"
        costo = f"  {C['dim']}{ev['sec']:.0f}s, {ev['tok']} token{C['end']}"
        dati = json.dumps(ev["data"]) if ev["data"] else ""
        s = a if ev["method"] == "A" else b
        s["giusti"] += ev["ok"]
        s["tok"] += ev["tok"]
        s["sec"] += ev["sec"]
        if ev["method"] == "A":
            a["validi"] += ev["valid"]
            print(f"  A#{ev['i']}: {dati or 'JSON non valido'}  {esito}{costo}")
        else:
            b["tool"] += ev["tool"]
            b["retry"] += ev["reprompt"]
            if ev["error"]:
                print(f"    {C['red']}{ev['error']}{C['end']}")
            print(f"  B#{ev['i']}: tool={'si' if ev['tool'] else 'NO'} (turni LLM {ev['turni']})  "
                  f"{dati}  {esito}{costo}")

    titolo("RISULTATO")
    n = args.n
    if not args.solo_b:
        print(f"A) LLM calcola, JSON forzato : JSON valido {a['validi']}/{n} | corretti {a['giusti']}/{n}"
              f" | media {a['sec'] / n:.0f}s, {a['tok'] // n} token")
    print(f"B) LLM chiama script Python  : tool chiamato {b['tool']}/{n} (re-prompt harness: {b['retry']})"
          f" | corretti {b['giusti']}/{n} | media {b['sec'] / n:.0f}s, {b['tok'] // n} token")
    print(f"\n{C['cyan']}A: lo schema e' sempre rispettato, ma la correttezza dei numeri NON e' garantita "
          f"(il modello predice token, non fa aritmetica):\n   anche quando indovina, non puoi saperlo "
          f"senza ricalcolare, e il costo cresce con le righe (prova --righe 150).\n"
          f"B: l'unica decisione del modello e' QUALE tool chiamare (verificabile dall'harness); "
          f"il calcolo e' deterministico,\n   ripetibile, auditabile e costa uguale con 10 o 10.000 righe."
          f"{C['end']}")
    salvato(rec)


def mock_confronto(messages, tools, rf):
    if rf:  # metodo A simulato: errori tipici di un LLM che fa somme lunghe
        f = json.loads(messages[0]["content"].split("\n", 1)[1])
        v = calcola_fattura.calcola(f)
        rng = random.Random(len(str(messages)) + mock_confronto.k)
        mock_confronto.k += 1
        err = rng.choice(["ok", "riga_persa", "arrotondamento", "cifre", "totale_arrotondato"])
        if err == "riga_persa":
            r = f["righe"][rng.randrange(len(f["righe"]))]
            v["imponibile"] = round(v["imponibile"] - r["quantita"] * r["prezzo_unitario"], 2)
            v["numero_righe"] -= 1
        elif err == "arrotondamento":
            v["imponibile"] = round(v["imponibile"] + rng.choice([-0.03, 0.02, 0.5]), 2)
        elif err == "cifre":
            s = f"{v['imponibile']:.2f}"
            v["imponibile"] = float(s[:-5] + s[-4] + s[-5] + s[-3:])
        if err in ("riga_persa", "arrotondamento", "cifre"):
            v["iva"] = round(v["imponibile"] * 0.22, 2)
            v["totale"] = round(v["imponibile"] + v["iva"], 2)
        if err == "totale_arrotondato":
            v["totale"] = round(v["totale"] + 0.04, 1)
        return {"role": "assistant", "content": json.dumps(v)}
    return tool_call("esegui_script", skill="fatturazione", script="calcola_fattura",
                     input={"file": FATTURA})


mock_confronto.k = 0

mock_confronto.k = 0


# ------------------------------------------------- 5. JSON LUNGHI: A / B / C
def demo_lunghi(make_factory, args):
    import pipeline as P  # import qui: pipeline importa main
    titolo("5. JSON LUNGHI: A (tutto al modello) vs B (modello + Python) vs C (multi-agente)")
    metodi = [m for m in "ABC" if m in args.metodi.upper()]
    agg: dict[str, list] = {m: [] for m in metodi}

    rec = recorder(args, "lunghi", {"righe": args.righe, "n": args.n, "chunk": args.chunk,
                                    "metodi": "".join(metodi), "judge": not args.no_judge,
                                    "judge_ab": args.judge_ab, "max_tokens": args.max_tokens})

    def emit(ev):
        rec.add(ev)
        t = ev["type"]
        if t == "truth":
            print(f"Email d'ordine con {ev['righe']} righe -> JSON con {ev['campi']} campi da compilare.")
            print(f"Validatori: {', '.join(ev['validatori'])}")
            if ev["simulated"]:
                print(f"{C['red']}Backend mock: gli errori sono SIMULATI.{C['end']}")
        elif t == "agent" and args.raw:
            print(f"    {C['dim']}[{ev['role']}] {json.dumps({k: v for k, v in ev.items() if k not in ('type', 'role')}, ensure_ascii=False)[:200]}{C['end']}")
        elif t == "agent" and ev["role"] in ("splitter", "calcolatore"):
            print(f"    {C['dim']}[{ev['role']}] {ev['msg']}{C['end']}")
        elif t == "result":
            agg[ev["method"]].append(ev)
            col = "grn" if ev["esatto"] else "red"
            extra = ""
            if ev["method"] == "C":
                extra = f" | correzioni {ev['correzioni']} | non verificate {len(ev['non_verificate'])}"
            if ev.get("giudice"):
                g = ev["giudice"]
                extra += f" | giudice: {g['veri_pos']} veri, {g['falsi_pos']} falsi allarmi, {g['mancati']} mancati"
            print(f"  {ev['method']}#{ev['i']}: {C[col]}{'ESATTO' if ev['esatto'] else 'ERRATO'}{C['end']} "
                  f"campi {ev['pct']}% | righe {ev['righe_ok']}/{ev['righe_tot']} | "
                  f"{ev['sec']:.0f}s | {ev['tok']} tok | {ev['calls']} chiamate LLM{extra}")
            if ev["error"]:
                print(f"    {C['red']}{ev['error']}{C['end']}")
            for e in ev["errori"][:4]:
                print(f"    {C['dim']}{e}{C['end']}")

    P.run_benchmark(make_factory, righe=args.righe, n=args.n, metodi=metodi, chunk=args.chunk,
                    judge=not args.no_judge, judge_ab=args.judge_ab, emit=emit)
    titolo("BENCHMARK")
    print(f"{'metodo':8} {'JSON esatti':>12} {'campi ok':>10} {'righe ok':>10} {'tempo medio':>12} "
          f"{'token medi':>11} {'chiamate':>9}")
    for m, rs in agg.items():
        if not rs:
            continue
        k = len(rs)
        print(f"{m:8} {sum(r['esatto'] for r in rs):>9}/{k:<2} {sum(r['pct'] for r in rs) / k:>9.2f}% "
              f"{sum(r['righe_ok'] for r in rs) / k:>6.1f}/{rs[0]['righe_tot']:<3} "
              f"{sum(r['sec'] for r in rs) / k:>11.1f}s {sum(r['tok'] for r in rs) // k:>11} "
              f"{sum(r['calls'] for r in rs) / k:>9.1f}")
    print(f"\n{C['cyan']}I validatori non conoscono la verita': controllano che ogni valore sia nel testo, "
          f"che non manchino righe, che i conti tornino.\nIn C le righe bocciate tornano a un agente correttore "
          f"con l'errore esatto; il giudice LLM aiuta ma puo' sbagliare, quindi le sue modifiche passano dai validatori.{C['end']}")
    salvato(rec)


# ------------------------------------------- 6. INCIDENTI SOC: S / A / B / C, piu' modelli
def demo_incidenti(args):
    import incidenti as I
    titolo("6. INCIDENTI SOC: S (script) vs A (solo LLM) vs B (LLM per incidente) vs C (ibrido multi-agente)")
    modelli = [m.strip() for m in args.modelli.split(",")] if args.modelli else [args.model]
    if args.backend == "mock":
        modelli = [f"mock-{i + 1}" for i in range(len(modelli))]
        make = I.mock_factory
    else:
        # come la web UI: piu' server insieme (i provider attivi in providers.json), ogni modello va sul suo
        import providers
        import webui
        dove = {m: webui.base_for({"base_url": args.base_url}, m) for m in modelli}
        for m, b in dove.items():
            print(f"{m} -> {b}")
        make = lambda truth, model: (lambda: LocalLLM(model, dove[model], max_tokens=args.max_tokens,
                                                      timeout=args.timeout, api_key=providers.api_key(dove[model])))
    metodi = [m for m in "SABC" if m in args.metodi.upper()] if args.metodi != "ABC" else list("SABC")
    rec = recorder(args, "incidenti", {"scala": args.scala, "dataset": args.dataset, "n": args.n, "metodi": "".join(metodi), "campione": args.campione,
                                       "judge": not args.no_judge, "max_tokens": args.max_tokens,
                                       "modelli": modelli})
    if args.backend == "local":
        import history
        rec.rec["model"] = history.model_info("local", ",".join(modelli), dove if len(modelli) > 1 else dove[modelli[0]])
        rec._flush()
    res = []

    def emit(ev):
        rec.add(ev)
        t = ev["type"]
        if t == "truth":
            s = ev["stats"]["_totale"]
            print(f"Corpus '{ev['scala']}': {len(ev['stats']) - 1} documenti, {s['righe']} righe, {s['kb']} KB, "
                  f"~{s['token_stimati']} token. {ev['incidenti']} incidenti -> {ev['campi']} campi.")
            if ev.get("dataset"):
                print(f"Dataset '{ev['dataset']}': {'con' if ev['etichettato'] else 'SENZA'} truth.json"
                      + ("" if ev["etichettato"] else " -> nessun punteggio, solo output e validatori"))
            else:
                print(f"Incidenti pescati a caso da un pool di {ev['pool']}; run ripetibile con --campione {ev['campione']}")
            for pr in ev.get("prove", []):
                print(f"  prova {pr['prova']}: estrazione {pr['campione']} -> {', '.join(pr['ids'])}")
            print(f"Trappole: {ev['trappole']}")
            print(f"Modelli in parallelo: {', '.join(ev['modelli'])}")
        elif t == "llm_call" and args.raw:
            print(f"    {C['dim']}[{ev['model']} {ev['method']} #{ev['n']} {ev['name']}] {ev['latency']}s "
                  f"{ev.get('completion_tokens')} tok -> {str(ev.get('response'))[:120]}{C['end']}")
        elif t == "result":
            res.append(ev)
            lat = ev["latenza"]
            if ev.get("non_valutato"):
                print(f"  [{ev['model']}] {ev['method']}#{ev['i']}: {len(ev['output'] or [])} incidenti in output, "
                      f"{ev['validatori']['segnalate']} segnalati dai validatori | {ev['sec']:.0f}s | {ev['tok']} tok"
                      + (f"\n    {C['red']}{ev['error'][:200]}{C['end']}" if ev["error"] else ""))
                return
            col = "grn" if ev["esatto"] else "red"
            print(f"  [{ev['model']}] {ev['method']}#{ev['i']}: {C[col]}{'ESATTO' if ev['esatto'] else 'ERRATO'}{C['end']} "
                  f"det {ev['pct_det']}% | categoria {ev['pct_sem']}% | derivati {ev['pct_der']}% | "
                  f"incidenti {ev['incidenti_ok']}/{ev['incidenti_tot']} | {ev['sec']:.0f}s | {ev['tok']} tok | "
                  f"{ev['calls']} chiamate" + (f" | latenza media {lat['media']}s p95 {lat['p95']}s {lat['tok_s']} tok/s" if lat.get("n") else ""))
            if ev["error"]:
                print(f"    {C['red']}{ev['error'][:200]}{C['end']}")

    try:
        I.run_benchmark(make, scala=args.scala, n=args.n, metodi=metodi, judge=not args.no_judge,
                        campione=args.campione, emit=emit, modelli=modelli, dataset=args.dataset)
    except ValueError as e:     # dataset non valido, mock senza etichette...
        print(f"{C['red']}Errore: {e}{C['end']}")
        rec.save("error")
        return
    except KeyboardInterrupt:   # Ctrl+C: le prove gia' finite restano nello storico
        print(f"\n{C['red']}Fermato con Ctrl+C.{C['end']} Run salvato nello storico: data/runs/{rec.save('stopped')}.json")
        return
    titolo("BENCHMARK")
    print(f"{'modello':28} {'met':4} {'det%':>6} {'cat%':>6} {'der%':>6} {'esatto':>7} {'tempo':>8} {'token':>8} {'lat.media':>10}")
    if res and res[0].get("non_valutato"):
        out = DATA / f"output_{args.dataset}.json"
        DATA.mkdir(exist_ok=True)
        out.write_text(json.dumps([{k: r[k] for k in ("model", "method", "i", "output", "validatori_problemi")}
                                   for r in res], indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Dataset senza etichette: output di ogni metodo salvati in {out.relative_to(ROOT)}")
        salvato(rec)
        return
    for r in res:
        print(f"{r['model'][:28]:28} {r['method']:4} {r['pct_det']:>6} {r['pct_sem']:>6} {r['pct_der']:>6} "
              f"{'si' if r['esatto'] else 'no':>7} {r['sec']:>7.0f}s {r['tok']:>8} {str(r['latenza'].get('media', '-')):>9}s")
    salvato(rec)


DEMOS = {"anatomia": (demo_anatomia, mock_anatomia), "agente": (demo_agente, mock_agente),
         "json": (demo_json, mock_json), "confronto": (demo_confronto, mock_confronto),
         "lunghi": (demo_lunghi, None), "incidenti": (demo_incidenti, None)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.system("")  # abilita i colori ANSI nella console Windows
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("demo", choices=list(DEMOS) + ["tutte", "esporta"],
                   help="esporta = scrive un corpus sintetico in datasets/<--dataset> come esempio da modificare")
    p.add_argument("--backend", choices=["mock", "local"], default="mock")
    p.add_argument("--model", default=os.environ.get("HARNESS_MODEL", "qwen/qwen3.6-35b-a3b"))
    p.add_argument("--base-url", default=os.environ.get("HARNESS_BASE_URL") or None,
                   help="uno o piu' server separati da virgola (default: i provider attivi in providers.json)")
    p.add_argument("--dataset", default=None,
                   help="incidenti: usa datasets/<nome>/ (i tuoi log, note, email) invece del corpus sintetico")
    p.add_argument("--raw", action="store_true", help="mostra le risposte grezze del server")
    p.add_argument("--righe", type=int, default=40, help="righe della fattura di prova")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="tetto di token per risposta (default: 8192, 24000 per confronto)")
    p.add_argument("--solo-b", action="store_true", help="confronto: salta il metodo A (lento)")
    p.add_argument("-n", type=int, default=None, help="numero di prove per 'confronto'")
    p.add_argument("--metodi", default="ABC", help="lunghi: quali metodi (es. BC)")
    p.add_argument("--chunk", type=int, default=10, help="lunghi: righe per blocco in C")
    p.add_argument("--no-judge", action="store_true", help="lunghi: C senza LLM-as-judge")
    p.add_argument("--judge-ab", action="store_true", help="lunghi: fai giudicare anche A e B")
    p.add_argument("--scala", choices=["piccolo", "medio", "enorme"], default="medio",
                   help="incidenti: dimensione del corpus")
    p.add_argument("--campione", type=int, default=None,
                   help="incidenti: quale estrazione dal pool di 100 (default: casuale a ogni run)")
    p.add_argument("--timeout", type=int, default=1800,
                   help="secondi massimi per una singola chiamata LLM (prompt enormi su PC remoti sono lenti)")
    p.add_argument("--modelli", default=None,
                   help="incidenti: piu' modelli separati da virgola, girano IN PARALLELO")
    args = p.parse_args()
    import providers
    args.base_url = args.base_url or providers.enabled_bases()
    single = args.base_url.split(",")[0].strip()          # demo con un solo modello: il suo server
    if args.backend == "local" and "," in args.base_url and args.demo not in ("incidenti", "esporta"):
        import webui
        single = webui.base_for({"base_url": args.base_url}, args.model)
    key = providers.api_key(single)
    if args.demo == "esporta":
        import incidenti as I
        d = I.esporta_dataset(args.dataset or "esempio", args.scala, campione=args.campione or 0)
        print(f"Corpus sintetico scritto in {d.relative_to(ROOT)} ({', '.join(sorted(f.name for f in d.iterdir()))})")
        return
    if args.max_tokens is None:
        args.max_tokens = 24000 if args.demo in ("confronto", "lunghi", "incidenti") else 8192
    if args.n is None:
        args.n = 10 if args.backend == "mock" and args.demo != "incidenti" else (
            1 if args.demo in ("lunghi", "incidenti") else 3)
    for name in (DEMOS if args.demo == "tutte" else [args.demo]):
        fn, mock = DEMOS[name]
        if name == "incidenti":
            fn(args)
            continue
        if name == "lunghi":
            import pipeline as P
            fn(P.mock_factory if args.backend == "mock" else
               (lambda truth: lambda: LocalLLM(args.model, single, max_tokens=args.max_tokens, api_key=key)), args)
            continue
        llm = MockLLM(mock) if args.backend == "mock" else LocalLLM(args.model, single, max_tokens=args.max_tokens, api_key=key)
        print(f"{C['dim']}backend={args.backend} model={llm.model}{C['end']}")
        fn(llm, args)


if __name__ == "__main__":
    main()
