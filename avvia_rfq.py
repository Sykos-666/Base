# -*- coding: utf-8 -*-
"""
Avvio RFQ: esegue diagnostica.py e, se e solo se la corrispondenza tra
settings.csv e i file STEP in input/ è completa al 100%, avvia
compila_rfq_form.py.

È il punto di ingresso dell'eseguibile AVVIA_RFQ (vedi build_exe.bat).
"""

import sys

import diagnostica


def main():
    print("FASE 1/2 - Diagnostica settings.csv <-> input\n")
    if not diagnostica.run():
        print("\nCompilazione NON avviata.")
        return 1

    print("\nFASE 2/2 - Compilazione form RFQ\n")
    # Import solo ora: CadQuery è pesante da caricare.
    import compila_rfq_form
    return compila_rfq_form.run()


if __name__ == "__main__":
    code = main()
    # Eseguibile lanciato con doppio clic: la finestra resta aperta.
    if getattr(sys, "frozen", False):
        input("\nPremi Invio per chiudere...")
    sys.exit(code)
