#!/usr/bin/env python3
"""concat_export.py — rebuild ShortsCut's cuts as a vertical project in Concat.

Reads the segments.json written by shorts.py and drives Concat's documented
JSON-RPC API (developer docs, apiVersion 0.2) to build a 1080x1920 project:

    version
    project.create   {location, name, video: {width: 1080, height: 1920}}
    media.import     {path, file}                       -> media id
    per segment, on track T1:
      edit.apply addClip     {mediaId, trackId: "T1", start: pos}
      edit.apply trimClip    {clipId, edge: "start", delta: +sourceStart}
      edit.apply trimClip    {clipId, edge: "end",
                              delta: sourceEnd - mediaDuration}
      edit.apply moveClips   {moves: [{clipId, start: pos, trackId: "T1"}]}
      edit.apply frameClip   {clipId, framing: "fill"}
    project.save
    export.run       {path, output}                     -> poll export.status

Why trim + move: addClip drops the whole file on the timeline. trimClip's
start edge drags the in-point forward; the end edge is shortened to the
segment's end. moveClips then puts the trimmed clip exactly at its
sequence position, so any timeline shift from trimming cannot leave gaps.
frameClip "fill" covers the vertical frame, centred, unstretched — the
same reframe shorts.py does with ffmpeg.

Two modes:
  * --dry-run (default when the server is unreachable): print the request
    sequence and write it to <project-dir>/<name>.jsonl WITHOUT contacting
    Concat. Ids are placeholders (m1, c1, ...) because live ids only come
    from server replies.
  * live: send JSON-RPC lines to Concat's Automation socket
    (default 127.0.0.1:7420 — the window's Settings > Automation page, or
    `concat-cli serve`). Every request and reply is logged. Live mode needs
    a running Concat with Automation enabled; it is NOT tested here because
    no Concat binary exists in this environment (see BUILD-REPORT.md).

Usage:
    python3 concat_export.py --input long-video.mp4 \\
        --segments ./shorts_out/segments.json \\
        --project-dir ./concat_projects --name ShortsCut \\
        --output ./shorts_concat.mp4 --dry-run
"""

import argparse
import json
import os
import socket
import sys
import time

VERSION = "1.0"


def die(msg, code=2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


class ConcatClient:
    """Minimal JSON-RPC line client for Concat's Automation socket."""

    def __init__(self, host, port, log):
        self.host = host
        self.port = port
        self.log = log
        self.sock = None
        self.buf = b""
        self.next_id = 1

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=10)
        except OSError as exc:
            return False
        self.sock.settimeout(120)
        return True

    def call(self, method, params):
        req_id = self.next_id
        self.next_id += 1
        req = {"jsonrpc": "2.0", "id": req_id, "method": method,
               "params": params}
        line = json.dumps(req)
        self.log.write(f">>> {line}\n")
        print(f">>> {method} {json.dumps(params)}")
        self.sock.sendall((line + "\n").encode())
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                die("connection to Concat closed unexpectedly.")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        self.log.write(f"<<< {raw.decode()}\n")
        reply = json.loads(raw.decode())
        if "error" in reply:
            die(f"Concat refused {method}: {reply['error']}")
        return reply.get("result", {})

    def close(self):
        if self.sock:
            self.sock.close()


def build_sequence(segments, media_duration, project_path, output_path,
                   media_id="m1"):
    """Build the request sequence with placeholder ids (dry-run shape).

    Returns a list of (method, params) tuples. In live mode the same
    sequence is built step by step with real ids from server replies.
    """
    seq = []
    seq.append(("version", {}))
    seq.append(("project.create", {
        "location": os.path.dirname(project_path),
        "name": os.path.basename(project_path),
        "video": {"width": 1080, "height": 1920,
                  "rateNum": 30, "rateDen": 1}}))
    seq.append(("media.import", {"path": project_path,
                                 "file": segments["input"]}))
    pos = 0.0
    for i, seg in enumerate(segments["clips"], start=1):
        clip_id = f"c{i}"
        seq.append(("edit.apply", {
            "path": project_path,
            "command": {"op": "addClip", "mediaId": media_id,
                        "trackId": "T1", "start": pos}}))
        seq.append(("edit.apply", {
            "path": project_path,
            "command": {"op": "trimClip", "clipId": clip_id,
                        "edge": "start", "delta": seg["start"]}}))
        seq.append(("edit.apply", {
            "path": project_path,
            "command": {"op": "trimClip", "clipId": clip_id,
                        "edge": "end",
                        "delta": seg["end"] - media_duration}}))
        seq.append(("edit.apply", {
            "path": project_path,
            "command": {"op": "moveClips", "moves": [
                {"clipId": clip_id, "start": pos, "trackId": "T1"}]}}))
        seq.append(("edit.apply", {
            "path": project_path,
            "command": {"op": "frameClip", "clipId": clip_id,
                        "framing": "fill"}}))
        pos += seg["duration"]
    seq.append(("project.save", {"path": project_path}))
    seq.append(("export.run", {"path": project_path, "output": output_path}))
    return seq


