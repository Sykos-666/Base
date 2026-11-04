#!/usr/bin/env python3
"""
Scarica le trascrizioni di tutti i video di un canale o di una playlist YouTube
e le salva in file di testo.

Esempi:
    python yt_transcripts.py https://www.youtube.com/@NomeCanale
    python yt_transcripts.py "https://www.youtube.com/playlist?list=PLxxxx" -l it,en
    python yt_transcripts.py @NomeCanale --single-file --timestamps

Dipendenze:  pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import yt_dlp
    from youtube_transcript_api import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
        YouTubeTranscriptApi,
    )
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"Dipendenza mancante ({exc.name}). Installa con:\n"
        "    pip install -r requirements.txt"
    )


# --------------------------------------------------------------------------- #
# Strutture dati
# --------------------------------------------------------------------------- #
@dataclass
class Video:
    id: str
    title: str
    url: str


@dataclass
class Segment:
    start: float
    text: str


@dataclass
class Transcript:
    language: str
    language_code: str
    kind: str  # "manuale", "automatica", "tradotta"
    source: str  # libreria usata per recuperarla
    segments: list[Segment]


class TranscriptError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Elenco dei video (canale / playlist)
# --------------------------------------------------------------------------- #
def normalize_url(raw: str) -> str:
    """Accetta anche forme brevi: '@handle', 'UCxxxx' (id canale), 'PLxxxx' (id playlist)."""
    raw = raw.strip()
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}"
    if re.fullmatch(r"UC[\w-]{22}", raw):
        return f"https://www.youtube.com/channel/{raw}"
    if re.fullmatch(r"(PL|UU|LL|FL|OL)[\w-]{10,}", raw):
        return f"https://www.youtube.com/playlist?list={raw}"
    if not re.match(r"https?://", raw):
        return "https://" + raw
    return raw


def _ydl_base_opts(args: argparse.Namespace) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if args.cookies:
        opts["cookiefile"] = args.cookies
    if args.proxy:
        opts["proxy"] = args.proxy
    return opts


def _is_video_entry(entry: dict) -> bool:
    url = entry.get("url") or ""
    return entry.get("ie_key") == "Youtube" or "/watch" in url or "/shorts/" in url


def list_videos(url: str, args: argparse.Namespace) -> tuple[str, list[Video]]:
    """Restituisce (titolo della raccolta, lista dei video) senza duplicati."""
    opts = _ydl_base_opts(args) | {"extract_flat": True, "ignoreerrors": True}
    videos: list[Video] = []
    seen: set[str] = set()

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError(f"Impossibile leggere {url}")

        collection_title = info.get("title") or info.get("channel") or info.get("id") or "youtube"

        # URL di un singolo video
        if info.get("_type") not in ("playlist", "multi_video") and not info.get("entries"):
            vid = info["id"]
            return collection_title, [Video(vid, info.get("title") or vid, f"https://www.youtube.com/watch?v={vid}")]

        def walk(node: dict, depth: int = 0) -> None:
            for entry in node.get("entries") or []:
                if not entry:
                    continue
                if _is_video_entry(entry):
                    vid = entry.get("id")
                    if not vid or vid in seen:
                        continue
                    if args.no_shorts and "/shorts/" in (entry.get("url") or ""):
                        continue
                    seen.add(vid)
                    videos.append(Video(vid, entry.get("title") or vid,
                                        f"https://www.youtube.com/watch?v={vid}"))
                    if args.limit and len(videos) >= args.limit:
                        return
                elif entry.get("entries"):
                    walk(entry, depth + 1)
                elif entry.get("url") and depth < 3:
                    # Sotto-scheda del canale (Video, Shorts, Live...) o playlist annidata
                    tab_url = entry["url"]
                    if args.no_shorts and tab_url.rstrip("/").endswith("/shorts"):
                        continue
                    sub = ydl.extract_info(tab_url, download=False)
                    if sub:
                        walk(sub, depth + 1)
                if args.limit and len(videos) >= args.limit:
                    return

        walk(info)

    return collection_title, videos


# --------------------------------------------------------------------------- #
# Recupero della trascrizione
# --------------------------------------------------------------------------- #
def _fetch_with_transcript_api(api: YouTubeTranscriptApi, video_id: str,
                               languages: list[str], translate: bool,
                               any_language: bool) -> Transcript:
    transcript_list = api.list(video_id)
    available = list(transcript_list)
    kind = None
    chosen = None

    try:
        chosen = transcript_list.find_manually_created_transcript(languages)
    except NoTranscriptFound:
        try:
            chosen = transcript_list.find_generated_transcript(languages)
        except NoTranscriptFound:
            pass

    if chosen is None and available:
        original = next((t for t in available if not t.is_generated), available[0])
        if translate and original.is_translatable and any(
            tl.language_code == languages[0] for tl in original.translation_languages
        ):
            chosen = original.translate(languages[0])
            kind = "tradotta"
        elif any_language:
            chosen = original

    if chosen is None:
        codes = ", ".join(t.language_code for t in available) or "nessuna"
        raise TranscriptError(f"nessuna trascrizione nelle lingue richieste (disponibili: {codes})")

    fetched = chosen.fetch()
    segments = [Segment(s.start, s.text) for s in fetched]
    return Transcript(
        language=chosen.language,
        language_code=chosen.language_code,
        kind=kind or ("automatica" if chosen.is_generated else "manuale"),
        source="youtube-transcript-api",
        segments=segments,
    )


def _lang_match(code: str, wanted: str) -> bool:
    code, wanted = code.lower(), wanted.lower()
    return code == wanted or code.split("-")[0] == wanted


def _fetch_with_ytdlp(video: Video, languages: list[str], args: argparse.Namespace,
                      any_language: bool) -> Transcript:
    """Alternativa: legge i sottotitoli (formato json3) tramite yt-dlp."""
    with yt_dlp.YoutubeDL(_ydl_base_opts(args)) as ydl:
        info = ydl.extract_info(video.url, download=False)
        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        # Le lingue "-orig" sono la traccia automatica originale (non tradotta)
        orig_auto = {k[:-5]: v for k, v in auto.items() if k.endswith("-orig")}

        candidates: list[tuple[str, str, list]] = []
        # Priorità: sottotitoli manuali, poi automatici originali, nelle lingue richieste
        for pool, kind in ((manual, "manuale"), (orig_auto, "automatica")):
            for lang in languages:
                candidates += [(c, kind, f) for c, f in pool.items()
                               if c != "live_chat" and _lang_match(c, lang)]
        # Le altre voci di automatic_captions sono traduzioni automatiche
        if args.translate:
            candidates += [(c, "tradotta", f) for c, f in auto.items()
                           if not c.endswith("-orig") and _lang_match(c, languages[0])]
        if any_language:
            candidates += [(c, "manuale", f) for c, f in manual.items() if c != "live_chat"]
            candidates += [(c, "automatica", f) for c, f in orig_auto.items()]

        for code, kind, formats in candidates:
            fmt = next((f for f in formats if f.get("ext") == "json3"), None)
            if not fmt:
                continue
            data = json.loads(ydl.urlopen(fmt["url"]).read().decode("utf-8"))
            segments = []
            for ev in data.get("events", []):
                text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
                if text:
                    segments.append(Segment(ev.get("tStartMs", 0) / 1000, text))
            if segments:
                return Transcript(fmt.get("name") or code, code, kind, "yt-dlp", segments)

    raise TranscriptError("nessun sottotitolo disponibile tramite yt-dlp")


def get_transcript(api: YouTubeTranscriptApi, video: Video, args: argparse.Namespace) -> Transcript:
    errors = []
    try:
        return _fetch_with_transcript_api(api, video.id, args.languages, args.translate, args.any_language)
    except (TranscriptsDisabled, VideoUnavailable) as exc:
        # Errori definitivi: inutile riprovare con yt-dlp
        raise TranscriptError(type(exc).__name__) from exc
    except TranscriptError:
        raise
    except Exception as exc:  # IpBlocked, PoTokenRequired, nessuna lingua, ...
        errors.append(f"transcript-api: {exc.__class__.__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")

    try:
        return _fetch_with_ytdlp(video, args.languages, args, args.any_language)
    except Exception as exc:
        errors.append(f"yt-dlp: {exc}")
    raise TranscriptError(" | ".join(errors))


# --------------------------------------------------------------------------- #
# Formattazione e salvataggio
# --------------------------------------------------------------------------- #
def safe_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len] or "senza_titolo"


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render(video: Video, tr: Transcript, timestamps: bool) -> str:
    header = (
        f"Titolo:  {video.title}\n"
        f"URL:     {video.url}\n"
        f"Lingua:  {tr.language} ({tr.language_code}) - trascrizione {tr.kind}\n"
        + "=" * 80 + "\n\n"
    )
    clean = [(s.start, re.sub(r"\s+", " ", s.text).strip()) for s in tr.segments]
    clean = [(t, txt) for t, txt in clean if txt]
    if timestamps:
        body = "\n".join(f"[{fmt_time(t)}] {txt}" for t, txt in clean)
    else:
        body = textwrap.fill(" ".join(txt for _, txt in clean), width=100)
    return header + body + "\n"


def find_existing(out_dir: Path, video_id: str) -> Path | None:
    suffix = f"[{video_id}].txt"
    return next((p for p in out_dir.glob("*.txt") if p.name.endswith(suffix)), None)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scarica le trascrizioni di tutti i video di un canale o playlist YouTube.",
    )
    p.add_argument("url", help="URL del canale/playlist (anche '@handle', id canale 'UC...' o id playlist 'PL...')")
    p.add_argument("-o", "--output-dir", default="trascrizioni", help="cartella di destinazione (default: trascrizioni)")
    p.add_argument("-l", "--languages", default="it,en",
                   help="lingue preferite in ordine di priorità, separate da virgola (default: it,en)")
    p.add_argument("--translate", action="store_true",
                   help="se manca una trascrizione nelle lingue richieste, traduci automaticamente nella prima lingua")
    p.add_argument("--only-languages", dest="any_language", action="store_false",
                   help="non salvare trascrizioni in lingue diverse da quelle richieste (default: le salva comunque)")
    p.add_argument("--single-file", action="store_true", help="salva tutte le trascrizioni in un unico file .txt")
    p.add_argument("--timestamps", action="store_true", help="includi i timestamp [hh:mm:ss] per ogni riga")
    p.add_argument("--no-shorts", action="store_true", help="ignora gli Shorts del canale")
    p.add_argument("--limit", type=int, default=0, help="numero massimo di video da elaborare (0 = tutti)")
    p.add_argument("--overwrite", action="store_true", help="riscarica anche le trascrizioni già salvate")
    p.add_argument("--delay", type=float, default=1.5,
                   help="pausa in secondi tra un video e l'altro, per evitare blocchi (default: 1.5)")
    p.add_argument("--cookies", help="file cookies.txt (formato Netscape) per video con restrizioni")
    p.add_argument("--proxy", help="proxy HTTP/HTTPS, es. http://utente:pass@host:porta")
    args = p.parse_args(argv)
    args.languages = [l.strip() for l in args.languages.split(",") if l.strip()] or ["en"]
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = normalize_url(args.url)

    print(f"Recupero l'elenco dei video da {url} ...")
    try:
        collection, videos = list_videos(url, args)
    except Exception as exc:
        print(f"Errore: impossibile ottenere l'elenco dei video: {exc}", file=sys.stderr)
        return 1
    if not videos:
        print("Nessun video trovato.")
        return 1
    print(f"Trovati {len(videos)} video in «{collection}».\n")

    root = Path(args.output_dir)
    out_dir = root if args.single_file else root / safe_filename(collection)
    out_dir.mkdir(parents=True, exist_ok=True)

    proxy_cfg = GenericProxyConfig(http_url=args.proxy, https_url=args.proxy) if args.proxy else None
    api = YouTubeTranscriptApi(proxy_config=proxy_cfg)

    single = None
    if args.single_file:
        single_path = out_dir / f"{safe_filename(collection)}.txt"
        single = single_path.open("w", encoding="utf-8")

    ok, skipped, failed = 0, 0, []
    width = len(str(len(videos)))
    try:
        for i, video in enumerate(videos, 1):
            prefix = f"[{i:>{width}}/{len(videos)}] {video.title[:70]}"

            if not args.single_file and not args.overwrite and find_existing(out_dir, video.id):
                print(f"{prefix} -> già presente, salto")
                skipped += 1
                continue

            try:
                tr = get_transcript(api, video, args)
            except TranscriptError as exc:
                print(f"{prefix} -> ERRORE: {exc}")
                failed.append((video, str(exc)))
            else:
                text = render(video, tr, args.timestamps)
                if single:
                    single.write(text + "\n\n")
                    single.flush()
                else:
                    name = f"{i:0{width}d} - {safe_filename(video.title)} [{video.id}].txt"
                    (out_dir / name).write_text(text, encoding="utf-8")
                print(f"{prefix} -> ok ({tr.language_code}, {tr.kind})")
                ok += 1

            if i < len(videos) and args.delay > 0:
                time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
    finally:
        if single:
            single.close()

    if failed:
        err_path = out_dir / "_errori.txt"
        err_path.write_text(
            "".join(f"{v.url}\t{v.title}\t{msg}\n" for v, msg in failed), encoding="utf-8"
        )

    print(f"\nCompletato: {ok} salvate, {skipped} già presenti, {len(failed)} senza trascrizione.")
    print(f"File in: {single_path if single else out_dir}")
    if failed:
        print(f"Elenco dei video non riusciti: {out_dir / '_errori.txt'}")
        if any("Blocked" in m for _, m in failed):
            print("Suggerimento: YouTube sta bloccando le richieste; aumenta --delay o usa --proxy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
