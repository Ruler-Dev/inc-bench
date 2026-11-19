"""Script deterministico della skill 'fatturazione'.
Input (stdin): {"file": "data/fattura.json"}   Output (stdout): JSON con i totali.
Stesso input -> stesso output, sempre. Niente allucinazioni possibili."""
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

CENT = Decimal("0.01")


def calcola(fattura: dict) -> dict:
    imponibile = Decimal("0")
    for r in fattura["righe"]:
        riga = (Decimal(str(r["quantita"])) * Decimal(str(r["prezzo_unitario"]))
                * (1 - Decimal(str(r.get("sconto_pct", 0))) / 100))
        imponibile += riga.quantize(CENT, ROUND_HALF_UP)
    iva = (imponibile * Decimal(str(fattura["iva_pct"])) / 100).quantize(CENT, ROUND_HALF_UP)
    return {"numero_righe": len(fattura["righe"]), "imponibile": float(imponibile),
            "iva": float(iva), "totale": float(imponibile + iva)}


if __name__ == "__main__":
    args = json.load(sys.stdin)
    path = Path(args["file"]).resolve()
    data_dir = (Path.cwd() / "data").resolve()
    if data_dir not in path.parents:  # sandbox minima: solo file in data/
        sys.exit(f"Accesso negato: {path} non e' in {data_dir}")
    print(json.dumps(calcola(json.loads(path.read_text(encoding="utf-8")))))
