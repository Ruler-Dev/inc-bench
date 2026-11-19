# open-harness

Uno scheletro di **harness agentico** per **modelli locali**, scritto per essere letto e per insegnare.
Mostra i pezzi che stanno dentro Claude Code, Codex, Cline e simili: loop, tool call, skill, sub-agenti e schemi JSON forzati.
Serve soprattutto a dimostrare una tesi:

> **Il modello decide *cosa* fare; il codice Python deterministico fa *i conti*.**
> Uno schema JSON forzato garantisce la *forma* dell'output, non la sua *verità*.

Usa solo la libreria standard di Python, senza dipendenze. Parla con qualsiasi server **OpenAI-compatibile**: LM Studio, Ollama, llama.cpp, vLLM, SGLang.

La nota tecnica con metodologia, risultati e analisi degli errori è in [`docs/open-harness_nota_tecnica.pdf`](docs/open-harness_nota_tecnica.pdf).

---

## Avvio rapido

Serve Python 3.11 o successivo. Nessuna dipendenza da installare.

```bash
git clone https://github.com/Ruler-Dev/open-harness.git
cd open-harness
python webui.py                 # apre http://127.0.0.1:8765
```

Su Windows c'è anche `avvia_webui.bat`, su Linux e macOS `avvia_webui.sh`: se la web UI termina in modo anomalo la riavviano.

Senza un modello, il backend `mock` fa girare tutte le demo con risposte simulate. Per un modello vero scegli `local` in alto: i modelli dei provider attivi compaiono nell'elenco (● = caricato).

### Schede della web UI
**Agente** (il loop in diretta, con i sub-agenti annidati), **Anatomia tool call**, **Schema JSON**, **Confronto: LLM vs Python**,
**JSON lunghi · A/B/C**, **Incidenti SOC · S/A/B/C**, **Codice & prompt**, **Storico**, **Provider e dati**, **Concetti**.
"JSON grezzo" mostra le risposte del server così come arrivano.
Sotto il cofano `webui.py` (solo libreria standard) esegue le demo in un thread e manda ogni evento al browser con **Server-Sent Events**.

---

## Provider

Un provider è un server OpenAI-compatibile: LM Studio, Ollama, llama.cpp, vLLM, SGLang o un servizio cloud con la stessa API.
L'elenco sta in `providers.json`, che non è versionato. Al primo avvio vale `providers.example.json`: è attivo solo LM Studio su `localhost:1234`.

Si configura in due modi:
- dalla scheda **Provider e dati** della web UI: aggiungi, attiva o disattiva e premi **Salva**;
- a mano, copiando `providers.example.json` in `providers.json`.

```json
{"providers": [
  {"name": "lmstudio", "base_url": "http://localhost:1234/v1", "enabled": true},
  {"name": "vllm-gpu2", "base_url": "http://localhost:8000/v1", "enabled": true},
  {"name": "cloud", "base_url": "https://api.example.com/v1", "api_key_env": "CLOUD_API_KEY",
   "models": ["vendor/model-id"], "enabled": true}
]}
```

- **`api_key_env`** è il *nome* della variabile d'ambiente che contiene la chiave. La chiave non va mai scritta nel file: la validazione lo rifiuta. Viene inviata come `Authorization: Bearer` solo al provider con quel `base_url`.
- **`models`** è facoltativo. Se c'è, l'elenco dei modelli non viene chiesto al server: è utile per i servizi cloud con cataloghi enormi.
- Con più provider attivi, ogni modello viene chiamato sul server che lo espone. Così il benchmark Incidenti può confrontare in parallelo modelli che girano su macchine diverse.

```bash
export CLOUD_API_KEY=...        # Windows PowerShell: $env:CLOUD_API_KEY="..."
python webui.py
```

Da riga di comando valgono gli stessi provider. In alternativa si passa `--base-url` con uno o più URL separati da virgola.

---

## I tuoi dati: dataset per il benchmark Incidenti

