# -*- coding: utf-8 -*-
"""
Pipeline NACE SystemOne - batch su una cartella di state.

Per ogni file JSON trovato nella cartella indicata (anche nelle sottocartelle) e usato
come "state", invia la richiesta definita in richiesta.json (stesse domande) all'API
NACE (https://console.nace.ai/v1/systemone) per ciascun modello selezionato, raccoglie
le risposte e produce un JSON con l'analisi completa:
  - filtro dei modelli troppo correlati (soglia r)
  - per ogni state e domanda: consenso tra modelli + test statistici
  - per ogni domanda: aggregati su tutti gli state, Fleiss kappa tra modelli
  - per ogni modello: profilo e accordo con il consenso
  - tutto sia sul dataset originale sia su quello filtrato

Requisiti:  pip install httpx numpy pandas scipy statsmodels
Chiave API: variabile d'ambiente NACE_API_KEY (mai scritta nel codice)
  Windows PowerShell:  $env:NACE_API_KEY = "nace_sk_..."
  Linux/macOS:         export NACE_API_KEY="nace_sk_..."

Uso:
  python jevai.py CARTELLA_STATE --richiesta richiesta.json --r 0.90
  python jevai.py CARTELLA_STATE --models drex-max-v1.1,altro-modello --r 0.90
  python jevai.py --analyze-only output_raw.json --r 0.85                 # solo analisi

In richiesta.json:
  - "state" è ignorato (viene sostituito da ciascun file della cartella; può restare un segnaposto)
  - "model": id del modello NACE, oppure un segnaposto (es. "xxxx") per usare il default
    (drex-max-v1.1). --models (lista separata da virgole) ha la precedenza.
    I test tra modelli (correlazione, Fleiss kappa, z/chi/ANOVA per state) richiedono >= 2 modelli.
"""
import argparse
import copy
import json
import os
import re
import sys
import time
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.stats as stats
import statsmodels.stats.proportion as smp
import statsmodels.stats.inter_rater as irr

# ============================== CONFIG ==============================
NACE_URL = os.environ.get("NACE_URL", "https://console.nace.ai/v1/systemone")
NACE_CLIENT = os.environ.get("NACE_CLIENT", "nace-quickstart/1")
DEFAULT_MODEL = "drex-max-v1.1"

RAW_OUT = "output_raw.json"
FILTERED_OUT = "output_filtered.json"
REPORT_OUT = "analisi_statistica.json"
OUTPUT_NAMES = {RAW_OUT, FILTERED_OUT, REPORT_OUT}  # mai trattati come state

DEFAULT_R = 0.87
DEFAULT_RICHIESTA = "richiesta.json"
RESULT_TIMEOUT_S = 120
PAUSE_S = 1.0

YES_ALIASES = {"yes", "y", "si", "sì", "true", "vero", "1"}
NO_ALIASES = {"no", "n", "false", "falso", "0"}


class FatalAPIError(RuntimeError):
    """Errore che rende inutile proseguire (es. chiave API non valida)."""


# ============================== UTILITY ==============================
def ask_threshold(cli_value):
    if cli_value is not None:
        r = cli_value
    else:
        raw = input(f"Soglia di correlazione r (0 < r <= 1) [default {DEFAULT_R}]: ").strip()
        raw = raw.replace(",", ".")
        try:
            r = float(raw) if raw else DEFAULT_R
        except ValueError:
            sys.exit("Valore non numerico: interrotto.")
    if not (0 < r <= 1):
        sys.exit(f"Soglia non valida: {r} (deve essere 0 < r <= 1).")
    print(f">> Soglia correlazione scelta: r > {r}")
    return r


def clean_keys(obj):
    if isinstance(obj, dict):
        return {k.strip(): clean_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_keys(v) for v in obj]
    return obj


def to_jsonable(o):
    """numpy -> tipi Python; NaN/inf -> null (JSON valido)."""
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return to_jsonable(o.tolist())
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if np.isfinite(f) else None
    return o


def write_json(path, obj):
    Path(path).write_text(json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False), encoding="utf-8")


