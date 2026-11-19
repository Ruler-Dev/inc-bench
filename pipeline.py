"""
JSON LUNGHI - benchmark A / B / C, con validatori e LLM-as-judge.

Compito: da un'email d'ordine scritta "a mano" (formati misti, virgole decimali,
righe di rumore) compilare un JSON lungo: una voce per riga d'ordine (6 campi) + totali.

  A  "tutto al modello"    : 1 chiamata, il modello estrae E calcola tutto il JSON.
  B  "modello + Python"    : 1 chiamata, il modello estrae; Python calcola importi e totali.
  C  "pipeline multi-agente":
        Splitter (Python)        trova le righe d'ordine e le divide in blocchi
        Estrattori xK (LLM)      in PARALLELO, uno per blocco: JSON piccoli = meno errori
        Validatori (Python)      schema, completezza, ancoraggio al testo, regole di business
        Correttore (LLM)         rifa' SOLO le righe bocciate, ricevendo l'errore esatto
        Giudice (LLM-as-judge)   rilegge testo + dati e segnala dubbi; ogni modifica che
                                 propone deve comunque passare i validatori
        Calcolatore (Python)     importi, imponibile, IVA, totale

Principio: l'LLM fa cio' che solo lui sa fare (capire il testo); tutto cio' che si
puo' VERIFICARE lo verifica il codice. Il giudice LLM aiuta, ma e' fallibile anche lui:
il benchmark misura quanto spesso ha ragione.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal

import main as base
from harness import MockLLM, ask_json
from harness.schema import validate

CODE = re.compile(r"ART-\d{3}")
CENT = Decimal("0.01")


# ====================================================================== dati
def eur(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def pct(s: float) -> str:
    return f"{s:g}".replace(".", ",")


TEMPLATES = [
    lambda r, s: f"- {r['quantita']} pz {r['descrizione']} (cod. {r['codice']}) a {eur(r['prezzo_unitario'])} € cad." + (f" — sconto {pct(s)}%" if s else ""),
    lambda r, s: f"{r['codice']} {r['descrizione']}: {r['quantita']} unità, prezzo {eur(r['prezzo_unitario'])} €/pz" + (f", sconto {pct(s)}%" if s else ""),
    lambda r, s: f"Ci servono anche {r['quantita']} × {r['descrizione']} [{r['codice']}], listino € {eur(r['prezzo_unitario'])}" + (f" meno il {pct(s)}%" if s else ""),
    lambda r, s: f"{r['codice']} | {r['descrizione']} | qtà {r['quantita']} | € {eur(r['prezzo_unitario'])}" + (f" | -{pct(s)}%" if s else ""),
    lambda r, s: f"Per {r['descrizione']} ({r['codice']}) la quantità è {r['quantita']}, prezzo unitario {eur(r['prezzo_unitario'])} euro" + (f", applicate lo sconto del {pct(s)}%" if s else ""),
]
NOISE = [  # trappole: numeri e percentuali che NON sono righe d'ordine
    "(la riga sopra è urgente, se possibile consegna entro venerdì 14)",
    "NB: il 20% delle staffe dell'ultimo ordine era difettoso, controllate bene.",
    "Il magazziniere è disponibile dalle 8 alle 17, citofono 3.",
    "Come da telefonata di ieri, questi articoli vanno al cantiere 2.",
]


def importo(r: dict) -> float:
    v = (Decimal(str(r["quantita"])) * Decimal(str(r["prezzo_unitario"]))
         * (1 - Decimal(str(r["sconto_pct"])) / 100))
    return float(v.quantize(CENT, ROUND_HALF_UP))


def completa(righe: list[dict], iva_pct: int = 22) -> dict:
    """CALCOLATORE (Python): aggiunge importi e totali. Deterministico."""
    out = [{**r, "importo": importo(r)} for r in righe]
    imp = sum(Decimal(str(r["importo"])) for r in out)
    iva = (imp * iva_pct / 100).quantize(CENT, ROUND_HALF_UP)
    return {"righe": out, "imponibile": float(imp), "iva": float(iva), "totale": float(imp + iva)}


def genera_ordine(n: int, seed: int = 11) -> tuple[str, dict]:
    f = base.genera_fattura(n, seed)
    rng = random.Random(seed)
    righe = [{k: r[k] for k in ("codice", "descrizione", "quantita", "prezzo_unitario")}
             | {"sconto_pct": r["sconto_pct"]} for r in f["righe"]]
    lines = ["Oggetto: ordine materiale - cantiere Via Roma", "", "Buongiorno,",
             "vi inviamo l'ordine come da preventivo 2026/77 (acconto del 15% già versato).", ""]
    for r in righe:
        lines.append(rng.choice(TEMPLATES)(r, r["sconto_pct"]))
        if rng.random() < 0.12:
            lines.append(rng.choice(NOISE))
    lines += ["", "Grazie, cordiali saluti", "Ufficio acquisti - Officine Rossi S.r.l."]
    return "\n".join(lines), completa(righe)


# =================================================================== schemi
RIGA = {"type": "object", "properties": {
    "codice": {"type": "string"}, "descrizione": {"type": "string"},
    "quantita": {"type": "integer"}, "prezzo_unitario": {"type": "number"},
    "sconto_pct": {"type": "number"}},
    "required": ["codice", "descrizione", "quantita", "prezzo_unitario", "sconto_pct"],
    "additionalProperties": False}
RIGA_A = {**RIGA, "properties": {**RIGA["properties"], "importo": {"type": "number"}},
          "required": RIGA["required"] + ["importo"]}
SCHEMA_A = {"type": "object", "properties": {
    "righe": {"type": "array", "items": RIGA_A}, "imponibile": {"type": "number"},
    "iva": {"type": "number"}, "totale": {"type": "number"}},
    "required": ["righe", "imponibile", "iva", "totale"], "additionalProperties": False}
SCHEMA_B = {"type": "object", "properties": {"righe": {"type": "array", "items": RIGA}},
            "required": ["righe"], "additionalProperties": False}
SCHEMA_GIUDICE = {"type": "object", "properties": {"verdetti": {"type": "array", "items": {
    "type": "object", "properties": {"codice": {"type": "string"}, "ok": {"type": "boolean"},
                                     "problema": {"type": "string"}},
    "required": ["codice", "ok", "problema"], "additionalProperties": False}}},
    "required": ["verdetti"], "additionalProperties": False}

REGOLE = ("Regole: quantita = numero intero di pezzi; prezzo_unitario in euro (la virgola è il "
          "separatore decimale); sconto_pct = percentuale di sconto della riga (0 se non c'è). "
          "Ignora le righe che non sono articoli (note, acconti, orari).")


# =============================================================== validatori
def righe_sorgente(testo: str) -> dict[str, str]:
    """codice -> riga di testo che lo contiene (Python, non LLM)."""
    return {m.group(0): line for line in testo.splitlines() for m in [CODE.search(line)] if m}


def _num_in(n: str, line: str) -> bool:
    # il numero deve comparire "intero": non dentro 103,33 / ART-033 / 3x1.5 ...
    # (una virgola conta come decimale solo se seguita/preceduta da una cifra)
    return re.search(rf"(?<![\d.\-])(?<!\d,){re.escape(n)}(?!\d|[,.]\d)", line) is not None


def valida_schema(righe: list, schema: dict) -> list[dict]:
    return [{"codice": "-", "campo": "schema", "msg": e} for e in validate({"righe": righe}, {
        "type": "object", "properties": {"righe": {"type": "array", "items": schema}}})]


def valida_completezza(righe: list, attesi: list[str]) -> list[dict]:
    prob, visti = [], {}
    for r in righe:
        visti[r.get("codice")] = visti.get(r.get("codice"), 0) + 1
    for c in attesi:
        if c not in visti:
            prob.append({"codice": c, "campo": "riga", "msg": "riga MANCANTE"})
    for c, k in visti.items():
        if c not in attesi:
            prob.append({"codice": c, "campo": "riga", "msg": "riga INVENTATA (codice non presente nel testo)"})
        elif k > 1:
            prob.append({"codice": c, "campo": "riga", "msg": f"riga duplicata ({k} volte)"})
    return prob


def valida_ancoraggio(r: dict, line: str) -> list[dict]:
    """Ogni valore estratto deve comparire nella riga sorgente. Niente da inventare."""
    c, p = r.get("codice"), []
    if not line:
        return p
    if str(r.get("descrizione", "")).casefold().strip() not in line.casefold():
        p.append({"codice": c, "campo": "descrizione", "msg": f"'{r.get('descrizione')}' non compare nel testo"})
    if not _num_in(str(r.get("quantita")), line):
        p.append({"codice": c, "campo": "quantita", "msg": f"{r.get('quantita')} non compare nel testo"})
    try:
        if not _num_in(eur(float(r.get("prezzo_unitario"))), line):
            p.append({"codice": c, "campo": "prezzo_unitario",
                      "msg": f"{eur(float(r['prezzo_unitario']))} non compare nel testo"})
    except (TypeError, ValueError):
        p.append({"codice": c, "campo": "prezzo_unitario", "msg": "non numerico"})
    sconti = [float(x.replace(",", ".")) for x in re.findall(r"(\d+(?:,\d+)?)\s*%", line)]
    atteso = sconti[0] if sconti else 0.0
    try:
        if abs(float(r.get("sconto_pct")) - atteso) > 1e-9:
            p.append({"codice": c, "campo": "sconto_pct",
                      "msg": f"{r.get('sconto_pct')} ma nel testo lo sconto è {pct(atteso)}"})
    except (TypeError, ValueError):
        p.append({"codice": c, "campo": "sconto_pct", "msg": "non numerico"})
    return p


def valida_regole(r: dict) -> list[dict]:
    c, p = r.get("codice"), []
    q, pu, s = r.get("quantita"), r.get("prezzo_unitario"), r.get("sconto_pct")
    if isinstance(q, (int, float)) and q <= 0:
        p.append({"codice": c, "campo": "quantita", "msg": "deve essere > 0"})
    if isinstance(pu, (int, float)) and pu <= 0:
        p.append({"codice": c, "campo": "prezzo_unitario", "msg": "deve essere > 0"})
    if isinstance(s, (int, float)) and not 0 <= s < 100:
        p.append({"codice": c, "campo": "sconto_pct", "msg": "fuori da [0, 100)"})
    return p


def valida_totali(out: dict) -> list[dict]:
    """Per l'output di A: gli importi e i totali sono coerenti con le righe?"""
    p = []
    for r in out.get("righe", []):
        try:
            if abs(r.get("importo", 0) - importo(r)) > 0.005:
                p.append({"codice": r.get("codice"), "campo": "importo",
                          "msg": f"{r.get('importo')} ma quantita*prezzo*(1-sconto) = {importo(r)}"})
        except (TypeError, ValueError, KeyError):
            pass
    try:
        ok = completa([{k: r[k] for k in RIGA["required"]} for r in out["righe"]])
        for k in ("imponibile", "iva", "totale"):
            if abs(out[k] - ok[k]) > 0.005:
                p.append({"codice": "TOTALI", "campo": k, "msg": f"{out[k]} ma dalle righe risulta {ok[k]}"})
    except (TypeError, ValueError, KeyError):
        pass
    return p