Oltre al corpus sintetico (un pool fisso di 100 incidenti, da cui ogni run estrae un campione) puoi analizzare **i tuoi log, note ed email**.
Un dataset è una cartella `datasets/<nome>/` con gli stessi file del corpus sintetico. Il formato completo è in [`datasets/README.md`](datasets/README.md).

| File | | Contenuto |
|---|---|---|
| `siem_export.log` | **obbligatorio** | eventi `chiave=valore`, uno per riga; `corr=<ID>` lega l'evento a un incidente |
| `analyst_notes.md` | facoltativo | note dei turni, paragrafi separati da una riga vuota |
| `email_thread.txt` | facoltativo | email separate da `-----` |
| `edr_alerts.jsonl` | facoltativo | alert EDR (`sha256`, `corr`) |
| `threat_intel.txt` | facoltativo | `sha256=… famiglia=… tipo=…` |
| `asset_inventory.csv` | facoltativo | `host,owner,criticality,location` |
| `policy.md` | facoltativo | regole di classificazione; se manca, vale quella predefinita |
| `truth.json` | facoltativo | etichette: con questo file i risultati vengono valutati |

- **Con `truth.json`** (anche solo `id` e `category` per incidente) ottieni gli stessi punteggi del corpus sintetico. Gli altri campi vengono ricavati dai log e dalla policy.
- **Senza `truth.json`** ottieni l'output di ogni metodo e le segnalazioni dei validatori, senza punteggio. In questo caso serve un modello vero: il backend `mock` simula gli errori partendo dalle etichette.

```bash
python main.py esporta --dataset mio --scala piccolo        # parte da un corpus sintetico da modificare
python main.py incidenti --dataset esempio                  # mock, dataset etichettato incluso nella repo
python main.py incidenti --backend local --dataset mio --metodi BC --modelli "modello-a,modello-b"
```

Dalla web UI: scheda **Provider e dati** → **Carica dataset** (i file devono avere i nomi previsti), poi scegli il dataset nel menu "Corpus" della scheda Incidenti.

> I dataset in `datasets/` sono esclusi da git, tranne `datasets/esempio/`. Prima di condividere log reali, rimuovi i dati personali.

---

## Storico delle esecuzioni
Ogni run viene salvato in `data/runs/<id>.json` (escluso da git), sia dalla web UI sia dalla riga di comando. Contiene:
- **il modello usato**: id, backend e server; con LM Studio anche quantizzazione, architettura e contesto caricato;
- data, durata, origine (`webui` o `cli`) e parametri;
- un riepilogo dei risultati e **tutti gli eventi**, comprese le chiamate LLM con prompt, ragionamento e risposta.

Nella scheda **Storico** filtri per demo e modello, cerchi per testo e premi **Apri**: il run viene riprodotto nella sua scheda, con i parametri di allora.

---

## Riga di comando
```bash
python main.py tutte                       # tutte le demo con il modello finto (mock): nessuna GPU
python main.py anatomia --backend local    # con il primo provider attivo
python main.py confronto --backend local -n 5 --righe 150
python main.py agente --backend local --raw   # --raw mostra le risposte grezze del server
python main.py incidenti --backend local --scala medio --modelli "modello-a,modello-b"
```

| Opzione | Default | |
|---|---|---|
| `--backend` | `mock` | `local` = server OpenAI-compatibile |
| `--model` | `qwen/qwen3.6-35b-a3b` | oppure la variabile d'ambiente `HARNESS_MODEL` |
| `--base-url` | provider attivi | uno o più URL separati da virgola, oppure `HARNESS_BASE_URL` |
| `--dataset` | | `incidenti`: usa `datasets/<nome>/`; `esporta`: dove scrivere |
| `--scala` | `medio` | `incidenti`: `piccolo` (6), `medio` (20), `enorme` (50 incidenti) |
| `--campione` | casuale | `incidenti`: quale estrazione dal pool di 100 (per ripetere un run) |
| `--modelli` | | `incidenti`: più modelli separati da virgola, in parallelo |
| `--metodi` | tutti | `incidenti`: sottoinsieme di `SABC`; `lunghi`: di `ABC` |
| `-n` | 10 (mock) / 3 (local) | numero di prove |
| `--righe` | 40 | numero di righe della fattura di prova |
| `--max-tokens` | 8192 (24000 per i benchmark) | tetto di token per ogni risposta del modello |
| `--timeout` | 1800 | secondi massimi per una singola chiamata |
| `--solo-b` | | `confronto`: salta il metodo A, che è lento |

