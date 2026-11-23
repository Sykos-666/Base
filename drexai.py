# -*- coding: utf-8 -*-
"""
drexai.py - batch di richieste a Drex (NACE SystemOne) su una cartella di state.

Flusso:
  cartella -> ricerca di tutti i file .json (state) -> per ogni state si genera una
  richiesta.json (modello + domande del modello di richiesta + state) -> richiesta API
  a Drex -> ripetizione per ogni state -> JSON riepilogativo.

Requisiti:  pip install httpx
Chiave API: variabile d'ambiente NACE_API_KEY (mai scritta nel codice)
  Windows PowerShell:  $env:NACE_API_KEY = "nace_sk_..."
  Linux/macOS:         export NACE_API_KEY="nace_sk_..."

Uso:
  python drexai.py CARTELLA_STATE
  python drexai.py CARTELLA_STATE --richiesta richiesta.json --out-dir drex_output
  python drexai.py CARTELLA_STATE --model drex-max-v1.1 --state-formato testo

Output (in --out-dir, default "drex_output"):
  richieste/<state>.richiesta.json   richiesta generata e inviata per ogni state
  risposte/<state>.risposta.json     risposta grezza di Drex per ogni state
  riepilogo.json                     riepilogo complessivo (aggiornato dopo ogni state)

Il modello di richiesta (default richiesta.json) contiene le domande; "state" viene
sostituito da ciascun file della cartella (può restare un segnaposto, anche non JSON);
"model" se è un segnaposto (es. "xxxx") diventa drex-max-v1.1.
"""
import argparse
import copy
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx

# ============================== CONFIG ==============================
NACE_BASE = os.environ.get("NACE_BASE", "https://console.nace.ai").rstrip("/")
ENDPOINT = f"{NACE_BASE}/v1/systemone"
CLIENT_HEADER = "nace-quickstart/1"
DEFAULT_MODEL = "drex-max-v1.1"
DEFAULT_RICHIESTA = "richiesta.json"
DEFAULT_OUT_DIR = "drex_output"

HTTP_TIMEOUT_S = 90       # come nel quickstart
JOB_MAX_WAIT_S = 600      # attesa massima di un job asincrono (risposta 202)
JOB_POLL_S = 3
PAUSE_S = 1.0

GENERATED_SUFFIXES = (".richiesta.json", ".risposta.json")
PENDING = {"queued", "pending", "running", "processing", "in_progress", "accepted", "started"}
FAILED = {"failed", "error", "cancelled", "canceled", "expired", "rejected"}
YES_ALIASES = {"yes", "y", "si", "sì", "true", "vero", "1"}
NO_ALIASES = {"no", "n", "false", "falso", "0"}


class FatalAPIError(RuntimeError):
    """Errore che rende inutile proseguire (es. chiave API non valida)."""


# ============================== UTILITY ==============================
def clean_keys(obj):
    if isinstance(obj, dict):
        return {str(k).strip(): clean_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_keys(v) for v in obj]
    return obj


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def to_float(x):
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def safe_name(rel):
    """Percorso relativo dello state -> nome file sicuro e univoco."""
    stem = re.sub(r"\.json$", "", rel, flags=re.I)
    return re.sub(r"[^\w.-]+", "__", stem).strip("_") or "state"


def nace_headers():
    key = os.environ.get("NACE_API_KEY", "").strip()
    if not key:
        sys.exit("Imposta la variabile d'ambiente NACE_API_KEY con la tua chiave NACE (nace_sk_...).")
    return {"Authorization": f"Bearer {key}", "X-Nace-Client": CLIENT_HEADER}


# ============================== INPUT ==============================
def load_template(path):
    """Legge il modello di richiesta; tollera un segnaposto non JSON nel campo state."""
    p = Path(path).expanduser()
    if not p.is_file():
        sys.exit(f"File richiesta non trovato: {path}")
    text = p.read_text(encoding="utf-8-sig")
    try:
        req = json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(r'("state"\s*:\s*)(?![{\["]|null\b|true\b|false\b|-?\d)[^,\n}]+', r"\1null", text)
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


def resolve_model(template, cli_model):
    if cli_model:
        return cli_model.strip()
    m = str(template.get("model") or "").strip()
    if not m or re.fullmatch(r"x+", m, re.I) or m.lower() in ("all", "tutti", "*", "auto"):
        return DEFAULT_MODEL
    return m


