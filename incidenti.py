"""
BENCHMARK SOC - classificazione di incidenti di cybersecurity da un CORPUS di documenti enormi.

Il corpus (sintetico, generato con un seed -> verita' nota):
  siem_export.log      migliaia di eventi SIEM (righe chiave=valore), incidenti mescolati a rumore
  edr_alerts.jsonl     alert EDR con hash dei file
  threat_intel.txt     hash -> famiglia di malware / tipo
  asset_inventory.csv  host -> criticita'
  analyst_notes.md     note in linguaggio naturale dei turni SOC (parafrasi, trappole, aggiornamenti)
  email_thread.txt     email tra SOC, red team, IT, management (riclassificazioni, falsi positivi)
  policy.md            come si calcolano i campi, severita' e MITRE

Output richiesto: JSON con un oggetto per incidente, 9 campi valutati, divisi per TIPO:
  deterministici (6)  hosts, src_ips, event_count, first_seen, last_seen, hashes   -> si calcolano dai log
  semantici (1)       category                                                    -> serve capire il testo
  derivati (2)        severity, mitre                                             -> regola fissa su category + inventario

Metodi:
  S  solo script      regex + regole; per la categoria usa parole chiave (euristica)
  A  solo LLM         tutto il corpus in un prompt, il modello produce tutto il JSON
  B  LLM per incidente  lo script recupera i testi di ogni incidente, il modello compila TUTTI i campi
  C  ibrido multi-agente  lo script calcola i campi deterministici (100%), l'LLM classifica SOLO la
                      categoria con una citazione, validatori + correttore + giudice, lo script deriva
                      severita' e MITRE
"""
from __future__ import annotations

import csv
import io
import json
import random
import re
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from harness import MockLLM, ask_json

# ================================================================ dominio
CATS = ["phishing", "malware", "ransomware", "brute_force", "data_exfiltration",
        "privilege_escalation", "lateral_movement", "ddos", "insider_threat", "false_positive"]
MITRE = {"phishing": "T1566", "malware": "T1204", "ransomware": "T1486", "brute_force": "T1110",
         "data_exfiltration": "T1048", "privilege_escalation": "T1068", "lateral_movement": "T1021",
         "ddos": "T1498", "insider_threat": "T1078", "false_positive": "N/A"}
BASE_SEV = {"phishing": "medium", "malware": "medium", "ransomware": "critical", "brute_force": "medium",
            "data_exfiltration": "high", "privilege_escalation": "high", "lateral_movement": "high",
            "ddos": "medium", "insider_threat": "high", "false_positive": "informational"}
LEVELS = ["informational", "low", "medium", "high", "critical"]
GB = 1024 ** 3

DET = ["hosts", "src_ips", "event_count", "first_seen", "last_seen", "hashes"]
SEM = ["category"]
DER = ["severity", "mitre"]

SCALE = {"piccolo": (6, 120, 15), "medio": (20, 900, 60), "enorme": (50, 5000, 300)}

# regole SIEM: volutamente ambigue (molte generiche), la categoria NON si deduce solo da qui
RULES = {
    "phishing": ["MAIL_SUSPICIOUS_LINK", "URL_CLICK", "AUTH_NEW_LOCATION", "SUSPICIOUS_ACTIVITY"],
    "malware": ["PROCESS_ANOMALY", "BEACONING", "DNS_RARE_DOMAIN", "SUSPICIOUS_ACTIVITY"],
    "ransomware": ["FILE_RENAME_BURST", "PROCESS_ANOMALY", "SMB_WRITE_SPIKE", "SUSPICIOUS_ACTIVITY"],
    "brute_force": ["AUTH_FAIL", "AUTH_FAIL", "ACCOUNT_LOCKOUT", "SUSPICIOUS_ACTIVITY"],
    "data_exfiltration": ["LARGE_UPLOAD", "DNS_RARE_DOMAIN", "SUSPICIOUS_ACTIVITY", "PROXY_UNCATEGORIZED"],
    "privilege_escalation": ["PRIV_CHANGE", "PROCESS_ANOMALY", "SUSPICIOUS_ACTIVITY"],
    "lateral_movement": ["RDP_INTERNAL", "SMB_ADMIN_SHARE", "AUTH_NEW_LOCATION", "SUSPICIOUS_ACTIVITY"],
    "ddos": ["TRAFFIC_SPIKE", "SYN_FLOOD", "SUSPICIOUS_ACTIVITY"],
    "insider_threat": ["USB_MASS_COPY", "LARGE_UPLOAD", "AUTH_OFF_HOURS", "SUSPICIOUS_ACTIVITY"],
}
NOISE_RULES = ["LOGIN_OK", "DNS_QUERY", "HTTP_200", "VPN_CONNECT", "PATCH_INSTALLED", "AV_UPDATE",
               "BACKUP_OK", "PRINT_JOB", "AUTH_FAIL", "SUSPICIOUS_ACTIVITY"]
USERS = ["m.rossi", "l.bianchi", "g.verdi", "a.russo", "f.ferrari", "s.esposito", "c.romano",
         "p.colombo", "e.ricci", "svc_backup", "svc_sql", "admin"]

# descrizioni in linguaggio naturale: (testo, contiene_parola_chiave)
DESCR = {
    "phishing": [("l'utente {user} ha ricevuto una mail che imitava la banca e ha inserito le credenziali sul sito clone", False),
                 ("campagna di phishing verso l'ufficio acquisti, link a un dominio typosquatting", True),
                 ("un fornitore 'compromesso' ha mandato una fattura con link a una pagina di login fasulla, {user} ci è cascato", False)],
    "malware": [("su {host} gira un processo sconosciuto che contatta un server esterno ogni 60 secondi", False),
                ("infezione malware rilevata dall'EDR su {host}, probabile infostealer", True),
                ("una macro di un documento Office ha scaricato ed eseguito un binario non firmato su {host}", False)],
    "ransomware": [("file rinominati con estensione .lock2 e nota di riscatto sul desktop di {host}", False),
                   ("attacco ransomware in corso, cifratura delle share di rete partita da {host}", True),
                   ("le share di {host} sono illeggibili, ogni cartella contiene LEGGIMI_PER_RECUPERARE.txt", False)],
    "brute_force": [("migliaia di tentativi di login falliti sull'account {user} dallo stesso IP in pochi minuti", False),
                    ("brute force SSH contro {host}", True),
                    ("l'account {user} si è bloccato 14 volte stanotte, sempre per password errata da IP esteri", False)],
    "data_exfiltration": [("trasferiti circa {gb} GB verso uno storage esterno mai visto prima, fuori orario", False),
                          ("esfiltrazione di dati via HTTPS verso un dominio sconosciuto da {host}", True),
                          ("da {host} escono flussi DNS molto lunghi verso lo stesso dominio, sembra tunneling di dati", False)],
    "privilege_escalation": [("l'utente {user} è passato da utente standard ad amministratore locale sfruttando un driver vulnerabile", False),
                             ("privilege escalation tramite exploit del kernel su {host}", True),
                             ("su {host} un servizio è stato riconfigurato per girare come SYSTEM da un utente senza diritti", False)],
    "lateral_movement": [("da {host} partono sessioni RDP e SMB verso altri server usando credenziali di servizio", False),
                         ("movimento laterale con PsExec a partire da {host}", True),
                         ("lo stesso account {user} si è autenticato su 9 server diversi in 4 minuti partendo da {host}", False)],
    "ddos": [("picco di traffico da migliaia di IP che satura il load balancer davanti a {host}", False),
             ("attacco DDoS volumetrico sul portale pubblico", True),
             ("il sito su {host} non risponde: arrivano 80.000 connessioni SYN al secondo da botnet", False)],
    "insider_threat": [("un dipendente in uscita ha copiato l'intero archivio clienti su una chiavetta personale", False),
                       ("minaccia interna: {user} scarica dati riservati prima delle dimissioni", True),
                       ("{user}, che lascia l'azienda venerdì, ha caricato 3 anni di offerte commerciali sul suo cloud personale", False)],
}
FP_REASONS = ["era la nostra attività di penetration test autorizzata (rif. CHG-{n}), potete chiuderlo come falso positivo",
              "la regola SIEM era configurata male dopo l'aggiornamento di ieri, nessuna attività malevola: falso positivo",
              "si tratta del test concordato con il fornitore (rif. CHG-{n}), nessun attacco reale"]