VALIDATORI = ["schema", "completezza", "ancoraggio al testo", "regole di business", "coerenza totali"]


def valida_tutto(out: dict, src: dict[str, str], attesi: list[str]) -> list[dict]:
    righe = out.get("righe", []) if out else []
    schema = RIGA_A if out and "imponibile" in out else RIGA
    prob = valida_schema(righe, schema) + valida_completezza(righe, attesi)
    for r in righe:
        prob += valida_ancoraggio(r, src.get(r.get("codice"), "")) + valida_regole(r)
    if out and "imponibile" in out:
        prob += valida_totali(out)
    return prob


# ================================================================== agenti
class Conta:
    """Contatore thread-safe di chiamate LLM e token."""

    def __init__(self):
        self.lock, self.calls, self.tok = threading.Lock(), 0, 0

    def add(self, llm):
        with self.lock:
            self.calls += 1
            self.tok += ((llm.last_response or {}).get("usage") or {}).get("completion_tokens", 0)


def estrai(llm, lines: list[str], attesi: list[str], conta: Conta) -> list[dict]:
    r = ask_json(llm, "Estrai le righe d'ordine da questo blocco di email.\n" + REGOLE +
                 f"\nCodici attesi ({len(attesi)}): {', '.join(attesi)}\n\nBLOCCO:\n" + "\n".join(lines),
                 SCHEMA_B, name="estrazione_blocco", max_retries=0,
                 system="Sei un agente ESTRATTORE: copi i dati, non fai calcoli.")
    conta.add(llm)
    return (r["data"] or {}).get("righe", [])