def find_states(folder, exclude_files=(), exclude_dirs=()):
    root = Path(folder).expanduser()
    if not root.is_dir():
        sys.exit(f"Cartella non trovata: {folder}")
    root = root.resolve()
    excl_f = {Path(e).resolve() for e in exclude_files if e}
    excl_d = [Path(d).resolve() for d in exclude_dirs if d]
    states, invalid = [], {}
    for p in sorted(root.rglob("*")):
        if not (p.is_file() and p.suffix.lower() == ".json"):
            continue
        rp = p.resolve()
        low = p.name.lower()
        if low.endswith(GENERATED_SUFFIXES) or low == "riepilogo.json":  # output di run precedenti
            continue
        if rp in excl_f or any(d == rp or d in rp.parents for d in excl_d):
            continue
        rel = rp.relative_to(root).as_posix()
        try:
            states.append((rel, clean_keys(json.loads(p.read_text(encoding="utf-8-sig")))))
        except Exception as e:
            invalid[rel] = str(e)[:200]
    if not states:
        sys.exit(f"Nessun file .json valido trovato in {root}")
    return root, states, invalid


def build_request(template, model, state_obj, state_mode):
    """Richiesta per uno state: tutte le caratteristiche del modello + model + state."""
    if state_mode == "testo" and not isinstance(state_obj, str):
        state_obj = json.dumps(state_obj, ensure_ascii=False, indent=1)
    req = {"model": model, "state": state_obj}
    req.update({k: copy.deepcopy(v) for k, v in template.items() if k not in ("model", "state")})
    return req


# ============================== PARSING RISPOSTE ==============================
def question_categories(q):
    if isinstance(q.get("criteria"), dict) and q["criteria"]:
        return list(q["criteria"])
    return ["yes", "no"]


def norm_label(label, q, categories):
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


def map_probs(d, q, categories):
    probs = {}
    for k, v in d.items():
        c, v = norm_label(k, q, categories), to_float(v)
        if c is not None and v is not None:
            probs[c] = probs.get(c, 0.0) + v
    return probs


def value_to_answer(val, q, categories, depth=0):
    """
    Converte la risposta di una domanda in {choice, probabilities, confidence, formato}.
    Gestisce: numero 0-1/0-100 (= p(yes)), bool, "yes"/"no"/etichetta,
    {"probabilities": {...}}, {"yes": p, "no": p}, {"value"/"answer"/...: <uno dei precedenti>}.
    """
    binary = len(categories) == 2
    conf, choice, probs, fmt = None, None, None, None

    if isinstance(val, dict):
        conf = to_float(val.get("confidence"))
        for key in ("choice", "answer", "label", "decision"):
            choice = norm_label(val.get(key), q, categories)
            if choice:
                break
        src = val["probabilities"] if isinstance(val.get("probabilities"), dict) else val
        probs, fmt = map_probs(src, q, categories) or None, "probabilities"
        if probs is None:
            if depth < 3:
                for key in (q.get("type"), "value", "probability", "prob", "p", "p_yes", "score",
                            "result", "answer", "choice", "label", "decision"):
                    if key and val.get(key) is not None:
                        inner, _ = value_to_answer(val[key], q, categories, depth + 1)
                        if inner:
                            if inner["confidence"] is None:
                                inner["confidence"] = conf
                            return inner, None
            if choice is None:
                return None, f"formato non riconosciuto: {json.dumps(val, ensure_ascii=False)[:150]}"
            probs, fmt = {choice: 1.0}, "categorica"
    elif isinstance(val, bool):
        if not binary:
            return None, f"booleano per domanda non binaria: {val}"
        probs, fmt = {categories[0]: 1.0 if val else 0.0}, "categorica"
    elif isinstance(val, (int, float)):
        v = to_float(val)
        if v is None or not binary:
            return None, f"numero non interpretabile: {val}"
        probs, fmt = {categories[0]: v / 100 if v > 1 else v}, "probabilita_yes"
    elif isinstance(val, str):
        c = norm_label(val, q, categories)
        v = to_float(val.strip().rstrip("%"))
        if c is not None:
            probs, fmt = {c: 1.0}, "categorica"
        elif v is not None and binary:
            probs, fmt = {categories[0]: v / 100 if v > 1 else v}, "probabilita_yes"
        else:
            return None, f"valore non riconosciuto: {val[:150]}"
    else:
        return None, f"tipo non gestito: {type(val).__name__}"

    if sum(probs.values()) > 1.5:  # percentuali
        probs = {c: v / 100 for c, v in probs.items()}
    if binary and len(probs) == 1:
        (c, v), = probs.items()
        probs[categories[1] if c == categories[0] else categories[0]] = max(0.0, 1.0 - v)
    vec = {c: round(probs.get(c, 0.0), 6) for c in categories}
    if choice is None:
        choice = max(categories, key=lambda c: vec[c])
    return {"choice": choice, "probabilities": vec, "confidence": conf, "formato": fmt}, None


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        r = find_key(v, key)
        if r is not None:
            return r
    return None


