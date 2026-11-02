# -*- coding: utf-8 -*-
"""
Diagnostica RFQ: verifica che file STEP in input/ e righe di settings.csv
si corrispondano al 100%, con lo stesso abbinamento usato da
compila_rfq_form.py.

Esito OK solo se:
- il template RFQ_molds_form.xlsx è presente;
- ogni file STEP ha una riga in settings.csv;
- ogni riga di settings.csv ha un file STEP;
- nessuna riga è abbinata a più file STEP (abbinamento ambiguo).

Uso autonomo: codice di uscita 0 se OK, 1 altrimenti.
"""

import os
import sys

from rfq_common import (
    PATH_INPUT,
    PATH_SETTINGS,
    PATH_TEMPLATE,
    cfg_get,
    find_cfg_for_step,
    list_step_files,
    load_settings,
)


def run():
    """Stampa il report e ritorna True se la corrispondenza è completa."""
    print("=" * 70)
    print("REPORT DIAGNOSTICA RFQ")
    print("=" * 70)

    problems = []

    if not os.path.exists(PATH_TEMPLATE):
        problems.append(f"Template non trovato: {PATH_TEMPLATE}")

    cfg_list, delimiter = load_settings()
    if delimiter is None:
        problems.append(f"settings.csv non trovato: {PATH_SETTINGS}")
    else:
        print(f"\nsettings.csv: {len(cfg_list)} righe (delimitatore '{delimiter}')")
        for idx, cfg in enumerate(cfg_list, start=1):
            print(
                f"   [{idx}] cad_filename: '{cfg_get(cfg, 'cad_filename')}'"
                f"  |  part_name: '{cfg_get(cfg, 'part_name')}'"
            )

    step_files = list_step_files()
    if not step_files:
        problems.append(f"Nessun file STEP in {PATH_INPUT}")

    # Abbinamento STEP -> riga CSV
    matched = {}
    missing = []
    for name in step_files:
        cfg, kind = find_cfg_for_step(name, cfg_list)
        if cfg is None:
            missing.append(name)
        else:
            matched[name] = (cfg, kind)

    print(f"\nMATCH TROVATI: {len(matched)}/{len(step_files)}")
    for name, (cfg, kind) in matched.items():
        print(f"   [{kind:>10}] STEP: {name}")
        print(
            f"                CAD:  '{cfg_get(cfg, 'cad_filename')}'"
            f" -> part_name: '{cfg_get(cfg, 'part_name')}'"
        )

    if missing:
        print(f"\n[X] FILE STEP SENZA RIGA IN settings.csv: {len(missing)}")
        for name in missing:
            print(f"   - {name}")
        problems.append(f"{len(missing)} file STEP senza riga in settings.csv")

    # Righe CSV senza STEP, o abbinate a più STEP
    users = {id(cfg): [] for cfg in cfg_list}
    for name, (cfg, _) in matched.items():
        users[id(cfg)].append(name)

    orphans = [cfg for cfg in cfg_list if not users[id(cfg)]]
    shared = [cfg for cfg in cfg_list if len(users[id(cfg)]) > 1]

    if orphans:
        print(f"\n[X] RIGHE DI settings.csv SENZA FILE STEP: {len(orphans)}")
        for cfg in orphans:
            print(
                f"   - cad_filename: '{cfg_get(cfg, 'cad_filename')}'"
                f"  |  part_name: '{cfg_get(cfg, 'part_name')}'"
            )
        problems.append(f"{len(orphans)} righe di settings.csv senza file STEP")

    if shared:
        print(f"\n[X] RIGHE DI settings.csv ABBINATE A PIÙ FILE STEP: {len(shared)}")
        for cfg in shared:
            print(f"   - cad_filename: '{cfg_get(cfg, 'cad_filename')}'")
            for name in users[id(cfg)]:
                print(f"       <- {name}")
        problems.append(
            f"{len(shared)} righe di settings.csv abbinate a più file STEP "
            "(rendere univoci i valori di cad_filename)"
        )

    print("\n" + "=" * 70)
    if problems:
        print("ESITO: BLOCCO")
        for p in problems:
            print(f"   - {p}")
        print("   Correggere settings.csv (colonna 'cad_filename') o i file in input.")
        return False

    print(f"ESITO: TUTTO OK, {len(matched)} file pronti per l'analisi geometrica.")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
