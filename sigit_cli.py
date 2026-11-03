#!/usr/bin/env python3
"""
sigit-cli
=========

CLI per accedere a SIGIT Infopoint (https://infopoint.sigit.it/infopoint/frm_home.aspx)
e scaricare in blocco i documenti del proprio account.

Il sito è un'applicazione ASP.NET WebForms "classica" (postback via
__doPostBack / __VIEWSTATE). Per questo motivo lo script non replica le
richieste HTTP a mano, ma pilota un browser reale (headless) con Playwright:
in questo modo i postback, i cookie di sessione e i download innescati da
javascript vengono gestiti automaticamente, come farebbe un utente umano.

--------------------------------------------------------------------------
COME CONFIGURARE I SELETTORI (passo obbligatorio prima del primo utilizzo)
--------------------------------------------------------------------------
Il sito blocca il fetch automatico via robots.txt, quindi i selettori qui
sotto sono dei segnaposto plausibili per un WebForm ASP.NET, NON garantiti
corretti. Per ottenere quelli reali:

    pip install playwright
    playwright install chromium
    playwright codegen https://infopoint.sigit.it/infopoint/frm_home.aspx

Playwright apre un browser e una finestra "Inspector": fai login a mano,
apri la pagina con l'elenco documenti e clicca su un download. Il codice
Python generato in tempo reale conterrà i selettori esatti (name/id dei
campi utente/password, del bottone di login, del link/bottone di download).
Copiali nella sezione CONFIG qui sotto.

--------------------------------------------------------------------------
USO
--------------------------------------------------------------------------
    export SIGIT_USER="tuo.utente"
    export SIGIT_PASS="tua.password"          # oppure verrà chiesta a runtime

    python sigit_cli.py login --show-browser         # test di solo login
    python sigit_cli.py download -o ./documenti      # scarica tutto
    python sigit_cli.py download -o ./documenti --dry-run   # elenca soltanto
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import getpass
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

try:
    from playwright.async_api import (
        async_playwright,
        Page,
        BrowserContext,
        TimeoutError as PWTimeoutError,
        Download,
    )
except ImportError:
    print(
        "Playwright non è installato. Esegui:\n"
        "    pip install playwright\n"
        "    playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)


# ===========================================================================
# CONFIG — da adattare con i selettori reali (vedi istruzioni sopra)
# ===========================================================================

BASE_URL = "https://infopoint.sigit.it/infopoint/frm_home.aspx"

SELECTORS = {
    # --- Login --- (confermati via `playwright codegen`)
    "username_field": "#_ctl7_txtUtente",
    "password_field": "#_ctl7_txtPassword",
    # login_button: ora si usa direttamente page.get_by_role("button", name="Login")
    # nella funzione login() — non più un selettore stringa qui, per usare la
    # semantica esatta confermata via codegen (match su sottostringa, case-insensitive).
    # testo/selettore di qualcosa visibile SOLO dopo login riuscito
    # (es. un link "Logout", il nome utente in un header, ecc.)
    "logged_in_indicator": "text=/logout|esci|disconnetti/i",
    # eventuale messaggio di errore credenziali
    "login_error": "text=/credenziali|password errata|utente non valido/i",

    # --- Navigazione verso l'elenco documenti ---
    # Se i documenti sono su una pagina/tab dedicata, indica qui il link da
    # cliccare dopo il login. Se sono già visibili in home, lascia None.
    "documents_nav_link": None,  # es: "text=Documenti"

    # --- Elenco documenti ---
    # Selettore delle righe della tabella/lista documenti
    "document_rows": "table tr:has(a)",
    # Dentro ogni riga, il link/bottone che avvia il download
    "download_trigger_in_row": "a",
    # Colonna/testo da usare come nome del documento (per il nome file)
    "document_name_in_row": "td:first-child",

    # --- Paginazione (se presente) ---
    "next_page_button": "text=/successiva|avanti|next/i",

    # --- Cartellino --- (confermato: link "Cartellino" in home; la pagina
    # mostra un mese intero come griglia calendario ASP.NET Calendar —
    # navigazione mese solo via postback JS, niente parametri in URL.
    # Per vedere le causali/ore di un giorno bisogna CLICCARE sul giorno:
    # il dettaglio compare nella stessa pagina, il calendario resta visibile.)
    # cartellino_link: ora si usa direttamente page.get_by_role("link", name="Cartellino")
    # nella funzione goto_cartellino() — stessa ragione del login_button sopra.
    # Bottoni per cambiare mese (confermati via codegen)
    # prev_month_button / next_month_button: ora get_by_role diretto in
    # click_month_nav() — stessa ragione di login_button/cartellino_link sopra.
}

# Timeout di rete/UI in millisecondi
NAV_TIMEOUT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 60_000

# Causali da estrarre dal cartellino: il testo dopo l'etichetta, fino al
# valore HH:MM, viene catturato con una regex (confermato: label e valore
# sono nello stesso nodo di testo, es. "ORE ORDINARIE 03: 08:00").
CAUSALI = [
    "ORE ORDINARIE",
    "STRAORD. FORFAIT",
    "PERMESSO/FERIE",
    "FERIE",
    "MALATTIA",
    "CASSA INTEGRAZ.ORDIN",
    "SMART WORKING",
    "MALA. LUNGA >30GG",
    "RIPOSO DOMENICA",
]

ITALIAN_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
ITALIAN_MONTHS_REV = {num: name for name, num in ITALIAN_MONTHS.items()}

# Ruoli ARIA in cui può comparire un'etichetta 'mese anno' o un elemento
# cliccabile: un'intestazione può essere <td> (role=cell) oppure <th>
# (role=columnheader) a seconda del markup, quindi si scansionano entrambi
# più qualche alternativa plausibile.
MONTH_LABEL_CANDIDATE_ROLES = ["cell", "columnheader", "gridcell", "rowheader", "heading", "link", "button"]


# ===========================================================================
# Utility
# ===========================================================================

def slugify_filename(name: str, fallback: str) -> str:
    """Rende una stringa sicura da usare come nome file."""
    name = (name or "").strip() or fallback
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\-. ]+", "_", name).strip("_ ")
    return name[:150] or fallback


def get_credentials(args: argparse.Namespace) -> tuple[str, str]:
    user = args.user or os.environ.get("SIGIT_USER")
    if not user:
        user = input("Utente SIGIT: ").strip()
    pwd = args.password or os.environ.get("SIGIT_PASS")
    if not pwd:
        pwd = getpass.getpass("Password SIGIT: ")
    return user, pwd


@dataclass
class DownloadResult:
    name: str
    ok: bool
    path: str | None = None
    error: str | None = None


def hhmm_to_hours(hhmm: str) -> float:
    """Converte una durata 'HH:MM' in ore decimali (es. '08:30' -> 8.5)."""
    h, m = hhmm.split(":")
    return round(int(h) + int(m) / 60, 4)


def parse_month_label(text: str) -> tuple[int, int]:
    """Estrae (anno, mese) da un'etichetta tipo 'Agosto 2017'.

    NOTA: richiede sempre un vero nome di mese italiano (niente fallback
    numerico tipo 'MM/AAAA'): un fallback del genere confonderebbe date in
    formato 'GG/MM/AAAA' presenti altrove in pagina (es. la data odierna
    nell'intestazione, 'Mercoledì 23/09/2026') con l'etichetta del
    calendario — bug reale riscontrato in produzione."""
    text_low = text.strip().lower()

    month = None
    for name, num in ITALIAN_MONTHS.items():
        if name in text_low:
            month = num
            break
    if month is None:
        raise ValueError(f"Impossibile trovare il mese in '{text}'")

    year_match = re.search(r"(\d{4})", text_low)
    if not year_match:
        raise ValueError(f"Impossibile trovare l'anno in '{text}'")
    year = int(year_match.group(1))

    return year, month


# Regex combinato per tutte le causali: cerca 'ETICHETTA HH:MM' (spazio, non
# due punti, tra etichetta e durata — confermato dal testo reale della
# pagina). Il lookbehind negativo impedisce che 'FERIE' catturi per sbaglio
# la coda di 'PERMESSO/FERIE' (le due causali condividono la sottostringa).
CAUSALE_VALUE_RE = re.compile(
    r"(?<![A-Za-zÀ-ÖØ-öø-ÿ./])(" + "|".join(re.escape(c) for c in CAUSALI) + r")\s+(\d{1,2}:\d{2})"
)


def find_causale_matches(text: str) -> list[tuple[str, float]]:
    """Trova TUTTE le occorrenze 'causale + ore' nel testo, nell'ordine in
    cui compaiono (non solo la prima per causale: serve per poter contare
    quante volte compare ciascuna, vedi harvest_month). Tutte le causali,
    SMART WORKING inclusa, seguono lo stesso formato 'ETICHETTA HH:MM'
    (confermato)."""
    result: list[tuple[str, float]] = []
    for label, value in CAUSALE_VALUE_RE.findall(text):
        result.append((label, hhmm_to_hours(value)))
    return result





async def dump_page_diagnostics(page: Page, context_label: str, limit: int = 60) -> None:
    """Aiuto diagnostico: quando un click/locator atteso non si trova, stampa
    URL corrente, eventuali frame (siti con <frameset> nascondono elementi
    ai locator sulla pagina principale), i link/bottoni visibili, e salva
    uno screenshot su disco — molto più veloce da interpretare che continuare
    a indovinare dal solo testo: carica il file .png qui in chat."""
    print(f"  [debug:{context_label}] URL corrente: {page.url}")

    try:
        safe_label = re.sub(r"[^\w\-]+", "_", context_label)
        screenshot_path = Path(f"debug_{safe_label}.png").resolve()
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"  [debug:{context_label}] screenshot salvato in: {screenshot_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  [debug:{context_label}] impossibile salvare lo screenshot: {e}")

    try:
        safe_label = re.sub(r"[^\w\-]+", "_", context_label)
        text_path = Path(f"debug_{safe_label}.txt").resolve()
        body_text = await page.locator("body").inner_text()
        text_path.write_text(body_text, encoding="utf-8")
        print(f"  [debug:{context_label}] testo grezzo della pagina salvato in: {text_path}")
    except Exception as e:  # noqa: BLE001
        print(f"  [debug:{context_label}] impossibile salvare il testo della pagina: {e}")

    frames = page.frames
    if len(frames) > 1:
        print(f"  [debug:{context_label}] ATTENZIONE: la pagina ha {len(frames)} frame/iframe:")
        for fr in frames:
            print(f"    - frame name={fr.name!r} url={fr.url}")
        print(
            "    Se il link cercato è dentro uno di questi frame (diverso da quello "
            "principale), page.get_by_role(...) sulla page non lo trova: serve "
            "page.frame_locator(<selettore-iframe-o-name>).get_by_role(...). "
            "Segnalamelo indicando quale frame contiene il link."
        )

    for role in MONTH_LABEL_CANDIDATE_ROLES:
        try:
            loc = page.get_by_role(role)
            count = await loc.count()
            texts = []
            for i in range(min(count, limit)):
                try:
                    t = (await loc.nth(i).inner_text()).strip()
                    if t:
                        texts.append(t)
                except Exception:
                    continue
            print(f"  [debug:{context_label}] {count} elementi role={role}, testi visibili: {texts}")
        except Exception as e:  # noqa: BLE001
            print(f"  [debug:{context_label}] impossibile enumerare role={role}: {e}")


# ===========================================================================
# Core: login
# ===========================================================================

async def login(page: Page, username: str, password: str) -> None:
    print(f"Apro {BASE_URL} ...")
    await page.goto(BASE_URL, timeout=NAV_TIMEOUT_MS, wait_until="networkidle")

    await page.locator(SELECTORS["username_field"]).first.fill(username)
    await page.locator(SELECTORS["password_field"]).first.fill(password)

    login_button = page.get_by_role("button", name="Login")
    try:
        await login_button.click(timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        print("⚠️  Non trovo/clicco il bottone 'Login'.")
        await dump_page_diagnostics(page, "login")
        raise
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(300)

    # Verifica esito login
    error_locator = page.locator(SELECTORS["login_error"])
    if await error_locator.count() > 0 and await error_locator.first.is_visible():
        error_text = await error_locator.first.inner_text()
        raise RuntimeError(f"Login fallito: {error_text.strip()}")

    ok_locator = page.locator(SELECTORS["logged_in_indicator"])
    try:
        await ok_locator.first.wait_for(state="visible", timeout=10_000)
        print("Login riuscito.")
    except PWTimeoutError:
        print(
            "⚠️  Non trovo l'indicatore di login riuscito configurato in "
            "SELECTORS['logged_in_indicator'] (non ancora verificato/confermato). "
            "Continuo comunque, ma se il resto fallisce probabilmente il login "
            "non è andato a buon fine, oppure questo selettore va corretto."
        )
        await dump_page_diagnostics(page, "login (indicatore non trovato)")


# ===========================================================================
# Core: navigazione verso la sezione documenti
# ===========================================================================

async def goto_documents(page: Page) -> None:
    nav = SELECTORS["documents_nav_link"]
    if nav:
        print("Navigo verso la sezione documenti...")
        async with page.expect_navigation(timeout=NAV_TIMEOUT_MS, wait_until="networkidle"):
            await page.locator(nav).first.click()


# ===========================================================================
# Core: raccolta elenco documenti (con paginazione)
# ===========================================================================

@dataclass
class DocRow:
    name: str
    row_index: int  # indice della riga nella pagina corrente, per ri-localizzarla


async def collect_rows_current_page(page: Page) -> list[DocRow]:
    rows = page.locator(SELECTORS["document_rows"])
    count = await rows.count()
    result = []
    for i in range(count):
        row = rows.nth(i)
        trigger = row.locator(SELECTORS["download_trigger_in_row"]).first
        if await trigger.count() == 0:
            continue
        name_locator = row.locator(SELECTORS["document_name_in_row"]).first
        try:
            name = (await name_locator.inner_text()).strip()
        except Exception:
            name = f"documento_{i}"
        result.append(DocRow(name=name, row_index=i))
    return result


async def has_next_page(page: Page) -> bool:
    btn = page.locator(SELECTORS["next_page_button"])
    if await btn.count() == 0:
        return False
    first = btn.first
    if not await first.is_visible():
        return False
    disabled = await first.get_attribute("disabled")
    return disabled is None


# ===========================================================================
# Core: download di una riga
# ===========================================================================

async def download_row(page: Page, row_index: int, out_dir: Path, used_names: set[str]) -> DownloadResult:
    rows = page.locator(SELECTORS["document_rows"])
    row = rows.nth(row_index)
    trigger = row.locator(SELECTORS["download_trigger_in_row"]).first
    name_locator = row.locator(SELECTORS["document_name_in_row"]).first
    try:
        raw_name = (await name_locator.inner_text()).strip()
    except Exception:
        raw_name = f"documento_{row_index}"

    try:
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            await trigger.click()
        download: Download = await dl_info.value
        suggested = download.suggested_filename or raw_name
        base = slugify_filename(raw_name, fallback=Path(suggested).stem)
        ext = Path(suggested).suffix or ""
        filename = f"{base}{ext}"
        n = 1
        while filename in used_names:
            filename = f"{base}_{n}{ext}"
            n += 1
        used_names.add(filename)
        dest = out_dir / filename
        await download.save_as(str(dest))
        return DownloadResult(name=raw_name, ok=True, path=str(dest))
    except PWTimeoutError:
        return DownloadResult(name=raw_name, ok=False, error="timeout durante il download")
    except Exception as e:  # noqa: BLE001
        return DownloadResult(name=raw_name, ok=False, error=str(e))


# ===========================================================================
# Core: Cartellino (raccolta ore per causale, mese per mese)
# ===========================================================================

async def goto_cartellino(page: Page) -> None:
    menu_link = page.get_by_role("link", name="Infopoint", exact=True)
    link = page.get_by_role("link", name="Cartellino")

    # Tentativo 1: il click su 'Infopoint' potrebbe essere un no-op se il
    # modulo è già quello corrente (l'URL è già /infopoint/...): proviamo
    # comunque, non fa danno.
    print("Apro il menu 'Infopoint' (click)...")
    try:
        await menu_link.click(timeout=NAV_TIMEOUT_MS)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(300)
    except PWTimeoutError:
        print("⚠️  Non trovo/clicco il link 'Infopoint'.")
        await dump_page_diagnostics(page, "goto_cartellino:click_infopoint")
        raise

    if await link.count() == 0:
        # Tentativo 2: menu a tendina classico che si apre solo al passaggio
        # del mouse (CSS/JS :hover), non al click.
        print("  'Cartellino' non ancora visibile, provo hover su 'Infopoint'...")
        try:
            await menu_link.hover(timeout=NAV_TIMEOUT_MS)
        except PWTimeoutError:
            print("⚠️  Non riesco a fare hover su 'Infopoint'.")
            await dump_page_diagnostics(page, "goto_cartellino:hover_infopoint")
            raise
        await page.wait_for_timeout(500)  # margine per eventuale dropdown CSS/JS

    if await link.count() == 0:
        print("⚠️  Il link 'Cartellino' non compare né dopo il click né dopo l'hover su 'Infopoint'.")
        await dump_page_diagnostics(page, "goto_cartellino")
        raise RuntimeError("Impossibile trovare il link 'Cartellino': vedi diagnostica/screenshot sopra.")

    print("Apro la sezione Cartellino...")
    try:
        await link.first.click(timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        print("⚠️  Trovato 'Cartellino' ma non riesco a cliccarlo (forse sparisce se il mouse si sposta).")
        await dump_page_diagnostics(page, "goto_cartellino:click_cartellino")
        raise
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(300)


MONTH_LABEL_MAX_LEN = 100  # l'intestazione vera è corta (es. "agosto 2026" o
# "Precedente\tsettembre 2016\tSuccessivo", poche decine di caratteri); le
# celle-contenitore innestate arrivano a includere l'intera pagina (centinaia/
# migliaia di caratteri) e possono contenere ALTRE date (es. quella odierna
# nell'intestazione) che farebbero leggere l'anno sbagliato — si scartano.


async def read_current_month(page: Page) -> tuple[int, int]:
    """Scansiona più ruoli ARIA (una cella d'intestazione può essere <td>
    'cell' oppure <th> 'columnheader', a seconda del markup) cercando quella
    che contiene 'nomemese anno' (es. 'Settembre 2026'): non è un selettore
    fisso perché il testo cambia ad ogni mese, quindi si prova a fare il
    parse di ogni elemento finché uno ha successo (di norma è solo
    l'intestazione). I candidati troppo lunghi (celle-contenitore che
    inglobano l'intera pagina) vengono scartati a priori: bug reale
    riscontrato in produzione, in cui la cella più esterna conteneva sia la
    data odierna sia l'intestazione del calendario, facendo leggere l'anno
    della data odierna invece di quello del mese mostrato.

    Se la scansione non trova nulla, ritenta un paio di volte con attesa
    crescente prima di arrendersi: la pagina potrebbe non aver ancora
    finito di ri-renderizzare dopo il postback del cambio mese precedente
    (bug reale riscontrato in produzione)."""
    for wait_ms in (0, 500, 1000, 1500):
        if wait_ms:
            await page.wait_for_timeout(wait_ms)
        for role in MONTH_LABEL_CANDIDATE_ROLES:
            try:
                elements = page.get_by_role(role)
                count = await elements.count()
            except Exception:
                continue
            for i in range(count):
                try:
                    text = (await elements.nth(i).inner_text()).strip()
                except Exception:
                    continue
                if len(text) > MONTH_LABEL_MAX_LEN:
                    continue
                try:
                    return parse_month_label(text)
                except ValueError:
                    continue

    print("⚠️  Non trovo nessun elemento con testo 'mese anno' nella griglia del calendario.")
    await dump_page_diagnostics(page, "read_current_month")
    raise RuntimeError(
        "Non trovo nessun elemento con testo 'mese anno' nella griglia del calendario "
        "(controlla che il Cartellino sia effettivamente aperto e mostri già un mese; "
        "guarda l'elenco di link/bottoni appena stampato per capire cosa c'è davvero in pagina)."
    )


async def click_month_nav(page: Page, direction: str) -> None:
    """direction: 'prev' o 'next'. Gestisce sia postback pieni che AJAX parziali."""
    if direction == "prev":
        locator = page.get_by_role("cell", name="Precedente", exact=True)
    else:
        locator = page.get_by_role("link", name="Successivo")
    try:
        await locator.click(timeout=NAV_TIMEOUT_MS)
    except PWTimeoutError:
        print(f"⚠️  Non trovo/clicco il controllo di navigazione mese '{direction}'.")
        await dump_page_diagnostics(page, f"click_month_nav:{direction}")
        raise
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(300)  # margine di sicurezza per script post-postback


async def goto_month(page: Page, target_year: int, target_month: int, max_steps: int = 300) -> None:
    """Naviga cliccando mese precedente/successivo finché non si raggiunge
    il mese/anno target, leggendo ogni volta l'etichetta mostrata a schermo
    (robusto: non assume da quale mese parte la pagina all'apertura).

    Se il mese letto resta identico per 3 letture di fila NONOSTANTE i
    click (es. il sito impedisce di andare oltre una certa data, o il
    pulsante prev/next smette di rispondere), si ferma subito con
    diagnostica invece di continuare a cliccare a vuoto fino a max_steps.
    """
    last_seen: tuple[int, int] | None = None
    stuck_count = 0

    for _ in range(max_steps):
        cur_year, cur_month = await read_current_month(page)
        if (cur_year, cur_month) == (target_year, target_month):
            return

        if last_seen == (cur_year, cur_month):
            stuck_count += 1
            if stuck_count >= 3:
                print(
                    f"⚠️  Il mese visualizzato resta fermo a {cur_month:02d}/{cur_year} "
                    f"nonostante i click (obiettivo: {target_month:02d}/{target_year}). "
                    "Probabile limite del sito su quanto si può navigare indietro/avanti "
                    "con 'Precedente'/'Successivo' (o serve un altro controllo, es. un "
                    "menu a tendina anno/mese)."
                )
                await dump_page_diagnostics(page, f"goto_month_stuck_{target_year}_{target_month:02d}")
                raise RuntimeError(
                    f"Impossibile raggiungere {target_month:02d}/{target_year}: la navigazione si "
                    f"è bloccata a {cur_month:02d}/{cur_year}. Vedi diagnostica/screenshot sopra."
                )
        else:
            stuck_count = 0
        last_seen = (cur_year, cur_month)

        if (cur_year, cur_month) < (target_year, target_month):
            await click_month_nav(page, "next")
        else:
            await click_month_nav(page, "prev")

    raise RuntimeError(
        f"Non riesco a raggiungere {target_month:02d}/{target_year} dopo {max_steps} tentativi "
        "(controlla i selettori prev_month_button/next_month_button, oppure che read_current_month "
        "riesca a leggere 'mese anno' da una cella della griglia)."
    )


@dataclass
class CartellinoRow:
    anno: int
    mese: int
    giorno: int
    causale: str
    ore: float


async def click_day(page: Page, year: int, month: int, day_num: int) -> bool:
    """Clicca sul link del giorno indicato nella griglia del mese corrente.

    NOTA: la griglia calendario mostra spesso, ai bordi, alcuni giorni del
    mese precedente/successivo (es. mentre sei su settembre puoi vedere
    anche '9' di ottobre) con lo stesso numero visibile: get_by_role da solo
    può quindi trovare più link con lo stesso nome ('9'). In quel caso si
    disambigua usando l'attributo title o aria-label del link (che nei
    controlli Calendar ASP.NET contiene tipicamente 'gg nomemese', es.
    '09 ottobre'), cercando quello il cui attributo contiene il nome del
    mese target. Se anche questo fallisce, usa il primo trovato e stampa un
    avviso: verifica i dati di quei giorni a campione.

    Ritorna False se il giorno non viene trovato in nessun modo.
    """
    month_name = ITALIAN_MONTHS_REV[month]
    day_regex = re.compile(rf"^\s*0?{day_num}\s*$")

    candidate = page.get_by_role("link", name=str(day_num), exact=True)
    count = await candidate.count()

    # A volte la pagina non ha ancora finito di ri-renderizzare dopo il
    # postback del giorno/mese precedente: ritenta un paio di volte con
    # attesa crescente prima di arrendersi (bug reale riscontrato: diversi
    # giorni di fila non trovati, poi tutto tornava a funzionare da solo).
    for wait_ms in (500, 1000, 1500):
        if count > 0:
            break
        await page.wait_for_timeout(wait_ms)
        count = await candidate.count()

    if count > 1:
        for attr in ("title", "aria-label"):
            refined = page.locator(f'a[{attr}*="{month_name}" i]', has_text=day_regex)
            refined_count = await refined.count()
            if refined_count >= 1:
                candidate, count = refined, refined_count
                break
        else:
            print(
                f"  ⚠️  Trovati {count} link per il giorno {day_num} in {month_name} {year}: "
                "uso il primo. Verifica a campione che i dati di questo giorno siano corretti."
            )

    if count == 0:
        return False

    await candidate.first.click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(300)  # margine per eventuale script post-postback/AJAX
    return True


async def harvest_month(page: Page, year: int, month: int, start: date, end: date) -> list[CartellinoRow]:
    """Per ogni giorno del mese nell'intervallo [start, end], clicca sul
    giorno nella griglia calendario. Il click aggiunge (non sostituisce) una
    riga 'CAUSALE HH:MM' al pannello 'Stampa cartellino mensile' a fianco
    del calendario — confermato guardando screenshot e testo reali della
    pagina. Siccome il pannello si accumula, non si può semplicemente
    rileggere tutta la pagina ad ogni click (si rileggerebbero anche i
    giorni precedenti): si confronta il conteggio di ogni 'causale+ore'
    PRIMA e DOPO il click con un Counter, e si attribuisce al giorno solo
    la differenza (le occorrenze aggiunte da QUESTO click). La baseline
    viene fissata leggendo la pagina subito dopo l'apertura del mese, PRIMA
    di cliccare qualsiasi giorno, così eventuali righe residue di mesi
    precedenti (se il pannello non si svuota cambiando mese) non vengono
    attribuite per errore al primo giorno del mese nuovo.
    """
    rows_out: list[CartellinoRow] = []
    clicked_at_least_one_day = False
    last_day = calendar.monthrange(year, month)[1]

    baseline_text = await page.locator("body").inner_text()
    previous_matches = Counter(find_causale_matches(baseline_text))

    for day_num in range(1, last_day + 1):
        try:
            current_date = date(year, month, day_num)
        except ValueError:
            continue
        if current_date < start or current_date > end:
            continue

        found = await click_day(page, year, month, day_num)
        if not found:
            print(f"  ⚠️  Giorno {day_num:02d}/{month:02d}/{year} non trovato nella griglia, salto.")
            continue
        clicked_at_least_one_day = True

        page_text = await page.locator("body").inner_text()
        current_matches = Counter(find_causale_matches(page_text))

        new_matches = current_matches - previous_matches  # solo ciò che è aumentato
        for (causale, ore), count in new_matches.items():
            if ore <= 0:
                continue  # salta le causali a zero ore; rimuovi il filtro se le vuoi comunque
            rows_out.extend(CartellinoRow(year, month, day_num, causale, ore) for _ in range(count))

        previous_matches = current_matches

    if clicked_at_least_one_day and not rows_out:
        print(
            f"  ⚠️  Nessuna causale trovata in tutto {month:02d}/{year} pur avendo cliccato "
            "giorni validi: salvo screenshot e testo grezzo dell'ultimo giorno aperto per capire "
            "cosa c'è davvero dopo un click (magari il formato non è quello atteso, o il mese è "
            "davvero senza causali)."
        )
        await dump_page_diagnostics(page, f"harvest_zero_{year}_{month:02d}")

    return rows_out


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cartellino (
            anno INTEGER NOT NULL,
            mese INTEGER NOT NULL,
            giorno INTEGER NOT NULL,
            causale TEXT NOT NULL,
            ore REAL,
            PRIMARY KEY (anno, mese, giorno, causale)
        )
        """
    )
    conn.commit()
    return conn


def save_rows(conn: sqlite3.Connection, rows: list[CartellinoRow]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO cartellino (anno, mese, giorno, causale, ore) VALUES (?, ?, ?, ?, ?)",
        [(r.anno, r.mese, r.giorno, r.causale, r.ore) for r in rows],
    )
    conn.commit()


def iter_months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m == 13:
            m = 1
            y += 1


# ===========================================================================
# Comandi CLI
# ===========================================================================

async def cmd_login(args: argparse.Namespace) -> None:
    username, password = get_credentials(args)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.show_browser)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            await login(page, username, password)
            print("OK: sessione autenticata correttamente.")
            if args.show_browser:
                print("Premi INVIO per chiudere il browser...")
                await asyncio.get_event_loop().run_in_executor(None, input)
        finally:
            await browser.close()


