#!/usr/bin/env python3
"""Browser bench for testing live video under bad network conditions.

Applies a network profile to the emulator and shows, on one timeline, what the viewer is actually
receiving — so a layer switch can be read against the network change that preceded it rather than
off a screen recording. Reads the probe line the app emits per stats sample (see README.md).

Run it, open the URL it prints (port 8770 upwards), pick a profile.
"""
import json
import os
import secrets
import signal
import re
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8770"))
HERE = Path(__file__).resolve().parent
NETSHAPE = HERE / "netshape.sh"
# io.agora.rtc2.Constants.REMOTE_VIDEO_STATE_*
REMOTE_VIDEO_STATE = {0: "stopped", 1: "starting", 2: "decoding", 3: "frozen", 4: "failed"}
PROFILES = ["edge", "3g", "lte", "5g", "lossy", "off"]

REC_DIR = Path(os.environ.get("STREAMLAB_HOME", Path.home() / ".streamlab")) / "recordings"

# Logcat tags read from each device, comma-separated. The probe line and the lifecycle lines are
# recognised by their content, so the tags only decide which lines reach the parser.
VIEWER_TAGS = os.environ.get("VIEWER_TAGS", "StreamProbe,StreamEvents").split(",")
PUBLISHER_TAGS = os.environ.get("PUBLISHER_TAGS", "StreamerProbe,StreamEvents").split(",")


def logcat_filter(tags: list[str]) -> list[str]:
    return [f"{tag.strip()}:D" for tag in tags if tag.strip()] + ["*:S"]

state = {"profile": "none", "device": None, "seq": 0}
# A recording is just the same event stream written to a file, so anything visible on the timeline
# can be handed to someone else by name afterwards.
recording: dict = {"id": None, "file": None, "started": 0.0, "count": 0}
rec_lock = threading.Lock()
clients: list[queue.Queue] = []
# One lock over numbering, history and fan-out. Registering a client and snapshotting history
# have to happen under it too: an event landing between those two steps would reach the client
# once from the snapshot and once from its queue, which is the duplication the sequence exists
# to prevent. It also keeps two reader threads from taking the same number, which would make a
# reconnect drop one of the events sharing it.
events_lock = threading.RLock()
history: list[dict] = []

PUB_PROBE = re.compile(
    r"room=(?P<room>\S+) w=(?P<w>-?\d+) h=(?P<h>-?\d+) fps=(?P<fps>-?\d+) enc=(?P<enc>-?\d+) "
    r"sent=(?P<sent>-?\d+) target=(?P<target>-?\d+) adapt=(?P<adapt>\w+) hw=(?P<hw>\w+) "
    r"dual=(?P<dual>\w+) txloss=(?P<txloss>-?\d+) link=(?P<link>\w+)"
)
PROBE = re.compile(
    r"room=(?P<room>\S+) surface=(?P<surface>\S+) layer=(?P<layer>\S+) w=(?P<w>-?\d+) h=(?P<h>-?\d+) fps=(?P<fps>-?\d+) "
    r"kbps=(?P<kbps>-?\d+) loss=(?P<loss>-?\d+) e2e=(?P<e2e>-?\d+) frozen=(?P<frozen>-?\d+)"
    r"(?: weak=(?P<weak>\w+) link=(?P<link>\w+))?"
)
# Host lifecycle. A run that ends in a frozen picture has to say whether the SDK reported
# anything at all — a host leaving, a camera muted, a remote video state — or whether the
# stats simply stopped with nothing said.
HOST = re.compile(r"(onRemoteUserJoined|onRemoteUserLeft|onRemoteVideoMuted) (?P<detail>.*)$")
VIDEO_STATE = re.compile(r"onRemoteVideoStateChanged uid=\S+ state=(?P<state>\d+) reason=(?P<reason>\d+)")
# The whole logcat stamp, milliseconds included: two readers feed this timeline and
# second precision cannot order their samples against each other.
TIME = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)")
TIME_FORMAT = "%m-%d %H:%M:%S.%f"


