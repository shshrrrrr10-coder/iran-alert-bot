#!/usr/bin/env python3
"""
Auto-detect vocal phrase/line boundaries in an isolated vocal track using
ffmpeg's silencedetect filter (pure signal processing -- no AI model
download required). Useful as a fast starting point for karaoke timing:
each gap in the singing becomes a candidate lyric-line boundary.

Usage:
    # Just see the detected segments:
    python3 auto_segment.py --vocal lead_vocal.mp3

    # Match segments 1:1 against a lyrics file (one line per lyric line)
    # and produce a karaoke_timing.json ready for render_karaoke.py:
    python3 auto_segment.py --vocal lead_vocal.mp3 --lyrics lyrics.txt --out karaoke_timing.json
"""
import argparse
import re
import subprocess
import sys
import json
from pathlib import Path

SILENCE_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def ffprobe_duration(audio_path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def detect_silence(audio_path, noise_db, min_silence_s):
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = result.stderr
    starts = [float(m.group(1)) for m in SILENCE_START_RE.finditer(log)]
    ends = [float(m.group(1)) for m in SILENCE_END_RE.finditer(log)]
    # silence_start entries without a matching silence_end (trailing
    # silence to EOF) are dropped -- we only care about *closed* gaps.
    silences = list(zip(starts, ends))
    return silences


def voice_segments(silences, total_duration, min_segment_s):
    """Complement of the silence intervals, clipped to [0, total_duration]."""
    segments = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor:
            segments.append((cursor, s_start))
        cursor = max(cursor, s_end)
    if cursor < total_duration:
        segments.append((cursor, total_duration))
    return [(s, e) for s, e in segments if (e - s) >= min_segment_s]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vocal", required=True, help="Isolated vocal track (clean singing, no music)")
    parser.add_argument("--lyrics", default=None, help="Text file, one lyric line per line, to pair with detected segments")
    parser.add_argument("--out", default=None, help="Write karaoke_timing.json here (requires --lyrics)")
    parser.add_argument("--noise-db", type=float, default=-30.0, help="Silence threshold in dB (default -30)")
    parser.add_argument("--min-silence", type=float, default=0.25, help="Minimum silence gap in seconds to count as a break (default 0.25)")
    parser.add_argument("--min-segment", type=float, default=0.2, help="Discard voice segments shorter than this (default 0.2s)")
    parser.add_argument("--pad-start", type=float, default=-0.05, help="Shift each segment start earlier by this many seconds (default 0.05s early)")
    args = parser.parse_args()

    vocal_path = Path(args.vocal)
    if not vocal_path.exists():
        sys.exit(f"Vocal file not found: {vocal_path}")

    duration = ffprobe_duration(vocal_path)
    silences = detect_silence(vocal_path, args.noise_db, args.min_silence)
    segments = voice_segments(silences, duration, args.min_segment)
    segments = [(max(0.0, s + args.pad_start), e) for s, e in segments]

    print(f"Audio duration: {duration:.2f}s")
    print(f"Detected {len(segments)} vocal segments (from {len(silences)} silence gaps)")

    if not args.lyrics:
        for i, (s, e) in enumerate(segments):
            print(f"{i+1:3d}. {s:7.2f} -> {e:7.2f}  ({e-s:.2f}s)")
        return

    lines = [l.strip() for l in Path(args.lyrics).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"Lyrics file has {len(lines)} lines")

    if len(lines) != len(segments):
        print(
            f"WARNING: segment count ({len(segments)}) != lyric line count ({len(lines)}). "
            "The mapping below is a best-effort 1:1 zip and will drift once the counts diverge -- "
            "review it in sync_editor.html or re-run with different --noise-db / --min-silence."
        )

    n = min(len(lines), len(segments))
    out_lines = []
    for i in range(n):
        s, e = segments[i]
        out_lines.append({"start": round(s, 3), "end": round(e, 3), "text": lines[i]})
    # any leftover lyric lines with no detected segment: stack them at the end, timeless
    for i in range(n, len(lines)):
        out_lines.append({"start": None, "end": None, "text": lines[i]})

    data = {"audio_file": vocal_path.name, "duration": duration, "lines": out_lines}

    if args.out:
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