def correggi(llm, riga_src: str, vecchia: dict, problemi: list[str], conta: Conta) -> dict | None:
    r = ask_json(llm, "Hai estratto questa riga d'ordine in modo sbagliato.\n" + REGOLE +
                 f"\nRiga sorgente: {riga_src}\nEstrazione precedente: {json.dumps(vecchia, ensure_ascii=False)}\n"
                 "Problemi trovati dai validatori:\n- " + "\n- ".join(problemi) +
                 "\nEstrai di nuovo la riga, correttamente.",
                 RIGA, name="correzione_riga", max_retries=0,
                 system="Sei un agente CORRETTORE: sistemi solo la riga indicata.")
    conta.add(llm)
    return r["data"]


def giudica(llm, lines: list[str], righe: list[dict], conta: Conta) -> dict[str, str]:
    """LLM-as-judge: ritorna {codice: problema} per le righe che ritiene sbagliate."""
    r = ask_json(llm, "Sei il revisore. Confronta ogni riga estratta con il testo sorgente e "
                 "segnala SOLO errori reali (valore diverso dal testo, sconto sbagliato, riga confusa).\n"
                 + REGOLE + "\n\nTESTO SORGENTE:\n" + "\n".join(lines) +
                 "\n\nDATI ESTRATTI:\n" + json.dumps(righe, ensure_ascii=False),
                 SCHEMA_GIUDICE, name="giudice", max_retries=0,
                 system="Sei un agente GIUDICE (LLM-as-judge): valuti, non estrai.")
    conta.add(llm)
    return {v["codice"]: v["problema"] for v in (r["data"] or {}).get("verdetti", []) if not v["ok"]}


