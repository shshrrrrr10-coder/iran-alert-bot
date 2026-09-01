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


def parse_lyrics_file(path):
    """Read a lyrics file, optionally split into timed sections.

    A line of the form '# <start> <end>' opens a section: every lyric
    line after it is aligned only within that time window. This keeps a
    verse from drifting into the next one across an instrumental break,
    and lets a repeated chorus be pinned to each of its returns.
    Without any '#' markers the whole file is aligned as one block.
    """
    sections = []
    current = None
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            parts = line[1:].split()
            if len(parts) < 2:
                continue  # a plain comment
            current = {"start": float(parts[0]), "end": float(parts[1]), "lines": []}
            sections.append(current)
            continue
        if current is None:
            current = {"start": None, "end": None, "lines": []}
            sections.append(current)
        current["lines"].append(line)
    return [s for s in sections if s["lines"]]


def align_evenly(lines, window_start, window_end):
    """Spread lines across a window purely by character count.

    Used where the vocal stem carries little energy but singing is
    happening anyway -- a crowd singing along, a backing-vocal passage,
    or a lead vocal buried by the separation. Energy-based alignment
    would cram every line into the few loud moments; even spacing at
    least keeps the lines marching through the section in order.
    """
    weights = [line_weight(l) for l in lines]
    total = sum(weights)
    span = max(0.5, window_end - window_start)
    result, cursor = [], window_start
    for text, w in zip(lines, weights):
        end = cursor + span * w / total
        words = text.split()
        ww = [line_weight(x) for x in words]
        total_ww, acc, word_times = sum(ww), 0.0, []
        for word, weight in zip(words, ww):
            ws = cursor + (end - cursor) * acc / total_ww
            acc += weight
            word_times.append({"start": round(ws, 3),
                               "end": round(cursor + (end - cursor) * acc / total_ww, 3),
                               "text": word})
        result.append({"start": round(cursor, 3), "end": round(end, 3),
                       "text": text, "words": word_times})
        cursor = end
    return result


def words_within(text, start, end):
    """Lay a line's words out inside one sung phrase, by character count."""
    words = text.split()
    weights = [line_weight(w) for w in words]
    total, acc, out = sum(weights), 0.0, []
    for word, weight in zip(words, weights):
        ws = start + (end - start) * acc / total
        acc += weight
        out.append({"start": round(ws, 3),
                    "end": round(start + (end - start) * acc / total, 3),
                    "text": word})
    return out


def align_one_to_one(lines, segments):
    """Exact case: one detected phrase per lyric line.

    Each line takes its own phrase, so the highlight sweeps that phrase
    and nothing else. The line stays on screen until the next one
    starts -- singers still want to read it through the breath -- but
    the sweep finishes with the singing rather than crawling on through
    the pause.
    """
    result = []
    for i, (text, (seg_start, seg_end)) in enumerate(zip(lines, segments)):
        display_end = segments[i + 1][0] if i + 1 < len(segments) else seg_end
        result.append({
            "start": round(seg_start, 3),
            "end": round(max(display_end, seg_end), 3),
            "text": text,
            "words": words_within(text, seg_start, seg_end),
        })
    return result


def align(lines, segments, snap_tolerance=0.45):
    if len(segments) == len(lines):
        return align_one_to_one(lines, segments)

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

        # Per-word times, warped through the active timeline so a breath
        # inside the line costs the words no time. Without these the
        # renderer can only spread the highlight evenly across the line's
        # whole span -- including its trailing pause -- so the sweep runs
        # slow and drifts further behind the singing with every word.
        words = text.split()
        word_weights = [line_weight(w) for w in words]
        total_ww = sum(word_weights)
        word_times, acc = [], 0.0
        for w in word_weights:
            ws = a_start + (a_end - a_start) * acc / total_ww
            acc += w
            we = a_start + (a_end - a_start) * acc / total_ww
            word_times.append({
                "start": round(active_to_wall(ws, segments, offsets), 3),
                "end": round(active_to_wall(max(we - 1e-6, ws), segments, offsets), 3),
            })
        for w, t in zip(words, word_times):
            t["text"] = w

        result.append({"start": round(w_start, 3), "end": round(w_end, 3),
                       "text": text, "words": word_times})

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
    parser.add_argument("--quiet-ratio", type=float, default=0.35,
                        help="If a section's vocal energy covers less than this fraction of its span, "
                             "space its lines evenly instead of by energy (default 0.35)")
    parser.add_argument("--lead", type=float, default=0.15,
                        help="Start every line this many seconds early (default 0.15). Silence detection "
                             "marks a phrase where it crosses the threshold, slightly after the note "
                             "starts, and a singer needs to see a word just before singing it.")
    args = parser.parse_args()

    vocal_path = Path(args.vocal)
    if not vocal_path.exists():
        sys.exit(f"Vocal file not found: {vocal_path}")

    sections = parse_lyrics_file(args.lyrics)
    if not sections:
        sys.exit("Lyrics file is empty.")

    duration = ffprobe_duration(vocal_path)
    silences = detect_silence(vocal_path, args.noise_db, args.min_silence)
    all_segments = voice_segments(silences, duration, args.min_segment)

    aligned = []
    for section in sections:
        sec_start = section["start"] if section["start"] is not None else args.start
        sec_end = section["end"] if section["end"] is not None else (args.end if args.end is not None else duration)
        segments = clip_segments(all_segments, sec_start, sec_end)
        _, total_active = build_active_map(segments)
        span = sec_end - sec_start
        if total_active < args.quiet_ratio * span:
            print(f"\nSection {sec_start:7.1f}s -> {sec_end:7.1f}s : only {total_active:.1f}s of "
                  f"{span:.1f}s carries vocal energy -- spacing {len(section['lines'])} lines evenly instead")
            aligned.extend(align_evenly(section["lines"], sec_start, sec_end))
            continue
        print(f"\nSection {sec_start:7.1f}s -> {sec_end:7.1f}s : "
              f"{len(segments):2d} phrases, {total_active:5.1f}s singing, "
              f"{len(section['lines'])} lines "
              f"({sum(line_weight(l) for l in section['lines']) / total_active:.1f} chars/s)")
        aligned.extend(align(section["lines"], segments, args.snap))
    if args.lead:
        for i, line in enumerate(aligned):
            earliest = aligned[i - 1]["end"] if i else 0.0
            line["start"] = round(max(earliest, line["start"] - args.lead), 3)
            for word in line.get("words", []):
                word["start"] = round(max(earliest, word["start"] - args.lead), 3)
                word["end"] = round(max(word["start"] + 0.05, word["end"] - args.lead), 3)

    data = {"audio_file": vocal_path.name, "duration": duration, "lines": aligned}
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")
    for i, l in enumerate(aligned):
        print(f"{i+1:3d}. {l['start']:7.2f} -> {l['end']:7.2f}  ({l['end']-l['start']:4.1f}s)  {l['text']}")


if __name__ == "__main__":
    main()