**LM Studio e la VRAM:** se il server risponde `{"error":"terminated"}`, nei log (`~/.lmstudio/server-logs`) di solito c'è `CUDA error: out of memory`. Il modello è caricato con un contesto troppo grande: ricaricalo con un contesto più piccolo (`lms load <modello> --context-length 32768`).

---


## Struttura

```
harness/
  llm.py         client HTTP verso il server locale + MockLLM per i test
  tools.py       Tool = funzione + JSON Schema; parse, validazione, esecuzione
  schema.py      validatore JSON Schema minimale
  agent.py       IL LOOP agentico, il tracer, il tool "delega" (sub-agente)
  skills.py      caricamento delle skill (SKILL.md + scripts/) e relativi tool
  structured.py  forzare JSON: prompt + retry  vs  constrained decoding
skills/fatturazione/
  SKILL.md                    istruzioni (caricate solo quando servono)
  scripts/calcola_fattura.py  calcolo DETERMINISTICO (Decimal)
main.py          le demo da riga di comando (+ build_agent, run_confronto riusati dalla UI)
pipeline.py      JSON lunghi: metodi A/B/C, validatori, LLM-as-judge, benchmark
incidenti.py     benchmark SOC: corpus sintetico (pool di 100), dataset utente, metodi S/A/B/C
providers.py     provider OpenAI-compatibili (providers.json), chiavi da variabili d'ambiente
history.py       storico dei run (data/runs/*.json): modello, parametri, riepilogo, eventi
webui.py         server web (http.server + Server-Sent Events)
webui/index.html la pagina: HTML + CSS + JS vanilla, nessuna build
datasets/        dataset per il benchmark Incidenti (esempio/ incluso)
docs/            nota tecnica (PDF)
tests/           test senza modello (python -m unittest)
```

---

## I concetti, in ordine

### 1. Una tool call è solo JSON scritto dal modello (`python main.py anatomia`)

Il modello non esegue niente. Riceve la descrizione dei tool:

```json
{"type": "function", "function": {
  "name": "calcola_iva",
  "description": "Calcola l'IVA al 22% di un imponibile in euro.",
  "parameters": {"type": "object",
                 "properties": {"imponibile": {"type": "number", "minimum": 0}},
                 "required": ["imponibile"], "additionalProperties": false}}}
```

e risponde con una **richiesta**:

```json
{"role": "assistant", "content": null, "tool_calls": [{
  "id": "call_1", "type": "function",
  "function": {"name": "calcola_iva", "arguments": "{\"imponibile\":1250}"}}]}
```

Nota che `arguments` è una **stringa**: può contenere JSON rotto, tipi sbagliati o campi inventati.
L'harness fa `json.loads`, valida contro lo schema, esegue la funzione Python e risponde con:

```json
{"role": "tool", "tool_call_id": "call_1", "content": "{\"iva\": 275.0}"}
```

**Sotto il cofano** il modello scrive testo nel formato del suo *chat template*. Qwen, per esempio, scrive
`<tool_call>{"name": "calcola_iva", "arguments": {"imponibile": 1250}}</tool_call>`.
Il server lo converte in `tool_calls`. Se non ci riesce, `llm.py` lo recupera dal testo con una regex (`TEXT_TOOL_CALL`).

### 2. L'agent loop (`harness/agent.py`)

```
messages = [system, user]
ripeti (max N passi):
    risposta = LLM(messages, tools)
    nessuna tool_call?  -> fine
    per ogni tool_call: esegui in Python, aggiungi {"role": "tool", ...}
```

Tutto il resto è contorno: permessi, gestione del contesto, retry, log.
Gli errori dei tool **non fanno crashare** l'agente: tornano al modello come testo, così può correggersi.