# ================================================================== metodi
def metodo_A(factory, testo, emit, **_):
    llm, conta = factory(), Conta()
    r = ask_json(llm, "Questa è un'email d'ordine. Compila il JSON con TUTTE le righe d'ordine "
                 "(una per ogni codice ART-xxx), calcola importo = quantita*prezzo_unitario*(1-sconto_pct/100) "
                 "arrotondato al centesimo, imponibile = somma degli importi, iva = 22% dell'imponibile, "
                 "totale = imponibile + iva.\n" + REGOLE + "\n\nEMAIL:\n" + testo,
                 SCHEMA_A, name="ordine_completo", max_retries=0)
    conta.add(llm)
    return r["data"], conta, {}


def metodo_B(factory, testo, emit, **_):
    llm, conta = factory(), Conta()
    r = ask_json(llm, "Questa è un'email d'ordine. Estrai TUTTE le righe d'ordine (una per ogni "
                 "codice ART-xxx). Non fare calcoli.\n" + REGOLE + "\n\nEMAIL:\n" + testo,
                 SCHEMA_B, name="estrazione", max_retries=0)
    conta.add(llm)
    righe = (r["data"] or {}).get("righe", [])
    try:
        return completa(righe), conta, {}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return {"righe": righe}, conta, {}


def metodo_C(factory, testo, emit, chunk=10, judge=True, workers=4, max_corr=2, **_):
    conta = Conta()
    src = righe_sorgente(testo)                                   # SPLITTER (Python)
    codici = list(src)
    blocchi = [codici[i:i + chunk] for i in range(0, len(codici), chunk)]
    emit({"type": "agent", "role": "splitter", "msg": f"{len(codici)} righe d'ordine trovate "
          f"con una regex, divise in {len(blocchi)} blocchi da max {chunk}", "blocchi": len(blocchi)})
    info = {"correzioni": 0, "non_verificate": [], "giudice_flag": {}, "giudice_accettate": 0,
            "visto_dal_giudice": {}}
    ilock = threading.Lock()

    def lavora(k: int, attesi: list[str]) -> list[dict]:
        llm = factory()
        lines = [src[c] for c in attesi]
        emit({"type": "agent", "role": "estrattore", "blocco": k, "stato": "start"})
        righe = estrai(llm, lines, attesi, conta)                 # ESTRATTORE (LLM)
        # tieni solo i codici attesi, una volta ciascuno; le mancanti le chiede il correttore
        per = {}
        for r in righe:
            if isinstance(r, dict) and r.get("codice") in attesi and r["codice"] not in per:
                per[r["codice"]] = r

        def problemi(c):                                         # VALIDATORI (Python)
            r = per.get(c)
            if r is None:
                return ["riga MANCANTE nell'estrazione"]
            return [f"{p['campo']}: {p['msg']}" for p in
                    valida_schema([r], RIGA) + valida_ancoraggio(r, src[c]) + valida_regole(r)]

        bocciate = {c: problemi(c) for c in attesi}
        bocciate = {c: p for c, p in bocciate.items() if p}
        emit({"type": "agent", "role": "validatore", "blocco": k,
              "bocciate": [{"codice": c, "problemi": p} for c, p in bocciate.items()]})
        for c, probs in bocciate.items():                         # CORRETTORE (LLM)
            for t in range(max_corr):
                nuova = correggi(llm, src[c], per.get(c, {}), probs, conta)
                with ilock:
                    info["correzioni"] += 1
                if nuova:
                    per[c] = nuova
                probs = problemi(c)
                emit({"type": "agent", "role": "correttore", "blocco": k, "codice": c,
                      "tentativo": t + 1, "ok": not probs, "problemi": probs})
                if not probs:
                    break
            if probs:
                with ilock:
                    info["non_verificate"].append(c)
        if judge:                                                 # GIUDICE (LLM-as-judge)
            flag = giudica(llm, lines, [per[c] for c in attesi if c in per], conta)
            with ilock:
                info["giudice_flag"].update(flag)
                info["visto_dal_giudice"].update({c: dict(per[c]) for c in attesi if c in per})
            emit({"type": "agent", "role": "giudice", "blocco": k,
                  "flag": [{"codice": c, "problema": p} for c, p in flag.items()]})
            for c, motivo in flag.items():
                if c not in attesi:
                    continue
                nuova = correggi(llm, src[c], per.get(c, {}), [f"il revisore dice: {motivo}"], conta)
                # la modifica proposta dal giudice si accetta SOLO se passa i validatori
                accettata = bool(nuova) and nuova != per.get(c) and not (
                    valida_schema([nuova], RIGA) + valida_ancoraggio(nuova, src[c]) + valida_regole(nuova))
                if accettata:
                    per[c] = nuova
                    with ilock:
                        info["giudice_accettate"] += 1
                        # ora passa i validatori: non e' piu' "non verificata"
                        if c in info["non_verificate"]:
                            info["non_verificate"].remove(c)
                emit({"type": "agent", "role": "correttore", "blocco": k, "codice": c,
                      "da_giudice": True, "ok": accettata,
                      "problemi": [] if accettata else ["modifica respinta o invariata: resta la versione validata"]})
        emit({"type": "agent", "role": "estrattore", "blocco": k, "stato": "fatto",
              "non_verificate": [c for c in attesi if c in info["non_verificate"]]})
        return [per[c] for c in attesi if c in per]

    with ThreadPoolExecutor(max_workers=workers) as ex:          # estrattori in PARALLELO
        risultati = list(ex.map(lambda kb: lavora(*kb), enumerate(blocchi)))
    righe = [r for blocco in risultati for r in blocco]
    try:
        out = completa(righe)                                    # CALCOLATORE (Python)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        out = {"righe": righe}
    emit({"type": "agent", "role": "calcolatore", "msg": "importi, imponibile, IVA e totale calcolati in Python"})
    return out, conta, info


