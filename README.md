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
- A browser with Media Source Extensions on the TV (most Chromium-based TV
  browsers). Safari-style browsers fall back to native HLS playback.

## Usage

```bash
python3 main.py
```

The console prints two URLs, for example:

```
TV:    http://10.0.0.5:8099/t/AbC123/tv.html
Phone: http://10.0.0.5:8099/t/AbC123/phone.html
```

1. Open the TV URL on the TV browser and tap to start the video.
2. Open the phone URL on the phone, tap **Play audio**, and use the nudge
   buttons if the sound drifts ahead or behind the video.

Both devices must be able to reach the computer over the network. The short
access token in the URL keeps other devices out.

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
| `--video-bitrate` | Video bitrate (default `2500k`) |
| `--audio-bitrate` | Audio bitrate (default `128k`) |
| `--preset` | x264 preset (default `veryfast`) |
| `--hls-time` | HLS segment duration in seconds (default `2.0`) |
| `--token` | Use a fixed access token instead of a random one |
| `--list-devices` | Print detected screen and audio devices and exit |
| `--dry-run` | Print the generated ffmpeg command and exit |

Example for a lighter 720p stream:

```bash
python3 main.py --scale 1280:-2 --fps 30 --video-bitrate 2000k
```

## Sync tuning

Both players target the same point on the server clock, about three segments
behind live. That buffer is the `buffer` value shown on the phone page. Use
**Audio -0.5s** / **Audio +0.5s** to shift only the phone and align the sound
with the TV. **Resync** re-measures the clock offset and jumps back to the
target position.

For a slower network, increase the buffer by editing the `bufferMs` default in
`web/tv.html` and `web/phone.html`, or lower `--video-bitrate`.

## Security notes

- The server binds to `0.0.0.0` so devices on the shared network can reach it.
  Use `--bind 127.0.0.1` to keep it local, or `--bind <lan-ip>` for one
  interface only.
- All content lives behind a random access token. Use `--token` to pin one.
- `ffmpeg` only reads the screen and the audio monitor, and the server only
  serves files from `web/` and the runtime `.run/` directory.

## Third-party

`web/vendor/hls.min.js` is [hls.js](https://github.com/video-dev/hls.js)
(Apache-2.0). Its license is in `web/vendor/hls.min.js.LICENSE.txt`.

## License

MIT. See `LICENSE`.