Il loop non stampa niente direttamente: emette **eventi** (`task`, `llm`, `tool_call`, `tool_result`, `final`, `stop`).
`Tracer` li stampa nel terminale, mentre `WebTracer` (in `webui.py`) li manda al browser.

**Due limiti che un harness deve sempre mettere:**
- `max_steps`: quante volte si può ripetere il loop.
- `max_tokens`: quanto può essere lunga ogni risposta. Un caso visto davvero in sviluppo: senza `max_tokens`, il sub-agente con Qwen 3.6 è entrato in un loop di ragionamento e ha generato **più di 28.000 token per scrivere un'email di 3 righe**.
  Quando una risposta viene troncata (`finish_reason: "length"`), la UI la segna in rosso.

### 3. Skill (`harness/skills.py`)

Una skill è una cartella con `SKILL.md` (frontmatter `name` + `description`) e degli `scripts/`.
Nel system prompt va **solo l'indice** (una riga per skill). Il modello carica le istruzioni complete con `carica_skill` quando gli servono (*progressive disclosure*) ed esegue gli script con `esegui_script`.
Gli script sono codice normale: testabile, versionato, deterministico.

### 4. Sub-agenti (`subagent_tool` in `agent.py`)

Il tool `delega` avvia **un altro Agent** con il contesto vuoto e meno tool (minimo privilegio).
Al padre torna solo il risultato finale, non tutta la conversazione del figlio. Così il contesto principale resta pulito.

### 5. Forzare uno schema JSON (`python main.py json`)

| Livello | Come | Garanzia |
|---|---|---|
| Prompt | "rispondi in JSON" + parse + validazione + retry | nessuna: testo intorno, tipi sbagliati, campi inventati |
| Constrained decoding | `response_format: {type: "json_schema", ...}`. Il server maschera i token non validi (XGrammar in vLLM/SGLang, GBNF in llama.cpp, LM Studio) | JSON valido **per costruzione** |
| Validazione harness | `schema.validate` sempre, anche col constrained | difesa in profondità |

**Nessuno dei tre livelli garantisce che i valori siano veri.**

### 6. La tesi: Python deterministico vs JSON dal modello (`python main.py confronto`)

Stessa fattura, due approcci:

- **A.** Il modello legge la fattura, **fa i conti** e risponde in JSON con lo schema forzato dal server.
- **B.** Il modello sceglie solo **quale tool chiamare**. I numeri li calcola `calcola_fattura.py` e vanno direttamente nell'output, **senza passare di nuovo dal modello**, che altrimenti potrebbe ricopiarli male.
  Se il modello non chiama il tool, è l'**harness** a insistere: la garanzia sta nel codice, non nel prompt.

Cosa conta nel confronto:

1. **Correttezza garantita vs probabile.** In A un JSON valido con numeri sbagliati è indistinguibile da uno giusto, a meno di ricalcolare. E se devi ricalcolare, tanto vale farlo in Python.
2. **Costo e scala.** In A il costo (token e tempo) cresce con la dimensione dei dati, e un modello con *reasoning* spende migliaia di token per fare somme. In B il costo è costante.
3. **Auditabilità.** Lo script si legge, si testa e dà lo stesso output ogni volta. Il ragionamento del modello no.
4. **Il modello resta utile dove serve.** Capisce la richiesta, sceglie lo strumento, scrive l'email al cliente (demo `agente`).

**Risultati misurati** (Qwen 3.6 35B-A3B Q6_K in LM Studio, contesto 32k, 3 prove per configurazione, 10/11/2026):

| Fattura | Metodo | JSON valido | Numeri corretti | Media per prova |
|---|---|---|---|---|
| 40 righe | A, il modello fa i conti | 3/3 | **2/3** | 52 s, ~9.800 token |
| 40 righe | B, script Python | n/a | **3/3** | 10 s, ~770 token |
| 150 righe | A, il modello fa i conti | 3/3 | **0/3** | 89 s, ~13.000 token |
| 150 righe | B, script Python | n/a | **3/3** | 53 s, ~11.800 token * |

