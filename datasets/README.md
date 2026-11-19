# Dataset per il benchmark Incidenti

Ogni sottocartella è un dataset: `datasets/<nome>/`. Il nome può contenere lettere, numeri, `_`, `.` e `-`.
`esempio/` è un corpus sintetico generato con `python main.py esporta --dataset esempio --scala piccolo --campione 0`
(6 incidenti, etichettato). Gli altri dataset sono esclusi da git.

## File

Solo `siem_export.log` è obbligatorio. Gli altri file arricchiscono il contesto che ricevono i metodi A, B e C.
Se un file manca, viene trattato come vuoto. Se manca `policy.md`, vale la policy predefinita (`incidenti.policy_md()`).

### `siem_export.log` (obbligatorio)
Un evento per riga: timestamp ISO 8601 in testa, poi coppie `chiave=valore`. I valori con spazi vanno tra virgolette.

```
2026-11-03T08:14:02Z sev=6 host=ws-012 src=203.0.113.40 dst=10.0.0.5 user=m.rossi rule=AUTH_FAIL bytes_out=512 corr=INC-0101 msg="auth fail"
2026-11-03T08:15:40Z sev=2 host=ws-044 src=10.0.3.7 dst=10.0.0.9 user=l.bianchi rule=LOGIN_OK bytes_out=900 corr=- msg="ok"
```

- `corr=<ID>` lega l'evento a un incidente; `corr=-` indica rumore. **Gli incidenti del dataset sono i valori distinti di `corr`.**
- Da questi eventi lo script calcola i campi deterministici: `hosts`, `src_ips`, `event_count`, `first_seen`, `last_seen`.
- Campi usati: `host`, `src`, `rule`, `bytes_out` (numero), `corr`. Gli altri sono contesto.
- Gli ID possono avere qualunque formato senza spazi. È meglio evitare ID che siano prefisso di altri (`INC-1` e `INC-12`): il retrieval cerca l'ID come sottostringa.

### `analyst_notes.md`
Testo libero. I paragrafi sono separati da una riga vuota. Ogni paragrafo che contiene l'ID di un incidente entra nel suo contesto.

### `email_thread.txt`
Email separate da una riga `-----`. Ogni email che contiene l'ID di un incidente entra nel suo contesto.

```
-----
Da: it-ops@example.com
Oggetto: Re: INC-0101
Data: 2026-11-04T10:00:00Z

Ciao SOC, riguardo INC-0101: era il test concordato, nessun attacco reale.
```

### `edr_alerts.jsonl`
Un oggetto JSON per riga. Campi usati: `sha256`, `corr` (ID dell'incidente o `null`).
`hashes` = hash distinti degli alert con `corr=<ID>`.

### `threat_intel.txt`
Una riga per hash: `sha256=<hash> famiglia=<nome> tipo=<tipo> fonte=<fonte> confidenza=<0-100>`.

### `asset_inventory.csv`
Intestazione `host,owner,criticality,location`; `criticality` ∈ `low`, `medium`, `high`, `critical`.
Serve a derivare la severità (+1 livello se un host è `critical`).

### `policy.md`
Regole di classificazione in linguaggio naturale, incluse in alcuni prompt. Le categorie ammesse sono fisse:
`phishing`, `malware`, `ransomware`, `brute_force`, `data_exfiltration`, `privilege_escalation`,
`lateral_movement`, `ddos`, `insider_threat`, `false_positive`.

### `truth.json` (etichette, facoltativo)

```json
{"incidents": [
  {"id": "INC-0101", "category": "false_positive"},
  {"id": "INC-0102", "category": "brute_force", "severity": "high"}
]}
```

- Basta `id` e `category`. I campi deterministici vengono calcolati dai log; `severity` e `mitre` vengono derivati dalla policy.
- Un valore scritto esplicitamente (per esempio `severity`) prevale su quello derivato.
- Ogni `id` deve comparire come `corr=` in `siem_export.log`.
- Gli incidenti presenti nei log ma assenti da `truth.json` vengono contati come "inventati" se un metodo li restituisce: etichettali tutti.

**Senza `truth.json`** il benchmark non assegna punteggi: mostra l'output di ogni metodo e ciò che i validatori deterministici hanno segnalato
(categoria non ammessa, citazione assente dai documenti, campi incoerenti con i log). In questo caso serve un modello vero (`--backend local`).

## Caricare un dataset

- **Dalla web UI**: scheda *Provider e dati* → *Carica dataset*. I file devono avere i nomi indicati sopra (massimo 50 MB ciascuno).
- **A mano**: crea la cartella e copia i file.
- **Partendo dall'esempio**: `python main.py esporta --dataset mio --scala medio --campione 7`, poi modifica i file.

## Riservatezza

Il benchmark manda il contenuto dei documenti ai modelli dei provider configurati. Con un provider cloud i dati escono dalla tua macchina.
Prima di usare log reali, rimuovi o pseudonimizza nomi, email e indirizzi.