# euristica dello script S: le regole che scriverebbe un ingegnere SENZA aver letto i dati,
# cioe' solo i nomi delle categorie. Le parafrasi ("chiavetta", "file rinominati .lock2") non le conosce.
KEYWORDS = {
    "ransomware": "ransomware", "phishing": "phishing", "malware": "malware",
    "brute force": "brute_force", "esfiltrazione": "data_exfiltration",
    "privilege escalation": "privilege_escalation", "movimento laterale": "lateral_movement",
    "ddos": "ddos", "minaccia interna": "insider_threat", "falso positivo": "false_positive",
}


# regole per i casi di confine tra categorie: senza, alcuni incidenti sono ambigui anche per un umano
# (un dipendente che carica dati sul suo cloud e' insider o esfiltrazione?). Le leggono A, B e C.
REGOLE_CAT = ("Casi di confine: se chi porta fuori o copia i dati è un DIPENDENTE o un interno (anche in uscita "
              "dall'azienda), la categoria è insider_threat, anche se i dati escono verso un cloud o uno storage "
              "esterno; data_exfiltration solo quando il trasferimento è opera di un attaccante esterno o di un "
              "malware, senza un dipendente identificato come autore DALLE NOTE O DALLE EMAIL (il campo user= dei log SIEM "
              "è solo l'account registrato dalla regola, non indica chi ha portato fuori i dati). phishing = una persona viene ingannata "
              "(mail, link, pagina falsa); malware = un codice malevolo gira su un host senza inganno descritto.")


def severity(cat: str, hosts: list[str], crit: dict[str, str]) -> str:
    """POLICY: severita' base per categoria, +1 livello se un host coinvolto e' 'critical'."""
    s = BASE_SEV[cat]
    if cat != "false_positive" and any(crit.get(h) == "critical" for h in hosts):
        s = LEVELS[min(LEVELS.index(s) + 1, len(LEVELS) - 1)]
    return s


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ================================================================ corpus
def policy_md() -> str:
    """La policy del SOC: come si calcolano i campi. Vale anche per i dataset senza policy.md."""
    return ("# Policy di classificazione incidenti\n\n"
            "Campi per ogni incidente (identificato dal campo corr= nei log SIEM e nell'EDR):\n"
            "- hosts, src_ips: valori distinti dei campi host= e src= degli eventi SIEM con corr=<ID> (ordinati)\n"
            "- event_count: numero di eventi SIEM con corr=<ID> (le righe con corr=- NON contano, anche se citano l'ID nel msg)\n"
            "- first_seen / last_seen: timestamp minimo e massimo di quegli eventi\n"
            "- hashes: sha256 degli alert EDR con corr=<ID> (ordinati)\n"
            f"- category: una tra {', '.join(CATS)}. Vale l'informazione PIÙ RECENTE (aggiornamenti forensi, email che riportano fatti); domande o supposizioni ('mi risulta che...') non sono prove. "
            "Attività autorizzate, test o regole mal configurate = false_positive. " + REGOLE_CAT + "\n"
            "- severity: base per categoria " + json.dumps(BASE_SEV) +
            "; +1 livello se almeno uno degli hosts ha criticality=critical in asset_inventory.csv (max critical; false_positive resta informational)\n"
            "- mitre: " + json.dumps(MITRE) + "\n")


POOL = 100  # dataset fisso di incidenti: ogni run ne pesca un sottoinsieme a caso