Errori reali del metodo A: un caso ha restituito `numero_righe: 1` con il totale della **sola prima riga**. Un altro ha `numero_righe: 150` (corretto) ma l'imponibile della prima riga. Un altro ancora è sbagliato di circa 2.400 €.
**Tutti erano JSON perfettamente conformi allo schema.**

\* Misura fatta con una versione precedente del metodo B, che accettava solo `esegui_script` come prima chiamata. Una prova è costata 33k token di ragionamento prima di arrivare al tool.
Nella versione attuale B è un piccolo agent loop: se il modello chiama prima `carica_skill` (una mossa corretta), l'harness lo esegue e va avanti.
Il re-prompt scatta solo se il modello risponde senza nessuna tool call.
Una nuova misura dalla web UI (40 righe, solo B, 2 prove) ha dato 2/2 corretti, in media 13 s e circa 960 token.

> **Onestà sui risultati:** i modelli locali moderni con *reasoning* (es. Qwen 3.6) spesso indovinano le somme su fatture piccole.
> Non è questo il punto. Il punto è che **non è garantito**, costa molto di più e peggiora con i dati più grandi.
> Prova `--righe 150` e confronta tempi e token.
> Con il backend `mock` gli errori di A sono **simulati** (il programma lo dice esplicitamente). Usa `--backend local` per numeri veri.

---

### 7. JSON lunghi: A / B / C, validatori e LLM-as-judge (`python main.py lunghi`, oppure la scheda "JSON lunghi")

Compito più realistico: da un'**email d'ordine scritta a mano** si compila un **JSON lungo**, con una voce per riga (codice, descrizione, quantità, prezzo, sconto, importo) più i totali.
Con 40 righe sono 203 campi. L'email mescola cinque formati diversi, usa la virgola decimale e contiene righe "trappola" con numeri e percentuali che non sono articoli (es. "acconto del 15%").

| | Strategia | Chiamate LLM |
|---|---|---|
| **A** | il modello estrae **e** calcola tutto il JSON in un colpo (schema forzato) | 1 |
| **B** | il modello estrae la lista in un colpo, Python calcola | 1 |
| **C** | **pipeline multi-agente** (vedi sotto) | ~10–40 |

**Pipeline C** (`pipeline.py → metodo_C`):
```
Splitter (Python)         regex: trova le righe d'ordine, le divide in blocchi da 10
   ↓
Estrattori ×K (LLM)       in PARALLELO, uno per blocco: tanti JSON piccoli invece di uno enorme
   ↓
Validatori (Python)       schema · completezza (nessuna riga mancante/inventata/duplicata)
                          · ancoraggio (ogni valore deve comparire nella riga sorgente)
                          · regole di business (quantità > 0, sconto in [0,100))
   ↓ righe bocciate
Correttore (LLM)          rifà SOLO la riga bocciata, ricevendo l'errore esatto (max 2 tentativi)
   ↓
Giudice (LLM-as-judge)    rilegge testo + dati e segnala dubbi → il correttore riprova,
                          ma la modifica si accetta SOLO se passa i validatori
   ↓
Calcolatore (Python)      importi, imponibile, IVA, totale
```

**Perché C arriva al 100%:** ogni valore finale è stato **verificato dal codice** contro il testo sorgente. Se non si riesce a verificarlo, la riga viene segnalata come "non verificata" invece di passare in silenzio.
Il 100% non dipende dalla fortuna del modello: dipende dal fatto che **nulla esce senza un controllo deterministico**.

**Validatori vs giudice LLM:** il benchmark misura per entrambi quante righe sbagliate trovano (*trovati*), quante righe giuste segnalano per errore (*falsi allarmi*) e quante ne lasciano passare (*mancati*).
I validatori girano sugli output di tutti e tre i metodi. Il giudice gira in C e, con `--judge-ab`, anche su A e B.

**Metriche del benchmark:** tempo di completamento del task, campi corretti (%), JSON esatto sì/no, righe corrette, mancanti e inventate, totali corretti, token generati, chiamate LLM, correzioni e righe non verificate (C).

```bash
python main.py lunghi                                   # mock (errori SIMULATI)
python main.py lunghi --backend local --righe 40 --judge-ab
python main.py lunghi --backend local --righe 100 --metodi BC -n 3
```

