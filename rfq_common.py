# -*- coding: utf-8 -*-
"""
Parti comuni a diagnostica.py e compila_rfq_form.py: percorsi, lettura di
settings.csv e abbinamento file STEP <-> righe del CSV. Stare in un solo
modulo garantisce che la diagnostica verifichi esattamente l'abbinamento
usato dalla compilazione.
"""

import csv
import os
import re
import sys


# =========================================================================
# PERCORSI
# =========================================================================
# Cartella del programma: quella dell'eseguibile se impacchettato con
# PyInstaller, altrimenti quella degli script.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PATH_SETTINGS = os.path.join(BASE_DIR, "settings.csv")
PATH_TEMPLATE = os.path.join(BASE_DIR, "RFQ_molds_form.xlsx")
PATH_INPUT = os.path.join(BASE_DIR, "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


# =========================================================================
# SETTINGS.CSV
# =========================================================================
def normalize_key(value):
    """'Part Name', 'part_name', 'PART-NAME' -> 'partname'."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def cfg_get(cfg, key, default=""):
    """Valore di una colonna del CSV, ignorando maiuscole, spazi e '_'."""
    if key in cfg:
        return cfg[key]

    target = normalize_key(key)
    for k, v in cfg.items():
        if normalize_key(k) == target:
            return v
    return default


def load_settings():
    """
    Righe di settings.csv e delimitatore usato. Il delimitatore (';' o ',')
    è riconosciuto: Excel in italiano salva i CSV con ';' perché la virgola
    è il separatore decimale. Righe vuote e colonne senza nome ignorate.
    """
    if not os.path.exists(PATH_SETTINGS):
        return [], None

    with open(PATH_SETTINGS, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","

        rows = []
        for row in csv.DictReader(f, delimiter=delimiter):
            clean = {
                k.strip(): (v or "").strip()
                for k, v in row.items()
                if isinstance(k, str) and k.strip()
            }
            if any(clean.values()):
                rows.append(clean)

    return rows, delimiter


def list_step_files():
    """Nomi dei file STEP in input/, in ordine alfabetico."""
    if not os.path.isdir(PATH_INPUT):
        return []
    return sorted(f for f in os.listdir(PATH_INPUT) if f.lower().endswith((".step", ".stp")))


def find_cfg_for_step(filename, cfg_list):
    """
    Riga di settings.csv per un file STEP e tipo di abbinamento:
    'esatto' se cad_filename è identico al nome file, 'flessibile' se uno
    dei due nomi (senza estensione, maiuscole ignorate) contiene l'altro.
    Ritorna (None, None) se nessuna riga corrisponde.
    """
    stem = os.path.splitext(filename)[0].lower()

    for cfg in cfg_list:
        if cfg_get(cfg, "cad_filename").strip() == filename:
            return cfg, "esatto"

    for cfg in cfg_list:
        cad = os.path.splitext(cfg_get(cfg, "cad_filename").strip())[0].lower()
        if cad and (cad in stem or stem in cad):
            return cfg, "flessibile"

    return None, None
