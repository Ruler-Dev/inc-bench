"""
Forzare uno schema JSON: tre livelli di garanzia crescente.

1. "prompt"      -> chiedi "rispondi in JSON" nel prompt, poi parse + validazione
                    + retry. Nessuna garanzia: il modello puo' aggiungere testo,
                    sbagliare tipi, inventare campi.
2. "constrained" -> response_format json_schema: il SERVER (LM Studio, vLLM con
                    XGrammar, llama.cpp con GBNF...) maschera i token non validi.
                    Il JSON e' valido PER COSTRUZIONE.
3. In ogni caso   -> validazione lato harness (difesa in profondita').

ATTENZIONE: anche al livello 2 lo schema garantisce la FORMA, non la VERITA'.
{"totale": 1234.56} e' JSON perfetto anche se il totale giusto e' 1243.65.
"""
from __future__ import annotations

import json

from .schema import validate


def ask_json(llm, prompt: str, schema: dict, mode: str = "constrained",
             max_retries: int = 2, name: str = "output", system: str | None = None) -> dict:
    """Ritorna {"data": ..., "attempts": n, "errors": [...], "raw": str}."""
    messages = [{"role": "user", "content": prompt}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    rf = None
    if mode == "prompt":
        messages[-1]["content"] += ("\n\nRispondi SOLO con un oggetto JSON conforme "
                                    f"a questo schema:\n{json.dumps(schema)}")
    else:
        rf = {"type": "json_schema",
              "json_schema": {"name": name, "strict": True, "schema": schema}}
    all_errors: list[str] = []
    raw = ""
    for attempt in range(1, max_retries + 2):
        msg = llm.chat(messages, response_format=rf)
        raw = msg.get("content") or ""
        # tolleriamo il testo intorno ("Ecco il JSON: ```json {...} ```"): prendiamo
        # dalla prima { all'ultima }. Gli harness veri fanno trucchi simili.
        start, end = raw.find("{"), raw.rfind("}")
        candidate = raw[start:end + 1] if start != -1 and end > start else raw
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as e:
            errs = [f"JSON non valido: {e}"]
        else:
            errs = validate(data, schema)
            if not errs:
                return {"data": data, "attempts": attempt, "errors": all_errors, "raw": raw}
        all_errors += [f"tentativo {attempt}: {x}" for x in errs]
        # retry: rimandiamo l'errore al modello (stesso principio dei tool_result)
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": "Output non valido:\n" + "\n".join(errs)
                      + "\nRiprova rispettando lo schema."}]
    return {"data": None, "attempts": max_retries + 1, "errors": all_errors, "raw": raw}
