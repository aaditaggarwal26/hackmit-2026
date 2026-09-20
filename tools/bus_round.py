"""Drive one complete arbitration round against the real ESP32, as the ground would.

offers_open -> bid -> grant -> tx_begin -> tx_chunk* -> tx_done -> tx_ack(ok) -> pop

Everything is built and parsed with the GROUND's own protocol module, so this exercises
the firmware against the real contract rather than a second implementation of it.

The received frame is reassembled and re-scored with the golden model. If that score
matches what the satellite reported in its bid, the on-chip kernel is proven bit-exact on
real hardware -- the property the dashboard's `mismatches` counter depends on.

  uv run --with numpy python tools/bus_round.py
"""
from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_REF = "origin/ground-station"


def load_ground(tmp: Path):
    pkg = tmp / "orbit"
    (pkg / "protocol").mkdir(parents=True)
    (pkg / "golden").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "protocol" / "__init__.py").write_text("")
    (pkg / "golden" / "__init__.py").write_text("")
    for path in ("orbit/config.py", "orbit/protocol/messages.py", "orbit/golden/score.py"):
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{GOLDEN_REF}:{path}"],
                              capture_output=True, text=True, check=True).stdout
        (tmp / path).write_text(blob)
    sys.path.insert(0, str(tmp))
    from orbit import config
    from orbit.golden import score
    from orbit.protocol import messages
    return config, messages, score