def genera_corpus(scala: str = "medio", n_incidenti: int | None = None, seed: int = 42,
                  campione: int | None = 0) -> tuple[dict, dict]:
    """seed     -> il POOL di 100 incidenti (sempre lo stesso: e' il 'dataset')
    campione -> quali incidenti vengono pescati e come sono scritti note, email e rumore.
                None = estrazione casuale nuova a ogni chiamata."""
    n, noise_siem, noise_edr = SCALE[scala]
    n = n_incidenti or n
    if campione is None:
        campione = random.SystemRandom().randrange(1_000_000)
    rng = random.Random(seed)  # pool
    t0 = datetime(2026, 11, 3, 0, 0, 0)

    # --- inventario
    hosts = ([f"ws-{i:03d}" for i in range(1, 61)] + [f"srv-app-{i:02d}" for i in range(1, 11)] +
             ["dc-01", "dc-02", "srv-db-01", "srv-db-02", "fs-01", "web-01", "web-02", "mail-01"])
    crit = {}
    for h in hosts:
        crit[h] = ("critical" if h.startswith(("dc-", "srv-db", "fs-")) else
                   "high" if h.startswith(("srv-", "web-", "mail-")) else rng.choice(["low", "low", "medium"]))
    inv = io.StringIO()
    w = csv.writer(inv)
    w.writerow(["host", "owner", "criticality", "location"])
    for h in hosts:
        w.writerow([h, rng.choice(["IT", "Finance", "Sales", "HR", "R&D"]), crit[h], rng.choice(["Milano", "Torino", "Roma"])])

    # --- POOL di incidenti (verita'), 10 per categoria
    size = max(POOL, n)
    ids = sorted(rng.sample(range(100, 9999), size))
    cats = (CATS * (size // len(CATS) + 1))[:size]
    rng.shuffle(cats)
    pool, extra = [], {}
    ext_ips = lambda: f"{rng.choice(['203.0.113', '198.51.100', '192.0.2'])}.{rng.randint(2, 254)}"
    for i, cat in zip(ids, cats):
        iid = f"INC-{i:04d}"
        siem_i, edr_i, ti_i = [], [], []
        apparente = rng.choice([c for c in CATS if c not in ("false_positive", "ddos")]) if cat == "false_positive" else cat
        hs = rng.sample(hosts, rng.randint(1, 3 if cat != "lateral_movement" else 5))
        ips = ([f"10.0.{rng.randint(1, 9)}.{rng.randint(2, 254)}" for _ in range(rng.randint(1, 2))]
               if apparente in ("lateral_movement", "insider_threat", "privilege_escalation")
               else [ext_ips() for _ in range(rng.randint(1, 3))])
        start = t0 + timedelta(minutes=rng.randint(0, 60 * 46))
        k = rng.randint(3, 28)
        evs = []
        for _ in range(k):
            ts = start + timedelta(seconds=rng.randint(0, 3600 * rng.choice([1, 2, 6])))
            big = apparente in ("data_exfiltration", "insider_threat")
            evs.append({"ts": ts, "host": rng.choice(hs), "src": rng.choice(ips),
                        "rule": rng.choice(RULES[apparente]), "user": rng.choice(USERS),
                        "bytes": rng.randint(200_000_000, 900_000_000) if big else rng.randint(200, 90_000)})
        hashes = []
        if apparente in ("malware", "ransomware", "phishing") or (apparente == "privilege_escalation" and rng.random() < .5):
            for _ in range(rng.randint(1, 2)):
                hx = "%064x" % rng.getrandbits(256)
                hashes.append(hx)
                fam = {"ransomware": ("LockStorm", "ransomware"), "malware": ("RedLine-like", "infostealer"),
                       "phishing": ("QuickLoad", "loader")}.get(apparente, ("KernelPwn", "exploit"))
                ti_i.append(f"sha256={hx} famiglia={fam[0]} tipo={fam[1]} fonte=feed-{rng.choice(['A', 'B', 'C'])} confidenza={rng.randint(60, 99)}")
                edr_i.append({"ts": iso(start + timedelta(seconds=rng.randint(0, 3000))), "host": rng.choice(hs),
                              "sha256": hx, "process": rng.choice(["rundll32.exe", "powershell.exe", "invoice_pdf.exe", "svch0st.exe"]),
                              "verdict": "malicious", "corr": iid})
        for e in evs:
            siem_i.append((e["ts"], f"{iso(e['ts'])} sev={rng.randint(3, 9)} host={e['host']} src={e['src']} "
                           f"dst=10.0.0.{rng.randint(2, 254)} user={e['user']} rule={e['rule']} bytes_out={e['bytes']} "
                           f"corr={iid} msg=\"{e['rule'].lower().replace('_', ' ')}\""))
        hs_ev = sorted({e["host"] for e in evs})
        pool.append({
            "id": iid, "category": cat, "apparente": apparente,
            "hosts": hs_ev, "src_ips": sorted({e["src"] for e in evs}), "event_count": len(evs),
            "first_seen": iso(min(e["ts"] for e in evs)), "last_seen": iso(max(e["ts"] for e in evs)),
            "hashes": sorted(hashes), "severity": severity(cat, hs_ev, crit), "mitre": MITRE[cat],
            "bytes_out": sum(e["bytes"] for e in evs)})
        extra[iid] = (siem_i, edr_i, ti_i)

    # --- CAMPIONE: n incidenti a caso dal pool; da qui in poi tutto dipende dall'estrazione
    rng = random.Random(campione)
    incidents = sorted(rng.sample(pool, n), key=lambda x: x["id"])
    siem, edr, ti = [], [], []
    for inc in incidents:
        s_, e_, t_ = extra[inc["id"]]
        siem += s_
        edr += e_
        ti += t_

    # --- rumore SIEM, con trappole: righe che CITANO un incidente ma non gli appartengono (corr=-)
    for _ in range(noise_siem):
        ts = t0 + timedelta(seconds=rng.randint(0, 3600 * 48))
        trap = rng.random() < 0.06
        msg = (f"ticket {rng.choice(incidents)['id']} aggiornato dall'operatore" if trap else
               rng.choice(["ok", "user login", "query", "scheduled task", "health check"]))
        siem.append((ts, f"{iso(ts)} sev={rng.randint(1, 4)} host={rng.choice(hosts)} src=10.0.{rng.randint(1, 9)}.{rng.randint(2, 254)} "
                         f"dst=10.0.0.{rng.randint(2, 254)} user={rng.choice(USERS)} rule={rng.choice(NOISE_RULES)} "
                         f"bytes_out={rng.randint(100, 50_000)} corr=- msg=\"{msg}\""))
    siem.sort(key=lambda x: x[0])
    for _ in range(noise_edr):
        edr.append({"ts": iso(t0 + timedelta(seconds=rng.randint(0, 3600 * 48))), "host": rng.choice(hosts),
                    "sha256": "%064x" % rng.getrandbits(256), "process": rng.choice(["chrome.exe", "teams.exe", "excel.exe"]),
                    "verdict": "clean", "corr": None})
    edr.sort(key=lambda x: x["ts"])
    for _ in range(noise_edr // 2):
        ti.append(f"sha256={'%064x' % rng.getrandbits(256)} famiglia=- tipo=benign fonte=feed-A confidenza=10")
    rng.shuffle(ti)

    # --- note degli analisti (linguaggio naturale, trappole, aggiornamenti)
    notes = ["# Note SOC – turni 03-04/11/2026", "", "Formato libero, scritte durante i turni. Le note più recenti prevalgono.", ""]
    updates, emails = [], []
    order = incidents[:]
    rng.shuffle(order)
    for idx, inc in enumerate(order):
        d = rng.choice(DESCR[inc["apparente"]])[0].format(user=rng.choice(USERS), host=inc["hosts"][0],
                                                            gb=round(inc["bytes_out"] / GB, 1))
        if idx % 7 == 0:
            notes.append(f"## Turno {'notte' if idx % 2 else 'giorno'} – {rng.choice(['R. Bianchi', 'S. Neri', 'A. Gallo'])}")
            notes.append("")
        notes.append(f"{inc['id']}: {d}. Host coinvolti tra cui {inc['hosts'][0]}. In analisi.")
        notes.append("")
        # trappola: paragrafo che cita una categoria ma per NEGARLA, su un altro incidente
        if rng.random() < 0.25:
            other = rng.choice([x for x in incidents if x["id"] != inc["id"]])
            notes.append(f"Nota: {inc['id']} NON è collegato all'episodio di {other['apparente'].replace('_', ' ')} di "
                         f"{other['id']}, anche se gli orari coincidono.")
            notes.append("")
        # riclassificazione (circa 1 su 4): la verita' e' scritta piu' avanti
        if inc["category"] != "false_positive" and rng.random() < 0.25:
            gemelli = {"data_exfiltration": "insider_threat", "insider_threat": "data_exfiltration"}
            nuova = rng.choice([c for c in CATS if c not in (inc["category"], "false_positive",
                                                             gemelli.get(inc["category"]))])
            # la nota iniziale diventa fuorviante: descrive 'nuova', l'aggiornamento riporta la verita'
            notes[-2 if notes[-1] == "" else -1] = (f"{inc['id']}: " + rng.choice(DESCR[nuova])[0].format(
                user=rng.choice(USERS), host=inc["hosts"][0], gb=round(inc["bytes_out"] / GB, 1)) + ". In analisi.")
            vera = rng.choice(DESCR[inc["category"]])[0].format(user=rng.choice(USERS), host=inc["hosts"][0],
                                                                gb=round(inc["bytes_out"] / GB, 1))
            updates.append(f"AGGIORNAMENTO {rng.randint(10, 23)}:{rng.randint(10, 59)} su {inc['id']}: dopo l'analisi "
                           f"forense la prima ipotesi era sbagliata. In realtà {vera}. Riclassificare.")
        if inc["category"] == "false_positive":
            emails.append(("red-team@azienda.it" if "test" in FP_REASONS[0] else "it-ops@azienda.it",
                           f"Re: {inc['id']}",
                           f"Ciao SOC, riguardo {inc['id']}: " + rng.choice(FP_REASONS).format(n=rng.randint(1000, 9999)) + "."))
    notes += ["", "## Aggiornamenti successivi", ""] + [u + "\n" for u in updates]

    # --- email: chiacchiere, riepiloghi (anche sbagliati!), falsi positivi
    chat = [("ciso@azienda.it", "Riepilogo settimanale",
             "Situazione sotto controllo. Ricordo che il report va consegnato venerdì. "
             f"Mi risulta che {rng.choice(incidents)['id']} fosse phishing, confermate?"),
            ("helpdesk@azienda.it", "Stampante 3° piano", "La stampante del terzo piano è di nuovo offline."),
            ("soc-lead@azienda.it", "Turni", "Promemoria: dalla settimana prossima i turni notte passano a 10 ore.")]
    all_mail = emails + chat
    rng.shuffle(all_mail)
    mail_txt = "\n".join(f"-----\nDa: {fr}\nOggetto: {sub}\nData: {iso(t0 + timedelta(hours=rng.randint(30, 60)))}\n\n{body}\n"
                         for fr, sub, body in all_mail)

    policy = policy_md()

    docs = {"siem_export.log": "\n".join(line for _, line in siem),
            "edr_alerts.jsonl": "\n".join(json.dumps(e) for e in edr),
            "threat_intel.txt": "\n".join(ti),
            "asset_inventory.csv": inv.getvalue(),
            "analyst_notes.md": "\n".join(notes),
            "email_thread.txt": mail_txt,
            "policy.md": policy}
    truth = {"incidents": incidents, "criticality": crit, "campione": campione, "pool": len(pool)}
    return docs, truth


CHARS_PER_TOKEN = 1.8  # misurato: 196 KB di corpus = 112.594 token (log, hash e IP tokenizzano male)


def stats(docs: dict) -> dict:
    out = {k: {"righe": v.count("\n") + 1, "kb": round(len(v.encode()) / 1024, 1),
               "token_stimati": round(len(v) / CHARS_PER_TOKEN)} for k, v in docs.items()}
    out["_totale"] = {"righe": sum(x["righe"] for x in out.values()), "kb": round(sum(x["kb"] for x in out.values()), 1),
                      "token_stimati": sum(x["token_stimati"] for x in out.values())}
    return out


# ================================================================ dataset dell'utente
DATASETS = Path(__file__).parent / "datasets"
DOC_FILES = ["siem_export.log", "edr_alerts.jsonl", "threat_intel.txt", "asset_inventory.csv",
             "analyst_notes.md", "email_thread.txt", "policy.md"]
OBBLIGATORI = ["siem_export.log"]
NOME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_FILE = 50 * 1024 * 1024


def _cartella(nome: str) -> Path:
    if not NOME_OK.match(nome or "") or ".." in nome:
        raise ValueError(f"nome di dataset non valido: {nome!r} (lettere, numeri, _ . -)")
    return DATASETS / nome


def elenco_dataset() -> list[dict]:
    """I dataset in datasets/<nome>/ (quelli con almeno siem_export.log)."""
    out = []
    for d in sorted(DATASETS.glob("*/")) if DATASETS.exists() else []:
        if not (d / "siem_export.log").exists() or not NOME_OK.match(d.name):
            continue
        ids = {e.get("corr") for e in parse_siem({"siem_export.log": (d / "siem_export.log").read_text(encoding="utf-8")})}
        out.append({"nome": d.name, "file": [f for f in DOC_FILES if (d / f).exists()],
                    "etichettato": (d / "truth.json").exists(), "incidenti": len(ids - {None, "-"})})
    return out


def _completa_truth(raw: dict, docs: dict) -> dict:
    """truth.json puo' contenere solo id + category: il resto si ricava dai log e dalla policy."""
    det, crit = calcola_deterministici(docs), criticita(docs)
    incs = []
    for x in raw.get("incidents", []):
        iid, cat = x.get("id"), x.get("category")
        if iid not in det:
            raise ValueError(f"truth.json: {iid} non ha eventi SIEM con corr={iid}")
        if cat not in CATS:
            raise ValueError(f"truth.json: {iid} ha category={cat!r}, ammesse: {', '.join(CATS)}")
        d = det[iid]
        inc = {"id": iid, **{k: d[k] for k in DET}, "category": cat,
               "severity": severity(cat, d["hosts"], crit), "mitre": MITRE[cat]}
        inc.update({k: v for k, v in x.items() if k in DET + DER})   # valori espliciti vincono
        inc["apparente"] = x.get("apparente", cat)
        incs.append(inc)
    return {"incidents": sorted(incs, key=lambda i: i["id"]), "criticality": crit, "pool": 0, "etichettato": True}


def carica_dataset(nome: str) -> tuple[dict, dict]:
    """Legge datasets/<nome>/. Solo siem_export.log e' obbligatorio (con corr=<ID> sugli eventi);
    i file mancanti restano vuoti, policy.md mancante = policy predefinita.
    truth.json (facoltativo) abilita il punteggio; senza, i risultati non vengono valutati."""
    d = _cartella(nome)
    if not d.is_dir():
        raise ValueError(f"dataset {nome!r} non trovato in {DATASETS.name}/")
    for f in OBBLIGATORI:
        if not (d / f).exists():
            raise ValueError(f"dataset {nome!r}: manca {f}")
    docs = {f: (d / f).read_text(encoding="utf-8-sig") if (d / f).exists() else "" for f in DOC_FILES}
    docs["policy.md"] = docs["policy.md"] or policy_md()
    if not calcola_deterministici(docs):
        raise ValueError(f"dataset {nome!r}: nessun evento SIEM con corr=<ID>; vedi datasets/README.md")
    if (d / "truth.json").exists():
        truth = _completa_truth(json.loads((d / "truth.json").read_text(encoding="utf-8")), docs)
    else:
        truth = {"incidents": [], "criticality": criticita(docs), "pool": 0, "etichettato": False}
    truth["campione"] = f"dataset:{nome}"
    return docs, truth


def salva_dataset(nome: str, files: dict[str, str], sovrascrivi: bool = False) -> dict:
    """Salva un dataset caricato dalla web UI. Accetta solo i nomi di file previsti (+ truth.json)."""
    d = _cartella(nome)
    ammessi = set(DOC_FILES) | {"truth.json"}
    extra = set(files) - ammessi
    if extra:
        raise ValueError(f"file non previsti: {', '.join(sorted(extra))}")
    if "siem_export.log" not in files and not (d / "siem_export.log").exists():
        raise ValueError("serve almeno siem_export.log")
    if d.exists() and not sovrascrivi:
        raise ValueError(f"il dataset {nome!r} esiste gia'")
    for f, txt in files.items():
        if len(txt.encode()) > MAX_FILE:
            raise ValueError(f"{f}: oltre {MAX_FILE // 2**20} MB")
        if f == "truth.json":
            json.loads(txt)
    d.mkdir(parents=True, exist_ok=True)
    for f, txt in files.items():
        (d / f).write_text(txt, encoding="utf-8")
    try:
        carica_dataset(nome)                      # verifica completa: se fallisce, il messaggio torna alla UI
    except Exception:
        if not sovrascrivi:
            for f in files:
                (d / f).unlink(missing_ok=True)
            if not any(d.iterdir()):
                d.rmdir()
        raise
    return next(x for x in elenco_dataset() if x["nome"] == nome)


def esporta_dataset(nome: str, scala: str = "piccolo", n_incidenti: int | None = None, seed: int = 42,
                    campione: int = 0, etichette: bool = True) -> Path:
    """Scrive un corpus sintetico nel formato dei dataset: un esempio da copiare e modificare."""
    docs, truth = genera_corpus(scala, n_incidenti, seed, campione)
    d = _cartella(nome)
    d.mkdir(parents=True, exist_ok=True)
    for f, txt in docs.items():
        (d / f).write_text(txt + "\n", encoding="utf-8")
    if etichette:
        etich = [{"id": i["id"], "category": i["category"]} for i in truth["incidents"]]
        (d / "truth.json").write_text(json.dumps({"incidents": etich}, indent=1) + "\n", encoding="utf-8")
    return d


# ================================================================ parte SCRIPT (deterministica)
KV = re.compile(r'(\w+)=("[^"]*"|\S+)')


def parse_siem(docs) -> list[dict]:
    out = []
    for line in docs["siem_export.log"].splitlines():
        d = {k: v.strip('"') for k, v in KV.findall(line)}
        d["ts"] = line.split(" ", 1)[0]
        out.append(d)
    return out


def calcola_deterministici(docs) -> dict[str, dict]:
    """Lo SCRIPT: raggruppa per corr=, calcola i 6 campi deterministici. Sempre esatto."""
    per: dict[str, dict] = {}
    for e in parse_siem(docs):
        c = e.get("corr")
        if not c or c == "-":
            continue
        p = per.setdefault(c, {"hosts": set(), "src_ips": set(), "ts": [], "bytes": 0, "rules": []})
        p["hosts"].add(e["host"])
        p["src_ips"].add(e["src"])
        p["ts"].append(e["ts"])
        p["bytes"] += int(e.get("bytes_out", 0))
        p["rules"].append(e["rule"])
    hashes: dict[str, set] = {}
    for line in docs["edr_alerts.jsonl"].splitlines():
        a = json.loads(line)
        if a.get("corr"):
            hashes.setdefault(a["corr"], set()).add(a["sha256"])
    return {c: {"hosts": sorted(p["hosts"]), "src_ips": sorted(p["src_ips"]), "event_count": len(p["ts"]),
                "first_seen": min(p["ts"]), "last_seen": max(p["ts"]), "hashes": sorted(hashes.get(c, set())),
                "_bytes_out": p["bytes"], "_rules": sorted(set(p["rules"]))} for c, p in per.items()}


def criticita(docs) -> dict[str, str]:
    return {r["host"]: r["criticality"] for r in csv.DictReader(io.StringIO(docs["asset_inventory.csv"]))}


def threat_intel(docs) -> dict[str, str]:
    out = {}
    for line in docs["threat_intel.txt"].splitlines():
        d = dict(KV.findall(line))
        out[d["sha256"]] = f"{d['famiglia']} ({d['tipo']})"
    return out


def paragrafi(docs) -> list[tuple[str, str]]:
    """(documento, paragrafo) dalle note e dalle email, in ordine."""
    out = [("analyst_notes.md", p.strip()) for p in docs["analyst_notes.md"].split("\n\n") if p.strip()]
    out += [("email_thread.txt", m.strip()) for m in docs["email_thread.txt"].split("-----") if m.strip()]
    return out


def contesto(docs, iid: str) -> list[str]:
    """RETRIEVAL (script): tutti i paragrafi di note/email che citano l'incidente."""
    return [f"[{d}] {p}" for d, p in paragrafi(docs) if iid in p]


def categoria_euristica(docs, iid: str) -> str:
    """Script S: l'ultima parola chiave trovata nei paragrafi che citano l'ID. Fragile apposta."""
    cat = "malware"
    for _, p in paragrafi(docs):
        if iid in p:
            low = p.lower()
            for kw, c in KEYWORDS.items():
                if kw in low:
                    cat = c
    return cat


def completa(det: dict, cats: dict[str, str], crit: dict) -> list[dict]:
    """Lo SCRIPT deriva severity e MITRE dalla categoria (regola fissa della policy)."""
    out = []
    for iid in sorted(det):
        c = cats.get(iid, "malware")
        d = det[iid]
        out.append({"id": iid, **{k: d[k] for k in DET}, "category": c,
                    "severity": severity(c, d["hosts"], crit), "mitre": MITRE[c]})
    return out


# ================================================================ validatori
def valida_incidente(r: dict, det: dict, ti: dict, evidenza_in: list[str] | None = None,
                     crit: dict | None = None) -> list[str]:
    """Controlli deterministici su UN incidente. Non conoscono la verita': usano i log."""
    p = []
    iid = r.get("id")
    d = det.get(iid)
    if d is None:
        return ["incidente INVENTATO: nessun evento SIEM con questo corr="]
    cat = r.get("category")
    if cat not in CATS:
        p.append(f"category '{cat}' non è tra le categorie ammesse")
    for k in DET:  # coerenza con i log (ricalcolo)
        if k in r and r[k] != d[k]:
            p.append(f"{k}: {str(r[k])[:60]} ma dai log risulta {str(d[k])[:60]}")
    fam = " ".join(ti.get(h, "") for h in d["hashes"])
    if cat in CATS:   # campi derivati: devono seguire la policy (regola fissa)
        if "mitre" in r and r["mitre"] != MITRE[cat]:
            p.append(f"mitre: {str(r['mitre'])[:40]} ma per {cat} la policy dice {MITRE[cat]}")
        if "severity" in r and crit is not None and r["severity"] != severity(cat, d["hosts"], crit):
            p.append(f"severity: {r['severity']} ma la policy dà {severity(cat, d['hosts'], crit)}")
    if cat in CATS:
        if "(ransomware)" in fam and cat not in ("ransomware", "false_positive"):
            p.append(f"un hash è noto come ransomware ({fam.strip()}) ma category={cat}")
        if cat == "ransomware" and "(ransomware)" not in fam:
            p.append("category=ransomware ma nessun hash dell'incidente è ransomware secondo la threat intel")
        if d["_bytes_out"] > GB and cat not in ("data_exfiltration", "insider_threat", "false_positive"):
            p.append(f"{d['_bytes_out'] / GB:.1f} GB in uscita ma category={cat}")
        if cat in ("data_exfiltration", "insider_threat") and d["_bytes_out"] < 100 * 1024 ** 2:
            p.append(f"category={cat} ma solo {d['_bytes_out'] / 1024 ** 2:.1f} MB in uscita")
    if evidenza_in is not None:
        ev = (r.get("evidence") or "").strip()
        if len(ev) < 12 or not any(ev in s for s in evidenza_in):
            p.append("la citazione 'evidence' non compare testualmente nei documenti dell'incidente")
    return p


# ================================================================ agenti LLM
CAT_DEF = ("Categorie: " + ", ".join(CATS) + ". false_positive = attività autorizzata, test o regola SIEM "
           "mal configurata. Vale l'informazione PIÙ RECENTE: gli AGGIORNAMENTI forensi e le email che riportano FATTI "
           "(es. IT o red team che confermano un test autorizzato) correggono le note. Domande, supposizioni o ricordi "
           "('mi risulta che...', 'confermate?') NON sono prove e non cambiano la categoria. "
           "Attenzione alle frasi che NEGANO un collegamento ('NON è collegato a...'). " + REGOLE_CAT)
INC_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string"}, "hosts": {"type": "array", "items": {"type": "string"}},
    "src_ips": {"type": "array", "items": {"type": "string"}}, "event_count": {"type": "integer"},
    "first_seen": {"type": "string"}, "last_seen": {"type": "string"},
    "hashes": {"type": "array", "items": {"type": "string"}},
    "category": {"type": "string", "enum": CATS}, "severity": {"type": "string", "enum": LEVELS},
    "mitre": {"type": "string"}},
    "required": ["id", "hosts", "src_ips", "event_count", "first_seen", "last_seen", "hashes",
                 "category", "severity", "mitre"], "additionalProperties": False}
SCHEMA_A = {"type": "object", "properties": {"incidents": {"type": "array", "items": INC_SCHEMA}},
            "required": ["incidents"], "additionalProperties": False}
SCHEMA_CLS = {"type": "object", "properties": {
    "category": {"type": "string", "enum": CATS},
    "evidence": {"type": "string", "description": "frase copiata ALLA LETTERA dai documenti che giustifica la categoria"}},
    "required": ["category", "evidence"], "additionalProperties": False}
SCHEMA_GIUD = {"type": "object", "properties": {"verdetti": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "ok": {"type": "boolean"}, "problema": {"type": "string"}},
    "required": ["id", "ok", "problema"], "additionalProperties": False}}},
    "required": ["verdetti"], "additionalProperties": False}


