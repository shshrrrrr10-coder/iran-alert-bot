#!/usr/bin/env python3
"""
Find repeated sections (chorus/refrain) in a vocal track by acoustic
self-similarity -- no speech-recognition model required.

A repeated chorus is sung with the same words and melody each time, so its
spectral fingerprint repeats too. This computes a log-mel self-similarity
matrix over the track, emphasises diagonal stripes (a stripe at lag T means
"what happens here happened T seconds ago"), and reports which time ranges
repeat and at what spacing.

Usage:
    python3 analyze_structure.py --vocal lead_vocal.mp3 --start 0 --end 320
"""
import argparse
import subprocess
import sys

import numpy as np

SR = 16000
N_FFT = 512
HOP = 160          # 10 ms
N_MELS = 40
FRAME_POOL = 25    # pool to 0.25 s frames


def decode_audio(path, start, end):
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def mel_filterbank(n_mels, n_fft, sr):
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    mels = np.linspace(hz_to_mel(50), hz_to_mel(sr / 2), n_mels + 2)
    freqs = mel_to_hz(mels)
    bins = np.floor((n_fft + 1) * freqs / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        left, center, right = bins[i], bins[i + 1], bins[i + 2]
        if center == left:
            center = left + 1
        if right == center:
            right = center + 1
        right = min(right, fb.shape[1] - 1)
        if center >= fb.shape[1]:
            break
        fb[i, left:center] = np.linspace(0, 1, center - left, endpoint=False)
        fb[i, center:right] = np.linspace(1, 0, right - center, endpoint=False)
    return fb


def log_mel(audio):
    window = np.hanning(N_FFT).astype(np.float32)
    n_frames = 1 + (len(audio) - N_FFT) // HOP
    if n_frames < 1:
        sys.exit("Audio window too short to analyse.")
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = audio[idx] * window
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    fb = mel_filterbank(N_MELS, N_FFT, SR)
    mel = spec @ fb.T
    return np.log(mel + 1e-8).astype(np.float32)


def pool(features, factor):
    n = (len(features) // factor) * factor
    return features[:n].reshape(-1, factor, features.shape[1]).mean(axis=1)


def normalise(features):
    f = features - features.mean(axis=0, keepdims=True)
    f = f / (f.std(axis=0, keepdims=True) + 1e-8)
    f = f / (np.linalg.norm(f, axis=1, keepdims=True) + 1e-8)
    return f


def voiced_mask(pooled_logmel, quantile=0.45):
    """Frames carrying actual singing. Silence matches silence almost
    perfectly, which would otherwise dominate every similarity score."""
    energy = pooled_logmel.mean(axis=1)
    return energy >= np.quantile(energy, quantile)


def diagonal_smooth(ssm, width):
    """Average the SSM along diagonals so that repeated *sequences*
    (not single instants) light up."""
    out = np.zeros_like(ssm)
    kernel = np.ones(width, dtype=np.float32) / width
    n = ssm.shape[0]
    for lag in range(-(n - 1), n):
        diag = np.diagonal(ssm, offset=lag)
        if len(diag) < width:
            continue
        smoothed = np.convolve(diag, kernel, mode="same")
        rows = np.arange(max(0, -lag), max(0, -lag) + len(diag))
        cols = rows + lag
        out[rows, cols] = smoothed
    return out


def fingerprint(pooled, frame_s, start_t, end_t, n_slots=16):
    """Time-normalised fingerprint of one sung phrase, so phrases of
    slightly different length still compare meaningfully."""
    a = int(round(start_t / frame_s))
    b = int(round(end_t / frame_s))
    b = min(max(b, a + 1), len(pooled))
    block = pooled[a:b]
    if len(block) < n_slots:
        idx = np.linspace(0, len(block) - 1, n_slots)
        block = block[np.round(idx).astype(int)]
    else:
        edges = np.linspace(0, len(block), n_slots + 1).astype(int)
        block = np.stack([block[edges[i]:max(edges[i] + 1, edges[i + 1])].mean(axis=0)
                          for i in range(n_slots)])
    v = block.reshape(-1)
    v = v - v.mean()
    return v / (np.linalg.norm(v) + 1e-8)


def cluster_segments(pooled, frame_s, segments, threshold):
    prints = [fingerprint(pooled, frame_s, s, e) for s, e in segments]
    sim = np.array(prints) @ np.array(prints).T
    groups = []
    assigned = [-1] * len(segments)
    for i in range(len(segments)):
        if assigned[i] != -1:
            continue
        group = [i]
        assigned[i] = len(groups)
        for j in range(i + 1, len(segments)):
            if assigned[j] == -1 and sim[i, j] >= threshold:
                assigned[j] = len(groups)
                group.append(j)
        groups.append(group)
    return groups, sim


def report_segment_repeats(args, pooled, frame_s):
    from auto_segment import detect_silence, ffprobe_duration, voice_segments

    duration = ffprobe_duration(args.vocal)
    end = args.end if args.end is not None else duration
    silences = detect_silence(args.vocal, -30.0, 0.25)
    segs = [(s, e) for s, e in voice_segments(silences, duration, 0.2)
            if s >= args.start and e <= end]
    rel = [(s - args.start, e - args.start) for s, e in segs]

    groups, _ = cluster_segments(pooled, frame_s, rel, args.similarity)
    repeated = [g for g in groups if len(g) > 1]
    repeated.sort(key=lambda g: -len(g))

    print(f"\n{len(segs)} sung phrases in window; "
          f"{len(repeated)} of them recur elsewhere in the song:")
    for g in repeated:
        times = ", ".join(f"{segs[i][0]:.1f}s" for i in g)
        lengths = np.mean([segs[i][1] - segs[i][0] for i in g])
        print(f"  x{len(g)}  (~{lengths:.1f}s each)  at {times}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vocal", required=True)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float, default=None)
    parser.add_argument("--min-lag", type=float, default=8.0, help="Ignore repeats closer together than this (seconds)")
    parser.add_argument("--stripe", type=float, default=6.0, help="Length of repeated passage to look for (seconds)")
    parser.add_argument("--top", type=int, default=8, help="How many candidate repeat spacings to report")
    parser.add_argument("--segments", action="store_true", help="Group the individual sung phrases by similarity instead of scanning lags")
    parser.add_argument("--similarity", type=float, default=0.55, help="Similarity threshold for calling two phrases the same (default 0.55)")
    args = parser.parse_args()

    audio = decode_audio(args.vocal, args.start, args.end)
    print(f"Analysing {len(audio)/SR:.1f}s from {args.start:.1f}s")

    pooled = pool(log_mel(audio), FRAME_POOL)
    voiced = voiced_mask(pooled)
    feats = normalise(pooled)
    frame_s = HOP * FRAME_POOL / SR
    print(f"{voiced.sum()} of {len(voiced)} frames carry singing")

    if args.segments:
        report_segment_repeats(args, pooled, frame_s)
        return

    ssm = feats @ feats.T
    # Only voiced-vs-voiced comparisons count; silence would match silence.
    pair_voiced = np.outer(voiced, voiced)
    ssm = np.where(pair_voiced, ssm, 0.0)
    stripe_frames = max(3, int(args.stripe / frame_s))
    enhanced = diagonal_smooth(ssm, stripe_frames)
    # A stripe only counts if most of it is actually singing on both sides.
    coverage = diagonal_smooth(pair_voiced.astype(np.float32), stripe_frames)
    enhanced = np.where(coverage >= 0.7, enhanced, 0.0)

    n = enhanced.shape[0]
    min_lag_frames = int(args.min_lag / frame_s)
    lag_scores = []
    for lag in range(min_lag_frames, n - stripe_frames):
        diag = np.diagonal(enhanced, offset=lag)
        if len(diag) >= stripe_frames:
            lag_scores.append((float(diag.max()), float(diag.mean()), lag))
    lag_scores.sort(reverse=True)

    print(f"\nStrongest repeat spacings (a chorus returning every N seconds):")
    seen = []
    for peak, mean, lag in lag_scores:
        secs = lag * frame_s
        if any(abs(secs - s) < 4.0 for s in seen):
            continue
        seen.append(secs)
        print(f"  every {secs:7.1f}s   strength {peak:.3f}")
        if len(seen) >= args.top:
            break

    best_lag_s = seen[0] if seen else 0.0
    best_lag = int(round(best_lag_s / frame_s))
    print(f"\nWhere the {best_lag_s:.1f}s repeat actually occurs:")
    diag = np.diagonal(enhanced, offset=best_lag)
    threshold = np.percentile(diag, 90)
    runs, in_run, run_start = [], False, 0
    for i, v in enumerate(diag):
        if v >= threshold and not in_run:
            in_run, run_start = True, i
        elif v < threshold and in_run:
            in_run = False
            if (i - run_start) * frame_s >= 2.0:
                runs.append((run_start, i))
    if in_run and (len(diag) - run_start) * frame_s >= 2.0:
        runs.append((run_start, len(diag)))
    for a, b in runs:
        t0 = args.start + a * frame_s
        t1 = args.start + b * frame_s
        print(f"  {t0:7.1f}s -> {t1:7.1f}s   repeats {t0 + best_lag_s:7.1f}s -> {t1 + best_lag_s:7.1f}s")


if __name__ == "__main__":
    main()
