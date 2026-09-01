#!/usr/bin/env python3
"""
Build karaoke timing from a speech-recognition pass over an isolated vocal
track, using the real lyrics rather than the recogniser's guess at them.

Whisper returns per-word timestamps, which is exactly what karaoke needs,
but its Hebrew transcript will contain mistakes. So the recognised words
are aligned to the lyrics you supply, and each lyric word takes the timing
of the recognised word it matched. Where the recogniser dropped or invented
a word, the gap is interpolated from its neighbours. The text on screen is
always yours; only the timing comes from the recogniser.

Run this where model downloads are allowed (an ordinary machine); the
managed sandbox this was developed in blocks them.

    pip install faster-whisper
    python3 transcribe_align.py --vocal lead_vocal.mp3 --lyrics song.txt \
        --out karaoke_timing.json

Then render as usual:

    python3 render_karaoke.py --audio instrumental.mp3 \
        --timing karaoke_timing.json --out karaoke.mp4
"""
import argparse
import json
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

FINALS = {"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"}


def normalise(word):
    """Fold a Hebrew word to its comparable core: no niqqud, no
    punctuation, final letters unified with their ordinary forms."""
    text = unicodedata.normalize("NFKD", word)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = "".join(c for c in text if c.isalnum())
    return "".join(FINALS.get(c, c) for c in text)


def similarity(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def align_sequences(lyric_words, heard_words, gap_penalty=0.45, match_floor=0.45):
    """Needleman-Wunsch over word similarity.

    Returns, for each lyric word, the index of the recognised word it
    matched, or None. Monotonic by construction, so the lyrics can never
    be reordered by a recogniser error.
    """
    n, m = len(lyric_words), len(heard_words)
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]

    for i in range(1, n + 1):
        score[i][0] = score[i - 1][0] - gap_penalty
        back[i][0] = "up"
    for j in range(1, m + 1):
        score[0][j] = score[0][j - 1] - gap_penalty
        back[0][j] = "left"

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sim = similarity(lyric_words[i - 1], heard_words[j - 1])
            diag = score[i - 1][j - 1] + (sim if sim >= match_floor else -gap_penalty)
            up = score[i - 1][j] - gap_penalty
            left = score[i][j - 1] - gap_penalty
            best = max(diag, up, left)
            score[i][j] = best
            back[i][j] = "diag" if best == diag else ("up" if best == up else "left")

    pairs = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        move = back[i][j]
        if move == "diag":
            if similarity(lyric_words[i - 1], heard_words[j - 1]) >= match_floor:
                pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif move == "up":
            i -= 1
        else:
            j -= 1
    return pairs


def interpolate_times(pairs, heard, n_lyrics):
    """Give every lyric word a start and end, filling unmatched runs by
    spreading them between the matched words on either side."""
    times = [None] * n_lyrics
    for i, j in enumerate(pairs):
        if j is not None:
            times[i] = [heard[j]["start"], heard[j]["end"]]

    anchors = [i for i, t in enumerate(times) if t is not None]
    if not anchors:
        sys.exit("No lyric word could be matched to the transcript. Check the lyrics file "
                 "matches this audio, or try a larger --model.")

    # Extend the ends outward so leading/trailing unmatched words get times.
    first, last = anchors[0], anchors[-1]
    for i in range(first):
        times[i] = [times[first][0], times[first][0]]
    for i in range(last + 1, n_lyrics):
        times[i] = [times[last][1], times[last][1]]

    for a, b in zip(anchors, anchors[1:]):
        gap = b - a
        if gap <= 1:
            continue
        start, end = times[a][1], times[b][0]
        step = (end - start) / gap
        for k in range(1, gap):
            times[a + k] = [start + step * (k - 1), start + step * k]
    return times


def transcribe(vocal, model_name, language, device):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed. Run:  pip install faster-whisper")

    print(f"Loading model '{model_name}' (first run downloads it)...")
    model = WhisperModel(model_name, device=device, compute_type="int8" if device == "cpu" else "float16")
    print("Transcribing with word timestamps...")
    segments, info = model.transcribe(str(vocal), language=language, word_timestamps=True,
                                      vad_filter=True, beam_size=5)
    heard = []
    for segment in segments:
        for word in (segment.words or []):
            text = word.word.strip()
            if text:
                heard.append({"text": text, "start": float(word.start), "end": float(word.end)})
    print(f"Recognised {len(heard)} words")
    return heard


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vocal", required=True, help="Isolated vocal track")
    parser.add_argument("--lyrics", required=True, help="Lyrics file, one line per karaoke line "
                                                        "(may carry '# start end' section markers)")
    parser.add_argument("--out", default="karaoke_timing.json")
    parser.add_argument("--model", default="large-v3", help="Whisper model: tiny/base/small/medium/large-v3 "
                                                            "(bigger is slower but much better in Hebrew)")
    parser.add_argument("--language", default="he")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--lead", type=float, default=0.12, help="Start each line this many seconds early")
    parser.add_argument("--transcript", default=None, help="Write the raw transcript here, "
                                                           "or read it back instead of re-running the model")
    args = parser.parse_args()

    from align_lyrics import parse_lyrics_file  # reuse the '# start end' format

    sections = parse_lyrics_file(args.lyrics)
    lines = [line for section in sections for line in section["lines"]]
    if not lines:
        sys.exit("Lyrics file is empty.")

    cache = Path(args.transcript) if args.transcript else None
    if cache and cache.exists():
        heard = json.loads(cache.read_text(encoding="utf-8"))
        print(f"Reusing cached transcript: {len(heard)} words")
    else:
        heard = transcribe(args.vocal, args.model, args.language, args.device)
        if cache:
            cache.write_text(json.dumps(heard, ensure_ascii=False, indent=2), encoding="utf-8")

    lyric_words, owner = [], []
    for index, line in enumerate(lines):
        for word in line.split():
            lyric_words.append(word)
            owner.append(index)

    pairs = align_sequences([normalise(w) for w in lyric_words],
                            [normalise(w["text"]) for w in heard])
    matched = sum(1 for p in pairs if p is not None)
    print(f"Matched {matched} of {len(lyric_words)} lyric words to the transcript "
          f"({matched / len(lyric_words) * 100:.0f}%)")

    times = interpolate_times(pairs, heard, len(lyric_words))

    out_lines = []
    for index, text in enumerate(lines):
        indices = [i for i, o in enumerate(owner) if o == index]
        words = [{"text": lyric_words[i],
                  "start": round(times[i][0], 3),
                  "end": round(max(times[i][1], times[i][0] + 0.05), 3)} for i in indices]
        start = min(w["start"] for w in words)
        end = max(w["end"] for w in words)
        out_lines.append({"start": round(start, 3), "end": round(end, 3), "text": text, "words": words})

    # A line stays up until the next one starts, so it can be read through
    # the breath, and gets a small head start on the voice.
    for i, line in enumerate(out_lines):
        earliest = out_lines[i - 1]["end"] if i else 0.0
        line["start"] = round(max(earliest, line["start"] - args.lead), 3)
        if i + 1 < len(out_lines):
            line["end"] = round(max(line["end"], out_lines[i + 1]["start"] - 0.01), 3)

    Path(args.out).write_text(
        json.dumps({"audio_file": Path(args.vocal).name, "lines": out_lines}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"Wrote {args.out} ({len(out_lines)} lines)")


if __name__ == "__main__":
    main()