**Risultati misurati** (Qwen 3.6 35B-A3B in LM Studio, contesto 32k, `max_tokens` 24000, 1 prova per configurazione, 11/11/2026):

| Righe (campi) | Metodo | JSON esatto | Campi ok | Tempo | Token | Chiamate LLM |
|---|---|---|---|---|---|---|
| 40 (203) | A | ✓ | 100% | 64 s | 14.389 | 1 |
| 40 (203) | B | ✓ | 100% | 36 s | 8.316 | 1 |
| 40 (203) | C | ✓ | 100% | 109 s | 28.703 | 8 |
| 150 (753) | A | ✗ | **0%** | 119 s | 24.000 | 1 |
| 150 (753) | B | ✓ | 100% | 82 s | 19.455 | 1 |
| 150 (753) | C | ✓ | **100%** | 885 s | 209.316 | 67 |

Come leggerli, onestamente:
- **A a 150 righe** ha esaurito `max_tokens` a metà del JSON: l'output è troncato e quindi inutilizzabile. È il modo tipico in cui i JSON lunghi falliscono.
- **B** è andato bene in entrambi i casi: estrarre senza calcolare è già un grande passo avanti. Però **niente lo verifica**: se avesse sbagliato, nessuno se ne sarebbe accorto.
- **In C, a 150 righe, un estrattore ha davvero sbagliato.** Su un blocco ha troncato i prezzi a numeri interi (`446` invece di `446,57`).
  I **validatori** hanno trovato tutte le 9 righe sbagliate con **0 falsi allarmi**. Il **correttore** ne ha sistemate la maggior parte.
  Le ultime 3 righe le ha segnalate il **giudice LLM** (3 su 3 giusti, 0 falsi allarmi), e la nuova estrazione è stata accettata perché passava i validatori.
  Risultato: 753 campi su 753 corretti.
- **Il prezzo di C** è tempo e token: questa è un'assicurazione, non un'ottimizzazione.
  Per ridurlo: blocchi più grandi (`--chunk 20`), meno effort, giudice solo sui blocchi corretti (`--no-judge` per misurare la differenza), un modello più piccolo come estrattore.
- Con il backend `mock` gli errori di A, B, degli estrattori e del giudice sono **simulati** apposta, per far vedere tutti i percorsi della pipeline.

### 8. Incidenti SOC: S / A / B / C su un corpus enorme, più modelli in parallelo (`python main.py incidenti`, scheda "Incidenti SOC")

Il compito è classificare incidenti di cybersecurity da **7 documenti**: log SIEM, alert EDR, threat intel, inventario degli asset, note degli analisti, email e policy.
Il corpus "medio" sono 196 KB, circa **112.000 token misurati** (log, hash e IP tokenizzano male: ~1,8 caratteri per token).

Trappole nel corpus:
- riclassificazioni scritte più avanti nelle note;
- falsi positivi dichiarati solo via email;
- righe SIEM che citano un incidente con `corr=-`;
- frasi che negano un collegamento;
- un riepilogo del CISO sbagliato.

I campi sono valutati **per tipo**:

| Tipo | Campi | Chi li sa fare |
|---|---|---|
| deterministici | hosts, src_ips, event_count, first_seen, last_seen, hashes | lo **script, sempre al 100%** |
| semantico | category | serve capire il testo |
| derivati | severity, MITRE | regola fissa sulla categoria |

I metodi:
- **S** solo script, con la categoria decisa da parole chiave. Le parole chiave sono **solo i nomi delle categorie** ("ransomware", "esfiltrazione", "falso positivo"…), cioè le regole che scriveresti senza aver letto i dati: le parafrasi ("file rinominati .lock2", "una chiavetta personale") non le riconosce. Indovina solo quando il testo nomina la categoria esplicitamente, e ci cade sulle trappole ("NON è collegato all'episodio di ransomware…"). Su 8 estrazioni da 20 incidenti: categoria 25–50%.
  *Nota*: fino all'11/11 la lista conteneva anche parafrasi copiate dal generatore del corpus (un'informazione che uno script reale non avrebbe), e S arrivava all'80%.
