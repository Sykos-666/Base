# NEON MANTRA — Kit di lancio virale + SEO

**Formato:** verticale 9:16 · 1080×1920 · 30 fps · 60 s esatti, in loop senza stacchi
**Traccia:** Indo-techno · 128 BPM · scala D Bhairav (Re Mi♭ Fa♯ Sol La Si♭ Do)
**File:** `out/NEON_MANTRA_9x16.mp4` · copertina `out/cover_9x16.jpg` · audio `out/neon_mantra.wav`

---

## 1. Struttura e momenti di retention

| Tempo | Sezione | Cosa succede | Testo a schermo |
|---|---|---|---|
| 0:00–0:07 | **HOOK** | Il riff di sitar parte dal primo frame, con tabla e tanpura. La cassa entra filtrata. | "POV: Indian classical meets TECHNO" → "NEON MANTRA / नियॉन मंत्र" |
| 0:07–0:22 | **GROOVE** | Cassa 4/4, basso techno in levare, chaal bhangra del dhol, laser | "sitar + tabla + dhol x 128 BPM techno" |
| 0:22–0:30 | **BUILD** | Rullante che accelera, riser, conto alla rovescia 4-3-2-1 | "WAIT FOR THE DROP" + numeri |
| **0:30** | **DROP** | Flash bianco, whip-zoom, i petali esplodono, supersaw sul riff di sitar, strobo sulle casse, giravolte delle danzatrici | "DROP" → "NEON MANTRA" |
| 0:52–0:60 | **OUTRO/LOOP** | Si torna all'hook, così il video riparte senza stacchi (più rewatch) | "LOOP IT / save + share" |

Il drop cade a metà (0:30) di proposito: chi guarda resta fino in fondo per sentirlo, e il loop spinge il tempo di visione medio oltre il 100%.

---

## 2. Titoli (A/B test)

**YouTube Shorts / YouTube (EN, portata globale):**
1. `Indian Classical Meets TECHNO 🔥 Sitar x Tabla x Dhol (wait for the drop)`
2. `What If Bollywood Went Cyberpunk? 🇮🇳⚡ Indian Techno Drop`
3. `NEON MANTRA – Indian Techno | Sitar + Dhol 128 BPM Rave`

**Versione italiana:**
- `Musica classica indiana + TECHNO = 🤯 (aspetta il drop)`

**TikTok / Reels (didascalia corta):**
> sitar + dhol + techno = ? 🤯 aspetta lo 0:30 🔥 #indiantechno #desitechno

---

## 3. Descrizione YouTube (le prime 2 righe sono le più importanti per la SEO)

```
NEON MANTRA 🔥 Indian classical meets hard techno: sitar, tabla and dhol over a 128 BPM rave groove, with neon dancers in a cyberpunk city. Wait for the drop at 0:30 ⚡

🎧 Indian Techno · Desi Techno · Bollywood Techno · Sitar Techno · Dhol Techno · Psytrance vibes
🎵 128 BPM · D Bhairav raag · Sitar x Tabla x Dhol x Techno bass

00:00 Sitar hook
00:07 Techno groove + Dhol bhangra chaal
00:22 Build-up
00:30 THE DROP
00:52 Loop

Comment your country 🌍 and tag someone who needs this in their gym playlist.

#IndianTechno #DesiTechno #Sitar #Dhol #Techno #Cyberpunk #Shorts
```

---

## 4. Hashtag

**TikTok/Reels (max 5–8, generici + di nicchia):**
`#indiantechno #desitechno #techno #sitar #dhol #bollywood #cyberpunk #fyp`

**YouTube Shorts (i primi 3 compaiono sopra il titolo):**
`#IndianTechno #DesiTechno #Shorts`

**Pool esteso da far ruotare:**
`#technomusic #rave #technoindia #bhangra #tabla #indianmusic #fusionmusic #psytrance #melodictechno #housemusic #edm #dancevideo #neon #synthwave #bharatanatyam #kathak #bollywoodremix #festivalvibes #gymmusic #workoutmusic`

---

## 5. Tag YouTube (incolla così com'è, < 500 caratteri)

```
indian techno, desi techno, sitar techno, dhol techno, indian classical techno, bollywood techno, tabla techno, indian edm, indian rave, cyberpunk india, techno 128 bpm, indian fusion music, bhangra techno, neon mantra, raag techno, psytrance india, indian dance music, techno drop, indian beat drop, gym techno
```

---

## 6. Copertina e thumbnail

- `out/cover_9x16.jpg` è il primo secondo dopo il drop: massima saturazione, scritta grande, danzatrici a metà giravolta.
- Per una thumbnail 16:9 su YouTube: ritaglia la parte centrale e aggiungi **"SITAR + TECHNO?!"** in giallo saffron (#FF8C0D) con contorno magenta.

---

## 7. Quando pubblicare e come spingere il video

- **Orari:** 18:00–21:00 ora locale. Per il pubblico indiano: 19:30–22:00 IST (16:00–18:30 CET). Per lo spillover USA: 21:00 CET.
- **Primi 60 minuti:** fissa un commento come *"0:30 🤯 what country are you listening from?"*. Le domande sul paese generano molti commenti.
- **Trend audio:** su TikTok e Instagram carica la traccia come **suono originale** chiamato *"NEON MANTRA – Indian Techno (wait for 0:30)"*, così altri creator possono riusarla. Il tempo dell'hook (0:00–0:07) e del drop (0:30) funziona bene per transizioni e cambi d'outfit.
- **Remix e duetti:** stitch e duetti di ballerini, DJ e suonatori di dhol estendono la portata.
- **Varianti da tagliare dallo stesso file:** 15 s (0:22–0:37, solo build e drop) e 30 s (0:15–0:45).
  ```
  ffmpeg -ss 22 -t 15 -i out/NEON_MANTRA_9x16.mp4 -c copy NEON_MANTRA_15s.mp4
  ```
- **Collegamento con i brani lunghi:** distribuisci la traccia (DistroKid, Spotify, Apple Music) con lo stesso titolo e linka lo Short alla versione lunga.

---

## 8. Rigenerare / modificare

```
pip install numpy scipy pillow     # + ffmpeg
python3 make_audio.py              # -> out/neon_mantra.wav + out/timeline.json
python3 make_video.py              # -> out/NEON_MANTRA_9x16.mp4 + out/cover_9x16.jpg
python3 make_video.py --preview 1,30.2,40   # solo fotogrammi PNG di prova
```

- Tempo, scala e struttura: costanti `BPM`, `SC`, `SECTIONS`, `HOOK` in `make_audio.py`.
- Colori, testi a schermo e passi di danza: `MAG/CYA/SAF`, `caption(...)`, `POSES` in `make_video.py`.
- Musica e immagini sono sintetizzate da zero (niente campioni né footage di terzi), quindi non ci sono problemi di copyright o Content ID.