METODI = {"A": metodo_A, "B": metodo_B, "C": metodo_C}


# ============================================================= valutazione
def punteggio(out: dict | None, truth: dict) -> dict:
    """Confronto campo per campo con la verita' (che i metodi NON vedono)."""
    tr = {r["codice"]: r for r in truth["righe"]}
    got: dict[str, dict] = {}
    inventate = []
    for r in (out or {}).get("righe", []) or []:
        c = r.get("codice") if isinstance(r, dict) else None
        if c in tr and c not in got:
            got[c] = r
        else:
            inventate.append(c)
    campi = ("descrizione", "quantita", "prezzo_unitario", "sconto_pct", "importo")
    ok_campi, errori, sbagliate = 0, [], set()
    for c, t in tr.items():
        g = got.get(c)
        if g is None:
            sbagliate.add(c)
            errori.append(f"{c}: riga mancante")
            continue
        for f in campi:
            gv, tv = g.get(f), t[f]
            if f == "descrizione":
                ok = str(gv).casefold().strip() == tv.casefold()
            else:
                ok = isinstance(gv, (int, float)) and abs(gv - tv) < 0.005
            ok_campi += ok
            if not ok:
                sbagliate.add(c)
                errori.append(f"{c}.{f}: {gv!r} invece di {tv!r}")
    tot_ok = 0
    for k in ("imponibile", "iva", "totale"):
        v = (out or {}).get(k)
        ok = isinstance(v, (int, float)) and abs(v - truth[k]) < 0.005
        tot_ok += ok
        if not ok:
            errori.append(f"{k}: {v!r} invece di {truth[k]!r}")
    n_campi = len(tr) * len(campi) + 3
    return {"campi_ok": ok_campi + tot_ok, "campi_tot": n_campi,
            "pct": round(100 * (ok_campi + tot_ok) / n_campi, 2),
            "righe_ok": len(tr) - len(sbagliate), "righe_tot": len(tr),
            "mancanti": len(tr) - len(got), "inventate": len(inventate),
            "totali_ok": tot_ok == 3, "esatto": not errori and not inventate,
            "errori": errori[:25], "n_errori": len(errori), "sbagliate": sorted(sbagliate)}


