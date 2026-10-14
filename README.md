# streamlab

A small macOS test bench for live video under bad network conditions. It shapes the network
the Android emulator uses, reads one stats line per sample from the app's logcat (viewer and,
optionally, publisher), and puts everything on one timeline in the browser, so a quality change
can be read against the network change that caused it. You can record runs and summarise them
later by id.

- `netshape.sh` shapes UDP and ICMP on the Mac with `dnctl` (dummynet) and `pfctl`. Loopback and
  TCP are left alone, so `adb` and Gradle keep working.
- `streamlab.py` is a dependency-free Python server. It tails `adb logcat`, parses the probe lines
  and streams them to the page over Server-Sent Events. It also writes recordings as JSONL.
- `streamlab.html` is the UI: profile buttons, live tiles, a chart with a dashed line at every
  profile change, a timeline, recordings and a per-room/surface/profile/layer summary.

## Requirements

- macOS (uses `dnctl`, `pfctl`, `nettop`) and `sudo`
- Python 3.10+
- `adb` on `PATH`
- An Android emulator as the **viewer**. Its traffic leaves through the Mac, so shaping the Mac
  shapes the emulator.
- Optional: a physical phone over USB as the **publisher**. It is on its own network and is not
  shaped; its line only shows what the sender was doing.

Only the emulator is shaped. Every UDP flow on the Mac shares the shaped pipe, so close music
apps, browsers (QUIC) and VPNs. The tool warns on the timeline when another process is
competing for the pipe, and `./netshape.sh talkers` lists them.

## What the app must log

The tool knows nothing about your app beyond these lines. Use any tag; tell the tool which tags
to read (see below). Extra text before the fields is fine because lines are matched by content.

**Viewer, one line per remote video stats sample** (about every 2 s):

```
room=<id> surface=<name> layer=<H|L|…> w=<int> h=<int> fps=<int> kbps=<int> loss=<int> e2e=<int> frozen=<int> weak=<bool> link=<word>
```

`weak` and `link` are optional: your app's own "bad network" verdict, shown next to the raw
numbers. `surface` is a free label for where the video is rendered (e.g. `fullscreen`,
`preview`). Summaries group by `room × surface × profile × layer`.

**Publisher, one line per local video stats sample** (optional):

```
room=<id> w=<int> h=<int> fps=<int> enc=<int> sent=<int> target=<int> adapt=<NONE|UP|DOWN> hw=<bool> dual=<bool> txloss=<int> link=<quality>
```

`link` is the uplink quality (`EXCELLENT`, `GOOD`, `POOR`, `BAD`, `VERY_BAD`, `DOWN`, `UNKNOWN`).

**Lifecycle lines (optional), anywhere in the line:**

```
onRemoteVideoStateChanged uid=<uid> state=<int> reason=<int>
onRemoteUserJoined <anything>
onRemoteUserLeft <anything>
onRemoteVideoMuted <anything>
onConnectionStatus status=Reconnecting
```

Example with the Agora Android SDK 4.x:

```kotlin
override fun onRemoteVideoStats(stats: IRtcEngineEventHandler.RemoteVideoStats) {
    val layer = if (stats.rxStreamType == Constants.VIDEO_STREAM_LOW) "L" else "H"
    Log.d(
        "StreamProbe",
        "room=$channelId surface=fullscreen layer=$layer w=${stats.width} h=${stats.height} " +
            "fps=${stats.decoderOutputFrameRate} kbps=${stats.receivedBitrate} " +
            "loss=${stats.packetLossRate} e2e=${stats.e2eDelay} frozen=${stats.frozenRate}",
    )
}

override fun onLocalVideoStats(
    source: Constants.VideoSourceType,
    stats: IRtcEngineEventHandler.LocalVideoStats,
) {
    val adapt = when (stats.qualityAdaptIndication) {
        Constants.ADAPT_UP_BANDWIDTH -> "UP"
        Constants.ADAPT_DOWN_BANDWIDTH -> "DOWN"
        else -> "NONE"
    }
    Log.d(
        "StreamerProbe",
        "room=$channelId w=${stats.encodedFrameWidth} h=${stats.encodedFrameHeight} " +
            "fps=${stats.encoderOutputFrameRate} enc=${stats.encodedBitrate} sent=${stats.sentBitrate} " +
            "target=${stats.targetBitrate} adapt=$adapt hw=${stats.hwEncoderAccelerating > 0} " +
            "dual=${stats.dualStreamEnabled} txloss=${stats.txPacketLossRate} link=$lastTxQuality",
    )
}

override fun onRemoteVideoStateChanged(uid: Int, state: Int, reason: Int, elapsed: Int) {
    Log.d("StreamEvents", "onRemoteVideoStateChanged uid=$uid state=$state reason=$reason")
}
```

