#!/usr/bin/env python3
"""VOTAAITE - Video On TV And Audio In The Ear.

Captures the desktop screen (and optionally system audio) with ffmpeg and
publishes it as live HLS over HTTP so a TV browser can play the video while,
in split mode, a phone browser plays the audio. Both players are locked to the
server wall clock using HLS PROGRAM-DATE-TIME so audio and video stay in sync.

Only the Python standard library and ffmpeg are required.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
RUN_DIR = os.path.join(ROOT, ".run")

HLS_FLAGS = "delete_segments+program_date_time+independent_segments+omit_endlist"


def run(cmd, env=None, timeout=10):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None


def detect_screen_size(display):
    env = dict(os.environ, DISPLAY=display)
    xdpyinfo = shutil.which("xdpyinfo")
    if xdpyinfo:
        out = run([xdpyinfo], env=env)
        if out and out.returncode == 0:
            match = re.search(r"dimensions:\s+(\d+)x(\d+)", out.stdout)
            if match:
                return f"{match.group(1)}x{match.group(2)}"
    xrandr = shutil.which("xrandr")
    if xrandr:
        out = run([xrandr, "--current"], env=env)
        if out and out.returncode == 0:
            best = None
            for line in out.stdout.splitlines():
                if " connected" not in line:
                    continue
                match = re.search(r"(\d+)x(\d+)\+\d+\+\d+", line)
                if match:
                    size = f"{match.group(1)}x{match.group(2)}"
                    if line.strip().startswith(("HDMI", "DP", "eDP")) or best is None:
                        best = size
            if best:
                return best
    return None


def pactl_sources():
    pactl = shutil.which("pactl")
    if not pactl:
        return []
    out = run([pactl, "list", "short", "sources"])
    names = []
    if out and out.returncode == 0:
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].strip():
                names.append(parts[1].strip())
    return names


def detect_audio_source():
    sources = pactl_sources()
    monitors = [name for name in sources if name.endswith(".monitor")]
    if not monitors:
        return None
    default_sink = None
    pactl = shutil.which("pactl")
    if pactl:
        out = run([pactl, "get-default-sink"])
        if out and out.returncode == 0:
            default_sink = out.stdout.strip()
    if default_sink and f"{default_sink}.monitor" in monitors:
        return f"{default_sink}.monitor"
    return monitors[0]


def get_sink_mute(sink):
    out = run(["pactl", "get-sink-mute", sink])
    if out and out.returncode == 0:
        return "yes" in out.stdout.strip().lower()
    out = run(["pactl", "list", "sinks"])
    if out and out.returncode == 0:
        current = None
        for line in out.stdout.splitlines():
            text = line.strip()
            if text.startswith("Name:"):
                current = text.split(":", 1)[1].strip()
            elif text.startswith("Mute:") and current == sink:
                return "yes" in text.lower()
    return None


class SinkMute:
    def __init__(self, sink):
        self.sink = sink
        self.previous = None
        self.silenced = False

    def mute(self):
        if not self.sink or not shutil.which("pactl"):
            return False
        self.previous = get_sink_mute(self.sink)
        if self.previous is True:
            return False
        run(["pactl", "set-sink-mute", self.sink, "1"])
        self.silenced = True
        return True

    def restore(self):
        if self.silenced and self.previous is not None:
            run(["pactl", "set-sink-mute", self.sink, "1" if self.previous else "0"])
        self.silenced = False


def local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def clean_run_dir():
    shutil.rmtree(RUN_DIR, ignore_errors=True)
    os.makedirs(RUN_DIR, exist_ok=True)


def hls_output(directory, hls_time, list_size):
    target = os.path.join(RUN_DIR, directory)
    os.makedirs(target, exist_ok=True)
    return [
        "-f", "hls",
        "-hls_time", str(hls_time),
        "-hls_list_size", str(list_size),
        "-hls_delete_threshold", "1",
        "-hls_flags", HLS_FLAGS,
        "-hls_allow_cache", "0",
        "-hls_segment_filename", os.path.join(target, "seg_%06d.ts"),
        os.path.join(target, "index.m3u8"),
    ]


def to_kbps(value):
    text = str(value).strip().lower()
    if text.endswith("k"):
        return int(float(text[:-1]))
    if text.endswith("m"):
        return int(float(text[:-1]) * 1000)
    return max(1, int(float(text) / 1000))


def build_ffmpeg_command(cfg):
    fps = cfg.fps
    gop = max(1, int(round(fps * cfg.hls_time)))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-thread_queue_size", "1024",
        "-f", "x11grab",
        "-framerate", str(fps),
        "-video_size", cfg.size,
        "-i", cfg.screen,
    ]
    if cfg.audio:
        cmd += [
            "-thread_queue_size", "1024",
            "-f", "pulse",
            "-i", cfg.audio,
        ]

    video_enc = [
        "-c:v", "libx264",
        "-preset", cfg.preset,
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-g", str(gop),
        "-keyint_min", str(gop),
        "-sc_threshold", "0",
        "-force_key_frames", f"expr:gte(t,n_forced*{cfg.hls_time})",
        "-b:v", cfg.video_bitrate,
        "-maxrate", cfg.video_bitrate,
        "-bufsize", f"{to_kbps(cfg.video_bitrate) * 2}k",
    ]
    if cfg.scale:
        video_enc += ["-vf", f"scale={cfg.scale}"]
    audio_enc = [
        "-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000", "-ac", "2",
    ]

    if cfg.mode in ("split", "both"):
        cmd += ["-map", "0:v"] + video_enc + hls_output("video", cfg.hls_time, cfg.list_size)
        if cfg.audio:
            cmd += ["-map", "1:a"] + audio_enc + hls_output("audio", cfg.hls_time, cfg.list_size)
    if cfg.mode in ("combined", "both"):
        cmd += ["-map", "0:v"]
        if cfg.audio:
            cmd += ["-map", "1:a"]
        cmd += video_enc + (audio_enc if cfg.audio else []) + hls_output("combined", cfg.hls_time, cfg.list_size)
    return cmd


class Ffmpeg:
    def __init__(self, cmd):
        self.cmd = cmd
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return self.proc

    def stream_logs(self, stop_event):
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            if stop_event.is_set():
                break
            line = line.rstrip()
            if line:
                print(f"[ffmpeg] {line}", flush=True)

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def safe_join(base, rel):
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, rel))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "VOTAAITE"
    protocol_version = "HTTP/1.1"
    token = ""
    cfg = None

    def log_message(self, fmt, *args):
        pass

    def send_headers(self, status, content_type, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()

    def send_bytes(self, status, content_type, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_headers(status, content_type, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload):
        self.send_bytes(200, "application/json; charset=utf-8", json.dumps(payload))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.serve_landing()
            return
        if path == "/favicon.ico":
            self.send_bytes(204, "image/x-icon", b"")
            return

        prefix = f"/t/{self.token}/"
        if not path.startswith(prefix):
            self.send_bytes(404, "text/plain; charset=utf-8", "Not found\n")
            return

        rel = path[len(prefix):]
        if rel in ("", "/", "index", "index.html"):
            self.serve_landing()
            return
        if rel == "api/sync":
            self.send_json({"t": int(time.time() * 1000), "started": self.cfg.started})
            return
        if rel == "api/config":
            self.send_json({
                "mode": self.cfg.mode,
                "audio": bool(self.cfg.audio),
                "hasAudioTrack": bool(self.cfg.audio) and self.cfg.mode != "combined",
                "hlsTime": self.cfg.hls_time,
                "video": "hls/video/index.m3u8" if self.cfg.mode in ("split", "both") else "hls/combined/index.m3u8",
                "audioUrl": "hls/audio/index.m3u8" if self.cfg.mode in ("split", "both") and self.cfg.audio else None,
                "combined": "hls/combined/index.m3u8" if self.cfg.mode in ("combined", "both") else None,
            })
            return

        if rel.startswith("hls/"):
            self.serve_file(RUN_DIR, rel[len("hls/"):])
            return
        self.serve_file(WEB_DIR, rel)

    def serve_file(self, base, rel):
        rel = rel.split("?")[0].split("#")[0]
        if not rel or rel.endswith("/"):
            rel += "index.html"
        target = safe_join(base, rel)
        if not target or not os.path.isfile(target):
            self.send_bytes(404, "text/plain; charset=utf-8", "Not found\n")
            return
        ext = os.path.splitext(target)[1].lower()
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self.send_bytes(500, "text/plain; charset=utf-8", "Read error\n")
            return
        self.send_bytes(200, content_type, body)

    def serve_landing(self):
        host = self.headers.get("Host") or f"{local_ip()}:{self.cfg.port}"
        base = f"http://{host}/t/{self.token}/"
        mode = self.cfg.mode
        if mode == "combined":
            roles = [("TV", base + "tv.html")]
        else:
            roles = [("TV", base + "tv.html"), ("Phone", base + "phone.html")]
        links = "\n".join(
            f'<li><span class="role">{role}</span>'
            f'<a href="{url}">{url}</a></li>'
            for role, url in roles
        )
        audio_note = ""
        if mode == "split" and not self.cfg.audio:
            audio_note = '<p class="warn">Audio capture is disabled, so only the video stream is available.</p>'
        html = LANDING_TEMPLATE.format(
            token=self.token,
            mode=mode,
            links=links,
            audio_note=audio_note,
            size=self.cfg.size,
            fps=self.cfg.fps,
        )
        self.send_bytes(200, "text/html; charset=utf-8", html)


LANDING_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VOTAAITE</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #0d1117; color: #e6edf3; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 32px 20px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.6rem; }}
  p.sub {{ color: #8b949e; margin: 0 0 24px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; word-break: break-all; }}
  .role {{ display: inline-block; min-width: 64px; font-weight: 600; color: #58a6ff; }}
  a {{ color: #79c0ff; }}
  .warn {{ color: #d29922; }}
  .meta {{ color: #8b949e; font-size: 0.85rem; margin-top: 24px; }}
</style>
</head>
<body>
<main>
  <h1>VOTAAITE</h1>
  <p class="sub">Video On TV And Audio In The Ear &mdash; live screen share, mode: {mode}</p>
  {audio_note}
  <ul>{links}</ul>
  <p class="meta">Capture {size} @ {fps}fps. Open the TV link on your TV browser and the phone link on your phone. Keep both open; playback is clock-locked so audio and video stay together.</p>
</main>
</body>
</html>
"""


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(cfg, token):
    class BoundHandler(Handler):
        pass
    BoundHandler.cfg = cfg
    BoundHandler.token = token
    return BoundHandler


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="votaite",
        description="Share the screen to a TV browser and the audio to a phone browser, in sync.",
    )
    parser.add_argument("-p", "--port", type=int, default=8099)
    parser.add_argument("-b", "--bind", default="0.0.0.0")
    parser.add_argument("--screen", default=os.environ.get("DISPLAY", ":0.0"),
                        help="X11 display to capture, e.g. :0.0")
    parser.add_argument("--size", default=None, help="Capture resolution WxH (auto-detected)")
    parser.add_argument("--scale", default=None, help="Optional ffmpeg scale target, e.g. 1280:-2")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--audio", default=None, help="PulseAudio source (default: monitor of default sink)")
    parser.add_argument("--no-audio", action="store_true", help="Disable audio capture entirely")
    parser.add_argument("--mute-local", action=argparse.BooleanOptionalAction, default=True,
                        help="Mute the captured sink so the computer speakers stay silent (default: on)")
    parser.add_argument("--mode", choices=["split", "combined", "both"], default="split",
                        help="split: video-only for TV + audio-only for phone; combined: muxed audio+video; both: all streams")
    parser.add_argument("--video-bitrate", default="2500k")
    parser.add_argument("--audio-bitrate", default="128k")
    parser.add_argument("--preset", default="veryfast",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"])
    parser.add_argument("--hls-time", type=float, default=2.0)
    parser.add_argument("--list-size", type=int, default=8)
    parser.add_argument("--token", default=None, help="Fixed access token instead of a random one")
    parser.add_argument("--list-devices", action="store_true", help="Print detected screen and audio and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print the ffmpeg command and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    screen = args.screen
    display = screen.split("+")[0]
    size = args.size or detect_screen_size(display)
    if not size:
        print("Could not detect screen size. Pass --size WxH.", file=sys.stderr)
        if not args.list_devices:
            return 2
    audio = None
    if not args.no_audio:
        audio = args.audio or detect_audio_source()

    if args.list_devices:
        print(f"display: {display}")
        print(f"size:    {size or 'unknown'}")
        print(f"audio:   {audio or 'none detected'}")
        sources = pactl_sources()
        if sources:
            print("sources:")
            for name in sources:
                print(f"  - {name}")
        return 0

    args.size = size
    args.screen = f"{screen}+0,0" if "+" not in screen else screen
    args.audio = audio
    args.started = int(time.time() * 1000)

    if not shutil.which("ffmpeg"):
        print("ffmpeg not found in PATH.", file=sys.stderr)
        return 2

    clean_run_dir()
    cmd = build_ffmpeg_command(args)
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    mute = None
    if args.mute_local and audio and audio.endswith(".monitor"):
        mute = SinkMute(audio[: -len(".monitor")])

    try:
        if mute:
            mute.mute()

        ffmpeg = Ffmpeg(cmd)
        proc = ffmpeg.start()
        time.sleep(0.5)
        if proc.poll() is not None:
            print("ffmpeg failed to start. Command:", file=sys.stderr)
            print(" ".join(cmd), file=sys.stderr)
            return 1

        token = args.token or secrets.token_urlsafe(9)
        handler = make_handler(args, token)
        try:
            httpd = Server((args.bind, args.port), handler)
        except OSError as exc:
            print(f"Cannot bind {args.bind}:{args.port}: {exc}", file=sys.stderr)
            ffmpeg.stop()
            return 1

        ip = local_ip()
        base = f"http://{ip}:{args.port}/t/{token}/"
        print("VOTAAITE is running", flush=True)
        print(f"  mode:  {args.mode}   capture: {args.size}@{args.fps}fps   audio: {audio or 'off'}", flush=True)
        if mute and mute.silenced:
            print("  local audio: muted (computer speakers are silent)", flush=True)
        print(f"  TV:    {base}tv.html", flush=True)
        if args.mode != "combined":
            print(f"  Phone: {base}phone.html", flush=True)
        print(f"  index: http://{ip}:{args.port}/", flush=True)
        print("Press Ctrl+C to stop.", flush=True)

        stop_event = threading.Event()
        log_thread = threading.Thread(target=ffmpeg.stream_logs, args=(stop_event,), daemon=True)
        log_thread.start()

        def shutdown(signum, frame):
            stop_event.set()
            ffmpeg.stop()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        try:
            httpd.serve_forever(poll_interval=0.5)
        finally:
            stop_event.set()
            ffmpeg.stop()
            httpd.server_close()
        return 0
    finally:
        if mute:
            mute.restore()


if __name__ == "__main__":
    sys.exit(main())
