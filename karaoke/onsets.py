#!/usr/bin/env python3
"""
Syllable-onset detection by spectral flux, used to place word boundaries
on real attacks in the singing instead of spreading them evenly.

Sung phrases are not linear: a singer delivers the words of a line and
then holds the final syllable. Dividing a phrase's duration by character
count therefore pushes every word later than it is actually sung, and
the error grows with the length of the held note. Snapping each word
boundary to a nearby onset keeps the highlight on the voice.
"""
import numpy as np

SR = 16000
N_FFT = 512
HOP = 160  # 10 ms


def spectral_flux(audio):
    """Positive spectral change per frame -- peaks mark note attacks."""
    window = np.hanning(N_FFT).astype(np.float32)
    n_frames = 1 + (len(audio) - N_FFT) // HOP
    if n_frames < 3:
        return np.zeros(0, dtype=np.float32)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    mag = np.abs(np.fft.rfft(audio[idx] * window, axis=1))
    mag = np.log1p(mag)
    diff = np.diff(mag, axis=0)
    flux = np.maximum(diff, 0).sum(axis=1)
    # Subtract a local baseline so a loud sustained note does not read as
    # a continuous attack.
    kernel = np.ones(15, dtype=np.float32) / 15
    baseline = np.convolve(flux, kernel, mode="same")
    return np.maximum(flux - baseline, 0)


def onset_times(audio, offset=0.0):
    """Return (times, strengths) of onset peaks, times offset into the track."""
    flux = spectral_flux(audio)
    if len(flux) < 3:
        return np.zeros(0), np.zeros(0)
    peaks = []
    for i in range(1, len(flux) - 1):
        if flux[i] >= flux[i - 1] and flux[i] > flux[i + 1] and flux[i] > 0:
            peaks.append(i)
    if not peaks:
        return np.zeros(0), np.zeros(0)
    peaks = np.array(peaks)
    strengths = flux[peaks]
    times = offset + (peaks + 1) * HOP / SR
    return times, strengths


def place_boundaries(priors, times, strengths, window, min_gap=0.12):
    """Snap each prior word boundary to the strongest onset near it.

    Boundaries stay in order and keep a minimum gap, and a prior with no
    onset nearby is simply left where it was.
    """
    placed = []
    previous = None
    for prior in priors:
        lower = prior - window
        upper = prior + window
        if previous is not None:
            lower = max(lower, previous + min_gap)
        mask = (times >= lower) & (times <= upper)
        if mask.any():
            candidates = times[mask]
            weights = strengths[mask]
            # Prefer strong onsets, but do not chase a distant one.
            score = weights / (1.0 + np.abs(candidates - prior) / max(window, 1e-6))
            chosen = float(candidates[int(np.argmax(score))])
        else:
            chosen = prior
        if previous is not None and chosen < previous + min_gap:
            chosen = previous + min_gap
        placed.append(chosen)
        previous = chosen
    return placed
