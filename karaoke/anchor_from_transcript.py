#!/usr/bin/env python3
"""
Pin lyric lines to times taken from a speech-recognition transcript.

A chunk-level transcript (one timestamp per phrase, not per word) is too
coarse to drive a karaoke highlight on its own, and Hebrew recognition
output is badly garbled besides. But it carries something the signal-only
path cannot infer: which words are being sung where. Aligning it to the
real lyrics gives a semantic anchor per line; word placement inside each
line is still done from the audio's syllable attacks.

Recognisers also hallucinate, looping a phrase for tens of seconds. Those
chunks are detected by their internal repetition and dropped, since their
timestamps are meaningless.

Input transcript format (a JSON list):
    [{"timestamp": [start, end], "text": "..."}, ...]

Usage:
    python3 anchor_from_transcript.py --transcript transcript.json \
        --lyrics song_structured.txt --out song_anchored.txt
    python3 align_lyrics.py --vocal lead_vocal.mp3 \
        --lyrics song_anchored.txt --out timing.json
"""
import argparse
import json
import re
from pathlib import Path

from transcribe_align import align_sequences, normalise

FINAL_CHUNK_GUESS = 605.0


def repetition_ratio(text):
    words = [normalise(w) for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return 1.0
    return 1.0 - len(set(words)) / len(words)


SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)")
# Free transcription services stamp their output with an advertisement.
WATERMARK = re.compile(r"\(?\s*(תומלל על ידי|Transcribed by)[^)]*\)?", re.IGNORECASE)


def parse_srt(path):
    def seconds(h, m, s, frac):
        return int(h) * 3600 + int(m) * 60 + int(s) + float(f"0.{frac}")

    chunks, current, text = [], None, []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        match = SRT_TIME.match(line)
        if match:
            if current and text:
                chunks.append({"timestamp": current, "text": " ".join(text)})
            g = match.groups()
            current, text = [seconds(*g[:4]), seconds(*g[4:])], []
        elif line and not line.isdigit():
            text.append(line)
    if current and text:
        chunks.append({"timestamp": current, "text": " ".join(text)})
    for chunk in chunks:
        chunk["text"] = WATERMARK.sub(" ", chunk["text"]).strip()
    return chunks


def load_chunks(path, max_repetition, min_loop_seconds):
    path = Path(path)
    if path.suffix.lower() in (".srt", ".vtt"):
        chunks = parse_srt(path)
    else:
        chunks = json.loads(path.read_text(encoding="utf-8"))
    kept, dropped = [], 0
    for chunk in chunks:
        start, end = chunk.get("timestamp", [None, None])
        if start is None:
            continue
        if end is None:
            end = max(float(start) + 5.0, FINAL_CHUNK_GUESS)
        start, end = float(start), float(end)
        if end <= start:
            continue
        if repetition_ratio(chunk["text"]) > max_repetition and (end - start) > min_loop_seconds:
            dropped += 1
            continue
        kept.append({"start": start, "end": end, "text": chunk["text"]})
    return kept, dropped


def expand_words(chunks):
    """Spread each chunk's words evenly across its span."""
    heard = []
    for chunk in chunks:
        words = [w for w in chunk["text"].split() if normalise(w)]
        if not words:
            continue
        step = (chunk["end"] - chunk["start"]) / len(words)
        for i, word in enumerate(words):
            heard.append({"text": word,
                          "start": chunk["start"] + i * step,
                          "end": chunk["start"] + (i + 1) * step})
    return heard


