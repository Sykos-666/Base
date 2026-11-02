# Base

## Compilazione form RFQ stampi

Compila `RFQ_molds_form.xlsx` a partire dai file STEP in `input\` e dai dati in `settings.csv`.

| File | Ruolo |
|---|---|
| `avvia_rfq.py` | Punto di ingresso: diagnostica e, solo se è tutto OK, compilazione |
| `diagnostica.py` | Verifica che file STEP e righe di `settings.csv` si corrispondano 1:1 |
| `compila_rfq_form.py` | Analisi geometrica e compilazione del form |
| `rfq_common.py` | Percorsi, lettura di `settings.csv`, abbinamento STEP ↔ righe (usati da entrambi) |
| `build_exe.bat` | Crea l'eseguibile `dist\AVVIA_RFQ\AVVIA_RFQ.exe` |
| `AVVIA_RFQ.bat` | Avvio con il Python installato, senza eseguibile |

### Cartella di lavoro

```
AVVIA_RFQ.exe (oppure gli script .py)
RFQ_molds_form.xlsx
settings.csv          separatore ';' o ',' (riconosciuto)
input\                file .step / .stp
output\               creata dal programma: Excel, viste\, analisi.log
```

La compilazione parte solo se:
- il template è presente;
- ogni file STEP ha una riga in `settings.csv` (colonna `cad_filename`);
- ogni riga ha un file STEP;
- nessuna riga è abbinata a più file STEP.

### Creare l'eseguibile (Windows)

Con Python 3.10–3.13 installato, doppio clic su `build_exe.bat`. Il risultato è la cartella
`dist\AVVIA_RFQ` (circa 1,3 GB, contiene Python e CadQuery): va copiata per intero, l'eseguibile
non funziona da solo. Accanto ad `AVVIA_RFQ.exe` vanno messi `settings.csv` e i file STEP in `input\`.