class Conta:
    def __init__(self):
        self.lock, self.calls, self.tok = threading.Lock(), 0, 0

    def add(self, llm):
        with self.lock:
            self.calls += 1
            self.tok += ((llm.last_response or {}).get("usage") or {}).get("completion_tokens", 0)


def corpus_txt(docs) -> str:
    return "\n\n".join(f"===== {k} =====\n{v}" for k, v in docs.items())


def fatti(det_i: dict, ti: dict) -> str:
    fam = [f"{h[:12]}… → {ti.get(h, 'sconosciuto')}" for h in det_i["hashes"]] or ["nessun hash"]
    return (f"Fatti dai log (calcolati dallo script): regole SIEM viste {', '.join(det_i['_rules'])}; "
            f"bytes_out totali {det_i['_bytes_out'] / 1024 ** 2:.1f} MB; threat intel: {'; '.join(fam)}.")


def classifica(llm, iid, snippets, det_i, ti, conta, errore: list[str] | None = None, prec=None,
               ordine: str = "cronologico", proposte: list[dict] | None = None):
    """Agente CLASSIFICATORE. ordine='recenti_prima' = seconda opinione indipendente;
    proposte=[...] = agente ARBITRO che decide tra due classificatori in disaccordo."""
    docs_txt = snippets if ordine == "cronologico" else list(reversed(snippets))
    intro = (f"Classifica l'incidente {iid}." if ordine == "cronologico" else
             f"Classifica l'incidente {iid}. I documenti sono in ordine dal PIÙ RECENTE al più vecchio.")
    prompt = (f"{intro}\n{CAT_DEF}\n{fatti(det_i, ti)}\n\nDOCUMENTI CHE CITANO {iid}:\n" + "\n\n".join(docs_txt))
    if proposte:
        prompt += ("\n\nDue analisti indipendenti NON sono d'accordo:\n" +
                   "\n".join(f"- analista {k + 1}: category={p.get('category')} evidence=\"{p.get('evidence')}\""
                             for k, p in enumerate(proposte)) +
                   "\nDecidi tu quale è corretta (o un'altra), applicando la regola dell'informazione più recente.")
    if errore:
        prompt += (f"\n\nLa tua risposta precedente {json.dumps(prec, ensure_ascii=False)} è stata RESPINTA dai "
                   "validatori:\n- " + "\n- ".join(errore) + "\nCorreggi.")
    ruolo = "ARBITRO" if proposte else "CLASSIFICATORE" + (" (seconda opinione)" if ordine != "cronologico" else "")
    r = ask_json(llm, prompt + "\n\nRispondi con la categoria e una citazione testuale (evidence) copiata dai documenti.",
                 SCHEMA_CLS, name="arbitro" if proposte else "classifica", max_retries=0,
                 system=f"Sei un analista SOC (agente {ruolo}).")
    conta.add(llm)
    return r["data"]


