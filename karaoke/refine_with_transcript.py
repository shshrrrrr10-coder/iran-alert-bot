#!/usr/bin/env python3
"""
Nudge individual line times using a speech-recognition transcript, keeping
an existing audio-derived timing as the baseline.

A transcript knows something the audio alone cannot: which words are being
sung. But a coarse, garbled transcript of a song whose chorus returns five
times also matches ambiguously, and rebuilding the whole timing around it
lets one bad match drag every later line out of place.

So the audio-derived timing stays in charge. A line moves only when the
transcript proposes a start that is close to where the line already sits
and that keeps the line in order between its neighbours. Everything else
is left exactly as it was, which makes this incapable of breaking a timing
that already works.

Usage:
    python3 refine_with_transcript.py --timing timing.json \
        --transcript lead_vocal.srt --out timing_refined.json
"""
import argparse
import json
from pathlib import Path

from anchor_from_transcript import expand_words, load_chunks
from transcribe_align import align_sequences, normalise


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--timing", required=True, help="Existing timing JSON to refine")
    parser.add_argument("--transcript", required=True, help="Transcript: .srt/.vtt, or chunk-level JSON")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-shift", type=float, default=2.5,
                        help="Largest correction to accept from the transcript (default 2.5s)")
    parser.add_argument("--max-repetition", type=float, default=0.6)
    parser.add_argument("--min-loop-seconds", type=float, default=15.0)
    args = parser.parse_args()

    data = json.loads(Path(args.timing).read_text(encoding="utf-8"))
    lines = data["lines"]

    chunks, dropped = load_chunks(args.transcript, args.max_repetition, args.min_loop_seconds)
    print(f"{len(chunks)} usable transcript chunks ({dropped} dropped as hallucination loops)")
    heard = expand_words(chunks)

    lyric_words, owner = [], []
    for index, line in enumerate(lines):
        for word in line["text"].split():
            lyric_words.append(word)
            owner.append(index)

    pairs = align_sequences([normalise(w) for w in lyric_words],
                            [normalise(h["text"]) for h in heard])
    matched = sum(1 for p in pairs if p is not None)
    print(f"matched {matched} of {len(lyric_words)} lyric words ({matched / len(lyric_words) * 100:.0f}%)")

    proposals = {}
    for i, j in enumerate(pairs):
        if j is not None and owner[i] not in proposals:
            proposals[owner[i]] = heard[j]["start"]

    applied, rejected = 0, 0
    for index, line in enumerate(lines):
        proposed = proposals.get(index)
        if proposed is None:
            continue
        delta = proposed - line["start"]
        if abs(delta) > args.max_shift:
            rejected += 1
            continue
        # The correction must not reorder the lines.
        previous_end = lines[index - 1]["words"][-1]["end"] if index else 0.0
        next_start = lines[index + 1]["start"] if index + 1 < len(lines) else float("inf")
        new_start = line["start"] + delta
        if new_start < previous_end - 0.5 or new_start >= next_start - 0.5:
            rejected += 1
            continue
        line["start"] = round(new_start, 3)
        for word in line.get("words", []):
            word["start"] = round(word["start"] + delta, 3)
            word["end"] = round(word["end"] + delta, 3)
        applied += 1

    print(f"applied {applied} corrections, rejected {rejected} as too large or out of order")

    # Pulling a line earlier leaves the line before it ending too late, and
    # two lines drawn at the same position at the same time overlap on
    # screen. Close every window at the next line's start.
    trimmed = 0
    for i in range(len(lines) - 1):
        if lines[i]["end"] > lines[i + 1]["start"]:
            lines[i]["end"] = round(lines[i + 1]["start"], 3)
            trimmed += 1
    if trimmed:
        print(f"trimmed {trimmed} line windows that would have overlapped on screen")

    starts = [l["start"] for l in lines]
    assert all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1)), "line order broken"
    assert all(lines[i]["end"] <= lines[i + 1]["start"] + 1e-6 for i in range(len(lines) - 1)), "windows overlap"

    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