async def cmd_download(args: argparse.Namespace) -> None:
    username, password = get_credentials(args)
    out_dir = Path(args.output).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.show_browser)
        context: BrowserContext = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            await login(page, username, password)
            await goto_documents(page)

            used_names: set[str] = {p.name for p in out_dir.glob("*") if p.is_file()}
            total_ok, total_fail = 0, 0
            page_num = 1

            while True:
                print(f"\n--- Pagina {page_num} ---")
                rows = await collect_rows_current_page(page)
                print(f"Trovati {len(rows)} documenti in questa pagina.")

                if args.dry_run:
                    for r in rows:
                        print(f"  • {r.name}")
                else:
                    for r in rows:
                        if args.limit and (total_ok + total_fail) >= args.limit:
                            break
                        result = await download_row(page, r.row_index, out_dir, used_names)
                        if result.ok:
                            total_ok += 1
                            print(f"  ✔ {result.name} -> {result.path}")
                        else:
                            total_fail += 1
                            print(f"  ✘ {result.name}: {result.error}")

                if args.limit and (total_ok + total_fail) >= args.limit:
                    break

                if not await has_next_page(page):
                    break
                async with page.expect_navigation(timeout=NAV_TIMEOUT_MS, wait_until="networkidle"):
                    await page.locator(SELECTORS["next_page_button"]).first.click()
                page_num += 1

            print(f"\nCompletato. Scaricati: {total_ok}  Falliti: {total_fail}  Cartella: {out_dir}")

        finally:
            await browser.close()