def giudica(llm, items, conta) -> dict[str, str]:
    """LLM-as-judge su un gruppo di incidenti: {id: problema} per quelli che ritiene sbagliati."""
    blocchi = "\n\n".join(f"### {it['id']} → category={it['category']} (evidence: \"{it['evidence']}\")\n"
                          + "\n".join(it["snippets"]) for it in items)
    r = ask_json(llm, f"Sei il revisore SOC. Per ogni incidente verifica che la categoria sia corretta "
                 f"rispetto ai documenti.\n{CAT_DEF}\n\n{blocchi}", SCHEMA_GIUD, name="giudice_incidenti",
                 max_retries=0, system="Sei un agente GIUDICE (LLM-as-judge): valuti, non classifichi da zero.")
    conta.add(llm)
    return {v["id"]: v["problema"] for v in (r["data"] or {}).get("verdetti", []) if not v["ok"]}


# ================================================================ metodi
def metodo_S(factory, docs, emit, **_):
    det, crit = calcola_deterministici(docs), criticita(docs)
    cats = {iid: categoria_euristica(docs, iid) for iid in det}
    emit({"type": "agent", "role": "script", "msg": f"{len(det)} incidenti trovati via corr=; categorie con parole chiave"})
    return completa(det, cats, crit), Conta(), {}


