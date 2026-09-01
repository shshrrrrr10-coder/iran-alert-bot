#!/usr/bin/env python3
"""
Align lyric lines to an isolated vocal track without any speech-recognition
model, by warping the lyrics onto the track's *active singing timeline*.

How it works
------------
1. ffmpeg's silencedetect finds where singing actually happens, producing
   voice-active segments (breaths, rests and instrumental breaks fall out).
2. Those segments are concatenated into one continuous "active timeline"
   whose total length is the real amount of singing in the window.
3. Lyric lines are laid out along that active timeline in proportion to
   their character counts (longer lines take proportionally longer to
   sing), then each line's active-time span is mapped back to real
   wall-clock time.
4. Line boundaries that land near a real segment edge are snapped to it,
   so lines begin on an actual vocal onset instead of mid-word.

This handles both directions of mismatch automatically: a line broken by a
mid-line breath spans several segments, and a segment holding several lines
is divided proportionally. Silences never consume lyric time, so long
instrumental breaks do not push the lyrics out of sync.

Usage:
    python3 align_lyrics.py --vocal lead_vocal.mp3 --lyrics song.txt \
        --start 0 --end 140 --out karaoke_timing.json
"""
import argparse
import json
import sys
from bisect import bisect_right
from pathlib import Path

from auto_segment import detect_silence, ffprobe_duration, voice_segments


def clip_segments(segments, window_start, window_end):
    clipped = []
    for s, e in segments:
        s2, e2 = max(s, window_start), min(e, window_end)
        if e2 > s2:
            clipped.append((s2, e2))
    return clipped


def build_active_map(segments):
    """Cumulative active-time offset at the start of each segment."""
    offsets = []
    total = 0.0
    for s, e in segments:
        offsets.append(total)
        total += e - s
    return offsets, total


def active_to_wall(active_t, segments, offsets):
    """Map a position on the active timeline back to wall-clock time."""
    if not segments:
        return 0.0
    i = bisect_right(offsets, active_t) - 1
    i = max(0, min(i, len(segments) - 1))
    seg_start, seg_end = segments[i]
    into = active_t - offsets[i]
    return min(seg_end, seg_start + max(0.0, into))


def snap(value, boundaries, tolerance):
    """Snap a time to the nearest segment boundary within tolerance."""
    if not boundaries:
        return value
    best = min(boundaries, key=lambda b: abs(b - value))
    return best if abs(best - value) <= tolerance else value


def line_weight(text):
    """Rough singing cost of a line: characters, ignoring spaces."""
    return max(1, len(text.replace(" ", "")))


def align(lines, segments, snap_tolerance=0.45):
    offsets, total_active = build_active_map(segments)
    if total_active <= 0:
        sys.exit("No vocal activity detected in the selected window.")

    weights = [line_weight(l) for l in lines]
    total_weight = sum(weights)

    starts_active = []
    acc = 0.0
    for w in weights:
        starts_active.append(acc)
        acc += w
    starts_active.append(acc)

    onsets = [s for s, _ in segments]
    offsets_ends = [e for _, e in segments]

    result = []
    for i, text in enumerate(lines):
        a_start = starts_active[i] / total_weight * total_active
        a_end = starts_active[i + 1] / total_weight * total_active
        w_start = active_to_wall(a_start, segments, offsets)
        w_end = active_to_wall(max(a_end - 1e-6, a_start), segments, offsets)
        # A line should begin on a vocal onset and end on a vocal offset.
        w_start = snap(w_start, onsets, snap_tolerance)
        w_end = snap(w_end, offsets_ends, snap_tolerance)
        if w_end <= w_start:
            w_end = w_start + 0.4
        result.append({"start": round(w_start, 3), "end": round(w_end, 3), "text": text})

    # Keep lines strictly ordered and non-overlapping.
    for i in range(len(result) - 1):
        if result[i]["end"] > result[i + 1]["start"]:
            result[i]["end"] = round(max(result[i]["start"] + 0.3, result[i + 1]["start"]), 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vocal", required=True, help="Isolated vocal track")
    parser.add_argument("--lyrics", required=True, help="Text file, one lyric line per line")
    parser.add_argument("--out", default="karaoke_timing.json")
    parser.add_argument("--start", type=float, default=0.0, help="Window start in seconds (e.g. where this song begins)")
    parser.add_argument("--end", type=float, default=None, help="Window end in seconds (e.g. where this song ends)")
    parser.add_argument("--noise-db", type=float, default=-30.0)
    parser.add_argument("--min-silence", type=float, default=0.25)
    parser.add_argument("--min-segment", type=float, default=0.2)
    parser.add_argument("--snap", type=float, default=0.45, help="Snap line edges to a vocal onset within this many seconds")
    args = parser.parse_args()

    vocal_path = Path(args.vocal)
    if not vocal_path.exists():
        sys.exit(f"Vocal file not found: {vocal_path}")

    lines = [l.strip() for l in Path(args.lyrics).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        sys.exit("Lyrics file is empty.")

    duration = ffprobe_duration(vocal_path)
    window_end = args.end if args.end is not None else duration
    silences = detect_silence(vocal_path, args.noise_db, args.min_silence)
    segments = voice_segments(silences, duration, args.min_segment)
    segments = clip_segments(segments, args.start, window_end)

    _, total_active = build_active_map(segments)
    print(f"Window {args.start:.1f}s -> {window_end:.1f}s")
    print(f"{len(segments)} vocal segments, {total_active:.1f}s of actual singing")
    print(f"{len(lines)} lyric lines -> {sum(line_weight(l) for l in lines) / total_active:.1f} characters per second of singing")

    aligned = align(lines, segments, args.snap)
    data = {"audio_file": vocal_path.name, "duration": duration, "lines": aligned}
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")
    for i, l in enumerate(aligned):
        print(f"{i+1:3d}. {l['start']:7.2f} -> {l['end']:7.2f}  ({l['end']-l['start']:4.1f}s)  {l['text']}")


if __name__ == "__main__":
    main()