async def cmd_cartellino(args: argparse.Namespace) -> None:
    username, password = get_credentials(args)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else (date.today() - timedelta(days=1))
    if start > end:
        raise RuntimeError(f"Intervallo non valido: start {start} è dopo end {end}.")

    db_path = Path(args.db).expanduser().resolve()
    conn = init_db(db_path)
    print(f"Database: {db_path}")
    print(f"Intervallo: {start.isoformat()} -> {end.isoformat()}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.show_browser)
        context: BrowserContext = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            await login(page, username, password)
            await goto_cartellino(page)

            months = list(iter_months(start, end))
            total_rows = 0
            for idx, (year, month) in enumerate(months, start=1):
                print(f"\n--- Mese {idx}/{len(months)}: {month:02d}/{year} ---")
                await goto_month(page, year, month)
                rows = await harvest_month(page, year, month, start, end)
                save_rows(conn, rows)
                total_rows += len(rows)
                print(f"  {len(rows)} righe causale/ore salvate (totale finora: {total_rows}).")

            print(f"\nCompletato. Righe totali salvate: {total_rows}  Database: {db_path}")

        finally:
            conn.close()
            await browser.close()


# ===========================================================================
# Entry point
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    # Argomenti comuni: vanno messi DOPO il sottocomando, es.
    #   sigit_cli.py cartellino --show-browser --db test.db
    # (in argparse, gli argomenti del parser principale andrebbero messi
    # PRIMA del sottocomando; per evitare questa trappola li definiamo su
    # un parser "parent" condiviso da ogni sottocomando, così funzionano
    # nella posizione più naturale, dopo il nome del comando.)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--user", help="Username (oppure variabile d'ambiente SIGIT_USER)")
    common.add_argument("--password", help="Password (oppure variabile d'ambiente SIGIT_PASS; sconsigliato passarla da riga di comando)")
    common.add_argument("--show-browser", action="store_true", help="Mostra il browser invece di girare in headless (utile per debug)")

    p = argparse.ArgumentParser(
        prog="sigit_cli.py",
        description="CLI per login e download massivo documenti da SIGIT Infopoint.",
    )

    sub = p.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Testa solo il login, senza scaricare nulla", parents=[common])
    p_login.set_defaults(func=cmd_login)

    p_dl = sub.add_parser("download", help="Scarica in blocco i documenti", parents=[common])
    p_dl.add_argument("-o", "--output", required=True, help="Cartella di destinazione")
    p_dl.add_argument("--dry-run", action="store_true", help="Elenca i documenti trovati senza scaricarli")
    p_dl.add_argument("--limit", type=int, default=0, help="Numero massimo di documenti da scaricare (0 = tutti)")
    p_dl.set_defaults(func=cmd_download)

    p_cart = sub.add_parser("cartellino", help="Raccoglie ore per causale dal Cartellino, mese per mese, in un DB SQLite", parents=[common])
    p_cart.add_argument("--db", default="cartellino.db", help="Percorso del file SQLite di destinazione (default: cartellino.db)")
    p_cart.add_argument("--start", default="2017-08-29", help="Data di inizio, formato YYYY-MM-DD (default: 2017-08-29)")
    p_cart.add_argument("--end", default=None, help="Data di fine, formato YYYY-MM-DD (default: ieri)")
    p_cart.set_defaults(func=cmd_cartellino)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(args.func(args))
    except RuntimeError as e:
        print(f"Errore: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
        sys.exit(130)


if __name__ == "__main__":
    main()