def parse_answers(j, questions):
    """Risposta Drex -> (risposte normalizzate per domanda, errori per domanda)."""
    answers = find_key(j, "answers")
    if not isinstance(answers, dict):
        answers = None
    out, errors = {}, {}
    for qid, q in questions.items():
        a = answers.get(qid) if answers is not None else find_key(j, qid)
        if a is None:
            errors[qid] = "domanda assente nella risposta"
            continue
        if isinstance(a, dict) and q.get("type") in a:  # es. answers["Funzioni"]["noul"]
            inner, conf = a[q.get("type")], to_float(a.get("confidence"))
            a = inner if isinstance(inner, dict) or conf is None else {"value": inner, "confidence": conf}
        res, err = value_to_answer(a, q, question_categories(q))
        if res:
            out[qid] = res
        else:
            errors[qid] = err
    return out, errors


# ============================== API DREX ==============================
def job_urls(j, response_url):
    """URL candidati per leggere un job asincrono (risposta 202)."""
    urls = []
    for key in ("status_url", "job_url", "url", "href"):
        v = j.get(key) if isinstance(j, dict) else None
        if isinstance(v, str) and v.startswith(("http", "/")):
            urls.append(v if v.startswith("http") else NACE_BASE + v)
    links = j.get("links") if isinstance(j, dict) else None
    if isinstance(links, dict):
        for v in links.values():
            if isinstance(v, str) and v.startswith(("http", "/")):
                urls.append(v if v.startswith("http") else NACE_BASE + v)
    job = j.get("job") if isinstance(j.get("job"), dict) else {}
    job_id = j.get("job_id") or j.get("id") or job.get("id")
    if job_id:
        # schema del quickstart: /v1/documents/parse -> /v1/documents/jobs/{job_id}
        urls += [f"{NACE_BASE}/v1/systemone/jobs/{job_id}", f"{NACE_BASE}/v1/jobs/{job_id}"]
    return list(dict.fromkeys(urls))


def wait_job(client, j, response_url):
    urls = job_urls(j, response_url)
    if not urls:
        return None, f"job asincrono senza id/URL: {json.dumps(j, ensure_ascii=False)[:150]}"
    t0, url_ok = time.time(), None
    while time.time() - t0 < JOB_MAX_WAIT_S:
        for url in ([url_ok] if url_ok else urls):
            r = client.get(url)
            if r.status_code == 404 and not url_ok:
                continue
            if r.status_code >= 400:
                return None, f"job HTTP {r.status_code}: {r.text[:150]}"
            url_ok, jj = url, r.json()
            status = str(jj.get("status", "")).lower()
            if status in FAILED:
                return None, f"job {status}: {json.dumps(jj.get('error', jj), ensure_ascii=False)[:150]}"
            if status not in PENDING:
                return jj, None
            break
        else:
            return None, f"nessun endpoint job valido tra {urls}"
        time.sleep(JOB_POLL_S)
    return None, f"job non completato entro {JOB_MAX_WAIT_S}s"


def call_drex(client, request_body, wait_seconds):
    """POST a Drex; gestisce 429/5xx (retry) e 202 (job asincrono). Ritorna (json, errore)."""
    params = {"wait_seconds": wait_seconds} if wait_seconds else None
    for attempt in range(5):
        try:
            r = client.post(ENDPOINT, params=params, json=request_body)
        except httpx.TransportError as e:
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            return None, f"errore di rete: {str(e)[:150]}"
        if r.status_code in (401, 403):
            raise FatalAPIError(f"HTTP {r.status_code}: chiave non valida o senza permessi ({r.text[:150]})")
        if r.status_code == 429 or r.status_code >= 500:
            if attempt < 4:
                time.sleep(min(to_float(r.headers.get("retry-after")) or 6 * (attempt + 1), 120))
                continue
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        try:
            j = r.json()
        except Exception:
            return None, f"risposta non JSON: {r.text[:150]}"
        status = str(j.get("status", "")).lower() if isinstance(j, dict) else ""
        if r.status_code == 202 or (status in PENDING and "answers" not in j):
            return wait_job(client, j, str(r.url))
        if status in FAILED:
            return j, f"richiesta {status}: {json.dumps(j.get('error', ''), ensure_ascii=False)[:150]}"
        return j, None
    return None, "rate limit / errore server persistente"


