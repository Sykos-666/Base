@echo off
rem Avvio senza eseguibile, con il Python installato: diagnostica e, solo se
rem la corrispondenza settings.csv <-> input e' completa, compilazione.
cd /d "%~dp0"
python avvia_rfq.py
pause
