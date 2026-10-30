#!/usr/bin/env python3
"""ShortsCut — turn a long horizontal video into vertical Shorts/Reels clips.

Standalone companion tool for the open-source video editor Concat: it does
NOT need Concat to run. It finds loud (non-silent) stretches of a video with
ffmpeg, cuts them into vertical 1080x1920 clips, and prints a summary.

Highlight algorithm (honest version):
  1. ffmpeg `silencedetect` finds silent stretches (default: quieter than
     -30 dB for at least 1 second).
  2. The gaps between silences are candidate "speech" intervals. Intervals
     separated by less than 2 seconds of silence are merged (a breath is not
     a scene change).
  3. Intervals shorter than --min-duration are dropped; longer ones are
     chopped into --max-duration pieces.
  4. Each piece is ranked by RMS loudness (ffmpeg `astats`), loudest first,
     capped at --max-clips.

LIMIT: this detects LOUDNESS, not interestingness. A loud cough outranks a
quiet punchline. The ranking is a heuristic, not a judgement.

Usage:
    python3 shorts.py --input long-video.mp4 --output ./shorts_out
    python3 shorts.py --input long-video.mp4 --output ./shorts_out \\
        --max-duration 45 --min-duration 10 --max-clips 5

The segment list is also written to <output>/segments.json so that
concat_export.py can rebuild the same cuts inside Concat.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys

VERSION = "1.0"


def die(msg, code=2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def check_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} is not installed or not on PATH. "
                f"Install ffmpeg (which provides both) and retry.")


def run(cmd, capture=True):
    """Run a command; return (returncode, stdout, stderr)."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def probe(input_path):
    """Return (duration_seconds, has_audio, width, height) via ffprobe."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,width,height",
           "-of", "json", input_path]
    rc, out, err = run(cmd)
    if rc != 0:
        die(f"ffprobe could not read '{input_path}'. {err.strip()}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        die(f"ffprobe returned unreadable output for '{input_path}'.")
    duration = float(data["format"]["duration"])
    has_audio = any(s.get("codec_type") == "audio"
                    for s in data.get("streams", []))
    width = height = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            width, height = s.get("width"), s.get("height")
            break
    return duration, has_audio, width, height


def detect_silences(input_path, noise_db, min_silence):
    """Return a list of (start, end) silence intervals, in seconds."""
    cmd = ["ffmpeg", "-hide_banner", "-i", input_path, "-af",
           f"silencedetect=noise={noise_db}dB:d={min_silence}",
           "-f", "null", "-"]
    rc, _out, err = run(cmd)
    if rc != 0:
        die(f"ffmpeg silence detection failed on '{input_path}'.")
    starts = [float(m.group(1)) for m in
              re.finditer(r"silence_start: ([0-9.]+)", err)]
    ends = [float(m.group(1)) for m in
            re.finditer(r"silence_end: ([0-9.]+)", err)]
    silences = list(zip(starts, ends))
    return silences


def invert_silences(silences, duration):
    """Silence intervals -> non-silent intervals over [0, duration]."""
    intervals, cursor = [], 0.0
    for start, end in silences:
        if start > cursor:
            intervals.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        intervals.append((cursor, duration))
    return intervals


def merge_intervals(intervals, max_gap):
    """Merge intervals separated by less than max_gap seconds."""
    merged = []
    for start, end in intervals:
        if merged and start - merged[-1][1] < max_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def chunk_interval(start, end, max_duration):
    """Chop one interval into pieces of at most max_duration seconds."""
    pieces, cursor = [], start
    while end - cursor > 1e-6:
        piece_end = min(cursor + max_duration, end)
        pieces.append((cursor, piece_end))
        cursor = piece_end
    return pieces


def rms_level(input_path, start, duration):
    """Mean RMS loudness (dB) of a segment via ffmpeg astats; None on failure."""
    cmd = ["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(duration),
           "-i", input_path, "-af", "astats=metadata=1:reset=0",
           "-f", "null", "-"]
    rc, _out, err = run(cmd)
    if rc != 0:
        return None
    # Take the "Overall" section's RMS level if present, else the first one.
    overall = re.search(r"Overall\s*\n(?:.*\n)*?.*?RMS level dB:\s*(-?[0-9.]+)",
                        err)
    m = overall or re.search(r"RMS level dB:\s*(-?[0-9.]+)", err)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def find_segments(input_path, duration, args):
    """Full highlight pipeline. Returns a list of segment dicts."""
    silences = detect_silences(input_path, args.noise, args.min_silence)
    speech = invert_silences(silences, duration)
    speech = merge_intervals(speech, args.merge_gap)
    speech = [(s, e) for (s, e) in speech if e - s >= args.min_duration]
    pieces = []
    for start, end in speech:
        pieces.extend(chunk_interval(start, end, args.max_duration))
    # Keep only pieces that still meet the minimum (chunking cannot shrink
    # below min_duration except a trailing piece — drop those).
    pieces = [(s, e) for (s, e) in pieces if e - s >= args.min_duration]
    if not pieces:
        return []
    ranked = []
    if len(pieces) <= 100:
        for i, (s, e) in enumerate(pieces):
            ranked.append({"start": s, "end": e, "duration": e - s,
                           "rms_db": rms_level(input_path, s, e - s),
                           "order": i})
        # Loudest first; unmeasured pieces keep chronological order at the end.
        ranked.sort(key=lambda d: (d["rms_db"] is None,
                                   -(d["rms_db"] or 0.0), d["order"]))
    else:
        ranked = [{"start": s, "end": e, "duration": e - s,
                   "rms_db": None, "order": i}
                  for i, (s, e) in enumerate(pieces)]
    return ranked[:args.max_clips]


def render_clip(input_path, segment, out_path):
    """Render one vertical 1080x1920 clip. Never stretches the picture.

    scale=-2:1920 scales the frame to 1920 px tall (width follows aspect),
    then crop=1080:1920 takes the centred 9:16 window. For an already
    vertical 1080x1920 input this is effectively a no-op.
    """
    vf = "scale=-2:1920:flags=lanczos,crop=1080:1920"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-ss", str(segment["start"]), "-t", str(segment["duration"]),
           "-i", input_path, "-vf", vf,
           "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-movflags", "+faststart", "-y", out_path]
    rc, _out, err = run(cmd)
    if rc != 0:
        die(f"ffmpeg failed rendering '{out_path}'. {err.strip()}")


def fmt(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="shorts.py",
        description="Cut a long horizontal video into vertical Shorts/Reels "
                    "clips (1080x1920) from its loudest non-silent stretches.")
    parser.add_argument("--input", required=True, help="input video file")
    parser.add_argument("--output", default="./shorts_out",
                        help="output folder for clips (default: ./shorts_out)")
    parser.add_argument("--max-duration", type=float, default=59.0,
                        help="longest clip in seconds (default: 59)")
    parser.add_argument("--min-duration", type=float, default=15.0,
                        help="shortest clip in seconds (default: 15)")
    parser.add_argument("--max-clips", type=int, default=10,
                        help="maximum clips to produce (default: 10)")
    parser.add_argument("--noise", type=float, default=-30.0,
                        help="silence threshold in dB (default: -30)")
    parser.add_argument("--min-silence", type=float, default=1.0,
                        help="shortest silence that counts, seconds (default: 1)")
    parser.add_argument("--merge-gap", type=float, default=2.0,
                        help="merge speech split by shorter silences, seconds "
                             "(default: 2)")
    args = parser.parse_args(argv)

    if args.max_duration <= 0 or args.min_duration <= 0:
        die("--max-duration and --min-duration must be positive.")
    if args.min_duration > args.max_duration:
        die("--min-duration cannot exceed --max-duration.")
    if not os.path.isfile(args.input):
        die(f"input file not found: {args.input}")
    check_tools()

    duration, has_audio, width, height = probe(args.input)
    if not has_audio:
        die(f"'{args.input}' has no audio stream, so loudness-based "
            f"highlight detection is impossible. Use a video with audio.")
    print(f"Input: {args.input} ({width}x{height}, {fmt(duration)} "
          f"= {duration:.1f}s)")

    segments = find_segments(args.input, duration, args)
    if not segments:
        die(f"no segments of at least {args.min_duration:g}s of non-silent "
            f"audio found in '{args.input}'. Try a lower --min-duration or "
            f"a higher --noise threshold (e.g. -25).", code=3)

    os.makedirs(args.output, exist_ok=True)
    made = []
    for i, seg in enumerate(segments, start=1):
        out_path = os.path.join(args.output, f"short_{i:02d}.mp4")
        render_clip(args.input, seg, out_path)
        seg = dict(seg, path=os.path.abspath(out_path), index=i)
        made.append(seg)
        rms = (f"{seg['rms_db']:.1f} dB" if seg["rms_db"] is not None
               else "n/a")
        print(f"  clip {i:02d}: {fmt(seg['start'])}–{fmt(seg['end'])} "
              f"({seg['duration']:.1f}s, RMS {rms}) -> {out_path}")

    seg_path = os.path.join(args.output, "segments.json")
    with open(seg_path, "w") as f:
        json.dump({"input": os.path.abspath(args.input),
                   "input_duration": duration,
                   "clips": made}, f, indent=2)
    print(f"\nDone: {len(made)} clip(s) from {fmt(duration)} of input.")
    print(f"Segment list for concat_export.py: {seg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
