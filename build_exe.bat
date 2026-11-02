@echo off
rem ==========================================================================
rem Crea l'eseguibile dist\AVVIA_RFQ\AVVIA_RFQ.exe con PyInstaller.
rem Requisiti: Windows, Python 3.10-3.13 nel PATH, connessione internet.
rem Da lanciare nella cartella degli script (doppio clic).
rem ==========================================================================
cd /d "%~dp0"

echo [1/3] Ambiente Python dedicato (.venv_build)...
if not exist .venv_build python -m venv .venv_build || goto :errore
call .venv_build\Scripts\activate.bat || goto :errore

echo [2/3] Installazione librerie...
python -m pip install --upgrade pip || goto :errore
python -m pip install -r requirements.txt pyinstaller || goto :errore

echo [3/3] Creazione eseguibile...
pyinstaller --noconfirm --clean --onedir --console --name AVVIA_RFQ avvia_rfq.py || goto :errore

rem Cartella pronta all'uso: template e cartella input accanto all'eseguibile.
copy /Y RFQ_molds_form.xlsx dist\AVVIA_RFQ\ >nul 2>&1
if not exist dist\AVVIA_RFQ\input mkdir dist\AVVIA_RFQ\input

echo.
echo Eseguibile creato: dist\AVVIA_RFQ\AVVIA_RFQ.exe
echo Copiare l'intera cartella dist\AVVIA_RFQ dove serve.
pause
exit /b 0

:errore
echo.
echo ERRORE durante la creazione dell'eseguibile: vedere i messaggi sopra.
pause
exit /b 1