def host_now() -> str:
    """Now, in the same shape and clock as every stamped event, so the two sort together."""
    return datetime.now().strftime(TIME_FORMAT)


def broadcast(event: dict) -> None:
    event["profile"] = state["profile"]
    # EventSource reconnects on its own and asks to resume from the last id it saw. Without a
    # sequence the replay would append the same samples again and skew every chart and summary.
    with events_lock:
        state["seq"] += 1
        event["seq"] = state["seq"]
        history.append(event)
        del history[:-600]
        for q in list(clients):
            q.put(event)
    with rec_lock:
        if recording["file"]:
            recording["file"].write(json.dumps(event) + "\n")
            recording["file"].flush()
            if event.get("kind") == "sample":
                recording["count"] += 1


def pick_device() -> str:
    if os.environ.get("DEVICE"):
        return os.environ["DEVICE"]
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devices = [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]
    if not devices:
        sys.exit("no adb device found")
    emulators = [d for d in devices if d.startswith("emulator")]
    return emulators[0] if emulators else devices[0]


def pick_publisher(viewer: str) -> str | None:
    """Whatever else is attached — the broadcaster is the device that is not the viewer."""
    if os.environ.get("PUBLISHER"):
        return os.environ["PUBLISHER"]
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    others = [l.split()[0] for l in out.splitlines()[1:]
              if l.strip().endswith("device") and l.split()[0] != viewer]
    return others[0] if others else None


def clock_offset(device: str) -> float:
    """Seconds to add to this device's logcat timestamps to put them on the host's clock.

    The two devices disagree by whole seconds — measured between one and four across runs — so a
    timeline built from their raw stamps interleaves wrongly no matter how it is sorted. Sorting
    only helps once both sides are expressed in the same clock.
    """
    out = subprocess.run(["adb", "-s", device, "shell", "date", "+%s.%N"],
                         capture_output=True, text=True).stdout.strip()
    try:
        return time.time() - float(out)
    except ValueError:
        return 0.0


def stamp(line: str, offset: float) -> str:
    """The logcat timestamp shifted onto the host clock, or the host's own time if unparsable."""
    match = TIME.match(line)
    if not match:
        return host_now()
    try:
        seen = datetime.strptime(f"{datetime.now().year}-{match.group(1)}", "%Y-" + TIME_FORMAT)
    except ValueError:
        return host_now()
    return (seen + timedelta(seconds=offset)).strftime(TIME_FORMAT)


