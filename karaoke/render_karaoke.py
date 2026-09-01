#!/usr/bin/env python3
"""
Render a Hebrew karaoke lyrics video from an instrumental audio track and a
timing file produced by sync_editor.html (or a plain .lrc file).

Usage:
    python3 render_karaoke.py --audio song.mp3 --timing karaoke_timing.json --out karaoke.mp4
    python3 render_karaoke.py --audio song.mp3 --timing karaoke.lrc --out karaoke.mp4 --bg background.jpg

Requires ffmpeg (with libass) installed and on PATH.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def load_json_timing(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    lines = data.get("lines", [])
    return [(float(l["start"]), float(l["end"]), l["text"]) for l in lines]


LRC_TAG_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def load_lrc_timing(path):
    raw_lines = Path(path).read_text(encoding="utf-8").splitlines()
    entries = []
    for raw in raw_lines:
        m = LRC_TAG_RE.match(raw.strip())
        if not m:
            continue
        minutes, seconds = m.groups()
        start = int(minutes) * 60 + float(seconds)
        text = raw[m.end():].strip()
        if text:
            entries.append((start, text))
    entries.sort(key=lambda e: e[0])
    result = []
    for i, (start, text) in enumerate(entries):
        end = entries[i + 1][0] if i + 1 < len(entries) else start + 4.0
        result.append((start, end, text))
    return result


def load_timing(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        return load_json_timing(path)
    if path.suffix.lower() == ".lrc":
        return load_lrc_timing(path)
    raise ValueError(f"Unsupported timing file type: {path.suffix}")


def ffprobe_duration(audio_path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def split_words_karaoke(text, duration_s):
    """Distribute a line's duration across its words proportionally to
    character length, producing ASS \\k tags (centiseconds per word)."""
    words = text.split()
    if not words:
        return text
    total_cs = max(1, round(duration_s * 100))
    weights = [max(1, len(w)) for w in words]
    total_w = sum(weights)
    raw = [w * total_cs / total_w for w in weights]
    cs = [int(r) for r in raw]
    remainder = total_cs - sum(cs)
    # largest-remainder method so the total matches exactly
    order = sorted(range(len(raw)), key=lambda i: raw[i] - cs[i], reverse=True)
    for i in range(remainder):
        cs[order[i % len(order)]] += 1
    parts = []
    for word, c in zip(words, cs):
        parts.append(f"{{\\k{c}}}{escape_ass_text(word)}")
    return " ".join(parts)


def escape_ass_text(text):
    return text.replace("{", r"\{").replace("}", r"\}")


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Current,{font},{fontsize},{primary},{secondary},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,{margin_current},1
Style: Next,{font},{fontsize_next},&H00AAAAAA,&H00AAAAAA,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,{margin_next},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(entries, width, height, font, fontsize, primary_color, secondary_color):
    fontsize_next = max(20, int(fontsize * 0.6))
    header = ASS_HEADER.format(
        width=width, height=height, font=font, fontsize=fontsize,
        fontsize_next=fontsize_next, primary=primary_color, secondary=secondary_color,
        margin_current=int(height * 0.22), margin_next=int(height * 0.09),
    )
    events = []
    for i, (start, end, text) in enumerate(entries):
        duration = max(0.1, end - start)
        karaoke_text = split_words_karaoke(text, duration)
        events.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Current,,0,0,0,,{karaoke_text}"
        )
        if i + 1 < len(entries):
            next_text = escape_ass_text(entries[i + 1][2])
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Next,,0,0,0,,{next_text}"
            )
    return header + "\n".join(events) + "\n"


def build_ffmpeg_cmd(audio_path, ass_path, out_path, duration, width, height, fps, background):
    ass_escaped = str(ass_path).replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")
    vf = f"ass='{ass_escaped}'"

    if background is None:
        video_input = ["-f", "lavfi", "-i", f"color=c=0x101018:s={width}x{height}:r={fps}:d={duration + 1}"]
    elif str(background).lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
        video_input = ["-stream_loop", "-1", "-i", str(background)]
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}," + vf
    else:
        video_input = ["-loop", "1", "-i", str(background)]
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}," + vf

    cmd = [
        "ffmpeg", "-y",
        *video_input,
        "-i", str(audio_path),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out_path),
    ]
    return cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", required=True, help="Instrumental/backing track audio file")
    parser.add_argument("--timing", required=True, help="Timing file: karaoke_timing.json or .lrc")
    parser.add_argument("--out", default="karaoke.mp4", help="Output video path")
    parser.add_argument("--bg", default=None, help="Background image or video file (default: solid dark color)")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--font", default="DejaVu Sans", help="Font family name (must be installed / fontconfig-visible, must support Hebrew)")
    parser.add_argument("--fontsize", type=int, default=64)
    parser.add_argument("--primary-color", default="&H0000D7FF", help="ASS &HAABBGGRR color for the active/highlighted word (default gold)")
    parser.add_argument("--secondary-color", default="&H00FFFFFF", help="ASS &HAABBGGRR color for not-yet-sung words (default white)")
    parser.add_argument("--offset", type=float, default=0.0, help="Shift all timings by N seconds (+ delay, - earlier)")
    parser.add_argument("--keep-ass", action="store_true", help="Keep the generated .ass subtitle file next to the output")
    args = parser.parse_args()

    audio_path = Path(args.audio)
    timing_path = Path(args.timing)
    out_path = Path(args.out)

    if not audio_path.exists():
        sys.exit(f"Audio file not found: {audio_path}")
    if not timing_path.exists():
        sys.exit(f"Timing file not found: {timing_path}")

    entries = load_timing(timing_path)
    if args.offset:
        entries = [(s + args.offset, e + args.offset, t) for s, e, t in entries]
    if not entries:
        sys.exit("No timed lines found in timing file.")

    duration = ffprobe_duration(audio_path)
    print(f"Audio duration: {duration:.2f}s, {len(entries)} lyric lines")

    ass_content = build_ass(
        entries, args.width, args.height, args.font, args.fontsize,
        args.primary_color, args.secondary_color,
    )

    if args.keep_ass:
        ass_path = out_path.with_suffix(".ass")
        ass_path.write_text(ass_content, encoding="utf-8")
    else:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".ass", delete=False, encoding="utf-8")
        tmp.write(ass_content)
        tmp.close()
        ass_path = Path(tmp.name)

    cmd = build_ffmpeg_cmd(audio_path, ass_path, out_path, duration, args.width, args.height, args.fps, args.bg)
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit("ffmpeg failed")
    print(f"Done: {out_path}")


if __name__ == "__main__":
    main()