- **A** tutto il corpus all'LLM: se il corpus non entra nel contesto del modello, fallisce.
- **B** retrieval + LLM che compila tutti i campi di un incidente.
- **C** ibrido: lo script calcola i 6 campi deterministici. Due classificatori indipendenti (cronologico e dal più recente) propongono la categoria con una citazione testuale; se non concordano decide un **arbitro**. Seguono i **validatori** (enum, citazione presente nei documenti, coerenza con threat intel e byte in uscita), il **correttore**, il **giudice**, e infine lo script deriva severità e MITRE.
  Se una chiamata fallisce, l'incidente usa l'euristica dello script ed è segnato "non verificato".

**Più modelli insieme**: `--modelli a,b` (o le caselle nella UI). Ogni modello gira in un thread separato; lo script S gira una volta sola.

```bash
python main.py incidenti --scala medio                                   # mock
python main.py incidenti --backend local --scala medio --modelli "qwen/qwen3.6-35b-a3b,qwen3.8-27b@q8_k_xl"
```

**Pool di incidenti**: il generatore crea un insieme fisso di 100 incidenti (10 per categoria) e ogni run ne pesca a caso 6, 20 o 50; cambiano anche note, email, trappole e rumore. Con più prove, **ogni prova pesca una nuova estrazione** (numeri consecutivi a partire da quello mostrato); dentro una prova tutti i metodi e i modelli vedono lo stesso corpus, così il confronto resta equo. Per ripetere un run identico scrivi il numero di partenza nel campo "Estrazione" della UI o usa `--campione N`.

**Casi di confine tra categorie** (`REGOLE_CAT` in `incidenti.py`, inserite in `policy.md` e nei prompt di classificatore e giudice): un dipendente che porta fuori dati è `insider_threat`, non `data_exfiltration`; phishing = una persona ingannata, malware = codice che gira senza inganno descritto. Il campo `user=` dei log SIEM non basta a identificare un dipendente come autore.

**Email del CISO**: la trappola "Mi risulta che INC-xxxx fosse phishing, confermate?" è una domanda, non un fatto. Le regole dicono che contano solo aggiornamenti forensi ed email che riportano fatti. Prima la regola diceva "le email correggono le note", e C metteva phishing su incidenti di lateral movement, privilege escalation e DDoS. Il generatore inoltre non nega mai un incidente con sé stesso e non usa insider/esfiltrazione come falsa pista l'una dell'altra (sarebbe ambiguo anche per un umano).
Senza queste regole, nella run dell'11/11 tutti gli errori di categoria di C erano insider scambiati per esfiltrazione: un'ambiguità del benchmark, non del modello. Su un caso ambiguo più opinioni dello stesso modello non aiutano: due classificatori in disaccordo e un arbitro che sceglie tra due risposte plausibili.

**Ogni chiamata LLM viene registrata**: system, prompt, schema imposto, ragionamento del modello (`reasoning_content`), risposta, token (prompt, output, reasoning), **latenza** e **token/s**.
Nella UI c'è la tabella "Tutte le chiamate LLM", con filtri per modello, metodo, ruolo e testo; clicca una riga per leggerla tutta.
Ogni risultato riporta tempo di completamento, latenza media e p95 e tok/s.

**Primi risultati** (corpus "medio", 11/11/2026, due modelli in parallelo). Le run successive, con le regole di confine corrette e più prove, sono nella nota tecnica in `docs/`:

| Metodo | Qwen 3.6 35B-A3B (ctx 128k, PC locale) | Qwen 3.8 27B Q8 (ctx 152k, PC remoto) |
|---|---|---|
| S script | deterministici **100%** · categoria 80% · derivati 80% · 0 s (non dipende dal modello; misurato con la vecchia lista di parole chiave, oggi 25–50%) | idem |
| A solo LLM | **0%** in 10 prove su 10, ~4,6 min l'una | **0%** in 2 prove, ~30 min l'una |
| B LLM per incidente | deterministici 100% / 99,2% · categoria 100% · derivati 97,5% · ~7,5 min · 20 chiamate | non misurato (run fermato prima) |
| **C ibrido multi-agente** | **100% su tutto · 20/20 · 4,4 min · 44 chiamate** · latenza media 23 s · 63 tok/s | **100% su tutto · 20/20 · 10,9 min · 45 chiamate** · latenza media 50 s · 15 tok/s |