# ============================== RIEPILOGO ==============================
def build_summary(meta, template, per_state):
    questions = template["questions"]
    ok = {s: d for s, d in per_state.items() if d.get("risposte")}
    per_q = {}
    for qid, q in questions.items():
        cats = question_categories(q)
        items = [(s, d["risposte"][qid]) for s, d in ok.items() if qid in d["risposte"]]
        n = len(items)
        counts = Counter(a["choice"] for _, a in items)
        confs = [a["confidence"] for _, a in items if a["confidence"] is not None]
        per_q[qid] = {
            "domanda": q.get("instructions"),
            "tipo": q.get("type"),
            "categorie": {c: (q["criteria"].get(c) if isinstance(q.get("criteria"), dict) else q.get(c))
                          for c in cats},
            "n_risposte": n,
            "conteggio": {c: counts.get(c, 0) for c in cats},
            "quota": {c: round(counts.get(c, 0) / n, 4) for c in cats} if n else {},
            "media_probabilita": ({c: round(sum(a["probabilities"][c] for _, a in items) / n, 4) for c in cats}
                                  if n else {}),
            "confidenza_media": round(sum(confs) / len(confs), 4) if confs else None,
            "state_per_risposta": {c: [s for s, a in items if a["choice"] == c] for c in cats},
            "errori": {s: d["errori_domande"][qid] for s, d in per_state.items()
                       if qid in (d.get("errori_domande") or {})},
        }
    formats = Counter(a["formato"] for d in ok.values() for a in d["risposte"].values())
    meta = dict(meta)
    meta.update({
        "aggiornato": datetime.now().isoformat(timespec="seconds"),
        "state_elaborati": len(per_state),
        "state_ok": len(ok),
        "state_con_errore": sorted(s for s, d in per_state.items() if d.get("errore")),
        "formato_risposte": dict(formats),
    })
    if formats.get("categorica"):
        meta["nota_formato"] = "alcune risposte sono solo sì/no: le probabilità relative valgono 1/0"
    return {
        "meta": meta,
        "matrice": {s: {qid: a["choice"] for qid, a in d.get("risposte", {}).items()} for s, d in per_state.items()},
        "per_domanda": per_q,
        "per_state": per_state,
    }


