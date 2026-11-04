# Trascrizioni YouTube

Script che, dato un **canale** o una **playlist** di YouTube, trova tutti i video,
ne scarica la trascrizione e la salva in file di testo.

- Elenca i video con [yt-dlp](https://github.com/yt-dlp/yt-dlp). Per un canale legge tutte le schede (Video, Shorts, Live) ed elimina i duplicati.
- Scarica le trascrizioni con [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api). Se questa fallisce (es. IP bloccato) prova a leggere i sottotitoli tramite yt-dlp.
- Sceglie prima i sottotitoli manuali, poi quelli automatici, seguendo l'ordine delle lingue indicato.
- Le trascrizioni già scaricate vengono saltate, quindi si può rilanciare lo script per aggiornare un canale.

## Installazione

```bash
pip install -r requirements.txt
```

## Uso

```bash
python yt_transcripts.py https://www.youtube.com/@NomeCanale
```

```bash
python yt_transcripts.py "https://www.youtube.com/playlist?list=PLxxxxxxxx" -l it,en
```

```bash
python yt_transcripts.py @NomeCanale --single-file --timestamps
```

Si possono passare anche forme brevi: `@handle`, l'id del canale (`UC...`) o l'id della playlist (`PL...`).

### Opzioni principali

| Opzione | Descrizione |
|---|---|
| `-o, --output-dir` | cartella di destinazione (default `trascrizioni`) |
| `-l, --languages` | lingue preferite in ordine di priorità (default `it,en`) |
| `--translate` | se manca la lingua richiesta, usa la traduzione automatica di YouTube nella prima lingua |
| `--only-languages` | non salvare trascrizioni in lingue diverse da quelle richieste |
| `--single-file` | tutte le trascrizioni in un unico file `.txt` |
| `--timestamps` | aggiunge `[hh:mm:ss]` a ogni riga |
| `--no-shorts` | ignora gli Shorts |
| `--limit N` | elabora al massimo N video |
| `--overwrite` | riscarica anche i file già presenti |
| `--delay S` | pausa tra un video e l'altro (default 1.5 s) |
| `--cookies FILE` | file `cookies.txt` per video con restrizioni d'età o riservati ai membri |
| `--proxy URL` | proxy da usare se YouTube blocca il tuo IP |

## Output

Senza `--single-file` viene creata una cartella con il nome del canale/playlist e un file per video:

```
trascrizioni/
└── Nome Canale/
    ├── 001 - Titolo del video [dQw4w9WgXcQ].txt
    ├── 002 - Altro video [xxxxxxxxxxx].txt
    └── _errori.txt        # video senza trascrizione e motivo
```

Ogni file contiene titolo, URL, lingua e tipo di trascrizione (manuale, automatica o tradotta), seguiti dal testo.

## Note

- Se compaiono errori `IpBlocked` / `RequestBlocked`, YouTube sta limitando le richieste: aumenta `--delay` o usa `--proxy`.
- Il recupero alternativo tramite yt-dlp funziona meglio se è installato un runtime JavaScript (es. [Deno](https://deno.com)), come richiesto dalle versioni recenti di yt-dlp.