Cosa è successo davvero:
- **A, 35B**: il prompt di 112.594 token lascia ~15.900 token liberi nel contesto da 128k. Il modello li spende tutti **ragionando**: conta a mano gli eventi SIEM riga per riga. Arriva al limite (`finish_reason: length`) senza aver scritto il JSON.
- **A, 27B**: il corpus entra nel contesto, ma il modello usa 31.833 token di ragionamento per rifare a 18 tok/s il lavoro dello script, raggiunge `max_tokens` (32.768) dopo 30 minuti e la risposta è vuota.
- **B, 35B**: ottimo ma non perfetto, e gli errori cambiano da una prova all'altra.
  - Un conteggio eventi sbagliato (13 invece di 12: ha contato anche una riga-trappola con `corr=-`).
  - Il campo MITRE scritto due volte come oggetto (`{"insider_threat": "T1078"}`, `{"id": "T1110"}`) invece del solo codice.
- **C**: 0 disaccordi tra i due classificatori, 0 interventi dell'arbitro, nessun dubbio dal giudice.
  Un caso reale di rete di sicurezza sul 27B: per INC-0746 la categoria era giusta ma la citazione era parafrasata. I **validatori** l'hanno respinta, il **correttore** l'ha richiesta con l'errore esatto, e la seconda risposta è passata.
- C è **più veloce di B** pur facendo più del doppio delle chiamate: ogni chiamata chiede solo la categoria, quindi prompt e ragionamento sono corti.

Lezioni sulla configurazione:
- Il **contesto** (impostato in LM Studio al caricamento) e **`max_tokens`** (tetto della singola risposta, impostato nella UI) sono due cose diverse.
- Per far riuscire A sul corpus medio servirebbero ~150–160k di contesto e un `max_tokens` molto alto, con tempi di ore.
- A B e C bastano 32k di contesto e ~16k di `max_tokens`.
- Il **timeout per chiamata** va alzato per prompt enormi su PC remoti (campo nella UI, `--timeout` da riga di comando).

### 9. Codice e prompt (scheda "Codice & prompt")
- Elenco dei file con righe, descrizione e funzioni/classi.
- Visualizzatore con numeri di riga ed evidenziazione; clicca una funzione per saltarci.
- I **prompt esatti di tutti gli agenti** (agente, sub-agente, A, B, classificatore, seconda opinione, arbitro, correttore, giudice...), con system prompt, schema JSON e la funzione che li costruisce.
  Sono catturati da un'esecuzione reale col modello finto, quindi sono il testo vero.

### Run in corso e ricaricamento della pagina
I run vengono salvati su disco **mentre girano** (ogni ~3 s, stato "in corso"), sia dalla UI sia da riga di comando.
- Se ricarichi la pagina, compare il banner "run in corso" con **Segui**, e la scheda aperta viene ricordata.
- Nello **Storico** basta cliccare una riga per aprire il run; se è ancora in corso, la pagina continua a leggerlo dal vivo finché finisce.

## Estendere

- **Un nuovo tool:** `Tool(nome, descrizione, json_schema, funzione)` e `registry.add(...)`.
- **Una nuova skill:** crea `skills/<nome>/SKILL.md` e `skills/<nome>/scripts/<script>.py` (input JSON su stdin, output JSON su stdout).
- **Un altro server:** aggiungi un provider (scheda **Provider e dati** o `providers.json`). Il formato è lo stesso.
- **I tuoi dati:** una cartella in `datasets/` (vedi sopra e `datasets/README.md`).

---

## Licenza

MIT, vedi [`LICENSE`](LICENSE). La nota tecnica in `docs/` incorpora i font Familjen Grotesk, DM Sans e JetBrains Mono (SIL Open Font License).