# ============================== MAIN ==============================
def main():
    ap = argparse.ArgumentParser(description="Richieste a Drex (NACE SystemOne) per ogni state JSON di una cartella.")
    ap.add_argument("cartella", nargs="?", help="cartella con i file JSON di state (ricerca ricorsiva)")
    ap.add_argument("--richiesta", default=None, help=f"modello di richiesta (default: {DEFAULT_RICHIESTA})")
    ap.add_argument("--model", default=None, help=f"modello Drex (default: 'model' della richiesta o {DEFAULT_MODEL})")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"cartella di output (default: {DEFAULT_OUT_DIR})")
    ap.add_argument("--state-formato", choices=("auto", "json", "testo"), default="auto",
                    help="state come oggetto JSON, come testo (JSON serializzato) o auto: prova l'oggetto e, "
                         "se Drex lo rifiuta (400/422), ripiega sul testo (default)")
    ap.add_argument("--wait-seconds", type=int, default=0,
                    help="parametro wait_seconds da passare a Drex (0 = non inviato)")
    ap.add_argument("--pausa", type=float, default=PAUSE_S, help="secondi di pausa tra richieste")
    args = ap.parse_args()

    headers = nace_headers()
    folder = args.cartella or input("Percorso della cartella con gli state JSON: ").strip().strip('"')
    if not folder:
        sys.exit("Cartella degli state non specificata.")

    tpl_path = args.richiesta
    if not tpl_path:
        for cand in (Path(DEFAULT_RICHIESTA), Path(folder) / DEFAULT_RICHIESTA,
                     Path(__file__).resolve().parent / DEFAULT_RICHIESTA):
            if cand.is_file():
                tpl_path = str(cand)
                break
    if not tpl_path:
        tpl_path = input("Percorso del modello richiesta.json: ").strip().strip('"')
    template, tpl_resolved = load_template(tpl_path)
    model = resolve_model(template, args.model)

    out_dir = Path(args.out_dir).expanduser().resolve()
    root, states, invalid = find_states(folder, exclude_files=[tpl_resolved], exclude_dirs=[out_dir])
    print(f">> Modello richiesta: {tpl_resolved}  ({len(template['questions'])} domande: {list(template['questions'])})")
    print(f">> Modello Drex: {model}  |  endpoint: {ENDPOINT}")
    print(f">> {len(states)} state trovati in {root}" + (f" ({len(invalid)} non validi, scartati)" if invalid else ""))
    for rel, err in invalid.items():
        print(f"   - scartato {rel}: {err}")

    meta = {
        "generato": datetime.now().isoformat(timespec="seconds"),
        "endpoint": ENDPOINT,
        "modello": model,
        "cartella_state": str(root),
        "modello_richiesta": str(tpl_resolved),
        "n_state": len(states),
        "state_scartati": invalid,
    }
    summary_path = out_dir / "riepilogo.json"
    per_state, used_names = {}, set()
    state_mode = args.state_formato

    with httpx.Client(headers=headers, timeout=HTTP_TIMEOUT_S) as client:
        try:
            for i, (rel, state_obj) in enumerate(states, 1):
                name = safe_name(rel)
                while name in used_names:
                    name += "_"
                used_names.add(name)
                print(f">> [{i}/{len(states)}] {rel} ... ", end="", flush=True)

                mode = "json" if state_mode == "auto" else state_mode
                body = build_request(template, model, state_obj, mode)
                j, err = call_drex(client, body, args.wait_seconds)
                if err and err.startswith(("HTTP 400", "HTTP 422")) and state_mode == "auto" \
                        and not isinstance(state_obj, str):
                    mode = "testo"
                    body = build_request(template, model, state_obj, mode)
                    j, err = call_drex(client, body, args.wait_seconds)
                if state_mode == "auto" and not err:
                    state_mode = mode  # formato accettato: usato per tutti gli state successivi
                    print(f"[state inviato come {'oggetto JSON' if mode == 'json' else 'testo'}] ", end="")

                req_file = out_dir / "richieste" / f"{name}.richiesta.json"
                write_json(req_file, body)
                entry = {"richiesta_file": req_file.relative_to(out_dir).as_posix()}
                if j is not None:
                    resp_file = out_dir / "risposte" / f"{name}.risposta.json"
                    write_json(resp_file, j)
                    entry["risposta_file"] = resp_file.relative_to(out_dir).as_posix()
                if err:
                    entry["errore"] = err
                    print(f"ERRORE ({err[:100]})")
                else:
                    answers, q_errors = parse_answers(j, template["questions"])
                    entry["risposte"] = answers
                    if q_errors:
                        entry["errori_domande"] = q_errors
                    if not answers:
                        entry["errore"] = "nessuna risposta interpretabile (vedi risposta_file)"
                    txt = ", ".join(f"{q}={a['choice']}" for q, a in answers.items())
                    print(f"OK ({txt})" + (f"  [problemi: {q_errors}]" if q_errors else ""))
                per_state[rel] = entry
                write_json(summary_path, build_summary(meta, template, per_state))  # salvataggio progressivo
                if i < len(states):
                    time.sleep(args.pausa)
        except FatalAPIError as e:
            write_json(summary_path, build_summary(meta, template, per_state))
            sys.exit(f"\n!! {e}")
        except KeyboardInterrupt:
            print("\n!! Interrotto: riepilogo con gli state elaborati finora.")

    summary = build_summary(meta, template, per_state)
    write_json(summary_path, summary)
    print(f"\n== Riepilogo ({summary['meta']['state_ok']}/{len(states)} state OK)")
    for qid, d in summary["per_domanda"].items():
        print(f"   {qid}: {d['conteggio']}  media prob {d['media_probabilita']}")
    print(f"\n>> Riepilogo salvato: {summary_path}")
    print(f">> Richieste generate: {out_dir / 'richieste'}  |  risposte: {out_dir / 'risposte'}")


if __name__ == "__main__":
    main()
