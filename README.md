# VOTAAITE

**V**ideo **O**n **T**V **A**nd **A**udio **I**n **T**he **E**ar.

Share your desktop screen to a TV browser while the sound plays on your phone
(through headphones), with audio and video kept in sync. Everything runs on the
local network that the computer, the TV and the phone share, for example the
phone's hotspot.

No pip packages. Only Python 3 (standard library) and `ffmpeg` are needed.

## How it works

`ffmpeg` captures the X11 screen and the PulseAudio/PipeWire monitor and writes
live HLS playlists. A small Python HTTP server publishes them together with a
web player. Because every HLS segment carries a wall-clock
`EXT-X-PROGRAM-DATE-TIME`, both browsers lock onto the same server clock and
stay aligned, so the video on the TV and the audio on the phone match.

## Requirements

- Linux with X11 (`x11grab`)
- `ffmpeg` in `PATH`
- `pactl` (PulseAudio or PipeWire) if you want audio capture
- A TV browser that can play HLS. Chromium-based browsers (Android TV, newer
  LG webOS) use hls.js; browsers with native HLS play it directly. The TV page
  falls back to native playback automatically if hls.js fails, and you can force
  it with `http://<ip>:8099/tv?native=1`.

## Usage

```bash
python3 main.py
```

The console prints two short URLs, for example:

```
TV:    http://10.0.0.5:8099/tv
Phone: http://10.0.0.5:8099/phone
```

1. Open the TV URL on the TV browser and tap to start the video.
2. Open the phone URL on the phone, tap **Play audio**, and use the nudge
   buttons if the sound drifts ahead or behind the video.

Both devices must be able to reach the computer over the network.

By default the computer speakers are muted while VOTAAITE runs, so the sound
only comes out of the phone. The original mute state is restored when you stop
the program with Ctrl+C. Pass `--no-mute-local` to keep hearing the sound on the
computer as well.

You can also open `http://<computer-ip>:8099/` to see the links as a list.

## Modes

- `--mode split` (default): video only to the TV, audio only to the phone.
- `--mode combined`: one muxed audio+video stream to the TV.
- `--mode both`: publish both the split and the combined streams.

## Common options

| Option | Description |
| --- | --- |
| `-p, --port` | HTTP port (default `8099`) |
| `--mode` | `split`, `combined` or `both` (default `split`) |
| `--screen` | X11 display to capture (default `$DISPLAY`) |
| `--size` | Capture resolution `WxH` (auto-detected) |
| `--scale` | Optional ffmpeg scale target, e.g. `1280:-2` |
| `--fps` | Framerate (default `30`) |
| `--audio` | PulseAudio source (default: monitor of the default sink) |
| `--no-audio` | Disable audio capture |
| `--mute-local` / `--no-mute-local` | Mute the computer speakers while running (default: `--mute-local`) |
| `--video-bitrate` | Video bitrate (default `2500k`) |
| `--audio-bitrate` | Audio bitrate (default `128k`) |
| `--preset` | x264 preset (default `veryfast`) |
| `--hls-time` | HLS segment duration in seconds (default `2.0`) |
| `--token` | Protect the URLs behind a short path token, e.g. `--token Z8N9` (off by default) |
| `--list-devices` | Print detected screen and audio devices and exit |
| `--dry-run` | Print the generated ffmpeg command and exit |

Example for a lighter 720p stream:

```bash
python3 main.py --scale 1280:-2 --fps 30 --video-bitrate 2000k
```

## Sync tuning

The TV page measures how far behind the live edge it is playing and reports it
to the server. The phone page reads that value and matches the same latency, so
the sound lines up with the picture even when the TV uses its own native player
and has a large internal buffer.

If the sound is still a touch early or late, drag the slider on the phone page
(or use the 0.1s buttons) until it fits. The offset is stored in the browser and
remembered for next time. **Resync** re-measures the clock and jumps back.

If the TV cannot report (for example the browser does not run the page), the
phone falls back to a fixed three-segment delay. For a slower network, lower
`--video-bitrate` or raise the offset.

## Security notes

- The server binds to `0.0.0.0` so devices on the shared network can reach it.
  Use `--bind 127.0.0.1` to keep it local, or `--bind <lan-ip>` for one
  interface only.
- URLs are open by default for easy typing on a TV remote. Add `--token Z8N9`
  to require a short path token, for example `http://<ip>:8099/Z8N9/tv`, so
  other devices on the shared network cannot watch.
- `ffmpeg` only reads the screen and the audio monitor, and the server only
  serves files from `web/` and the runtime `.run/` directory.

## Third-party

`web/vendor/hls.min.js` is [hls.js](https://github.com/video-dev/hls.js)
(Apache-2.0). Its license is in `web/vendor/hls.min.js.LICENSE.txt`.

## License

MIT. See `LICENSE`.