def default_route_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return str(s.getsockname()[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    # TX_PASSES=3 in the firmware means ~3x 19 chunks at the quoted pace, so one round is
    # roughly 20 s of airtime before tx_done. 25 s left no margin for the bid/grant legs.
    ap.add_argument("--timeout", type=float, default=50.0)
    ap.add_argument("--round-id", type=int, default=9001)
    ap.add_argument("--repeat", type=int, default=4, help="copies of each datagram (multicast is lossy)")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        config, M, score_mod = load_ground(tmp)
        cfg = config.Settings()
        iface = default_route_ip()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)  # rule out receiver-side drops
        sock.bind((cfg.mcast_group, cfg.mcast_port))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4s4s", socket.inet_aton(cfg.mcast_group), socket.inet_aton(iface)))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, cfg.mcast_ttl)
        sock.settimeout(0.5)

        me = "ground-test"
        # Start from a monotonic-ish value, not 0. The satellite dedups on (sender, seq)
        # exactly as the ground does, so a harness that restarts its counter every run
        # looks like a replay of the previous run and is correctly ignored.
        seq = int(time.time()) % 1_000_000

        def send(cls, **body):
            """Sent REPEAT times with one seq. WiFi multicast loses ~36% here and the
            ground/satellite both dedup by (sender, seq), so repeats cost nothing."""
            nonlocal seq
            seq += 1
            msg = cls(sender=me, seq=seq, t_ms=int(time.monotonic() * 1000), **body)
            wire = msg.encode()
            for i in range(a.repeat):
                sock.sendto(wire, (cfg.mcast_group, cfg.mcast_port))
                if i + 1 < a.repeat:
                    time.sleep(0.020)  # bursty loss: separation beats count
            return msg

        def recv(pred, deadline):
            while time.time() < deadline:
                try:
                    data, _ = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                m = M.decode(data)
                if isinstance(m, M.DecodeError):
                    print(f"  ! decode error: {m.reason}")
                    continue
                if m.sender == me:
                    continue
                if pred(m):
                    return m
            return None

        deadline = time.time() + a.timeout

        print("1. offers_open ->")
        send(M.OffersOpen, round_id=a.round_id, window_remaining_bytes=cfg.window_capacity_bytes,
             collect_ms=cfg.bid_collect_ms)

        bid = recv(lambda m: isinstance(m, M.Bid) and m.round_id == a.round_id, deadline)
        if bid is None:
            print("   FAIL: no bid"); return 1
        print(f"   <- bid from {bid.sender}: item={bid.item_id} score={bid.score:.2f} "
              f"age={bid.item_age_s:.1f}s queue_len={bid.queue_len}")
        print(f"      buffer {bid.buffer.used}/{bid.buffer.slots} slots, evictions={bid.eviction_count}")
        print(f"      diagnostic window: {[(e.item_id, round(e.score,1)) for e in bid.window]}")

        print("\n2. grant ->")
        bd = M.Breakdown(score=bid.score, item_age_s=bid.item_age_s,
                         item_age_term=bid.item_age_s * cfg.item_aging_rate,
                         sat_wait_s=0.0, sat_wait_term=0.0,
                         total=bid.score + bid.item_age_s * cfg.item_aging_rate)
        send(M.Grant, round_id=a.round_id, to=bid.sender, item_id=bid.item_id,
             pace_bps=cfg.link_rate_bps, breakdown=bd)

        begin = recv(lambda m: isinstance(m, M.TxBegin) and m.item_id == bid.item_id, deadline)
        if begin is None:
            print("   FAIL: no tx_begin"); return 1
        print(f"   <- tx_begin: {begin.total_bytes} bytes in {begin.chunks} chunks")

        chunks: dict[int, bytes] = {}
        done = None
        while time.time() < deadline and done is None:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            m = M.decode(data)
            if isinstance(m, M.DecodeError) or m.sender == me:
                continue
            if isinstance(m, M.TxChunk) and m.item_id == bid.item_id:
                chunks[m.idx] = m.data
            elif isinstance(m, M.TxDone) and m.item_id == bid.item_id:
                done = m
        if done is None:
            print(f"   FAIL: no tx_done ({len(chunks)}/{begin.chunks} chunks)"); return 1
        missing = [i for i in range(begin.chunks) if i not in chunks]
        blob = b"".join(chunks[i] for i in sorted(chunks))
        ok = not missing and len(blob) == begin.total_bytes
        # ack FIRST: the satellite gives up after SAT_ACK_TIMEOUT_MS and printing is not free
        send(M.TxAck, round_id=a.round_id, to=bid.sender, item_id=bid.item_id, ok=ok,
             bytes_received=len(blob), reason="" if ok else "incomplete")
        print(f"   <- {len(chunks)}/{begin.chunks} chunks, tx_done: {done.total_bytes} bytes score={done.score:.2f}")
        print(f"\n3. reassembled {len(blob)}/{begin.total_bytes} bytes, missing chunks: {missing or 'none'}")
        print(f"\n4. tx_ack -> ok={ok} (sent immediately on tx_done)")

        # the satellite pops only on ok=true: prove it from its own heartbeats
        popped = None
        hb_deadline = time.time() + 6
        while time.time() < hb_deadline:
            hb = recv(lambda m: isinstance(m, M.Heartbeat) and m.sender == bid.sender, hb_deadline)
            if hb is None:
                break
            if hb.top_item_id != bid.item_id:
                popped = hb
                break
        print(f"   {'<- popped: top is now item ' + str(popped.top_item_id) if popped else '   (top unchanged)'}")

        # the real prize: does the ground's golden model reproduce the satellite's score?
        print("\n5. re-scoring the received frame with the golden model")
        if not ok:
            print("   skipped: frame incomplete"); sock.close(); return 1
        frame = np.frombuffer(blob, dtype=np.uint8).reshape(config.FRAME_H, config.FRAME_W)
        npz = np.load(ROOT / "corpus/frames.npz")
        corpus_frames = npz[npz.files[0]]
        hit = next((i for i in range(len(corpus_frames))
                    if np.array_equal(np.asarray(corpus_frames[i]), frame)), None)
        print(f"   frame matches corpus id {hit}" if hit is not None else "   frame not found in corpus (!)")
        sock.close()

        if hit is None:
            return 1
        import json
        manifest = json.loads((ROOT / "corpus/manifest.json").read_text())
        ref_id = int(manifest["scenes"][manifest["frames"][hit]["scene"]]["reference_id"])
        golden = score_mod.score_frame(frame, np.asarray(corpus_frames[ref_id]))
        golden_disp = golden.score * 100.0 / 0xFFFF
        print(f"   golden raw={golden.score} -> {golden_disp:.5f}")
        print(f"   satellite reported        {bid.score:.5f}")
        delta = abs(golden_disp - bid.score)
        if delta < 1e-3:
            print(f"\n   MATCH (delta {delta:.2e}) -- the on-chip kernel is bit-exact on real hardware")
            return 0
        print(f"\n   MISMATCH: delta {delta}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