def metodo_A(factory, docs, emit, **_):
    llm, conta = factory(), Conta()
    r = ask_json(llm, "Sei un analista SOC. Dai documenti seguenti compila il JSON con TUTTI gli incidenti "
                 "(uno per ogni ID corr=INC-xxxx), seguendo ESATTAMENTE policy.md.\n\n" + corpus_txt(docs),
                 SCHEMA_A, name="incidenti_completo", max_retries=0)
    conta.add(llm)
    return (r["data"] or {}).get("incidents", []), conta, {}


def metodo_B(factory, docs, emit, workers=4, **_):
    """Lo script recupera i testi di ogni incidente (grep dell'ID), l'LLM compila TUTTI i campi. Nessun controllo."""
    conta = Conta()
    ids = sorted(calcola_deterministici(docs))
    righe_siem = docs["siem_export.log"].splitlines()
    edr = docs["edr_alerts.jsonl"].splitlines()

    def uno(iid):
        try:
            return _uno(iid)
        except Exception as e:  # una chiamata fallita non deve far perdere tutte le altre
            emit({"type": "agent", "role": "estrattore", "id": iid, "stato": "errore", "errore": str(e)[:200]})
            return None

    def _uno(iid):
        llm = factory()
        ctx = "\n".join(l for l in righe_siem if iid in l) + "\n\nEDR:\n" + "\n".join(l for l in edr if iid in l)
        ctx += "\n\nNOTE ED EMAIL:\n" + "\n\n".join(contesto(docs, iid))
        hosts_ctx = "\n".join(l for l in docs["asset_inventory.csv"].splitlines() if any(h in l for h in re.findall(r"host=(\S+)", ctx)))
        ti_ctx = "\n".join(l for l in docs["threat_intel.txt"].splitlines() if any(h in l for h in re.findall(r"[0-9a-f]{64}", ctx)))
        r = ask_json(llm, f"Compila il JSON dell'incidente {iid} seguendo la policy.\n\n{docs['policy.md']}\n\n"
                     f"RIGHE SIEM E EDR CHE CONTENGONO {iid}:\n{ctx}\n\nINVENTARIO:\n{hosts_ctx}\n\nTHREAT INTEL:\n{ti_ctx}",
                     INC_SCHEMA, name="incidente_singolo", max_retries=0)
        conta.add(llm)
        emit({"type": "agent", "role": "estrattore", "id": iid, "stato": "fatto",
              "category": (r["data"] or {}).get("category")})
        return r["data"]

    with ThreadPoolExecutor(workers) as ex:
        out = [x for x in ex.map(uno, ids) if x]
    return out, conta, {}


def metodo_C(factory, docs, emit, workers=4, judge=True, max_corr=2, consenso=True, **_):
    conta = Conta()
    det, crit, ti = calcola_deterministici(docs), criticita(docs), threat_intel(docs)   # SCRIPT
    ids = sorted(det)
    emit({"type": "agent", "role": "script", "msg": f"{len(ids)} incidenti, 6 campi deterministici calcolati "
          f"dai log ({sum(d['event_count'] for d in det.values())} eventi SIEM correlati)", "ids": ids})
    info = {"correzioni": 0, "non_verificate": [], "giudice_flag": {}, "giudice_accettate": 0,
            "visto_dal_giudice": {}, "arbitrati": 0}
    lock = threading.Lock()
    cats, evid = {}, {}

    def lavora(iid):
        try:
            return _lavora(iid)
        except Exception as e:  # chiamate fallite: ripiego sull'euristica dello script, segnalato
            with lock:
                info["non_verificate"].append(iid)
            cats[iid], evid[iid] = categoria_euristica(docs, iid), ""
            emit({"type": "agent", "role": "validatore", "id": iid, "category": cats[iid],
                  "problemi": [f"errore LLM, uso l'euristica dello script: {str(e)[:150]}"]})
            return iid, contesto(docs, iid)

    def _lavora(iid):
        llm = factory()
        snip = contesto(docs, iid)                                    # RETRIEVAL (script)
        emit({"type": "agent", "role": "retrieval", "id": iid, "snippets": len(snip)})
        out = classifica(llm, iid, snip, det[iid], ti, conta)         # CLASSIFICATORE 1 (LLM)
        if consenso:                                                  # CLASSIFICATORE 2: seconda opinione
            out2 = classifica(factory(), iid, snip, det[iid], ti, conta, ordine="recenti_prima")
            d1, d2 = (out or {}).get("category"), (out2 or {}).get("category")
            emit({"type": "agent", "role": "consenso", "id": iid, "c1": d1, "c2": d2, "accordo": d1 == d2})
            if d1 != d2:                                              # ARBITRO (LLM)
                out = classifica(llm, iid, snip, det[iid], ti, conta, proposte=[out or {}, out2 or {}]) or out
                with lock:
                    info["arbitrati"] += 1
                emit({"type": "agent", "role": "arbitro", "id": iid, "category": (out or {}).get("category")})
        rec = {"id": iid, **(out or {})}
        probs = valida_incidente(rec, det, ti, snip) if out else ["risposta vuota"]   # VALIDATORI
        emit({"type": "agent", "role": "validatore", "id": iid, "category": rec.get("category"), "problemi": probs})
        t = 0
        while probs and t < max_corr:                                 # CORRETTORE (LLM)
            t += 1
            out2 = classifica(llm, iid, snip, det[iid], ti, conta, errore=probs, prec=out)
            with lock:
                info["correzioni"] += 1
            if out2:
                out, rec = out2, {"id": iid, **out2}
            probs = valida_incidente(rec, det, ti, snip)
            emit({"type": "agent", "role": "correttore", "id": iid, "tentativo": t, "category": rec.get("category"),
                  "ok": not probs, "problemi": probs})
        if probs:
            with lock:
                info["non_verificate"].append(iid)
        cats[iid], evid[iid] = rec.get("category", "malware"), rec.get("evidence", "")
        return iid, snip

    with ThreadPoolExecutor(workers) as ex:
        snippets = dict(ex.map(lavora, ids))

    if judge:                                                          # GIUDICE (LLM-as-judge), a gruppi di 5
        gruppi = [ids[i:i + 5] for i in range(0, len(ids), 5)]

        def giudica_gruppo(g):
            try:
                _giudica_gruppo(g)
            except Exception as e:
                emit({"type": "agent", "role": "giudice", "ids": g, "flag": [], "errore": str(e)[:150]})

        def _giudica_gruppo(g):
            llm = factory()
            items = [{"id": i, "category": cats[i], "evidence": evid[i], "snippets": snippets[i]} for i in g]
            flag = giudica(llm, items, conta)
            with lock:
                info["giudice_flag"].update(flag)
                info["visto_dal_giudice"].update({i: cats[i] for i in g})
            emit({"type": "agent", "role": "giudice", "ids": g, "flag": [{"id": k, "problema": v} for k, v in flag.items()]})
            for iid, motivo in flag.items():
                if iid not in g:
                    continue
                nuovo = classifica(llm, iid, snippets[iid], det[iid], ti, conta,
                                   errore=[f"il revisore dice: {motivo}"], prec={"category": cats[iid], "evidence": evid[iid]})
                ok = bool(nuovo) and nuovo.get("category") != cats[iid] and not valida_incidente(
                    {"id": iid, **nuovo}, det, ti, snippets[iid])
                if ok:   # la modifica del giudice passa SOLO se rispetta i validatori
                    with lock:
                        cats[iid], evid[iid] = nuovo["category"], nuovo["evidence"]
                        info["giudice_accettate"] += 1
                        if iid in info["non_verificate"]:
                            info["non_verificate"].remove(iid)
                emit({"type": "agent", "role": "correttore", "id": iid, "da_giudice": True, "ok": ok,
                      "category": cats[iid], "problemi": [] if ok else ["modifica respinta o invariata"]})

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(giudica_gruppo, gruppi))

    emit({"type": "agent", "role": "script", "msg": "severity e MITRE derivati dalla policy (regola fissa)"})
    out = completa(det, cats, crit)                                     # SCRIPT
    for r in out:
        r["evidence"] = evid.get(r["id"], "")
    return out, conta, info


