"""NEON MANTRA - Indo-techno track synthesizer.

128 BPM, random length: BARS = rand in [64, 160] (2:00 - 5:00), arrangement
adapts to whatever length comes out. Scale: D Bhairav (D Eb F# G A Bb C).
Everything is synthesized from scratch with numpy/scipy:
  techno kick, rolling bass, hats, clap, supersaw lead, riser,
  Karplus-Strong sitar (with jawari buzz), tabla (na / tin / ghe / ka),
  dhol (dagga + tilli) in bhangra chaal, tanpura drone,
  keys: FM electric piano (Rhodes-style), synths: 303-style acid line,
  pulse arpeggiator, supersaw pad.
Electronic effects: clipping (hard clip on acid + drum bus), wah-wah
(LFO-swept resonant band-pass), phaser (6-stage swept all-pass),
echo (tempo-synced, ping-pong, darkening repeats).

Usage: python3 make_audio.py [--seed N] [--bars N]
Writes out/neon_mantra.wav, out/timeline.json (beat/section map used by the
video) and out/chapters.txt (YouTube chapters).
"""
import argparse
import json
import os

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, lfilter, sosfilt

ap = argparse.ArgumentParser()
ap.add_argument("--seed", type=int, help="random seed (default: new random track every run)")
ap.add_argument("--bars", type=int, help="force the length in bars (default: rand in [64, 160])")
args = ap.parse_args()

SR = 44100
BPM = 128
BEAT = 60 / BPM
BAR = 4 * BEAT
STEP = BEAT / 4
SEED = args.seed if args.seed is not None else int(np.random.SeedSequence().entropy % 2**31)
rng = np.random.default_rng(SEED)
BARS = args.bars or int(rng.integers(64, 161))  # rand in [64, 160]
LEN = int(round(BARS * BAR * SR))

OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)


def build_sections(bars):
    """Hook first (grab in the first second), then groove/build/drop/break
    cycles; the last cycle stretches to fit, and the outro returns to the hook
    so the track still loops seamlessly."""
    secs = [("hook", 0, 4)]
    pos, end = 4, bars - 4
    cycle = [("groove", 8), ("build", 4), ("drop", 12), ("break", 8)]
    while end - pos >= 32 + 8:
        for name, ln in cycle:
            secs.append((name, pos, pos + ln))
            pos += ln
    rest = end - pos  # 8..39 bars left: finish with groove? + build + big final drop
    if rest >= 24:
        secs.append(("groove", pos, pos + rest - 20))
        pos += rest - 20
    elif len(secs) > 1:  # too little room: shorten the last break so the final drop is >= 8 bars
        total = 8 + rest
        brk = 4 if total < 24 else 8
        name, a, _ = secs.pop()
        secs.append((name, a, a + brk))
        pos = a + brk
    secs.append(("build", pos, pos + 4))
    secs.append(("drop", pos + 4, end))
    secs.append(("outro", end, bars))
    return secs


SECTIONS = build_sections(BARS)
SEC_OF_BAR = []
for _i, (_n, _a, _b) in enumerate(SECTIONS):
    for _bar in range(_a, _b):
        SEC_OF_BAR.append((_n, _a, _b, sum(1 for m, *_ in SECTIONS[:_i] if m == _n)))


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


# D Bhairav degrees as semitone offsets from D
D4 = 62
SC = {"S": 0, "r": 1, "G": 4, "M": 5, "P": 7, "d": 8, "n": 10,
      "S'": 12, "r'": 13, "G'": 16, "M'": 17, "P'": 19}


def lp(x, fc, order=2):
    return sosfilt(butter(order, min(fc, SR / 2 - 100) / (SR / 2), "low", output="sos"), x)


def hp(x, fc, order=2):
    return sosfilt(butter(order, fc / (SR / 2), "high", output="sos"), x)


def bp(x, lo, hi, order=2):
    return sosfilt(butter(order, [lo / (SR / 2), hi / (SR / 2)], "band", output="sos"), x)


