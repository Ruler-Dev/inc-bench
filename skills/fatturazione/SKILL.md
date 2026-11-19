---
name: fatturazione
description: Calcolo di imponibile, IVA e totale di una fattura. Usala per QUALSIASI conto su fatture.
---
# Skill: fatturazione

Regole:
1. NON fare mai somme o moltiplicazioni a mente: i numeri li calcola lo script.
2. Esegui lo script `calcola_fattura` con input `{"file": "<percorso del file fattura>"}`.
3. Riporta i numeri ESATTAMENTE come li restituisce lo script, senza arrotondarli o riscriverli.
4. Se lo script restituisce un errore, segnalalo: non inventare un risultato.

Formato della fattura (JSON): `righe[]` con `quantita`, `prezzo_unitario`, `sconto_pct`; `iva_pct` globale.
Ogni riga vale `quantita * prezzo_unitario * (1 - sconto_pct/100)`, arrotondata al centesimo.
