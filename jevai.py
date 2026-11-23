# -*- coding: utf-8 -*-
"""
Pipeline JevAI Playground - batch su una cartella di state.

Per ogni file JSON trovato nella cartella indicata (anche nelle sottocartelle) e usato
come "state", invia la richiesta definita in richiesta.json (stesse domande) a tutti
i modelli selezionati, raccoglie le probabilità e produce un JSON con l'analisi completa:
  - filtro dei modelli troppo correlati (soglia r)
  - per ogni state e domanda: consenso tra modelli + test statistici
  - per ogni domanda: aggregati su tutti gli state, Fleiss kappa tra modelli
  - per ogni modello: profilo e accordo con il consenso
  - tutto sia sul dataset originale sia su quello filtrato

Uso:
  python jevai.py CARTELLA_STATE --richiesta richiesta.json --r 0.90             # primo run (headed): login
  python jevai.py CARTELLA_STATE --richiesta richiesta.json --r 0.90 --headless  # run successivi
  python jevai.py --analyze-only output_raw.json --r 0.85                        # solo analisi

In richiesta.json:
  - "state" è ignorato (viene sostituito da ciascun file della cartella; può restare un segnaposto)
  - "model": un id di modello preciso, oppure un segnaposto (es. "xxxx", "", "all") per
    interrogare tutti i modelli del menu a tendina (meno SKIP_MODELS). --models ha la precedenza.
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
URL = "https://thejevai.com/playground"
RAW_OUT = "output_raw.json"
FILTERED_OUT = "output_filtered.json"
REPORT_OUT = "analisi_statistica.json"
AUTH_STATE = "auth_state.json"
OUTPUT_NAMES = {RAW_OUT, FILTERED_OUT, REPORT_OUT, AUTH_STATE}  # mai trattati come state

DEFAULT_R = 0.87
DEFAULT_RICHIESTA = "richiesta.json"
RESULT_TIMEOUT_S = 120
PAUSE_S = 1.5
API_REGEX = re.compile(r"(systemone|decision|decide|generate)", re.I)
SKIP_MODELS = {"convaiinnovations/laya"}  # consuma crediti account: escluso di default

YES_ALIASES = {"yes", "y", "si", "sì", "true", "vero", "1"}
NO_ALIASES = {"no", "n", "false", "falso", "0"}

GH_USER = os.environ.get("JEVAI_GH_USER", "")
GH_PASS = os.environ.get("JEVAI_GH_PASS", "")


# ============================== UTILITY ==============================
def debug_dump(page, tag="login_fail"):
    try:
        page.screenshot(path=f"{tag}.png", full_page=True)
        Path(f"{tag}.html").write_text(page.content(), encoding="utf-8")
        print(f">> Dump salvato: {tag}.png / {tag}.html")
    except Exception:
        pass
    try:
        buttons = [b.strip() for b in page.get_by_role("button").all_inner_texts() if b.strip()]
        print(f">> Bottoni nella pagina ({len(buttons)}): {buttons[:40]}")
    except Exception:
        pass


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


def locate_request_body(j):
    if isinstance(j, dict):
        if "questions" in j and "model" in j:
            return j
        for v in j.values():
            r = locate_request_body(v)
            if r is not None:
                return r
    return None


def safe_headers(h):
    keep = {}
    for k, v in h.items():
        if k.lower().startswith(("content-type", "accept", "authorization", "x-", "user-agent")):
            keep[k] = v
    return keep


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


def _is_decision(d):
    return isinstance(d, dict) and ("probabilities" in d or "choice" in d)


def find_decision(obj, qid):
    """Cerca nella risposta il blocco decisione della domanda qid (ignora l'eco delle domande)."""
    if isinstance(obj, dict):
        v = obj.get(qid)
        if _is_decision(v):
            return v
        if _is_decision(obj) and any(obj.get(k) == qid for k in ("id", "question", "question_id", "name", "key")):
            return obj
        for v in obj.values():
            r = find_decision(v, qid)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_decision(v, qid)
            if r is not None:
                return r
    return None


def find_any_decision(obj):
    if isinstance(obj, dict):
        if "probabilities" in obj:
            return obj
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        r = find_any_decision(v)
        if r is not None:
            return r
    return None


def normalize_decision(raw, q, categories):
    probs_raw = raw.get("probabilities")
    if not isinstance(probs_raw, dict) or not probs_raw:
        return None, "risposta senza probabilities"
    probs = {}
    for k, v in probs_raw.items():
        c = norm_label(k, q, categories)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if c is not None:
            probs[c] = probs.get(c, 0.0) + v
    if not probs:
        return None, f"categorie non riconosciute: {list(probs_raw)[:6]}"
    if sum(probs.values()) > 1.5:  # espresse in percentuale
        probs = {c: v / 100 for c, v in probs.items()}
    if len(categories) == 2 and len(probs) == 1:
        (c, v), = probs.items()
        probs[categories[1] if c == categories[0] else categories[0]] = max(0.0, 1.0 - v)
    vec = [probs.get(c, 0.0) for c in categories]

    choice = None
    for key in ("choice", "answer", "decision"):
        choice = norm_label(raw.get(key), q, categories)
        if choice:
            break
    if choice is None:
        choice = categories[int(np.argmax(vec))]
    try:
        conf = float(raw.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    return {"choice": choice, "probabilities": dict(zip(categories, vec)), "confidence": conf}, None


def parse_response(j, questions):
    decisions, errors = {}, {}
    for qid, q in questions.items():
        raw = find_decision(j, qid)
        if raw is None and len(questions) == 1:
            raw = find_any_decision(j)
        if raw is None:
            errors[qid] = "domanda assente nella risposta"
            continue
        d, err = normalize_decision(raw, q, question_categories(q))
        if d:
            decisions[qid] = d
        else:
            errors[qid] = err
    return decisions, errors


# ============================== LOGIN ==============================
def complete_github_login(page):
    """Completa il login GitHub quando JevAI lo richiede dopo Generate decisions."""
    if not GH_USER or not GH_PASS:
        raise RuntimeError("Imposta JEVAI_GH_USER e JEVAI_GH_PASS nelle variabili d'ambiente.")

    page.get_by_role("button", name="Sign in with GitHub").click(timeout=15_000)
    page.get_by_role("textbox", name="Username or email address").fill(GH_USER, timeout=20_000)
    page.get_by_role("textbox", name="Password").fill(GH_PASS, timeout=20_000)
    page.get_by_role("button", name="Sign in", exact=True).click()
    print(">> Credenziali GitHub inviate. Eventuale 2FA/device verification: completa a mano...")
    page.wait_for_url(re.compile(r"thejevai\.com/playground"), timeout=180_000)
    page.get_by_role("button", name="Generate decisions").wait_for(timeout=30_000)
    page.context.storage_state(path=AUTH_STATE)
    print(f">> Login OK, sessione salvata in {AUTH_STATE}.")


def login(page):
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    try:
        page.get_by_role("button", name=re.compile(r"accept|accetta|agree|consenti|allow all", re.I)).first.click(timeout=2500)
    except Exception:
        pass

    if page.get_by_role("button", name="Generate decisions").count() > 0:
        print(">> Sessione già attiva: login saltato.")
        return

    candidates = [
        page.get_by_role("button", name=re.compile(r"sign in with github", re.I)),
        page.get_by_role("link", name=re.compile(r"sign in with github", re.I)),
        page.get_by_text(re.compile(r"sign in with github", re.I)),
        page.locator("text=/github/i"),
    ]
    clicked = False
    for loc in candidates:
        try:
            loc.first.click(timeout=6000)
            clicked = True
            break
        except Exception:
            continue
    if not clicked:
        debug_dump(page, "login_fail")
        raise RuntimeError("Pulsante GitHub non trovato: guarda login_fail.png/.html")

    try:
        page.wait_for_url(re.compile(r"github\.com"), timeout=15_000)
    except Exception:
        pass

    if "github.com" in page.url:
        user_box = page.get_by_role("textbox", name="Username or email address")
        try:
            user_box.fill(GH_USER, timeout=8000)
        except Exception:
            for alt in (r"use.*password", r"sign in with.*password", r"another method", r"password"):
                done = False
                for role in ("button", "link"):
                    try:
                        page.get_by_role(role, name=re.compile(alt, re.I)).first.click(timeout=3000)
                        done = True
                        break
                    except Exception:
                        continue
                if done:
                    break
            user_box.fill(GH_USER, timeout=8000)
        page.get_by_role("textbox", name="Password").fill(GH_PASS)
        page.get_by_role("button", name="Sign in", exact=True).click()
        print(">> Credenziali inviate. Eventuale 2FA/device verification: completa a mano (max 180s)...")

    page.wait_for_url(re.compile(r"thejevai\.com/playground"), timeout=180_000)
    page.get_by_role("button", name="Generate decisions").wait_for(timeout=30_000)
    page.context.storage_state(path=AUTH_STATE)
    print(f">> Login OK, sessione salvata in {AUTH_STATE}.")


# ============================== FASE 0: RACCOLTA VIA API ==============================
def list_models(page):
    sel = page.get_by_label(re.compile(r"^Model", re.I)).first
    values = sel.locator("option").evaluate_all("els => els.map(e => e.value)")
    out, seen = [], set()
    for v in values:
        v = (v or "").strip().lstrip("~").strip()
        if v and v.lower() not in ("", "select", "none") and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def discover_api(page):
    """Un click su Generate (preset di default) per catturare endpoint, header e payload reali."""
    captured = {}

    def on_request(req):
        try:
            if req.method == "POST" and API_REGEX.search(req.url) and req.post_data:
                j = json.loads(req.post_data)
                if locate_request_body(j) is not None:
                    captured["url"] = req.url
                    captured["headers"] = safe_headers(req.headers)
                    captured["payload"] = j
        except Exception:
            pass

    page.on("request", on_request)
    print(">> Intercetto l'endpoint API con un run del preset di default...")
    page.get_by_role("button", name="Generate decisions").click()

    # Su alcune sessioni il login GitHub viene richiesto solo al primo Generate.
    github_login = page.get_by_role("button", name="Sign in with GitHub")
    try:
        github_login.wait_for(state="visible", timeout=4000)
    except Exception:
        pass
    else:
        complete_github_login(page)
        # Il primo click ha aperto il login: ripeti Generate dopo l'autenticazione.
        page.get_by_role("button", name="Generate decisions").click()

    t0 = time.time()
    while "url" not in captured and time.time() - t0 < 60:
        page.wait_for_timeout(300)
    page.remove_listener("request", on_request)

    if "url" not in captured:
        debug_dump(page, "api_fail")
        raise RuntimeError("Endpoint API non intercettato: guarda api_fail.png/.html")
    print(f">> Endpoint catturato: {captured['url']}")
    return captured


def call_model(context, api, model, state_obj, req):
    """Invia richiesta.json (con model e state sostituiti) e restituisce (decisioni, errori, errore_globale)."""
    body = copy.deepcopy(api["payload"])
    target = locate_request_body(body) or body
    for k, v in req.items():
        if k not in ("model", "state"):
            target[k] = copy.deepcopy(v)
    target["model"] = model
    target["state"] = state_obj

    for attempt in range(3):
        resp = context.request.post(
            api["url"],
            headers=api["headers"],
            data=json.dumps(body, ensure_ascii=False),
            timeout=RESULT_TIMEOUT_S * 1000,
        )
        if (resp.status == 429 or resp.status >= 500) and attempt < 2:
            time.sleep(6 * (attempt + 1))
            continue
        if not resp.ok:
            return {}, {}, f"HTTP {resp.status}: {resp.text()[:120]}"
        try:
            j = resp.json()
        except Exception:
            j = extract_json(resp.text())
        if not j:
            return {}, {}, "risposta non JSON"
        decisions, errors = parse_response(j, req["questions"])
        if not decisions:
            return {}, errors, "nessuna decisione valida nella risposta"
        return decisions, errors, None
    return {}, {}, "rate limit / errore server persistente"


def collect(states, req, folder, models_cli=None, headless=False, pause=PAUSE_S):
    from playwright.sync_api import sync_playwright

    if not GH_USER or not GH_PASS:
        sys.exit("Imposta JEVAI_GH_USER e JEVAI_GH_PASS nelle variabili d'ambiente.")

    raw = {
        "generato": datetime.now().isoformat(timespec="seconds"),
        "cartella_state": str(folder),
        "richiesta": {k: v for k, v in req.items() if k != "state"},
        "state_files": [rel for rel, _ in states],
        "modelli": [],
        "records": [],
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_kwargs = {"storage_state": AUTH_STATE} if Path(AUTH_STATE).exists() else {}
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        login(page)
        fixed = requested_model(req)
        if models_cli:
            models = models_cli
        elif fixed:
            models = [fixed]
        else:
            models = [m for m in list_models(page) if m not in SKIP_MODELS]
        raw["modelli"] = models
        total = len(models) * len(states)
        print(f">> {len(models)} modelli x {len(states)} state = {total} richieste "
              f"(esclusi: {sorted(SKIP_MODELS)}).")

        api = discover_api(page)

        i = 0
        for rel, state_obj in states:
            print(f"\n== State: {rel}")
            for m in models:
                i += 1
                print(f">> [{i}/{total}] {m} ... ", end="", flush=True)
                rec = {"state": rel, "model": m}
                try:
                    decisions, errors, err = call_model(context, api, m, state_obj, req)
                    rec["decisions"] = decisions
                    if errors:
                        rec["errors"] = errors
                    if err:
                        rec["error"] = err
                        print(f"SKIP ({err})")
                    else:
                        summary = ", ".join(f"{q}={d['choice']}" for q, d in decisions.items())
                        print(f"OK ({summary})" + (f"  [mancanti: {list(errors)}]" if errors else ""))
                except Exception as e:
                    rec["decisions"] = {}
                    rec["error"] = str(e)[:200]
                    print(f"ERRORE: {str(e)[:80]}")
                raw["records"].append(rec)
                write_json(RAW_OUT, raw)  # salvataggio progressivo
                time.sleep(pause)

        browser.close()

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
    ap = argparse.ArgumentParser(description="Batch JevAI: richiesta.json x tutti gli state di una cartella.")
    ap.add_argument("cartella", nargs="?", help="cartella con i file JSON di state (ricerca ricorsiva)")
    ap.add_argument("--richiesta", default=None, help=f"file richiesta (default: {DEFAULT_RICHIESTA})")
    ap.add_argument("--analyze-only", metavar="FILE", help="salta la raccolta, analizza questo output_raw.json")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--models", help="lista modelli separati da virgola (precedenza su 'model' di richiesta.json)")
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
        raw = collect(states, req, root, args.models.split(",") if args.models else None,
                      args.headless, args.pausa)

    questions = raw["richiesta"]["questions"]
    records = raw["records"]
    obs = build_observations(records, questions)
    if not obs:
        sys.exit("Nessuna decisione valida raccolta: analisi impossibile (vedi output_raw.json).")

    kept, filt_info = filter_correlated(obs, questions, r_threshold)
    obs_f = [o for o in obs if o["model"] in kept]
    write_json(FILTERED_OUT, {**raw, "records": [r for r in records if r["model"] in kept]})

    an_orig = analyze(obs, questions, "dataset originale")
    an_filt = analyze(obs_f, questions, "dataset filtrato")
    print_summary(an_orig, questions)
    print_summary(an_filt, questions)

    failures = [{"state": r["state"], "model": r["model"], "errore": r.get("error"), "domande": r.get("errors")}
                for r in records if r.get("error") or r.get("errors")]
    report = {
        "meta": {
            "generato": datetime.now().isoformat(timespec="seconds"),
            "raccolta": raw.get("generato"),
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