def place(buf, sig, t, gain=1.0):
    i = int(round(t * SR))
    if i >= len(buf):
        return
    n = min(len(sig), len(buf) - i)
    buf[i:i + n] += sig[:n] * gain


def movavg(x, w):
    """Centered moving average in O(n) (np.convolve is O(n*w) on these long windows)."""
    c = np.concatenate([[0.0], np.cumsum(np.pad(x, (w // 2, w - 1 - w // 2), mode="edge"))])
    return (c[w:] - c[:-w]) / w


def env_exp(n, tau):
    return np.exp(-np.arange(n) / (tau * SR))


# -------------------------------------------------------------------- effects
def hardclip(x, drive, ceil=1.0):
    """Clipping: drive into a hard ceiling (square-ish, aggressive harmonics)."""
    return np.clip(x * drive, -ceil, ceil) / ceil


def _rbj(kind, fc, q):
    w = 2 * np.pi * min(fc, SR * 0.45) / SR
    al, cw = np.sin(w) / (2 * q), np.cos(w)
    if kind == "bp":
        b = [al, 0, -al]
    else:  # resonant low-pass
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
    a = [1 + al, -2 * cw, 1 - al]
    return np.array(b) / a[0], np.array(a) / a[0]


def lfo_sweep(n, period_s, fmin, fmax, phase=0.0):
    t = np.arange(n) / SR
    return fmin * (fmax / fmin) ** (0.5 - 0.5 * np.cos(2 * np.pi * t / period_s + phase))


def wah(x, period_s, fmin=350, fmax=2800, q=5.0, block=128):
    """Wah-wah: resonant band-pass whose centre is swept by a tempo-synced LFO."""
    fc = lfo_sweep(len(x), period_s, fmin, fmax)
    y = np.empty_like(x)
    zi = np.zeros(2)
    for i in range(0, len(x), block):
        b, a = _rbj("bp", fc[i], q)
        y[i:i + block], zi = lfilter(b, a, x[i:i + block], zi=zi)
    return 0.35 * x + 2.2 * y


def phaser(x, period_s, fmin=250, fmax=3500, stages=6, mix=0.7, block=64):
    """Phaser: chain of first-order all-passes with LFO-swept corner, summed with dry."""
    fc = lfo_sweep(len(x), period_s, fmin, fmax)
    y = x.copy()
    zis = [np.zeros(1) for _ in range(stages)]
    out = np.empty_like(x)
    for i in range(0, len(x), block):
        tn = np.tan(np.pi * fc[i] / SR)
        c = (tn - 1) / (tn + 1)
        seg = x[i:i + block]
        for k in range(stages):
            seg, zis[k] = lfilter([c, 1.0], [1.0, c], seg, zi=zis[k])
        out[i:i + block] = seg
    return (x + mix * out) / (1 + mix)


def echo(x, delay_s, fb=0.45, taps=6, fc=3500, pingpong=True):
    """Echo: tempo-synced repeats that darken each time; ping-pong L/R. Returns wet (L, R)."""
    D = int(delay_s * SR)
    L, R = np.zeros_like(x), np.zeros_like(x)
    tap = x
    for k in range(1, taps + 1):
        if k * D >= len(x):
            break
        tap = lp(tap, fc) * fb
        sh = np.zeros_like(x)
        sh[k * D:] = tap[:len(x) - k * D]
        if pingpong:
            (L if k % 2 else R)[:] += sh
        else:
            L += sh
            R += sh
    return L, R


# ---------------------------------------------------------------- instruments
def kick():
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t / 0.035)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / 0.22)
    click = hp(rng.standard_normal(n), 3000) * np.exp(-t / 0.004) * 0.35
    return np.tanh(1.8 * (body + click))


def hat(open_=False):
    n = int((0.25 if open_ else 0.06) * SR)
    x = hp(rng.standard_normal(n), 8000, 4)
    return x * env_exp(n, 0.07 if open_ else 0.012) * 0.5


def clap():
    n = int(0.3 * SR)
    x = bp(rng.standard_normal(n), 900, 4000)
    e = np.zeros(n)
    for k, d in enumerate([0, 0.011, 0.022]):
        i = int(d * SR)
        e[i:] += env_exp(n - i, 0.006 if k < 2 else 0.09)
    return x * e * 0.8


def snare():
    n = int(0.18 * SR)
    t = np.arange(n) / SR
    tone = np.sin(2 * np.pi * 190 * t) * np.exp(-t / 0.03)
    noise = bp(rng.standard_normal(n), 1500, 9000) * np.exp(-t / 0.05)
    return 0.5 * tone + 0.7 * noise


def bass_note(f, dur, cutoff=900):
    n = int(dur * SR)
    t = np.arange(n) / SR
    saw = 2 * ((f * t) % 1) - 1
    sub = np.sin(2 * np.pi * f / 2 * t)
    e = np.exp(-t / 0.05)  # filter-envelope "pluck": crossfade bright -> dark
    x = (lp(saw, cutoff) * e + lp(saw, cutoff * 0.4) * (1 - e)) * 0.7 + sub * 0.6
    a = np.minimum(1, t / 0.004)
    return np.tanh(1.5 * x) * a * np.exp(-t / (dur * 0.7))


def saws(f, n, voices=(-0.19, -0.11, -0.04, 0, 0.04, 0.11, 0.19)):
    t = np.arange(n) / SR
    x = np.zeros(n)
    for d in voices:
        ff = f * 2 ** (d / 12)
        x += 2 * (((ff * t) + rng.random()) % 1) - 1
    return x / len(voices)


def supersaw(f, dur, bright=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = lp(saws(f, n), 1500 + 6000 * bright, 2)
    a = np.minimum(1, t / 0.005) * np.minimum(1, (dur - t) / 0.02)
    return x * np.clip(a, 0, 1)


def pad(freqs, dur):
    """Supersaw pad: slow attack/release chord (gets the phaser later)."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = sum(saws(f, n, (-0.12, 0, 0.12)) for f in freqs) / len(freqs)
    x = lp(x, 2200)
    return x * np.clip(np.minimum(t / 0.4, (dur - t) / 0.5), 0, 1)


def epiano(freqs, dur, vel=1.0):
    """FM electric piano (Rhodes-style): sine carrier + decaying FM bark + tine ping + tremolo."""
    n = int((dur + 0.6) * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for f in freqs:
        idx = (1.2 + 2.5 * vel) * np.exp(-t / 0.35)
        x += np.sin(2 * np.pi * f * t + idx * np.sin(2 * np.pi * f * t))
        x += 0.08 * vel * np.sin(2 * np.pi * f * 14 * t) * np.exp(-t / 0.02)
    x /= len(freqs)
    rel = np.clip((dur + 0.6 - t) / 0.6, 0, 1)
    return x * np.minimum(1, t / 0.002) * np.exp(-t / 1.6) * rel * (1 + 0.15 * np.sin(2 * np.pi * 5 * t))


def acid(f, dur, cutoff, accent, q=7.0):
    """303-style acid synth: saw -> resonant low-pass with envelope -> hard clipping."""
    n = int((dur + 0.05) * SR)
    t = np.arange(n) / SR
    saw = 2 * ((f * t) % 1) - 1
    e = np.exp(-t / (0.09 + 0.08 * accent))
    fc = cutoff * (1 + accent)
    bright = lfilter(*_rbj("lp", min(fc * 4, 12000), q), saw)
    dark = lfilter(*_rbj("lp", fc, q), saw)
    x = bright * e + dark * (1 - e)
    amp = np.minimum(1, t / 0.003) * np.clip((dur + 0.05 - t) / 0.03, 0, 1) * (0.7 + 0.5 * accent)
    return hardclip(x * amp, 2.5 + 3 * accent, 0.9) * 0.6


def arp_note(f, dur):
    """Pulse-wave pluck for the arpeggiator."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.where((f * t) % 1 < 0.25, 1.0, -0.33)
    return lp(x, 4500) * np.exp(-t / 0.09) * np.minimum(1, t / 0.002)


def sitar(f, dur, bend=0.0):
    """Karplus-Strong string + jawari buzz + sympathetic shimmer (+ optional meend)."""
    n = int(dur * SR)
    N = max(2, int(round(SR / f)))
    out = np.zeros(n + N + 1)
    out[:N + 1] = rng.uniform(-1, 1, N + 1) * np.hanning(N + 1) ** 0.3
    damp = 0.4985
    i = N + 1
    while i < len(out):
        j = min(i + N, len(out))
        out[i:j] = damp * (out[i - N:j - N] + out[i - N - 1:j - N - 1])
        i = j
    x = out[N + 1:N + 1 + n]
    if bend:  # meend: glide up by `bend` semitones over the note via resampling
        rate = 2 ** (np.linspace(0, bend, n) / 12)
        pos = np.cumsum(rate)
        pos = pos[pos < n - 1]
        x = np.interp(pos, np.arange(n), x)
        x = np.pad(x, (0, n - len(x)))
    buzz = np.tanh(6 * x) - np.tanh(x)  # jawari: bright nonlinear bridge buzz
    x = x + 0.55 * buzz
    x = x + 0.25 * bp(x, 2500, 7000)
    t = np.arange(n) / SR
    return x * np.exp(-t / 1.2) * 0.6


def tabla(kind):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    if kind == "na":  # dayan rim, bright ringing with harmonic overtones
        f0 = 520
        x = sum(np.sin(2 * np.pi * f0 * h * t) / h * np.exp(-t / (0.12 / h)) for h in (1, 2, 3, 4, 5))
        x += bp(rng.standard_normal(n), 2000, 8000) * np.exp(-t / 0.004) * 0.5
        return x * 0.5
    if kind == "tin":
        f0 = 520
        x = sum(np.sin(2 * np.pi * f0 * h * t) / h * np.exp(-t / (0.3 / h)) for h in (1, 2, 3))
        return x * 0.35
    if kind == "ghe":  # bayan with upward pitch glide (the iconic "wooo")
        f = 85 + 70 * (1 - np.exp(-t / 0.12))
        x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.28)
        return x * 0.9
    if kind == "ka":
        return bp(rng.standard_normal(n), 300, 2500) * np.exp(-t / 0.015) * 0.6
    raise ValueError(kind)


def dhol(kind):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    if kind == "dagga":  # bass side: big boom
        f = 70 + 60 * np.exp(-t / 0.03)
        body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.18)
        slap = bp(rng.standard_normal(n), 200, 1200) * np.exp(-t / 0.02) * 0.5
        return np.tanh(1.6 * (body + slap))
    # tilli: high stick side, sharp crack
    x = bp(rng.standard_normal(n), 900, 6000) * np.exp(-t / 0.03)
    x += np.sin(2 * np.pi * 330 * t) * np.exp(-t / 0.05) * 0.5
    return x * 0.6


def tanpura(f, dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for h in range(1, 14):
        x += np.sin(2 * np.pi * f * h * t + rng.random() * 6) / h ** 1.1 * (1 + 0.3 * np.sin(2 * np.pi * 0.3 * h * t))
    return x * np.minimum(1, t / 0.3) * np.exp(-t / (dur * 0.8)) * 0.08


# ------------------------------------------------------------------- patterns
# Hook: 2-bar sitar phrase, 16th grid (None = rest). (degree, length_in_steps, meend)
HOOK = [
    [("S'", 2, 0), None, ("r'", 1, 0), ("S'", 1, 0), ("n", 2, 0), None, ("d", 2, 0), None, ("P", 3, 0), None, None, ("d", 1, 0), ("P", 2, 0), None, ("G", 2, 1), None],
    [("M", 2, 0), None, ("G", 1, 0), ("r", 1, 0), ("S", 4, 0), None, None, None, ("S", 1, 0), ("r", 1, 0), ("G", 1, 0), ("M", 1, 0), ("P", 2, 0), None, ("d", 2, -1), None],
]
BASS_ROOTS = ["S", "S", "d", "n"]  # per bar, D - D - Bb - C
# chords over the bass: D | Eb/D (the Bhairav b2 colour) | Bb aug | Cm
CHORDS = [["S", "G", "P"], ["r", "M", "d"], ["d", "S'", "G'"], ["n", "r'", "M'"]]
DHOL_CHAAL = {0: "dagga", 3: "tilli", 6: "dagga", 8: "dagga", 11: "tilli", 14: "dagga"}  # bhangra
TABLA = {0: "ghe", 2: "na", 3: "na", 4: "tin", 6: "na", 7: "ka", 8: "ghe", 10: "na", 11: "ghe", 12: "tin", 14: "na", 15: "ka"}
ARP = [0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 0, 2, 4, 5, 3, 1]  # index into 2-octave chord tones


def acid_pattern():
    """Random (seeded) 16-step acid line in D Bhairav: (degree, octave, accent) or None."""
    degs = ["S", "S", "r", "G", "P", "d", "n"]
    pat = []
    for s in range(16):
        if s == 0 or rng.random() < 0.7:
            pat.append((degs[rng.integers(len(degs))], int(rng.random() < 0.25), float(rng.random() < 0.3)))
        else:
            pat.append(None)
    return pat


ACID = [acid_pattern(), acid_pattern()]

STEMS = ("kick", "bass", "drums", "sitar", "lead", "indian", "drone", "fx", "keys", "pad", "acid", "arp")
tracks = {k: np.zeros(LEN) for k in STEMS}
events = []  # for the video sync
KICK, CLAP, SNARE = kick(), clap(), snare()

for bar in range(BARS):
    sec, sa, sb, occ = SEC_OF_BAR[bar]
    bi = bar - sa  # bar index inside the section
    t0 = bar * BAR
    root = D4 - 24 + SC[BASS_ROOTS[bar % 4]]
    chord = CHORDS[bar % 4]
    for s in range(16):
        t = t0 + s * STEP
        beat_step = s % 4 == 0
        # kick
        if beat_step and sec not in ("hook", "break"):
            if not (sec == "build" and bi == sb - sa - 1 and s >= 8):
                place(tracks["kick"], KICK, t)
                events.append({"t": t, "type": "kick"})
        if sec == "hook" and s in (0, 8) and bar >= 2:
            place(tracks["kick"], lp(KICK, 400), t, 0.7)
            events.append({"t": t, "type": "kick_soft"})
        # bass
        if sec in ("groove", "drop") and not beat_step:
            place(tracks["bass"], bass_note(midi(root), STEP * 0.9, 1400 if sec == "drop" else 800), t, 0.55)
        # hats / clap
        if sec in ("groove", "drop", "outro"):
            if s % 4 == 2:
                place(tracks["drums"], hat(True), t, 0.5)
            elif sec == "drop" or s % 2 == 1:
                place(tracks["drums"], hat(), t, 0.35 + 0.15 * (s % 2))
        if sec in ("groove", "drop") and s in (4, 12):
            place(tracks["drums"], CLAP, t, 0.6)
            events.append({"t": t, "type": "clap"})
        # dhol chaal
        if sec in ("groove", "drop", "build") and s in DHOL_CHAAL:
            k = DHOL_CHAAL[s]
            place(tracks["indian"], dhol(k), t, 0.7 if k == "dagga" else 0.55)
            events.append({"t": t, "type": "dhol"})
        # tabla (softer in the break)
        if sec in ("hook", "groove", "outro", "build", "break") and s in TABLA:
            place(tracks["indian"], tabla(TABLA[s]), t, 0.35 if sec == "break" else 0.6)
            events.append({"t": t, "type": "tabla"})
        # sitar hook
        if sec != "build" or bi < 2:
            note = HOOK[bar % 2][s]
            if note:
                deg, ln, bend = note
                f = midi(D4 + SC[deg])
                place(tracks["sitar"], sitar(f, ln * STEP + 0.6, bend), t, 1.0)
                events.append({"t": t, "type": "sitar", "deg": deg})
                if sec == "drop":
                    place(tracks["lead"], supersaw(midi(D4 + SC[deg]), ln * STEP * 0.95, 1.0), t, 0.32)
                    place(tracks["lead"], supersaw(midi(D4 + 12 + SC[deg]), ln * STEP * 0.95, 0.6), t, 0.12)
        # keys: off-beat e-piano stabs in groove / drop / outro
        if sec in ("groove", "drop", "outro") and s in (2, 6, 10, 14):
            place(tracks["keys"], epiano([midi(D4 + SC[d]) for d in chord], STEP * 1.5, 0.9), t, 1.0)
            events.append({"t": t, "type": "keys"})
        # acid synth: from the 2nd groove on, in every drop, and rising through the break
        acid_on = (sec == "drop") or (sec == "groove" and occ >= 1) or (sec == "break" and bi >= (sb - sa) // 2)
        if acid_on:
            n = ACID[bar % 2][s]
            if n:
                deg, octv, acc = n
                cyc = (bar % 8 + s / 16) / 8  # 8-bar cutoff sweep
                cut = 250 + 2200 * (0.5 - 0.5 * np.cos(2 * np.pi * cyc))
                if sec == "break":
                    cut *= 0.5
                place(tracks["acid"], acid(midi(D4 - 24 + SC[deg] + 12 * octv), STEP * 0.8, cut, acc), t)
                events.append({"t": t, "type": "acid"})
        # arpeggiator: drop, build (rising) and 2nd half of grooves
        if sec in ("drop", "build") or (sec == "groove" and bi >= 4):
            tones = [midi(D4 + 12 + SC[d] + 12 * o) for o in (0, 1) for d in chord]
            g = 0.4 + 0.6 * (bi + s / 16) / max(1, sb - sa) if sec == "build" else 1.0
            place(tracks["arp"], arp_note(tones[ARP[s]], STEP * 0.9), t, g)
    # pad + sustained keys: whole-bar chords in break, drop and outro
    if sec in ("break", "drop", "outro"):
        place(tracks["pad"], pad([midi(D4 - 12 + SC[d]) for d in chord], BAR), t0)
    if sec in ("break", "hook") and bar >= 2:
        place(tracks["keys"], epiano([midi(D4 + SC[d]) for d in chord], BAR * 0.9, 0.6), t0, 1.2)
        events.append({"t": t0, "type": "keys"})
    # snare roll in the build (accelerating over its 4 bars)
    if sec == "build":
        div = [4, 8, 16, 32][min(bi, 3)]
        for k in range(div):
            g = 0.15 + 0.5 * (bi * div + k) / (4 * 32)
            place(tracks["drums"], SNARE, t0 + k * BAR / div, g)
            events.append({"t": t0 + k * BAR / div, "type": "snare"})
    # tanpura drone pluck pattern (Pa Sa Sa Sa) each bar
    for k, deg in enumerate(["P", "S'", "S'", "S"]):
        place(tracks["drone"], tanpura(midi(D4 - 12 + SC[deg]), BAR * 0.9), t0 + k * BEAT, 1.0)

# riser into every drop + impact on every drop
for name, a, b in SECTIONS:
    if name == "build":
        r0, r1 = a * BAR, b * BAR
        n = int((r1 - r0) * SR)
        tt = np.linspace(0, 1, n)
        noise = rng.standard_normal(n)
        riser = np.zeros(n)
        for i in range(0, n, 2048):  # sweeping band-pass noise
            fc = 400 + 9000 * tt[i] ** 2
            seg = noise[max(0, i - 4096):i + 2048]
            riser[i:i + 2048] = bp(seg, fc * 0.7, min(fc * 1.4, 20000))[-len(riser[i:i + 2048]):]
        riser *= tt ** 2 * 0.5
        sweep = np.sin(2 * np.pi * np.cumsum(200 * 2 ** (tt * 4)) / SR) * tt ** 3 * 0.15
        place(tracks["fx"], riser + sweep, r0)
    if name == "drop":
        imp_n = int(2.5 * SR)
        it = np.arange(imp_n) / SR
        impact = np.sin(2 * np.pi * np.cumsum(60 * np.exp(-it / 0.5) + 30) / SR) * np.exp(-it / 0.8)
        impact += lp(rng.standard_normal(imp_n), 3000) * np.exp(-it / 0.6) * 0.4
        place(tracks["fx"], impact, a * BAR, 0.9)
        events.append({"t": a * BAR, "type": "impact"})

# filter the hook section drone for the "muffled -> open" effect
hook_end = int(4 * BAR * SR)
tracks["drone"][:hook_end] = lp(tracks["drone"][:hook_end], 1200)


def sec_mask(names, fade=0.25):
    """Per-sample 0..1 mask that is 1 inside the named sections (smoothed)."""
    m = np.zeros(LEN)
    for name, a, b in SECTIONS:
        if name in names:
            m[int(a * BAR * SR):int(b * BAR * SR)] = 1
    return movavg(m, int(fade * SR))


# ---------------------------------------------------------------- effects bus
print("effects: wah / phaser / echo / clipping ...", flush=True)
M_BREAK, M_DROP, M_GROOVE = sec_mask({"break"}), sec_mask({"drop"}), sec_mask({"groove", "outro"})
# wah-wah: sitar in break + drop (1-bar sweep), e-piano funk-wah in groove (half-bar sweep)
sit_wah = wah(tracks["sitar"], BAR)
m = np.clip(M_BREAK + M_DROP, 0, 1)
tracks["sitar"] = tracks["sitar"] * (1 - m) + sit_wah * m
keys_wah = wah(tracks["keys"], BAR / 2, 500, 3500, 4.0)
keys_ph = phaser(tracks["keys"], 2 * BAR)
tracks["keys"] = tracks["keys"] * (1 - M_GROOVE - M_BREAK) + keys_wah * M_GROOVE + keys_ph * M_BREAK
# phaser: pad always, hats in the drop
tracks["pad"] = phaser(tracks["pad"], 4 * BAR, 200, 3000, 6, 0.9)
tracks["drums"] = tracks["drums"] * (1 - M_DROP) + phaser(tracks["drums"], 2 * BAR, 1000, 9000, 4) * M_DROP
# clipping: drum bus (mild) - the acid line is already hard-clipped per note
tracks["drums"] = hardclip(tracks["drums"] / (np.max(np.abs(tracks["drums"])) + 1e-9), 1.6, 0.9)

# normalise every stem to a target RMS (measured where it plays) -> balanced mix
TARGET = {"kick": 0.30, "bass": 0.13, "drums": 0.07, "sitar": 0.11, "lead": 0.08, "indian": 0.12,
          "drone": 0.03, "fx": 0.07, "keys": 0.06, "pad": 0.045, "acid": 0.075, "arp": 0.04}
for k in STEMS:
    x = tracks[k]
    blk = np.sqrt(np.mean(x[:len(x) // 2205 * 2205].reshape(-1, 2205) ** 2, 1))
    act = blk[blk > blk.max() * 0.1] if blk.max() > 0 else blk
    if act.size and act.mean() > 0:
        tracks[k] = x * TARGET[k] / act.mean()

# echo sends (dotted-eighth on sitar + acid, ping-pong 3/16 on arp, quarter on keys in the break)
eL, eR = np.zeros(LEN), np.zeros(LEN)
for src, d, amt, fb in (
    (tracks["sitar"] * (0.25 + 0.45 * M_BREAK), 0.75 * BEAT, 1.0, 0.45),
    (tracks["arp"], 0.75 * BEAT, 0.8, 0.5),
    (tracks["acid"] * (0.15 + 0.5 * M_BREAK), 0.75 * BEAT, 1.0, 0.4),
    (tracks["keys"] * M_BREAK, BEAT, 0.6, 0.5),
):
    l, r = echo(src, d, fb)
    eL += l * amt
    eR += r * amt

# sidechain pump keyed from the kick
key = np.abs(tracks["kick"])
win = int(0.01 * SR)
key = movavg(key, win)
duck = 1 - 0.65 * np.clip(key / (key.max() + 1e-9) * 2.5, 0, 1)
duck = movavg(duck, win * 3)

ducked = tracks["bass"] + tracks["lead"] + tracks["drone"] + tracks["fx"] * 0.8 + tracks["pad"] + tracks["acid"] + tracks["arp"]
mix = (
    tracks["kick"]
    + ducked * duck
    + tracks["sitar"] * (0.55 + 0.45 * duck)
    + tracks["keys"] * (0.6 + 0.4 * duck)
    + tracks["indian"]
    + tracks["drums"]
)

# stereo: Haas widening on lead/sitar/drone/pad, hats slightly right, echoes ping-pong
wide = (tracks["lead"] + tracks["drone"] + tracks["pad"]) * duck + tracks["sitar"] * 0.3
d = int(0.012 * SR)
L = mix + 0.25 * wide + eL * duck
R = mix + 0.25 * np.concatenate([np.zeros(d), wide[:-d]]) + 0.1 * tracks["drums"] + eR * duck
st = np.stack([L, R], 1)
st = hp(st.T, 28).T
st = np.tanh(1.3 * st / np.max(np.abs(st)) * 1.6) / np.tanh(1.3 * 1.6)  # glue + loudness
st = hardclip(st, 1.08, 0.97)  # final clipper (adds loudness, catches peaks)
st *= 0.89 / np.max(np.abs(st))
# tiny fades so the loop is click-free
f = int(0.005 * SR)
st[:f] *= np.linspace(0, 1, f)[:, None]
st[-f:] *= np.linspace(1, 0, f)[:, None]
wavfile.write(os.path.join(OUT, "neon_mantra.wav"), SR, (st * 32767).astype(np.int16))

# per-video-frame energy (30 fps) for audio-reactive visuals
FPS = 30
hop = SR // FPS
mono = st.mean(1)
nfr = LEN // hop
rms = np.sqrt(np.mean(mono[:nfr * hop].reshape(nfr, hop) ** 2, 1))
lowb = lp(mono, 150)
low = np.sqrt(np.mean(lowb[:nfr * hop].reshape(nfr, hop) ** 2, 1))
json.dump({
    "bpm": BPM, "bars": BARS, "seed": SEED, "duration": LEN / SR, "fps": FPS,
    "sections": [{"name": s, "start": a * BAR, "end": b * BAR} for s, a, b in SECTIONS],
    "events": sorted(events, key=lambda e: e["t"]),
    "rms": (rms / rms.max()).round(4).tolist(),
    "low": (low / low.max()).round(4).tolist(),
}, open(os.path.join(OUT, "timeline.json"), "w"))

LABEL = {"hook": "Sitar hook", "groove": "Techno groove + dhol + keys", "build": "Build-up",
         "drop": "THE DROP", "break": "Breakdown: keys, phaser, echo", "outro": "Hook / loop"}
with open(os.path.join(OUT, "chapters.txt"), "w") as fh:
    for s, a, b in SECTIONS:
        sec_t = int(a * BAR)
        fh.write(f"{sec_t // 60:02d}:{sec_t % 60:02d} {LABEL[s]}\n")
print(f"seed={SEED} bars={BARS} duration={LEN / SR:.1f}s sections={[s for s, *_ in SECTIONS]}")