def enforce_monotonic(anchors):
    """Keep only anchors that form an increasing run, longest first.

    A garbled transcript produces occasional wild matches; requiring the
    anchors to advance in time discards them without discarding the many
    correct ones around them.
    """
    best_run, current = [], []
    for index, time in anchors:
        if current and time < current[-1][1]:
            if len(current) > len(best_run):
                best_run = current
            current = []
        current.append((index, time))
    if len(current) > len(best_run):
        best_run = current
    return best_run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--lyrics", required=True, help="Lyrics file, optionally with '# start end' sections")
    parser.add_argument("--out", required=True, help="Lyrics file to write, with one window per line")
    parser.add_argument("--max-repetition", type=float, default=0.6)
    parser.add_argument("--min-loop-seconds", type=float, default=15.0)
    parser.add_argument("--max-shift", type=float, default=4.0,
                        help="Ignore an anchor this far from where the section says the line should be")
    parser.add_argument("--min-line-gap", type=float, default=1.0,
                        help="Drop an anchor that does not advance at least this far past the previous one")
    args = parser.parse_args()

    from align_lyrics import parse_lyrics_file

    sections = parse_lyrics_file(args.lyrics)
    lines, bounds = [], []
    for section in sections:
        for line in section["lines"]:
            lines.append(line)
            bounds.append((section["start"], section["end"]))

    chunks, dropped = load_chunks(args.transcript, args.max_repetition, args.min_loop_seconds)
    print(f"{len(chunks)} usable transcript chunks ({dropped} dropped as hallucination loops)")

    heard = expand_words(chunks)
    lyric_words, owner = [], []
    for index, line in enumerate(lines):
        for word in line.split():
            lyric_words.append(word)
            owner.append(index)

    pairs = align_sequences([normalise(w) for w in lyric_words],
                            [normalise(h["text"]) for h in heard])
    matched = sum(1 for p in pairs if p is not None)
    print(f"matched {matched} of {len(lyric_words)} lyric words ({matched / len(lyric_words) * 100:.0f}%)")

    # First matched word of each line proposes that line's start.
    proposals = {}
    for i, j in enumerate(pairs):
        if j is None:
            continue
        line_index = owner[i]
        if line_index not in proposals:
            proposals[line_index] = heard[j]["start"]

    anchors = enforce_monotonic(sorted(proposals.items()))
    print(f"{len(anchors)} of {len(lines)} lines anchored after discarding out-of-order matches")

    # A line whose text recurs -- a chorus sung five times -- cannot be
    # anchored from a transcript at all: its words match every repeat
    # equally well, and a wrong pick drags every later line with it. Those
    # lines are left to the audio, which does distinguish the repeats.
    counts = {}
    for line in lines:
        key = " ".join(normalise(w) for w in line.split())
        counts[key] = counts.get(key, 0) + 1
    unique = [counts[" ".join(normalise(w) for w in line.split())] == 1 for line in lines]
    print(f"{sum(unique)} of {len(lines)} lines have text that appears only once")

    anchored, last_time = {}, None
    for line_index, time in anchors:
        if not unique[line_index]:
            continue
        low, high = bounds[line_index]
        if low is not None and not (low - args.max_shift <= time <= high + args.max_shift):
            continue  # transcript disagrees wildly with the known section
        if last_time is not None and time < last_time + args.min_line_gap:
            # Repeated choruses make the transcript match ambiguously and
            # several lines collapse onto one time. Anchors that do not
            # advance are worthless, so drop them and let the section-based
            # pass handle those lines instead.
            continue
        anchored[line_index] = time
        last_time = time
    print(f"{len(anchored)} anchors kept after the section and progression checks")

    # An anchored line gets its own tight window, ending where the next
    # anchored line begins. Lines with no trustworthy anchor are left in
    # their original section window, so the audio-only pass places them
    # exactly as it did before rather than being dragged somewhere worse.
    lines_out, index = [], 0
    while index < len(lines):
        if index in anchored:
            start = anchored[index]
            low, high = bounds[index]
            nxt = next((k for k in range(index + 1, len(lines)) if k in anchored), None)
            end = anchored[nxt] if nxt is not None else (high if high is not None else start + 6.0)
            if high is not None:
                end = min(end, high)
            if end <= start:
                end = start + 2.0
            lines_out.append(f"# {start:.2f} {end:.2f}")
            lines_out.append(lines[index])
            lines_out.append("")
            index += 1
            continue

        run_start = index
        while index < len(lines) and index not in anchored:
            index += 1
        low, high = bounds[run_start]
        span_low = low if low is not None else 0.0
        span_high = anchored.get(index, high if high is not None else span_low + 10.0)
        if high is not None:
            span_high = min(span_high, high)
        previous_anchor = anchored.get(run_start - 1)
        if previous_anchor is not None:
            span_low = max(span_low, previous_anchor)
        lines_out.append(f"# {span_low:.2f} {max(span_high, span_low + 1.0):.2f}")
        lines_out.extend(lines[run_start:index])
        lines_out.append("")

    Path(args.out).write_text("\n".join(lines_out), encoding="utf-8")
    anchored_runs = sum(1 for i in range(len(lines)) if i in anchored)
    print(f"Wrote {args.out}: {anchored_runs} lines pinned by transcript, "
          f"{len(lines) - anchored_runs} left to the audio-only pass")


if __name__ == "__main__":
    main()