def read_publisher(device: str) -> None:
    """The broadcaster's own numbers, so a collapse can be blamed on the right side of the link."""
    offset = clock_offset(device)
    while True:
        proc = subprocess.Popen(
            ["adb", "-s", device, "logcat", "-v", "time", *logcat_filter(PUBLISHER_TAGS)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        for line in proc.stdout:
            at = stamp(line, offset)
            probe = PUB_PROBE.search(line)
            if probe:
                fields = probe.groupdict()
                event = {k: int(v) for k, v in fields.items()
                         if k not in ("room", "adapt", "hw", "dual", "link")}
                event.update(
                    kind="pub", at=at, room=fields["room"], adapt=fields["adapt"],
                    link=fields["link"], hw=(fields["hw"] == "true"),
                    dual=(fields["dual"] == "true"),
                )
                broadcast(event)
            elif "onLinkQuality" in line:
                quality = re.search(r"txQuality=(\d+)", line)
                if quality:
                    broadcast({"kind": "pub_link", "at": at, "tx": int(quality.group(1))})
        proc.wait()
        time.sleep(2)


def watch_udp_contention() -> None:
    """Warns when something other than the emulator is using the shaped pipe.

    Every UDP flow on this host shares one dummynet pipe, so a music stream or a browser quietly
    takes a share of the bandwidth the test thinks it is giving the viewer — and the run looks like
    the video stack failing. Cheap to detect, expensive to miss.
    """
    while True:
        time.sleep(12)
        if state["profile"] in ("none", "off"):
            continue
        try:
            out = subprocess.run(
                ["nettop", "-m", "udp", "-d", "-l", "3", "-P", "-J", "bytes_in,bytes_out"],
                capture_output=True, text=True, timeout=30,
            ).stdout
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        # nettop's first block is cumulative since process start; only the later ones are rates.
        blocks = out.split("bytes_in")
        noisy = []
        for line in blocks[-1].splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[0].startswith("netsim") or parts[0].startswith("qemu"):
                continue
            if any(unit in parts[1] or unit in parts[2] for unit in ("KiB", "MiB")):
                noisy.append(parts[0].rsplit(".", 1)[0])
        if noisy:
            broadcast({
                "kind": "note", "at": host_now(),
                "text": f"competing for the shaped pipe: {', '.join(sorted(set(noisy))[:4])}",
            })


def read_logcat(device: str) -> None:
    """One reader thread; restarts itself if the device drops so a reinstall does not end the run."""
    offset = clock_offset(device)
    while True:
        proc = subprocess.Popen(
            ["adb", "-s", device, "logcat", "-v", "time", *logcat_filter(VIEWER_TAGS)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        for line in proc.stdout:
            at = stamp(line, offset)
            probe = PROBE.search(line)
            if probe:
                fields = probe.groupdict()
                sample = {k: int(v) for k, v in fields.items()
                          if k not in ("room", "surface", "layer", "weak", "link")}
                sample.update(
                    kind="sample", at=at, room=fields["room"], surface=fields["surface"],
                    layer=fields["layer"], weak=(fields.get("weak") == "true"),
                    link=fields.get("link") or "none",
                )
                broadcast(sample)
                continue
            # Layer changes need no event of their own: the layer each sample arrived on is
            # already in the probe line.
            if "onConnectionStatus status=Reconnecting" in line:
                broadcast({"kind": "reconnect", "at": at})
                continue
            video_state = VIDEO_STATE.search(line)
            host = HOST.search(line)
            if video_state:
                broadcast({
                    "kind": "video_state", "at": at,
                    "state": REMOTE_VIDEO_STATE.get(int(video_state.group("state")), video_state.group("state")),
                    "reason": int(video_state.group("reason")),
                })
            elif host:
                broadcast({"kind": "host", "at": at, "event": host.group(1),
                           "detail": host.group("detail").strip()})
        proc.wait()
        broadcast({"kind": "note", "at": host_now(), "text": "logcat dropped, retrying"})
        time.sleep(2)


def apply_profile(name: str) -> tuple[bool, str]:
    if name not in PROFILES:
        return False, f"unknown profile {name}"
    args = ["sudo", "-n", str(NETSHAPE), "stop" if name == "off" else name]
    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        hint = detail[-1] if detail else "failed"
        if "password" in hint.lower() or "sudo" in hint.lower():
            hint = "sudo credential expired — run 'sudo -v' in a terminal and try again"
        return False, hint
    state["profile"] = name
    broadcast({"kind": "profile", "at": host_now(), "name": name})
    return True, ""


def rec_start() -> dict:
    with rec_lock:
        if recording["file"]:
            return {"ok": False, "error": f"already recording {recording['id']}"}
        REC_DIR.mkdir(parents=True, exist_ok=True)
        rec_id = f"{time.strftime('%m%d-%H%M')}-{secrets.token_hex(2)[:3]}"
        recording.update(
            id=rec_id,
            file=(REC_DIR / f"{rec_id}.jsonl").open("w"),
            started=time.time(),
            count=0,
        )
    broadcast({"kind": "note", "at": host_now(), "text": f"recording {rec_id} started"})
    return {"ok": True, "id": rec_id}


def rec_stop() -> dict:
    with rec_lock:
        if not recording["file"]:
            return {"ok": False, "error": "not recording"}
        rec_id = recording["id"]
        seconds = int(time.time() - recording["started"])
        samples = recording["count"]
        recording["file"].close()
        recording.update(id=None, file=None, started=0.0, count=0)
    return {
        "ok": True, "id": rec_id, "seconds": seconds, "samples": samples,
        "path": str(REC_DIR / f"{rec_id}.jsonl"), "summary": summarise(rec_id),
    }


def short_room(room: str) -> str:
    """Enough of the room id to read in a summary row. For display only — grouping uses the id."""
    return room[:8]


def summarise(rec_id: str) -> list[dict]:
    """Mean of each measurement per room, surface, network profile and layer.

    Both the room and the surface belong in the key. A grid renders many tiles at once,
    each its own connection to its own room, and every one of them reports the same
    surface name — averaging those together would describe a stream nobody watched.
    """
    path = REC_DIR / f"{rec_id}.jsonl"
    if not path.exists():
        return []
    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("kind") == "sample":
            key = (event.get("room") or "?", event.get("surface", "?"),
                   event.get("profile", "none"), event["layer"])
            groups.setdefault(key, []).append(event)
    rows = []
    for (room, surface, profile, layer), events in sorted(groups.items()):
        mean = lambda k: sum(e[k] for e in events) / len(events)
        rows.append({
            "room": short_room(room), "surface": surface, "profile": profile, "layer": layer,
            "samples": len(events),
            "fps": round(mean("fps"), 1), "kbps": round(mean("kbps")),
            "loss": round(mean("loss"), 1), "e2e": round(mean("e2e")),
        })
    return rows


def rec_list() -> list[dict]:
    if not REC_DIR.exists():
        return []
    files = sorted(REC_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"id": f.stem, "kb": round(f.stat().st_size / 1024)} for f in files[:12]]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console for the run itself
        pass

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/recordings":
            self.json_response({"recording": recording["id"], "items": rec_list()})
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            last_seen = int(self.headers.get("Last-Event-ID") or 0)
            with events_lock:
                backlog = [e for e in history[-200:] if e.get("seq", 0) > last_seen]
                clients.append(q)
            try:
                for event in backlog:
                    self.wfile.write(
                        f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
                self.wfile.flush()
                while True:
                    try:
                        event = q.get(timeout=15)
                        self.wfile.write(
                            f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n".encode()
                        )
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with events_lock:
                    if q in clients:
                        clients.remove(q)
        else:
            self.send_error(404)

    def json_response(self, payload: dict | list) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/rec/start":
            return self.json_response(rec_start())
        if self.path == "/rec/stop":
            return self.json_response(rec_stop())
        if self.path.startswith("/profile/"):
            ok, error = apply_profile(self.path.rsplit("/", 1)[-1])
            return self.json_response({"ok": ok, "error": error})
        self.send_error(404)


PAGE = (HERE / "streamlab.html").read_text() if (HERE / "streamlab.html").exists() else "<h1>missing streamlab.html</h1>"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        wanted = sys.argv[1]
        target = REC_DIR / f"{wanted}.jsonl"
        if not target.exists():
            sys.exit(f"no recording {wanted} in {REC_DIR}")
        print(json.dumps({"id": wanted, "summary": summarise(wanted)}, indent=2))
        sys.exit(0)

    if not shutil.which("adb"):
        sys.exit("adb not on PATH")
    # Cache the credential now so switching a profile mid-run never stalls on a hidden prompt.
    if subprocess.run(["sudo", "-v"]).returncode != 0:
        sys.exit("sudo is needed to shape the network")
    state["device"] = pick_device()
    threading.Thread(target=read_logcat, args=(state["device"],), daemon=True).start()
    threading.Thread(target=watch_udp_contention, daemon=True).start()
    publisher = pick_publisher(state["device"])
    if publisher:
        threading.Thread(target=read_publisher, args=(publisher,), daemon=True).start()
    # Walk to the next free port rather than dying: unrelated dev servers sit on these for days,
    # and the exact port does not matter to anything here.
    server = None
    for port in range(PORT, PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"no free port in {PORT}-{PORT + 19}")

    print(f"streamlab  viewer={state['device']}  publisher={publisher or 'none attached'}"
          f"  http://localhost:{server.server_address[1]}")
    # Leaving the machine shaped after a crash or a SIGTERM is the worst failure this tool has:
    # the network stays broken and nothing on screen says why.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nstopping, removing shaping")
        subprocess.run(["sudo", "-n", str(NETSHAPE), "stop"], capture_output=True)
