# ShortsCut

**Author: Cipher** — a companion tool for the open-source video editor
[Concat](https://github.com/jub0t/Concat) (free, local CapCut alternative,
AGPL-3.0). ShortsCut turns a long horizontal video into vertical
Shorts/Reels clips (1080×1920, H.264, each under 60 seconds).

It works **standalone** — Concat is not required. An optional second script
rebuilds the same cuts as a vertical project *inside* Concat (see
"Concat mode" below).

## Install

- Python 3.8+ (standard library only — no pip packages)
- `ffmpeg` + `ffprobe` on PATH (the tool exits with a plain message if
  they are missing)

```sh
python3 shorts.py --help
```

## Usage

```sh
# Basic: loudest non-silent stretches -> vertical clips
python3 shorts.py --input long-video.mp4 --output ./shorts_out

# Shorter clips, more of them, lower minimum length
python3 shorts.py --input long-video.mp4 --output ./shorts_out \
    --max-duration 45 --min-duration 10 --max-clips 5
```

| Flag | Default | Meaning |
| ---- | ------- | ------- |
| `--input` | (required) | input video file |
| `--output` | `./shorts_out` | folder for clips |
| `--max-duration` | 59 | longest clip, seconds |
| `--min-duration` | 15 | shortest clip, seconds |
| `--max-clips` | 10 | maximum clips produced |
| `--noise` | -30 | silence threshold, dB (higher = more counts as silent) |
| `--min-silence` | 1 | shortest silence that counts, seconds |
| `--merge-gap` | 2 | merge speech split by shorter silences, seconds |

Output: `short_01.mp4`, `short_02.mp4`, … plus `segments.json`
(start/end/duration/RMS/path per clip).

## How highlights are found (honest version)

1. ffmpeg `silencedetect` finds silent stretches (quieter than the
   `--noise` threshold for at least `--min-silence` seconds).
2. The gaps between silences are candidate "speech" intervals. Intervals
   separated by less than `--merge-gap` seconds of silence are merged —
   a breath is not a scene change.
3. Intervals shorter than `--min-duration` are dropped; longer ones are
   chopped into `--max-duration` pieces.
4. Pieces are ranked by RMS loudness (ffmpeg `astats`), loudest first,
   capped at `--max-clips`.

**Limit, stated plainly:** this detects **loudness, not interestingness**.
A loud cough outranks a quiet punchline. If your video has music over
everything, silence detection finds almost nothing — use a lower
`--min-duration` or pick timestamps by hand. Videos with no audio stream
are refused with a clear error (loudness detection needs audio).

**Vertical reframe:** `scale=-2:1920, crop=1080:1920` — the frame is
scaled to 1920 px tall and the centred 9:16 window is taken. The picture
is never stretched. (Blurred-background padding is a possible future
improvement, not present.)

## Concat mode (Phase 2)

`concat_export.py` rebuilds the same cuts as a vertical Concat project
through Concat's **documented** JSON-RPC API (developer docs, apiVersion
0.2):

```sh
# Dry run: print/write the request sequence, send nothing
python3 concat_export.py --input long-video.mp4 \
    --segments ./shorts_out/segments.json \
    --project-dir ./concat_projects --name ShortsCut \
    --output ./shorts_concat.mp4 --dry-run

# Live: send to Concat's Automation socket (window's Settings > Automation
# page, or `concat-cli serve`, default 127.0.0.1:7420). Every request and
# reply is logged to concat_calls.log next to the export.
python3 concat_export.py --input long-video.mp4 \
    --segments ./shorts_out/segments.json \
    --project-dir ./concat_projects --name ShortsCut \
    --output ./shorts_concat.mp4
```

The sequence it drives, all from the public docs:

| Step | API call | What it does |
| ---- | -------- | ------------ |
| 1 | `project.create` with `video: {width: 1080, height: 1920, rateNum: 30, rateDen: 1}` | vertical canvas (the docs' own example uses 1080×1920) |
| 2 | `media.import` | source file into the bin (reply gives the media id) |
| 3 | `edit.apply` → `addClip` | whole file onto track T1 |
| 4 | `edit.apply` → `trimClip` (start edge, then end edge) | isolate the segment |
| 5 | `edit.apply` → `moveClips` | put the trimmed clip at its sequence position |
| 6 | `edit.apply` → `frameClip` with `framing: "fill"` | cover the vertical frame, centred, unstretched |
| 7 | `project.save`, then `export.run` | save + render (polled via `export.status`) |

### Phase 2 verdict: YES — with one honest caveat

The API **does** cover the whole Shorts workflow: create a project,
set a vertical 1080×1920 canvas, import media, place + trim clips, fill
the frame, and export. Evidence: Concat's MCP doc (`docs/MCP.md`) lists
`project_create`, `project_set_video`, `clip_add`, `export_start` etc.,
and the developer docs' Methods page documents `project.create` (with a
1080×1920 example), `project.setVideo`, `media.import`, `edit.apply`,
and the Edit-commands page documents `trimClip`, `moveClips`, and
`frameClip` (`fill`).

**Caveat:** the live path is **not tested against a real Concat** — no
Concat binary exists in this build environment, and building Concat from
source was out of scope. The dry-run output (request JSON) is validated;
the live socket flow is written from the docs and must be run once
against a real Concat (Automation enabled) before it is called working.
If the server is unreachable, the script falls back to dry run and says
so instead of pretending.

## Licence note

ShortsCut itself is MIT-licensed (see `LICENSE`). It is a standalone
client: it talks to Concat over its public API and ships no Concat code. Concat is AGPL-3.0-or-later; a
program that talks to it over the API is a client (per Concat's own
docs). No Concat code was modified.