METODI = {"S": metodo_S, "A": metodo_A, "B": metodo_B, "C": metodo_C}


# ================================================================ valutazione
def _norm(v):
    return sorted(v) if isinstance(v, list) else v


def punteggio(out: list | None, truth: dict) -> dict:
    tr = {i["id"]: i for i in truth["incidents"]}
    got, inventati = {}, 0
    for r in out or []:
        if isinstance(r, dict) and r.get("id") in tr and r["id"] not in got:
            got[r["id"]] = r
        else:
            inventati += 1
    ok = {"det": 0, "sem": 0, "der": 0}
    errori, sbagliati = [], set()
    for iid, t in tr.items():
        g = got.get(iid)
        if g is None:
            errori.append(f"{iid}: incidente mancante")
            sbagliati.add(iid)
            continue
        for grp, campi in (("det", DET), ("sem", SEM), ("der", DER)):
            for f in campi:
                if _norm(g.get(f)) == _norm(t[f]):
                    ok[grp] += 1
                else:
                    sbagliati.add(iid)
                    errori.append(f"{iid}.{f}: {str(g.get(f))[:50]} invece di {str(t[f])[:50]}")
    n = len(tr)
    pct = lambda k, m: round(100 * ok[k] / (n * m), 2) if n else 0
    tot = round(100 * sum(ok.values()) / (n * 9), 2) if n else 0
    return {"pct_det": pct("det", 6), "pct_sem": pct("sem", 1), "pct_der": pct("der", 2), "pct": tot,
            "incidenti_ok": n - len(sbagliati), "incidenti_tot": n, "mancanti": n - len(got), "inventati": inventati,
            "esatto": not errori and not inventati, "errori": errori[:30], "n_errori": len(errori),
            "sbagliati": sorted(sbagliati)}


def rilevazione(segnalati: set, sbagliati: set) -> dict:
    return {"segnalate": len(segnalati), "veri_pos": len(segnalati & sbagliati),
            "falsi_pos": len(segnalati - sbagliati), "mancati": len(sbagliati - segnalati)}