`lastTxQuality` is the latest `txQuality` from `onNetworkQuality` for your own uid (`uid == 0`),
mapped to the names above. Keep these logs out of release builds.

## Run

```bash
sudo -v                 # cache sudo so a profile switch never waits on a hidden prompt
./streamlab.py          # prints http://localhost:8770 (or the next free port)
```

Open the URL, start the stream on the publisher, open it on the emulator, pick a profile and press
**record**. Stopping gives you an id; summarise it later with:

```bash
./streamlab.py <recording-id>
```

Shaping is removed when the tool exits. If something goes wrong, run `sudo ./netshape.sh stop`.

Configuration (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `VIEWER_TAGS` | `StreamProbe,StreamEvents` | logcat tags read from the viewer |
| `PUBLISHER_TAGS` | `StreamerProbe,StreamEvents` | logcat tags read from the publisher |
| `DEVICE` | first emulator | adb serial of the viewer |
| `PUBLISHER` | the other attached device | adb serial of the publisher |
| `PORT` | `8770` | first port to try |
| `STREAMLAB_HOME` | `~/.streamlab` | where recordings are stored |
| `IFACE` | `en0` | interface `netshape.sh` shapes |

## Recordings and working with an AI agent

A recording is the same event stream the page shows, written to
`~/.streamlab/recordings/<id>.jsonl`, one JSON object per line. Every event has a `kind`, a
host-clock `at` timestamp and the network `profile` active at that moment:

```json
{"kind": "profile", "at": "18:00:14", "name": "3g", "profile": "3g"}
{"kind": "sample", "at": "18:00:16", "layer": "H", "w": 720, "h": 1280, "fps": 1, "kbps": 914, "loss": 0, "e2e": 810, "frozen": 5, "profile": "3g"}
{"kind": "sample", "at": "18:00:18", "layer": "L", "w": 272, "h": 480, "fps": 10, "kbps": 220, "loss": 0, "e2e": 450, "frozen": 1, "profile": "3g"}
```

Other kinds are `note`, `host`, `video_state`, `reconnect` and the publisher's samples. Because
the format is plain and self-describing, a recording is easy to hand to an AI coding agent:

1. Press **record** and do something physical: switch the profile, background the publisher app,
   mute the camera.
2. Stop the recording and copy the id, e.g. `0915-1736-668`.
3. Ask the agent to read `~/.streamlab/recordings/0915-1736-668.jsonl` and tell you what happened
   and when.

You watch the chart while the run happens; the agent reads every sample afterwards and compares
runs with each other. Logging the app's own verdict (`weak`, `link`) next to the raw numbers lets
it replay a different rule against the same recording.

## Network profiles

Delay is one-way, so the round trip is twice that. Each profile has a shallow queue on purpose.
Dummynet's default queue is seconds of buffering at these rates and would turn every overshoot
into a latency cliff.

| Profile | Bandwidth | Delay | Loss | Queue | Meant as |
|---|---|---|---|---|---|
| `edge` | 240 Kbit/s | 250 ms | 0% | 8 | a narrow link |
| `3g` | 1 Mbit/s | 100 ms | 1% | 20 | usable but constrained |
| `lte` | 5 Mbit/s | 60 ms | 0.5% | 40 | room for the main layer, no headroom |
| `5g` | 50 Mbit/s | 20 ms | 0% | 100 | baseline: shaping on, nothing constrained |
| `lossy` | 5 Mbit/s | 10 ms | 20% | 50 | wide and fast, but dropping packets |

`edge` and `lossy` separate narrow from lossy on purpose. Use
`sudo ./netshape.sh custom 500Kbit/s 80 0.05 12` for anything else.

These are lab profiles. Dummynet has no jitter and no cell handovers, so use the numbers to
compare builds, not to predict production.

## License

MIT, see [LICENSE](LICENSE).
