# Note SOC – turni 03-04/11/2026

Formato libero, scritte durante i turni. Le note più recenti prevalgono.

## Turno giorno – R. Bianchi

INC-6769: da ws-010 escono flussi DNS molto lunghi verso lo stesso dominio, sembra tunneling di dati. Host coinvolti tra cui ws-010. In analisi.

INC-4192: l'utente svc_sql è passato da utente standard ad amministratore locale sfruttando un driver vulnerabile. Host coinvolti tra cui fs-01. In analisi.

INC-0921: privilege escalation tramite exploit del kernel su ws-033. Host coinvolti tra cui ws-033. In analisi.

Nota: INC-0921 NON è collegato all'episodio di privilege escalation di INC-4773, anche se gli orari coincidono.

INC-9671: una macro di un documento Office ha scaricato ed eseguito un binario non firmato su web-01. In analisi.

INC-4773: su srv-app-07 un servizio è stato riconfigurato per girare come SYSTEM da un utente senza diritti. Host coinvolti tra cui srv-app-07. In analisi.

INC-3106: esfiltrazione di dati via HTTPS verso un dominio sconosciuto da ws-010. Host coinvolti tra cui ws-010. In analisi.


## Aggiornamenti successivi

AGGIORNAMENTO 11:37 su INC-9671: dopo l'analisi forense la prima ipotesi era sbagliata. In realtà da web-01 partono sessioni RDP e SMB verso altri server usando credenziali di servizio. Riclassificare.

