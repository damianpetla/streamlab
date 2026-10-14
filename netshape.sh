#!/bin/bash
# Shapes UDP leaving this Mac with dummynet. The Android emulator reaches the network through the
# host, so this is what degrades its traffic — Agora media is UDP and ignores HTTP proxies, and the
# emulator's own network profiles only inject loss while leaving bandwidth and latency untouched.
#
# Loopback is never matched, so adb keeps working. Only UDP and ICMP are shaped, so Gradle and
# adb-over-TCP are unaffected. ICMP rides the same pipes purely so that ping is a valid probe of
# what the media traffic is getting.
#
# The pipe is shared by every UDP flow on the host, so anything else talking steals bandwidth from
# the stream under test and the measurement quietly becomes meaningless. Local-discovery chatter is
# excluded below; a browser is not, because QUIC is indistinguishable from any other media flow —
# close it, and use "talkers" to confirm nothing else is competing.
set -euo pipefail

IFACE="${IFACE:-en0}"
ANCHOR="netshape"
TOKEN_FILE="${TMPDIR:-/tmp}/netshape.pfctl-token"

usage() {
    cat <<USAGE
usage: $(basename "$0") <profile|command>

profiles (bandwidth / one-way delay / loss):
  edge      240Kbit/s   250ms   0%     a genuinely narrow link
  3g          1Mbit/s   100ms   1%     usable but constrained
  lte         5Mbit/s    60ms 0.5%     room for the main layer, but no headroom
  5g         50Mbit/s    20ms   0%     a baseline: shaping on, nothing constrained
  lossy       5Mbit/s    10ms  20%     wide and fast but dropping packets

Queue depth is set per profile so an overshoot is dropped rather than buffered for seconds.
  custom <bw> <delay_ms> <plr> [queue] e.g. custom 500Kbit/s 80 0.05 12

commands:
  stop      remove all shaping
  status    show active pipes and rules
  talkers   show which processes are sending UDP right now

Delay is per direction, so 'edge' means a 500ms round trip.
USAGE
    exit 1
}

apply() {
    local bw=$1 delay=$2 plr=$3 queue=$4
    # A shallow queue drops once it is full instead of holding packets for seconds. Dummynet's
    # default of 50 slots is several seconds of buffering at these bitrates, which turns every
    # overshoot into a latency cliff the stream never climbs back from — real radios drop sooner.
    sudo dnctl pipe 1 config bw "$bw" delay "$delay" plr "$plr" queue "$queue"
    sudo dnctl pipe 2 config bw "$bw" delay "$delay" plr "$plr" queue "$queue"

    # The anchor has to exist in the main ruleset before rules can be loaded into it.
    {
        cat /etc/pf.conf
        echo "dummynet-anchor \"$ANCHOR\""
        echo "anchor \"$ANCHOR\""
    } | sudo pfctl -q -f -

    sudo pfctl -q -a "$ANCHOR" -f - <<RULES
dummynet out on $IFACE proto udp from any to any port { != 53, != 5353, != 137, != 138, != 1900 } pipe 1
dummynet in on $IFACE proto udp from any port { != 53, != 5353, != 137, != 138, != 1900 } to any pipe 2
dummynet out on $IFACE proto icmp all pipe 1
dummynet in on $IFACE proto icmp all pipe 2
RULES

    # -E hands back a reference token. Keeping it means stop can release just our reference
    # instead of disabling pf for whatever else on this machine is relying on it.
    sudo pfctl -E 2>&1 | awk '/Token/ {print $3}' > "$TOKEN_FILE"
    echo "shaping UDP on $IFACE — bw=$bw delay=${delay}ms each way loss=$plr queue=$queue"
}

# Removes only what apply created. A blanket `pfctl -d` plus `dnctl flush` would take a VPN,
# the host firewall, or someone else's pipes down with it.
stop() {
    sudo pfctl -a "$ANCHOR" -F all 2>/dev/null || true
    sudo pfctl -f /etc/pf.conf 2>/dev/null || true
    if [ -s "$TOKEN_FILE" ]; then
        sudo pfctl -X "$(cat "$TOKEN_FILE")" 2>/dev/null || true
        rm -f "$TOKEN_FILE"
    fi
    sudo dnctl -q pipe 1 delete 2>/dev/null || true
    sudo dnctl -q pipe 2 delete 2>/dev/null || true
    echo "shaping removed"
}

case "${1:-}" in
    edge)   apply 240Kbit/s 250 0 8 ;;
    3g)     apply 1Mbit/s 100 0.01 20 ;;
    lte)    apply 5Mbit/s 60 0.005 40 ;;
    5g)     apply 50Mbit/s 20 0 100 ;;
    lossy)  apply 5Mbit/s 10 0.2 50 ;;
    custom) [ $# -ge 4 ] || usage; apply "$2" "$3" "$4" "${5:-10}" ;;
    stop)   stop ;;
    status) sudo dnctl list; echo; sudo pfctl -a "$ANCHOR" -s all 2>/dev/null || echo "(anchor empty)" ;;
    talkers)
        echo "UDP per process over the next few seconds — anything busy here is competing with the stream:"
        # The first block nettop prints is cumulative since each process started; only the blocks
        # after it are per-interval, so everything before the last header is thrown away.
        nettop -m udp -d -l 3 -P -J bytes_in,bytes_out 2>/dev/null \
            | awk '/bytes_in/ { block = ""; next } { block = block $0 "\n" } END { printf "%s", block }' \
            | awk '$2 ~ /KiB|MiB/ || $3 ~ /KiB|MiB/' \
            | sort -k2 -hr | head -12
        ;;
    *)      usage ;;
esac