def rilevazione(segnalate: set, sbagliate: set) -> dict:
    """Quanto e' bravo un controllore (validatori o giudice) a trovare le righe sbagliate."""
    tp, fp, fn = len(segnalate & sbagliate), len(segnalate - sbagliate), len(sbagliate - segnalate)
    return {"segnalate": len(segnalate), "veri_pos": tp, "falsi_pos": fp, "mancati": fn}


# ================================================================ benchmark
def run_benchmark(make_factory, righe=40, n=1, metodi=("A", "B", "C"), chunk=10,
                  judge=True, judge_ab=False, emit=print):
    """make_factory(truth) -> factory() -> llm. Emette eventi dict con emit()."""
    testo, truth = genera_ordine(righe)
    src = righe_sorgente(testo)
    base_factory = make_factory(truth)
    corrente = {"method": None, "i": 0}

    def factory():  # ogni chiamata LLM viene registrata (prompt, risposta, latenza)
        llm = base_factory()
        llm.on_call = lambda rec: emit({**rec, **corrente})
        return llm
    emit({"type": "truth", "righe": righe, "testo": testo, "imponibile": truth["imponibile"],
          "iva": truth["iva"], "totale": truth["totale"], "campi": righe * 5 + 3,
          "simulated": isinstance(factory(), MockLLM), "validatori": VALIDATORI})
    for m in metodi:
        for i in range(n):
            corrente.update(method=m, i=i + 1)
            emit({"type": "start", "method": m, "i": i + 1})
            t0 = time.perf_counter()
            try:
                out, conta, info = METODI[m](factory, testo, emit, chunk=chunk, judge=judge)
                errore = None
            except Exception as e:  # es. server che cade: il benchmark continua
                out, conta, info, errore = None, Conta(), {}, f"{type(e).__name__}: {e}"
            sec = time.perf_counter() - t0                    # TEMPO DI COMPLETAMENTO del task
            sc = punteggio(out, truth)
            sbagliate = set(sc["sbagliate"])
            # i validatori girano su TUTTI gli output (anche A e B): non conoscono la verita'
            prob = valida_tutto(out or {}, src, list(src))
            seg_val = {p["codice"] for p in prob if p["codice"] in src}
            ev = {"type": "result", "method": m, "i": i + 1, "sec": sec, "tok": conta.tok,
                  "calls": conta.calls, "error": errore, **sc,
                  "validatori": rilevazione(seg_val, sbagliate),
                  "validatori_problemi": [f"{p['codice']}.{p['campo']}: {p['msg']}" for p in prob][:25],
                  "validatori_totali": any(p["codice"] == "TOTALI" for p in prob)}
            if m == "C":
                ev["correzioni"] = info.get("correzioni", 0)
                ev["non_verificate"] = info.get("non_verificate", [])
                ev["giudice_accettate"] = info.get("giudice_accettate", 0)
                if judge:
                    # il giudice ha visto le righe GIA' validate: misuriamo i suoi flag
                    # rispetto alla verita' di quelle stesse righe
                    tr = {r["codice"]: r for r in truth["righe"]}
                    viste = info.get("visto_dal_giudice", {})
                    sbagliate_viste = {c for c, r in viste.items()
                                       if any(r.get(k) != tr[c][k] for k in RIGA["required"])}
                    ev["giudice"] = rilevazione(set(info.get("giudice_flag", {})) & set(viste),
                                                sbagliate_viste)
            elif judge_ab and out and out.get("righe"):
                t1 = time.perf_counter()
                conta_j, flag = Conta(), {}
                codici = list(src)
                per = {r.get("codice"): r for r in out["righe"] if isinstance(r, dict)}
                for k in range(0, len(codici), chunk):
                    blocco = codici[k:k + chunk]
                    flag |= giudica(factory(), [src[c] for c in blocco],
                                    [per[c] for c in blocco if c in per], conta_j)
                ev["giudice"] = rilevazione(set(flag) & set(src), sbagliate)
                ev["giudice_sec"] = time.perf_counter() - t1
            emit(ev)
    emit({"type": "done"})