# ================================================================ benchmark
def latenze(calls: list[dict]) -> dict:
    """Statistiche di latenza delle chiamate LLM di un run."""
    lat = sorted(c["latency"] for c in calls)
    if not lat:
        return {"n": 0}
    tok = sum(c.get("completion_tokens") or 0 for c in calls)
    return {"n": len(lat), "media": round(sum(lat) / len(lat), 2), "p50": lat[len(lat) // 2],
            "p95": lat[min(len(lat) - 1, int(len(lat) * .95))], "max": lat[-1],
            "tok_s": round(tok / sum(lat), 1) if sum(lat) else None,
            "reasoning_tok": sum(c.get("reasoning_tokens") or 0 for c in calls),
            "prompt_tok": sum(c.get("prompt_tokens") or 0 for c in calls), "errori": sum(bool(c.get("error")) for c in calls)}


def run_benchmark(make_factory, scala="medio", n_incidenti=None, n=1, metodi=("S", "A", "B", "C"),
                  judge=True, emit=print, seed=42, modelli=("mock",), campione=None, stop=None, dataset=None):
    """make_factory(truth, model) -> factory() -> llm.
    Con piu' modelli, ognuno gira in un THREAD separato, in contemporanea.
    dataset=<nome>: usa datasets/<nome>/ invece del corpus sintetico (n = ripetizioni sullo stesso corpus)."""
    # ogni PROVA pesca la sua estrazione dal pool: cosi' si misura anche quanto cambia il risultato
    # con incidenti diversi. Dentro una prova, tutti i metodi e i modelli vedono lo STESSO corpus.
    base_c = campione if campione is not None else random.SystemRandom().randrange(1_000_000)
    corpora = []
    if dataset:
        base_c, scala = f"dataset:{dataset}", f"dataset {dataset}"
    for i in range(n):
        docs, truth = carica_dataset(dataset) if dataset else genera_corpus(scala, n_incidenti, seed, base_c + i)
        truth.setdefault("etichettato", True)
        corpora.append({"docs": docs, "truth": truth, "det": calcola_deterministici(docs),
                        "ti": threat_intel(docs), "crit": criticita(docs)})

    def riepilogo(c, i):
        docs, truth = c["docs"], c["truth"]
        ids = [x["id"] for x in truth["incidents"]] if truth["etichettato"] else sorted(c["det"])
        return {"prova": i + 1, "campione": truth["campione"], "ids": ids,
                "token_stimati": stats(docs)["_totale"]["token_stimati"],
                "categorie": {k: sum(x["category"] == k for x in truth["incidents"]) for k in CATS},
                "trappole": {"riclassificati": docs["analyst_notes.md"].count("AGGIORNAMENTO"),
                             "falsi_positivi": sum(x["category"] == "false_positive" for x in truth["incidents"]),
                             "righe_che_citano_ID_senza_corr": sum(1 for l in docs["siem_export.log"].splitlines()
                                                                   if "corr=-" in l and "INC-" in l)}}

    docs, truth = corpora[0]["docs"], corpora[0]["truth"]
    simulated = isinstance(make_factory(truth, modelli[0])(), MockLLM)
    r0 = riepilogo(corpora[0], 0)
    n_inc = len(r0["ids"])
    if not truth["etichettato"] and simulated:
        raise ValueError("il backend mock simula gli errori partendo dalle etichette: per un dataset senza "
                         "truth.json usa un modello vero (backend local)")
    emit({"type": "truth", "scala": scala, "incidenti": n_inc, "campi": n_inc * 9,
          "etichettato": truth["etichettato"], "dataset": dataset,
          "pool": truth["pool"], "campione": base_c, "ids": r0["ids"], "prove": [riepilogo(c, i) for i, c in enumerate(corpora)],
          "stats": stats(docs), "simulated": simulated, "modelli": list(modelli), "metodi": list(metodi),
          "preview": {k: "\n".join(v.splitlines()[:60]) for k, v in docs.items()},
          "categorie": r0["categorie"], "trappole": r0["trappole"]})

    fermato = lambda: stop is not None and stop.is_set()   # pulsante Stop della web UI

    def un_run(model, m, i, base_factory):
        if fermato():
            return
        c = corpora[i]
        tag = {"model": model, "method": m, "i": i + 1, "campione": c["truth"]["campione"]}
        calls: list[dict] = []
        lock = threading.Lock()

        def on_call(rec):                                   # ogni chiamata LLM: prompt, risposta, latenza
            rec = {**rec, **tag, "n": 0}
            with lock:
                calls.append(rec)
                rec["n"] = len(calls)
            emit(rec)

        def factory():
            llm = base_factory()
            llm.on_call = on_call
            return llm

        emit_t = lambda ev: emit({**ev, **tag})
        emit_t({"type": "start"})
        t0 = time.perf_counter()
        try:
            out, conta, info = METODI[m](factory, c["docs"], emit_t, judge=judge)
            errore = None
        except Exception as e:
            out, conta, info, errore = None, Conta(), {}, f"{type(e).__name__}: {str(e)[:300]}"
        sec = time.perf_counter() - t0                      # tempo di completamento del task
        if fermato():   # interrotto a meta': niente punteggio, sarebbe falsato
            emit({"type": "agent", **tag, "role": "script", "msg": "fermato dall'utente: prova scartata"})
            return
        risultato(out, conta, info, errore, sec, calls, tag, c)

    def risultato(out, conta, info, errore, sec, calls, tag, c):
        m = tag["method"]
        truth, det, ti, crit = c["truth"], c["det"], c["ti"], c["crit"]
        if truth["etichettato"]:
            sc = punteggio(out, truth)
        else:   # dataset senza truth.json: niente punteggio, solo output e validatori
            nomi = {r.get("id") for r in out or [] if isinstance(r, dict)}
            sc = {"pct_det": None, "pct_sem": None, "pct_der": None, "pct": None, "incidenti_ok": None,
                  "incidenti_tot": len(det), "mancanti": len(set(det) - nomi), "inventati": len(nomi - set(det)),
                  "esatto": None, "errori": [], "n_errori": None, "sbagliati": [], "non_valutato": True}
        # validatori su TUTTI gli output (non conoscono la verita')
        segn, probs = set(), []
        for r in out or []:
            if isinstance(r, dict):
                p = valida_incidente(r, det, ti, crit=crit)
                if p:
                    segn.add(r.get("id"))
                    probs += [f"{r.get('id')}: {x}" for x in p]
        segn |= set(det) - {r.get("id") for r in out or [] if isinstance(r, dict)}  # mancanti
        ev = {"type": "result", **tag, "sec": sec, "tok": conta.tok, "calls": conta.calls,
              "latenza": latenze(calls), "error": errore, **sc,
              "validatori": rilevazione(segn & set(det), set(sc["sbagliati"])) if truth["etichettato"] else
              {"segnalate": len(segn & set(det))}, "validatori_problemi": probs[:30],
              "output": out if isinstance(out, list) else None}
        if m == "C":
            ev.update({"correzioni": info.get("correzioni", 0), "non_verificate": info.get("non_verificate", []),
                       "giudice_accettate": info.get("giudice_accettate", 0), "arbitrati": info.get("arbitrati", 0)})
            if judge and truth["etichettato"]:
                tr = {x["id"]: x["category"] for x in truth["incidents"]}
                viste = info.get("visto_dal_giudice", {})
                ev["giudice"] = rilevazione(set(info.get("giudice_flag", {})) & set(viste),
                                            {k for k, v in viste.items() if v != tr.get(k)})
        emit(ev)

    # S: solo script, non dipende dal modello -> una volta per prova
    if "S" in metodi:
        for i in range(n):
            un_run("script", "S", i, lambda: None)
    llm_metodi = [m for m in metodi if m != "S"]

    def per_modello(model):
        for i in range(n):                       # prova per prova: i metodi si confrontano sullo stesso corpus
            base = make_factory(corpora[i]["truth"], model)
            for m in llm_metodi:
                un_run(model, m, i, base)

    with ThreadPoolExecutor(max_workers=max(1, len(modelli))) as ex:  # i modelli in PARALLELO
        list(ex.map(per_modello, modelli if llm_metodi else []))
    emit({"type": "done"})


# ================================================================ mock
def mock_factory(truth: dict, model: str = "mock", seed: int = 5):
    """Modello FINTO: errori SIMULATI, solo per provare il benchmark senza GPU."""
    tr = {i["id"]: i for i in truth["incidents"]}
    rng = random.Random(seed + sum(map(ord, model)))
    lock = threading.Lock()
    ID = re.compile(r"INC-\d{4}")

    def rompi(inc: dict, p_det: float, p_cat: float, p_der: float) -> dict:
        r = {k: inc[k] for k in ["id"] + DET + SEM + DER}
        if rng.random() < p_det:
            k = rng.choice(["event_count", "first_seen", "last_seen", "hosts"])
            if k == "event_count":
                r[k] += rng.choice([1, 1, 2, -1])        # tipico: conta anche le righe-trappola
            elif k == "hosts":
                r[k] = r[k][:-1] or ["ws-001"]
            else:
                r[k] = r[k][:-2] + "00"
        if rng.random() < p_cat:
            r["category"] = inc["apparente"] if inc["apparente"] != inc["category"] else rng.choice(CATS)
        if rng.random() < p_der:
            r["severity"] = rng.choice(LEVELS)
        return r

    def policy(messages, tools, rf):
        with lock:
            name = (rf or {}).get("json_schema", {}).get("name")
            prompt = messages[-1]["content"]
            if name == "incidenti_completo":
                if len(prompt) / CHARS_PER_TOKEN > 30000:
                    raise RuntimeError("Il server ha risposto 400: context length exceeded "
                                       f"(~{len(prompt) / CHARS_PER_TOKEN:.0f} token > 32768) [simulato]")
                data = {"incidents": [rompi(i, .35, .3, .25) for i in tr.values() if rng.random() > .1]}
            elif name == "incidente_singolo":
                iid = ID.search(prompt).group(0)
                data = rompi(tr[iid], .3, .18, .15)
            elif name in ("classifica", "arbitro"):
                iid = ID.search(prompt).group(0)
                inc = tr[iid]
                corretto = (rng.random() < .85 if name == "arbitro" else
                            "RESPINTA" in prompt and rng.random() < .9 or rng.random() > .2)
                cat = inc["category"] if corretto else inc["apparente"] if inc["apparente"] != inc["category"] else rng.choice(CATS)
                testo = prompt.split("DOCUMENTI CHE CITANO", 1)[1].split(":\n", 1)[1]
                testo = re.split(r"\n\n(?:Rispondi con|La tua risposta|Due analisti)", testo)[0]  # solo i documenti
                frasi = [s.strip() for s in re.split(r"(?<=[.:])\s|\n", testo) if iid in s and len(s.strip()) > 20]
                data = {"category": cat, "evidence": (frasi[-1] if frasi else testo.strip().split("\n")[0])[:120]}
            elif name == "giudice_incidenti":
                ver = []
                for m in re.finditer(r"### (INC-\d{4}) → category=(\w+)", prompt):
                    giusta = tr[m.group(1)]["category"] == m.group(2)
                    ok = (rng.random() > .08) if giusta else (rng.random() > .8)
                    ver.append({"id": m.group(1), "ok": ok, "problema": "" if ok else "l'aggiornamento più recente dice altro"})
                data = {"verdetti": ver}
            else:
                data = {}
            return {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}

    def factory():
        llm = MockLLM(policy)
        llm.model = model
        return llm

    return factory
