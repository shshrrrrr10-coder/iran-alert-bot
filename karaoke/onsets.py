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


def onset_times(audio, offset=0.0, local_max_ms=60, mean_ms=300,
                delta_factor=0.55, min_gap_ms=130):
    """Return (times, strengths) of syllable attacks.

    Naive three-point peak picking on spectral flux finds a peak every
    few frames -- around 21 per second on sung Hebrew, against the 3-6
    syllables per second actually being sung. Snapping word boundaries
    to that is snapping to noise. So a peak must additionally dominate
    its neighbourhood, stand above a local adaptive threshold, and keep
    a minimum distance from the previous onset.
    """
    flux = spectral_flux(audio)
    if len(flux) < 5:
        return np.zeros(0), np.zeros(0)

    frame_ms = HOP / SR * 1000.0
    half = max(1, int(local_max_ms / frame_ms / 2))
    mean_half = max(2, int(mean_ms / frame_ms / 2))
    min_gap = max(1, int(min_gap_ms / frame_ms))

    # Local mean as an adaptive floor, so a quiet passage still yields
    # onsets and a loud one does not yield a forest of them.
    kernel = np.ones(2 * mean_half + 1, dtype=np.float32) / (2 * mean_half + 1)
    local_mean = np.convolve(flux, kernel, mode="same")
    threshold = local_mean + delta_factor * flux.std()

    times, strengths, last = [], [], -min_gap
    for i in range(half, len(flux) - half):
        value = flux[i]
        if value < threshold[i]:
            continue
        if value < flux[i - half:i + half + 1].max():
            continue
        if i - last < min_gap:
            # Keep whichever of the two is stronger.
            if strengths and value > strengths[-1]:
                times[-1] = offset + (i + 1) * HOP / SR
                strengths[-1] = value
                last = i
            continue
        times.append(offset + (i + 1) * HOP / SR)
        strengths.append(value)
        last = i
    return np.array(times), np.array(strengths)


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