# ===================================================================== mock
def mock_factory(truth: dict, seed: int = 3):
    """Modello FINTO con errori SIMULATI tipici dei JSON lunghi. Solo per provare la UI senza GPU."""
    tr = {r["codice"]: r for r in truth["righe"]}
    rng = random.Random(seed)
    lock = threading.Lock()

    def sbaglia(r: dict) -> dict:
        r = dict(r)
        campo = rng.choice(["quantita", "prezzo_unitario", "sconto_pct", "sconto_pct"])
        if campo == "quantita":
            r["quantita"] += rng.choice([-1, 1, 10])
        elif campo == "prezzo_unitario":
            s = f"{r['prezzo_unitario']:.2f}"
            r["prezzo_unitario"] = float(s[:-2] + s[-1] + s[-2])
        else:
            r["sconto_pct"] = rng.choice([15, 20]) if r["sconto_pct"] == 0 else 0
        return r

    def base_riga(c):
        return {k: tr[c][k] for k in RIGA["required"]}

    def righe(codici, p_err, p_skip):
        out = []
        for c in codici:
            x = rng.random()
            if x < p_skip:
                continue
            out.append(sbaglia(base_riga(c)) if x < p_skip + p_err else base_riga(c))
        return out

    def policy(messages, tools, rf):
        with lock:
            name = (rf or {}).get("json_schema", {}).get("name")
            prompt = messages[-1]["content"]
            codici = list(dict.fromkeys(CODE.findall(prompt)))
            if name == "ordine_completo":
                rr = righe(codici, 0.10, 0.03)
                for r in rr:
                    r["importo"] = importo(r) + (rng.choice([0.01, -0.1, 1]) if rng.random() < 0.06 else 0)
                tot = completa([{k: r[k] for k in RIGA["required"]} for r in rr])
                if rng.random() < 0.4:
                    tot["imponibile"] = round(tot["imponibile"] + rng.choice([-0.02, 0.5, 10]), 2)
                data = {"righe": rr, "imponibile": tot["imponibile"], "iva": tot["iva"], "totale": tot["totale"]}
            elif name == "estrazione":
                data = {"righe": righe(codici, 0.04, 0.02)}
            elif name == "estrazione_blocco":
                data = {"righe": righe(codici[:len(codici)], 0.07, 0.02)}
            elif name == "correzione_riga":
                c = codici[0]
                data = base_riga(c) if rng.random() < 0.95 else sbaglia(base_riga(c))
            elif name == "giudice":
                est = json.loads(prompt.split("DATI ESTRATTI:\n", 1)[1])
                verdetti = []
                for r in est:
                    giusta = all(r.get(k) == tr[r["codice"]][k] for k in RIGA["required"])
                    ok = (rng.random() > 0.06) if giusta else (rng.random() > 0.85)
                    verdetti.append({"codice": r["codice"], "ok": ok,
                                     "problema": "" if ok else "lo sconto non sembra corrispondere"})
                data = {"verdetti": verdetti}
            else:
                data = {}
            return {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}

    return lambda: MockLLM(policy)