def rnd(x, nd=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if np.isfinite(x) else None


def extract_json(text):
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


# ============================== INPUT: RICHIESTA E STATE ==============================
def load_richiesta(path):
    p = Path(path).expanduser()
    if not p.is_file():
        sys.exit(f"File richiesta non trovato: {path}")
    text = p.read_text(encoding="utf-8-sig")
    try:
        req = json.loads(text)
    except json.JSONDecodeError:
        # Il campo state può contenere un segnaposto non JSON (es.  "state": file state.json,)
        fixed = re.sub(r'("state"\s*:\s*)(?![{\["]|null\b|true\b|false\b|-?\d)[^,\n}]+',
                       r"\1null", text)
        try:
            req = json.loads(fixed)
        except json.JSONDecodeError as e:
            sys.exit(f"{path} non è un JSON valido: {e}")
    req = clean_keys(req)
    if not isinstance(req, dict) or not isinstance(req.get("questions"), dict) or not req["questions"]:
        sys.exit(f"{path}: campo 'questions' mancante o vuoto.")
    for qid, q in req["questions"].items():
        if not isinstance(q, dict):
            sys.exit(f"{path}: la domanda '{qid}' non è un oggetto JSON.")
    return req, p.resolve()


def requested_model(req):
    """Modello fisso indicato in richiesta.json, o None se è un segnaposto."""
    m = str(req.get("model") or "").strip()
    if not m or re.fullmatch(r"x+", m, re.I) or m.lower() in ("all", "tutti", "*", "auto"):
        return None
    return m


def question_categories(q):
    """Categorie di risposta: criteri per le domande 'choice', altrimenti yes/no."""
    if isinstance(q.get("criteria"), dict) and q["criteria"]:
        return list(q["criteria"])
    return ["yes", "no"]


def find_state_files(folder, exclude=()):
    root = Path(folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Cartella non trovata: {folder}")
    excl = {Path(e).resolve() for e in exclude if e}
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() == ".json"
        and p.name not in OUTPUT_NAMES and p.resolve() not in excl
    )
    if not files:
        sys.exit(f"Nessun file .json trovato in {root}")
    return root.resolve(), files


def load_states(root, files):
    states, invalid = [], {}
    for p in files:
        rel = p.resolve().relative_to(root).as_posix()
        try:
            states.append((rel, clean_keys(json.loads(p.read_text(encoding="utf-8-sig")))))
        except Exception as e:
            invalid[rel] = str(e)[:200]
    print(f">> {len(states)} state validi trovati in {root}" + (f" ({len(invalid)} scartati)" if invalid else ""))
    for rel, err in invalid.items():
        print(f"   - scartato {rel}: {err}")
    if not states:
        sys.exit("Nessuno state JSON valido.")
    return states, invalid


# ============================== PARSING RISPOSTE ==============================
def norm_label(label, q, categories):
    """Riconduce un'etichetta (chiave di probabilities o choice) a una categoria della domanda."""
    if label is None or isinstance(label, (dict, list)):
        return None
    low = str(label).strip().lower()
    for c in categories:
        if low == c.lower():
            return c
    if "yes" in categories and "no" in categories:
        if low in YES_ALIASES:
            return "yes"
        if low in NO_ALIASES:
            return "no"
    descr = q["criteria"] if isinstance(q.get("criteria"), dict) else {c: q.get(c) for c in categories}
    for c, d in descr.items():
        if isinstance(d, str) and low == d.strip().lower():
            return c
    return None



def _float(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def map_probs(d, q, categories):
    """Dizionario {etichetta: probabilità} -> {categoria: probabilità} (solo voci riconosciute)."""
    probs = {}
    for k, v in d.items():
        c = norm_label(k, q, categories)
        v = _float(v) if not isinstance(v, bool) else None
        if c is not None and v is not None:
            probs[c] = probs.get(c, 0.0) + v
    return probs


def value_to_decision(val, q, categories, depth=0):
    """
    Converte la risposta NACE di una domanda (answers[qid][type]) in
    {choice, probabilities, confidence, tipo_valore}. Formati gestiti:
      numero 0-1 (o 0-100)  -> probabilità di "yes" (domande binarie)
      bool / "yes" / "no"   -> risposta categorica (probabilità 1/0)
      {"probabilities": {...}, "choice": ..., "confidence": ...}
      {"yes": 0.8, "no": 0.2}  o  {"value"/"probability"/"answer"/...: <uno dei precedenti>}
    """
    binary = len(categories) == 2
    conf, choice, probs, kind = None, None, None, None

    if isinstance(val, dict):
        conf = _float(val.get("confidence"))
        for key in ("choice", "answer", "label", "decision"):
            choice = norm_label(val.get(key), q, categories)
            if choice:
                break
        if isinstance(val.get("probabilities"), dict):
            probs, kind = map_probs(val["probabilities"], q, categories), "probabilities"
        else:
            probs, kind = map_probs(val, q, categories), "probabilities"
        if not probs:
            probs = None
            if depth < 3:
                for key in (q.get("type"), "value", "probability", "prob", "p", "p_yes", "score",
                            "result", "answer", "choice", "label", "decision", "noul"):
                    if key and key in val and val[key] is not None:
                        inner = value_to_decision(val[key], q, categories, depth + 1)
                        if inner[0]:
                            d = inner[0]
                            if conf is not None and d["confidence"] is None:
                                d["confidence"] = conf
                            if choice and choice != d["choice"] and d["tipo_valore"] == "categorica":
                                d["choice"] = choice
                            return d, None
            if choice is None:
                return None, f"formato risposta non riconosciuto: {json.dumps(val, ensure_ascii=False)[:150]}"
            probs, kind = {choice: 1.0}, "categorica"
    elif isinstance(val, bool):
        if not binary:
            return None, f"valore booleano per domanda non binaria: {val}"
        probs, kind = {categories[0]: 1.0 if val else 0.0}, "categorica"
    elif isinstance(val, (int, float)):
        v = _float(val)
        if v is None or not binary:
            return None, f"valore numerico non interpretabile: {val}"
        probs, kind = {categories[0]: v / 100 if v > 1 else v}, "probabilita_yes"
    elif isinstance(val, str):
        c = norm_label(val, q, categories)
        if c is not None:
            probs, kind = {c: 1.0}, "categorica"
        elif _float(val.strip().rstrip("%")) is not None and binary:
            v = _float(val.strip().rstrip("%"))
            probs, kind = {categories[0]: v / 100 if v > 1 else v}, "probabilita_yes"
        else:
            return None, f"valore non riconosciuto: {val[:150]}"
    else:
        return None, f"tipo di risposta non gestito: {type(val).__name__}"

    if sum(probs.values()) > 1.5:  # espresse in percentuale
        probs = {c: v / 100 for c, v in probs.items()}
    if binary and len(probs) == 1:
        (c, v), = probs.items()
        probs[categories[1] if c == categories[0] else categories[0]] = max(0.0, 1.0 - v)
    vec = [probs.get(c, 0.0) for c in categories]
    if choice is None:
        choice = categories[int(np.argmax(vec))]
    return {"choice": choice, "probabilities": dict(zip(categories, vec)),
            "confidence": conf, "tipo_valore": kind}, None


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        r = _find_key(v, key)
        if r is not None:
            return r
    return None


def parse_response(j, questions):
    """Risposta NACE -> (decisioni normalizzate, errori per domanda)."""
    answers = j.get("answers") if isinstance(j, dict) else None
    if not isinstance(answers, dict):
        answers = None
    decisions, errors = {}, {}
    for qid, q in questions.items():
        a = answers.get(qid) if answers is not None else _find_key(j, qid)
        if a is None:
            errors[qid] = "domanda assente nella risposta"
            continue
        if isinstance(a, dict) and q.get("type") in a:  # es. answers["Funzioni"]["noul"]
            outer_conf = _float(a.get("confidence"))
            inner = a[q.get("type")]
            if isinstance(inner, dict) or outer_conf is None:
                a = inner
            else:
                a = {"value": inner, "confidence": outer_conf}
        d, err = value_to_decision(a, q, question_categories(q))
        if d:
            decisions[qid] = d
        else:
            errors[qid] = err
    return decisions, errors


# ============================== FASE 0: RACCOLTA VIA API NACE ==============================
def nace_headers():
    key = os.environ.get("NACE_API_KEY", "").strip()
    if not key:
        sys.exit("Imposta la variabile d'ambiente NACE_API_KEY con la tua chiave NACE (nace_sk_...).")
    return {"Authorization": f"Bearer {key}", "X-Nace-Client": NACE_CLIENT}


def state_payload(state_obj, mode):
    if mode == "testo" and not isinstance(state_obj, str):
        return json.dumps(state_obj, ensure_ascii=False, indent=1)
    return state_obj


def call_nace(client, model, state_obj, req, opts):
    """
    Invia richiesta.json (con model e state sostituiti).
    Restituisce (risposta_grezza, decisioni, errori_per_domanda, errore_globale).
    opts["state_mode"]: "json" (oggetto), "testo" (JSON serializzato in stringa) o "auto"
    (prova l'oggetto e, se l'API lo rifiuta con 400/422, ripiega sulla stringa).
    """
    import httpx

    body = {k: copy.deepcopy(v) for k, v in req.items() if k not in ("model", "state")}
    body["model"] = model
    mode = "json" if opts["state_mode"] == "auto" else opts["state_mode"]
    can_fallback = opts["state_mode"] == "auto" and not isinstance(state_obj, str)
    attempt = 0
    while True:
        body["state"] = state_payload(state_obj, mode)
        try:
            resp = client.post(NACE_URL, json=body)
        except httpx.TransportError as e:
            if attempt < 3:
                attempt += 1
                time.sleep(5 * attempt)
                continue
            return None, {}, {}, f"errore di rete: {str(e)[:120]}"

        if resp.status_code in (401, 403):
            raise FatalAPIError(f"HTTP {resp.status_code}: chiave NACE_API_KEY non valida o senza permessi "
                                f"({resp.text[:150]})")
        if (resp.status_code == 429 or resp.status_code >= 500) and attempt < 4:
            attempt += 1
            wait = _float(resp.headers.get("retry-after")) or 6 * attempt
            time.sleep(min(wait, 120))
            continue
        if resp.status_code in (400, 422) and can_fallback and mode == "json":
            mode = "testo"  # l'API potrebbe accettare solo state testuale
            continue
        if resp.status_code >= 400:
            return None, {}, {}, f"HTTP {resp.status_code}: {resp.text[:200]}"

        try:
            j = resp.json()
        except Exception:
            j = extract_json(resp.text)
        if not j:
            return None, {}, {}, "risposta non JSON"
        if opts["state_mode"] == "auto":
            opts["state_mode"] = mode
            print(f"[state inviato come {'oggetto JSON' if mode == 'json' else 'testo'}] ", end="")
        raw_answers = j.get("answers", j) if isinstance(j, dict) else j
        decisions, errors = parse_response(j, req["questions"])
        if not decisions:
            return raw_answers, {}, errors, "nessuna risposta interpretabile"
        return raw_answers, decisions, errors, None


def resolve_models(req, models_cli):
    if models_cli:
        return [m.strip() for m in models_cli if m.strip()]
    return [requested_model(req) or DEFAULT_MODEL]


def collect(states, req, folder, models, pause=PAUSE_S, state_mode="auto"):
    try:
        import httpx
    except ImportError:
        sys.exit("Manca httpx: pip install httpx")
    headers = nace_headers()

    raw = {
        "generato": datetime.now().isoformat(timespec="seconds"),
        "endpoint": NACE_URL,
        "cartella_state": str(folder),
        "richiesta": {k: v for k, v in req.items() if k != "state"},
        "state_files": [rel for rel, _ in states],
        "modelli": models,
        "records": [],
    }
    total = len(models) * len(states)
    print(f">> {len(models)} modelli x {len(states)} state = {total} richieste a {NACE_URL}")
    if len(models) < 2:
        print("   (con un solo modello i test tra modelli non sono calcolabili: usa --models m1,m2,...)")

    opts = {"state_mode": state_mode}
    i = 0
    with httpx.Client(headers=headers, timeout=RESULT_TIMEOUT_S) as client:
        try:
            for rel, state_obj in states:
                print(f"\n== State: {rel}")
                for m in models:
                    i += 1
                    print(f">> [{i}/{total}] {m} ... ", end="", flush=True)
                    rec = {"state": rel, "model": m}
                    try:
                        answers, decisions, errors, err = call_nace(client, m, state_obj, req, opts)
                        rec["decisions"] = decisions
                        rec["risposta"] = answers
                        if errors:
                            rec["errors"] = errors
                        if err:
                            rec["error"] = err
                            print(f"SKIP ({err})" + (f" {errors}" if errors else ""))
                        else:
                            summary = ", ".join(f"{q}={d['choice']}" for q, d in decisions.items())
                            print(f"OK ({summary})" + (f"  [problemi: {errors}]" if errors else ""))
                    except FatalAPIError:
                        raise
                    except Exception as e:
                        rec["decisions"] = {}
                        rec["error"] = str(e)[:200]
                        print(f"ERRORE: {str(e)[:80]}")
                    raw["records"].append(rec)
                    write_json(RAW_OUT, raw)  # salvataggio progressivo
                    time.sleep(pause)
        except FatalAPIError as e:
            write_json(RAW_OUT, raw)
            sys.exit(f"\n!! {e}")
        except KeyboardInterrupt:
            print("\n!! Interrotto dall'utente: analizzo quanto raccolto finora.")
            write_json(RAW_OUT, raw)

    print(f"\n>> Raccolta completata: {RAW_OUT}")
    return raw

# ============================== DATASET ==============================
def build_observations(records, questions):
    """Una riga per (state, modello, domanda) con decisione valida."""
    obs = []
    for r in records:
        for qid, d in (r.get("decisions") or {}).items():
            if qid not in questions or "probabilities" not in d:
                continue
            cats = question_categories(questions[qid])
            obs.append({
                "state": r["state"], "model": r["model"], "qid": qid,
                "choice": d.get("choice") if d.get("choice") in cats else None,
                "p": [float(d["probabilities"].get(c, 0.0)) for c in cats],
                "conf": d.get("confidence"),
            })
    for o in obs:
        if o["choice"] is None:
            cats = question_categories(questions[o["qid"]])
            o["choice"] = cats[int(np.argmax(o["p"]))]
    return obs


# ============================== FASE 1: FILTRO CORRELAZIONE ==============================
def filter_correlated(obs, questions, r_threshold):
    """Correla i modelli sul vettore di probabilità concatenato su tutti gli (state, domanda)."""
    feats = {}
    for o in obs:
        cats = question_categories(questions[o["qid"]])
        use = cats[:1] if len(cats) == 2 else cats  # nel binario p(no)=1-p(yes): ridondante
        for c in use:
            feats.setdefault(o["model"], {})[f"{o['state']}|{o['qid']}|{c}"] = o["p"][cats.index(c)]

    conf = {}
    for m in feats:
        vals = [o["conf"] if o["conf"] is not None else max(o["p"]) for o in obs if o["model"] == m]
        conf[m] = float(np.mean(vals)) if vals else 0.0

    info = {"soglia": r_threshold, "n_feature": 0, "eliminati": {}, "superstiti": sorted(feats),
            "matrice_correlazione": {}}
    if len(feats) < 2:
        info["nota"] = "meno di 2 modelli: nessun filtro"
        return set(feats), info

    df = pd.DataFrame.from_dict(feats, orient="index")
    info["n_feature"] = int(df.shape[1])
    corr = df.T.corr(min_periods=3)
    info["matrice_correlazione"] = {m: {o: rnd(corr.loc[m, o]) for o in corr.columns} for m in corr.index}
    if df.shape[1] < 3:
        info["nota"] = "meno di 3 coppie state/domanda: correlazione non calcolabile, nessun filtro"
        return set(feats), info

    kept, removed = [], {}
    for m in sorted(conf, key=conf.get, reverse=True):
        if m in removed:
            continue
        kept.append(m)
        for o in corr.columns:
            rv = corr.loc[m, o]
            if o != m and o not in kept and o not in removed and pd.notna(rv) and rv > r_threshold:
                removed[o] = {"tenuto": m, "r": rnd(rv)}

    info["eliminati"] = removed
    info["superstiti"] = kept
    info["confidenza_media"] = {m: rnd(v) for m, v in conf.items()}
    print(f"\n== FASE 1: {len(kept)} superstiti, {len(removed)} eliminati (r>{r_threshold}, "
          f"{df.shape[1]} feature)")
    for o, inf in removed.items():
        print(f"   - {o}  (r={inf['r']} con {inf['tenuto']})")
    return set(kept), info


# ============================== FASE 2: TEST STATISTICI ==============================
def _safe(fn):
    try:
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore")
            return fn()
    except Exception as e:
        return e


def run_stats(P, choices, categories):
    """Test tra modelli su un insieme di decisioni (righe di P = modelli)."""
    P = np.asarray(P, dtype=float)
    n, k = P.shape
    obs = [choices.count(c) for c in categories]
    top = categories[int(np.argmax(obs))]
    out = {
        "n": n,
        "scelte": dict(zip(categories, obs)),
        "maggioranza": top,
        "quota_maggioranza": rnd(obs[categories.index(top)] / n) if n else None,
        "unanime": max(obs) == n,
        "pareggio": n > 0 and obs.count(max(obs)) > 1,
        "media_probabilita": dict(zip(categories, [rnd(v) for v in P.mean(axis=0)])) if n else {},
    }
    if n < 2:
        out["nota"] = "dati insufficienti per i test (servono almeno 2 modelli)"
        return out
    cnt = obs[categories.index(top)]

    res = _safe(lambda: smp.proportions_ztest(cnt, n, value=1 / k, alternative="larger", prop_var=1 / k))
    out["z_test"] = ({"categoria": top, "count": cnt, "H0": rnd(1 / k), "Z": rnd(res[0]), "p": rnd(res[1], 6)}
                     if not isinstance(res, Exception) else {"errore": str(res)})

    res = _safe(lambda: stats.binomtest(cnt, n, 1 / k, alternative="greater").pvalue)
    out["binomiale_esatto"] = ({"categoria": top, "p": rnd(res, 6)}
                               if not isinstance(res, Exception) else {"errore": str(res)})

    idx = [categories.index(c) for c in choices]
    chosen = np.array([P[i, idx[i]] for i in range(n)])
    other = np.array([(P[i].sum() - P[i, idx[i]]) / (k - 1) for i in range(n)])
    res = _safe(lambda: stats.ttest_rel(chosen, other))
    out["t_student"] = ({"media_scelta": rnd(chosen.mean()), "media_altre": rnd(other.mean()),
                         "T": rnd(res[0]), "p": rnd(res[1], 6)}
                        if not isinstance(res, Exception) else {"errore": str(res)})

    res = _safe(lambda: stats.chisquare(obs, f_exp=[n / k] * k))
    out["chi_quadro"] = ({"chi2": rnd(res[0]), "p": rnd(res[1], 6),
                          "nota": "frequenze attese <5: validità approssimata" if n / k < 5 else ""}
                         if not isinstance(res, Exception) else {"errore": str(res)})

    res = _safe(lambda: stats.f_oneway(*P.T))
    out["anova"] = ({"F": rnd(res[0]), "p": rnd(res[1], 6)}
                    if not isinstance(res, Exception) else {"errore": str(res)})

    votes = np.round(P * 100).astype(int)
    for i in range(n):
        votes[i, int(np.argmax(P[i]))] += 100 - votes[i].sum()
    res = _safe(lambda: irr.fleiss_kappa(votes, method="fleiss"))
    out["fleiss_kappa_prob"] = ({"K": rnd(res), "metodo": "100 voti simulati/modello dalle probabilità"}
                                if not isinstance(res, Exception) else {"errore": str(res)})
    return out


def fleiss_between_models(obs_q, categories):
    """Fleiss kappa: soggetti = state, valutatori = modelli, categorie = scelte."""
    by_state = {}
    for o in obs_q:
        by_state.setdefault(o["state"], Counter())[o["choice"]] += 1
    table = np.array([[c[cat] for cat in categories] for c in by_state.values()])
    if len(table) == 0:
        return {"nota": "nessun dato"}
    raters = table.sum(axis=1)
    full = table[raters == raters.max()]  # Fleiss richiede lo stesso numero di valutatori per soggetto
    if len(full) < 2 or raters.max() < 2:
        return {"nota": "servono almeno 2 state con almeno 2 modelli ciascuno"}
    res = _safe(lambda: irr.fleiss_kappa(full, method="fleiss"))
    if isinstance(res, Exception):
        return {"errore": str(res)}
    out = {"K": rnd(res), "n_state": int(len(full)), "n_modelli": int(raters.max())}
    if out["K"] is None:
        out["nota"] = "non definito (tutti i modelli hanno dato sempre la stessa risposta)"
    return out


def analyze(obs, questions, label):
    """Analisi completa di un insieme di osservazioni."""
    out = {"label": label, "n_osservazioni": len(obs),
           "modelli": sorted({o["model"] for o in obs}),
           "state": sorted({o["state"] for o in obs})}
    groups = {}
    for o in obs:
        groups.setdefault((o["state"], o["qid"]), []).append(o)

    # --- per state e domanda
    per_state, consenso = {}, {}
    for (st, qid), g in sorted(groups.items()):
        cats = question_categories(questions[qid])
        s = run_stats([o["p"] for o in g], [o["choice"] for o in g], cats)
        s["voti_modelli"] = {o["model"]: o["choice"] for o in g}
        per_state.setdefault(st, {})[qid] = s
        consenso.setdefault(st, {})[qid] = {"scelta": s["maggioranza"], "quota": s["quota_maggioranza"],
                                            "unanime": s["unanime"], "pareggio": s["pareggio"],
                                            "n_modelli": s["n"]}

    # --- per domanda (aggregato su tutti gli state)
    per_domanda = {}
    for qid, q in questions.items():
        cats = question_categories(q)
        oq = [o for o in obs if o["qid"] == qid]
        if not oq:
            per_domanda[qid] = {"nota": "nessuna risposta valida"}
            continue
        cons = [consenso[st][qid]["scelta"] for st in consenso if qid in consenso[st]]
        pooled = run_stats([o["p"] for o in oq], [o["choice"] for o in oq], cats)
        pooled["nota_indipendenza"] = "osservazioni modello x state non indipendenti: p-value indicativi"
        per_domanda[qid] = {
            "domanda": q.get("instructions"),
            "categorie": {c: (q.get("criteria", {}).get(c) if isinstance(q.get("criteria"), dict) else q.get(c))
                          for c in cats},
            "n_state": len(cons),
            "consenso_per_state": dict(Counter(cons)),
            "quota_state_per_categoria": {c: rnd(cons.count(c) / len(cons)) for c in cats} if cons else {},
            "state_unanimi": sum(1 for st in consenso if qid in consenso[st] and consenso[st][qid]["unanime"]),
            "fleiss_kappa_modelli": fleiss_between_models(oq, cats),
            "aggregato_osservazioni": pooled,
        }

    # --- per modello
    per_modello = {}
    for m in out["modelli"]:
        om = [o for o in obs if o["model"] == m]
        agree = [o["choice"] == consenso[o["state"]][o["qid"]]["scelta"] for o in om]
        confs = [o["conf"] for o in om if o["conf"] is not None]
        per_q = {}
        for qid, q in questions.items():
            cats = question_categories(q)
            omq = [o for o in om if o["qid"] == qid]
            if omq:
                ch = [o["choice"] for o in omq]
                per_q[qid] = {"n": len(omq), "scelte": {c: ch.count(c) for c in cats},
                              "media_probabilita": dict(zip(cats, [rnd(v) for v in np.mean([o["p"] for o in omq], axis=0)]))}
        per_modello[m] = {
            "n_decisioni": len(om),
            "accordo_con_consenso": rnd(np.mean(agree)) if agree else None,
            "confidenza_media": rnd(np.mean(confs)) if confs else None,
            "per_domanda": per_q,
        }

    out.update({"matrice_consenso": consenso, "per_domanda": per_domanda,
                "per_modello": per_modello, "per_state": per_state})
    return out


def print_summary(an, questions):
    cons = an["matrice_consenso"]
    if not cons:
        print(f"\n== [{an['label']}] nessun dato.")
        return
    rows = {st: {qid: ("pari" if v.get("pareggio") else
                       f"{v['scelta']} ({v['quota']:.0%})" if v.get("quota") is not None else "-")
                 for qid, v in qs.items()} for st, qs in cons.items()}
    df = pd.DataFrame.from_dict(rows, orient="index").reindex(columns=list(questions))
    print(f"\n== FASE 2 [{an['label']}]: consenso per state (quota modelli)  N oss={an['n_osservazioni']}")
    with pd.option_context("display.max_rows", 200, "display.max_columns", 50, "display.width", 200):
        print(df.fillna("-").to_string())
    for qid, d in an["per_domanda"].items():
        k = d.get("fleiss_kappa_modelli", {}).get("K")
        print(f"   {qid}: consenso state {d.get('consenso_per_state')}  Fleiss K modelli={k}")


# ============================== MAIN ==============================
def main():
    ap = argparse.ArgumentParser(description="Batch NACE SystemOne: richiesta.json x tutti gli state di una cartella.")
    ap.add_argument("cartella", nargs="?", help="cartella con i file JSON di state (ricerca ricorsiva)")
    ap.add_argument("--richiesta", default=None, help=f"file richiesta (default: {DEFAULT_RICHIESTA})")
    ap.add_argument("--analyze-only", metavar="FILE", help="salta la raccolta, analizza questo output_raw.json")
    ap.add_argument("--models", help=f"modelli NACE separati da virgola (default: 'model' di richiesta.json "
                                     f"o {DEFAULT_MODEL})")
    ap.add_argument("--state-formato", choices=("auto", "json", "testo"), default="auto",
                    help="come inviare lo state: oggetto JSON, testo (JSON serializzato) o auto (default)")
    ap.add_argument("--r", type=float, default=None, help="soglia correlazione (se assente, chiesta da tastiera)")
    ap.add_argument("--pausa", type=float, default=PAUSE_S, help="secondi di pausa tra richieste")
    ap.add_argument("--out", default=REPORT_OUT, help=f"file JSON dell'analisi (default: {REPORT_OUT})")
    args = ap.parse_args()

    r_threshold = ask_threshold(args.r)
    invalid_states = {}

    if args.analyze_only:
        raw = json.loads(Path(args.analyze_only).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "records" not in raw:
            sys.exit("Formato non riconosciuto: serve un output_raw.json prodotto da questa versione dello script.")
        richiesta_path = None
    else:
        nace_headers()  # verifica subito la presenza della chiave
        folder = args.cartella or input("Percorso della cartella con gli state JSON: ").strip().strip('"')
        if not folder:
            sys.exit("Cartella degli state non specificata.")
        req_file = args.richiesta
        if not req_file:
            for cand in (Path(DEFAULT_RICHIESTA), Path(folder) / DEFAULT_RICHIESTA,
                         Path(__file__).resolve().parent / DEFAULT_RICHIESTA):
                if cand.is_file():
                    req_file = str(cand)
                    break
        if not req_file:
            req_file = input("Percorso di richiesta.json: ").strip().strip('"')
        req, richiesta_path = load_richiesta(req_file)
        print(f">> Richiesta: {richiesta_path} ({len(req['questions'])} domande: {list(req['questions'])})")

        root, files = find_state_files(folder, exclude=[richiesta_path, args.out])
        states, invalid_states = load_states(root, files)
        models = resolve_models(req, args.models.split(",") if args.models else None)
        raw = collect(states, req, root, models, args.pausa, args.state_formato)

    questions = raw["richiesta"]["questions"]
    records = raw["records"]
    obs = build_observations(records, questions)
    if not obs:
        sys.exit(f"Nessuna decisione valida raccolta: analisi impossibile (vedi {RAW_OUT}, campo 'risposta').")

    kept, filt_info = filter_correlated(obs, questions, r_threshold)
    obs_f = [o for o in obs if o["model"] in kept]
    write_json(FILTERED_OUT, {**raw, "records": [r for r in records if r["model"] in kept]})

    an_orig = analyze(obs, questions, "dataset originale")
    an_filt = analyze(obs_f, questions, "dataset filtrato")
    print_summary(an_orig, questions)
    print_summary(an_filt, questions)

    failures = [{"state": r["state"], "model": r["model"], "errore": r.get("error"), "domande": r.get("errors")}
                for r in records if r.get("error") or r.get("errors")]
    tipi = Counter(d.get("tipo_valore") for r in records for d in (r.get("decisions") or {}).values())
    report = {
        "meta": {
            "generato": datetime.now().isoformat(timespec="seconds"),
            "raccolta": raw.get("generato"),
            "endpoint": raw.get("endpoint"),
            "cartella_state": raw.get("cartella_state"),
            "richiesta": str(richiesta_path) if richiesta_path else args.analyze_only,
            "soglia_correlazione": r_threshold,
            "domande": {qid: {"tipo": q.get("type"), "categorie": question_categories(q)}
                        for qid, q in questions.items()},
            "n_state": len(raw.get("state_files", [])),
            "state_files": raw.get("state_files", []),
            "state_scartati": invalid_states,
            "modelli_interrogati": raw.get("modelli", []),
            "richieste_totali": len(records),
            "richieste_ok": sum(1 for r in records if r.get("decisions") and not r.get("error")),
            "decisioni_valide": len(obs),
            "formato_risposte": dict(tipi),
            "nota_formato": ("risposte categoriche (yes/no): probabilità 1/0, test su probabilità poco informativi"
                             if tipi.get("categorica") else ""),
            "fallimenti": failures,
        },
        "filtro_correlazione": filt_info,
        "dataset_originale": an_orig,
        "dataset_filtrato": an_filt,
    }
    write_json(args.out, report)
    print(f"\n>> Analisi completa salvata: {args.out}  (grezzi: {RAW_OUT}, filtrati: {FILTERED_OUT})")


if __name__ == "__main__":
    main()
