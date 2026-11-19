# Policy di classificazione incidenti

Campi per ogni incidente (identificato dal campo corr= nei log SIEM e nell'EDR):
- hosts, src_ips: valori distinti dei campi host= e src= degli eventi SIEM con corr=<ID> (ordinati)
- event_count: numero di eventi SIEM con corr=<ID> (le righe con corr=- NON contano, anche se citano l'ID nel msg)
- first_seen / last_seen: timestamp minimo e massimo di quegli eventi
- hashes: sha256 degli alert EDR con corr=<ID> (ordinati)
- category: una tra phishing, malware, ransomware, brute_force, data_exfiltration, privilege_escalation, lateral_movement, ddos, insider_threat, false_positive. Vale l'informazione PIÙ RECENTE (aggiornamenti forensi, email che riportano fatti); domande o supposizioni ('mi risulta che...') non sono prove. Attività autorizzate, test o regole mal configurate = false_positive. Casi di confine: se chi porta fuori o copia i dati è un DIPENDENTE o un interno (anche in uscita dall'azienda), la categoria è insider_threat, anche se i dati escono verso un cloud o uno storage esterno; data_exfiltration solo quando il trasferimento è opera di un attaccante esterno o di un malware, senza un dipendente identificato come autore DALLE NOTE O DALLE EMAIL (il campo user= dei log SIEM è solo l'account registrato dalla regola, non indica chi ha portato fuori i dati). phishing = una persona viene ingannata (mail, link, pagina falsa); malware = un codice malevolo gira su un host senza inganno descritto.
- severity: base per categoria {"phishing": "medium", "malware": "medium", "ransomware": "critical", "brute_force": "medium", "data_exfiltration": "high", "privilege_escalation": "high", "lateral_movement": "high", "ddos": "medium", "insider_threat": "high", "false_positive": "informational"}; +1 livello se almeno uno degli hosts ha criticality=critical in asset_inventory.csv (max critical; false_positive resta informational)
- mitre: {"phishing": "T1566", "malware": "T1204", "ransomware": "T1486", "brute_force": "T1110", "data_exfiltration": "T1048", "privilege_escalation": "T1068", "lateral_movement": "T1021", "ddos": "T1498", "insider_threat": "T1078", "false_positive": "N/A"}