def dry_run(segments, media_duration, project_path, output_path, out_dir):
    seq = build_sequence(segments, media_duration, project_path, output_path)
    os.makedirs(out_dir, exist_ok=True)
    jsonl_path = os.path.join(
        out_dir, os.path.basename(project_path) + ".jsonl")
    with open(jsonl_path, "w") as f:
        for i, (method, params) in enumerate(seq, start=1):
            req = {"jsonrpc": "2.0", "id": i, "method": method,
                   "params": params}
            f.write(json.dumps(req) + "\n")
            print(f"[{i}] {method} {json.dumps(params)}")
    print(f"\nDry run only: {len(seq)} requests written to {jsonl_path}.")
    print("Nothing was sent to Concat (no server reachable, or --dry-run).")
    print("Live ids (m1, c1, ...) are placeholders here; a live run "
          "substitutes the createdId values from server replies.")
    return jsonl_path


def live_run(segments, media_duration, project_path, output_path,
             host, port, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "concat_calls.log")
    log = open(log_path, "w")
    client = ConcatClient(host, port, log)
    if not client.connect():
        log.close()
        print(f"Concat Automation socket {host}:{port} is not reachable.")
        print("Falling back to dry run (no requests sent).")
        return dry_run(segments, media_duration, project_path, output_path,
                       out_dir)
    try:
        version = client.call("version", {})
        print(f"Concat API: {version}")
        client.call("project.create", {
            "location": os.path.dirname(project_path),
            "name": os.path.basename(project_path),
            "video": {"width": 1080, "height": 1920,
                      "rateNum": 30, "rateDen": 1}})
        imported = client.call("media.import", {
            "path": project_path, "file": segments["input"]})
        media_id = imported.get("createdId")
        if not media_id:
            die("media.import reply carried no createdId; cannot continue.")
        print(f"Imported media id: {media_id}")
        pos = 0.0
        for seg in segments["clips"]:
            added = client.call("edit.apply", {
                "path": project_path,
                "command": {"op": "addClip", "mediaId": media_id,
                            "trackId": "T1", "start": pos}})
            clip_id = added.get("createdId")
            if not clip_id:
                die("addClip reply carried no createdId; cannot continue.")
            client.call("edit.apply", {
                "path": project_path,
                "command": {"op": "trimClip", "clipId": clip_id,
                            "edge": "start", "delta": seg["start"]}})
            client.call("edit.apply", {
                "path": project_path,
                "command": {"op": "trimClip", "clipId": clip_id,
                            "edge": "end",
                            "delta": seg["end"] - media_duration}})
            client.call("edit.apply", {
                "path": project_path,
                "command": {"op": "moveClips", "moves": [
                    {"clipId": clip_id, "start": pos, "trackId": "T1"}]}})
            client.call("edit.apply", {
                "path": project_path,
                "command": {"op": "frameClip", "clipId": clip_id,
                            "framing": "fill"}})
            print(f"Placed clip {clip_id}: source "
                  f"{seg['start']:.1f}s–{seg['end']:.1f}s at timeline {pos:.1f}s")
            pos += seg["duration"]
        client.call("project.save", {"path": project_path})
        started = client.call("export.run", {
            "path": project_path, "output": output_path})
        job = started.get("job")
        print(f"Export started (job {job}); polling status...")
        deadline = time.time() + 30 * 60
        while time.time() < deadline:
            status = client.call("export.status", {
                "path": project_path, "job": job} if job else
                {"path": project_path})
            print(f"  export status: {status}")
            state = str(status.get("state", status))
            if "done" in state or "complete" in state:
                print(f"Export finished: {output_path}")
                return output_path
            if "fail" in state or "error" in state or "cancel" in state:
                die(f"Export ended abnormally: {status}")
            time.sleep(5)
        die("Export polling timed out after 30 minutes.")
    finally:
        client.close()
        log.close()
        print(f"Full request/reply log: {log_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="concat_export.py",
        description="Rebuild ShortsCut's cuts as a vertical 1080x1920 "
                    "project inside Concat via its documented JSON-RPC API.")
    parser.add_argument("--input", required=True,
                        help="source video file (same file shorts.py used)")
    parser.add_argument("--segments", required=True,
                        help="segments.json written by shorts.py")
    parser.add_argument("--project-dir", required=True,
                        help="directory the Concat project folder is made in")
    parser.add_argument("--name", default="ShortsCut",
                        help="Concat project name (default: ShortsCut)")
    parser.add_argument("--output", required=True,
                        help="exported vertical video path")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Concat Automation host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7420,
                        help="Concat Automation port (default: 7420)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only print/write the request sequence, send nothing")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.segments):
        die(f"segments file not found: {args.segments}")
    with open(args.segments) as f:
        segments = json.load(f)
    if segments.get("input") != os.path.abspath(args.input):
        print("warning: --input differs from the file recorded in "
              "segments.json; using --input for media.import but segment "
              "timestamps come from segments.json.")
    if not segments.get("clips"):
        die("segments.json contains no clips; run shorts.py first.")
    media_duration = segments["input_duration"]
    project_path = os.path.join(os.path.abspath(args.project_dir), args.name)
    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."

    if args.dry_run:
        return dry_run(segments, media_duration, project_path,
                       os.path.abspath(args.output), out_dir) and 0
    return live_run(segments, media_duration, project_path,
                    os.path.abspath(args.output), args.host, args.port,
                    out_dir) and 0


if __name__ == "__main__":
    sys.exit(main())
