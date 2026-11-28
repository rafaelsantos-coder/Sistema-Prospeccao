"""Real PJSUA2 SIP/RTP regression on loopback, no carrier, OpenAI or credentials.

This tiny, deterministic SIP peer is deliberately test-only. CI runs it with
Docker --network none. No remote host, real telephone, or account is used.
Both call modes go through the production PjSession, actual UDP signaling,
native G.711 RTP and the native conference bridge.
"""
from __future__ import annotations

import asyncio
import math
import re
import select
import socket
import struct
import threading
import time
from contextlib import asynccontextmanager

import pjsua2 as pj
from types import SimpleNamespace

from app.services import lia_voz_call
from app.services.voice_pjsua2 import PjSession


def configure_isolated_native_stack():
    """Explicitly bind and advertise loopback in this isolated CI process.

    Docker --network none has no non-loopback interface or default gateway.
    Do not change production NAT, media, or SIP settings. Detailed native
    traces are enabled only here with synthetic numbers and synthetic audio.
    """
    original_init = pj.Endpoint.libInit
    original_transport = pj.TransportConfig
    original_account = pj.AccountConfig

    def init(self, cfg):
        cfg.logConfig.level = 5
        cfg.logConfig.consoleLevel = 5
        return original_init(self, cfg)

    def transport_config():
        cfg = original_transport()
        cfg.boundAddress = "127.0.0.1"
        cfg.publicAddress = "127.0.0.1"
        return cfg

    def account_config():
        cfg = original_account()
        cfg.mediaConfig.transportConfig.boundAddress = "127.0.0.1"
        cfg.mediaConfig.transportConfig.publicAddress = "127.0.0.1"
        return cfg

    pj.Endpoint.libInit = init
    pj.TransportConfig = transport_config
    pj.AccountConfig = account_config


class LoopbackPeer:
    def __init__(self):
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 0))
        # Use the standard even RTP / consecutive odd RTCP port pair.
        # Retry only local ephemeral binds, never a real telephone call.
        for _ in range(64):
            try:
                self.rtp.bind(("127.0.0.1", 0))
                port = self.rtp.getsockname()[1]
                if port % 2 or port >= 65535:
                    raise OSError("Need an even local RTP port")
                self.rtcp.bind(("127.0.0.1", port + 1))
                break
            except OSError:
                self.rtp.close()
                self.rtcp.close()
                self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            self.sip.close()
            self.rtp.close()
            self.rtcp.close()
            raise RuntimeError("Unable to allocate loopback RTP/RTCP pair")
        self.port = self.sip.getsockname()[1]
        self.calls = {}
        self.failure = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(2)
        for sock in (self.sip, self.rtp, self.rtcp):
            sock.close()
        if self.failure:
            raise RuntimeError("Loopback SIP peer failed") from self.failure

    @staticmethod
    def parse(raw):
        head, _, body = raw.decode("ascii", "replace").partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, colon, value = line.partition(":")
            if colon:
                headers.setdefault(key.lower(), []).append(value.strip())
        return lines[0], headers, body

    def response(self, headers, address, code, reason, *, body="", contact=False):
        to = headers["to"][0]
        if ";tag=" not in to:
            to += ";tag=loopback-peer"
        lines = [f"SIP/2.0 {code} {reason}"]
        lines.extend("Via: " + v for v in headers["via"])
        lines.extend(["From: " + headers["from"][0], "To: " + to,
                      "Call-ID: " + headers["call-id"][0], "CSeq: " + headers["cseq"][0]])
        if contact:
            lines.append(f"Contact: <sip:lab@127.0.0.1:{self.port}>")
        if code == 200 and headers["cseq"][0].endswith("REGISTER"):
            lines.extend("Contact: " + v for v in headers.get("contact", []))
            lines.append("Expires: " + headers.get("expires", ["120"])[0])
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append("Content-Length: " + str(len(body.encode("ascii"))))
        self.sip.sendto(("\r\n".join(lines) + "\r\n\r\n" + body).encode("ascii"), address)

    def invite(self, headers, address, offer, cid):
        if cid in self.calls:
            # Retransmitted INVITE, same dialog; don't count as a new call.
            call = self.calls[cid]
            code = 200 if call["confirmed"] else 183
            self.response(headers, address, code, "OK" if code == 200 else "Session Progress",
                          body=call["sdp"], contact=True)
            return
        match = re.search(r"^m=audio (\d+) RTP/AVP ([0-9 ]+)\r?$", offer, re.M)
        if not match or "0" not in match.group(2).split():
            raise RuntimeError("Expected G.711 PCMU in loopback offer")
        if "c=IN IP4 127.0.0.1" not in offer:
            raise RuntimeError("Unexpected advertised media address in loopback-only lab")
        # Preserve the number/order of offered streams; reject non-audio streams.
        media = []
        for line in offer.splitlines():
            if not line.startswith("m="):
                continue
            fields = line[2:].split()
            if fields[0] == "audio":
                media.extend([
                    f"m=audio {self.rtp.getsockname()[1]} RTP/AVP 0",
                    "a=rtpmap:0 PCMU/8000",
                    f"a=rtcp:{self.rtcp.getsockname()[1]} IN IP4 127.0.0.1",
                    "a=sendrecv", "a=ptime:20",
                ])
            else:
                media.append("m=" + fields[0] + " 0 " + " ".join(fields[2:]))
        sdp = "\r\n".join(["v=0", "o=lab 1 1 IN IP4 127.0.0.1", "s=Loopback",
                          "c=IN IP4 127.0.0.1", "t=0 0", *media, ""])
        self.calls[cid] = {
            "headers": headers, "address": address, "sdp": sdp,
            "media_offer": [l.split()[0] + " " + " ".join(l.split()[2:])
                            for l in offer.splitlines() if l.startswith("m=")],
            "rtp_target": ("127.0.0.1", int(match.group(1))),
            "answer_at": time.monotonic() + 0.8, "confirmed": False,
            "acked": False, "ended": False, "packets_rx": 0, "packets_tx": 0,
            "voice_packets_rx": 0,
            "next_rtp": 0.0, "seq": 0, "ts": 0,
        }
        self.response(headers, address, 183, "Session Progress", body=sdp, contact=True)

    def handle(self, raw, address):
        line, headers, body = self.parse(raw)
        if line.startswith("SIP/"):
            return
        method = line.split()[0]
        cid = headers.get("call-id", [""])[0]
        if method == "REGISTER":
            self.response(headers, address, 200, "OK")
        elif method == "INVITE":
            self.invite(headers, address, body, cid)
        elif method == "ACK":
            if cid in self.calls:
                self.calls[cid]["acked"] = True
        elif method == "BYE":
            self.response(headers, address, 200, "OK")
            if cid in self.calls:
                self.calls[cid]["ended"] = True
        elif method == "CANCEL":
            self.response(headers, address, 200, "OK")
            if cid in self.calls:
                call = self.calls[cid]
                self.response(call["headers"], address, 487, "Request Terminated")
                call["ended"] = True
        else:
            self.response(headers, address, 200, "OK")

    @staticmethod
    def mulaw_magnitude(value):
        u = (~value) & 0xff
        return (((u & 15) << 3) + 132) * (1 << ((u >> 4) & 7)) - 132

    def run(self):
        try:
            while not self.stop.is_set():
                readable, _, _ = select.select([self.sip, self.rtp, self.rtcp], [], [], 0.01)
                for sock in readable:
                    packet, address = sock.recvfrom(65535)
                    if sock is self.sip:
                        self.handle(packet, address)
                    elif sock is self.rtp and len(packet) >= 12 and packet[0] >> 6 == 2:
                        for call in self.calls.values():
                            if call["confirmed"] and call["acked"] and not call["ended"]:
                                call["packets_rx"] += 1
                                # Independently confirm non-silent G.711 PCMU payload,
                                # not merely RTP headers or silence/comfort-noise.
                                if packet[1] & 0x7f == 0:
                                    offset = 12 + (packet[0] & 0x0f) * 4
                                    if packet[0] & 0x10 and len(packet) >= offset + 4:
                                        words = struct.unpack_from("!H", packet, offset + 2)[0]
                                        offset += 4 + words * 4
                                    payload = packet[offset:]
                                    if sum(self.mulaw_magnitude(x) > 500 for x in payload) >= 20:
                                        call["voice_packets_rx"] += 1
                now = time.monotonic()
                for call in self.calls.values():
                    if call["ended"]:
                        continue
                    if not call["confirmed"] and now >= call["answer_at"]:
                        self.response(call["headers"], call["address"], 200, "OK",
                                      body=call["sdp"], contact=True)
                        call["confirmed"] = True
                    if call["confirmed"] and call["acked"] and now >= call["next_rtp"]:
                        # Synthetic non-silent PCMU, no captured human audio.
                        header = struct.pack("!BBHII", 0x80, 0, call["seq"],
                                             call["ts"], 0x12345678)
                        self.rtp.sendto(header + bytes([0x9F, 0xBF, 0x1F, 0x3F]) * 40,
                                        call["rtp_target"])
                        call["packets_tx"] += 1
                        call["seq"] = (call["seq"] + 1) & 65535
                        call["ts"] += 160
                        call["next_rtp"] = now + 0.02
        except Exception as exc:
            self.failure = exc
            self.stop.set()


class SyntheticRealtime:
    ready = True

    def __init__(self):
        self.received = 0
        self.non_silent = 0
        self.greeted = False
        self.sample_offset = 0

    async def greet(self):
        self.greeted = True

    async def receive_once(self):
        await asyncio.Future()

    async def append_pcm(self, pcm):
        self.received += 1
        if any(pcm):
            self.non_silent += 1

    def pull_pcm(self, size):
        # A 12 kHz alternating-sample signal is outside the 8 kHz
        # telephone codec's Nyquist band; the resampler correctly removes it.
        # Use speech-band tones with a changing amplitude instead. This tests
        # actual audible transport without disabling the production SIP VAD.
        samples = []
        for i in range(size // 2):
            t = (self.sample_offset + i) / 24000
            envelope = 0.55 + 0.4 * math.sin(2 * math.pi * 3 * t)
            samples.append(int(envelope * (
                9000 * math.sin(2 * math.pi * 650 * t)
                + 4000 * math.sin(2 * math.pi * 1100 * t)
            )))
        self.sample_offset += len(samples)
        return struct.pack("<" + "h" * len(samples), *samples)

    def clear_pcm(self):
        pass


def main():
    configure_isolated_native_stack()
    with LoopbackPeer() as peer:
        account = SimpleNamespace(
            username="lab", domain="127.0.0.1", sip_port=peer.port,
            transport="UDP", proxy_host="", proxy_port=5060,
            dial_domain="127.0.0.1", caller_id="", dial_prefix="",
            strip_brazil_ddi=False,
        )
        # Same PJSUA2 account setup and INVITE routine as production.
        with PjSession(account, "loopback-only") as driver:
            assert driver.wait_for_registration()[0] == 200
            probe = driver.perform_call(
                "5555999990000", mode="PROBE", dial_format="NATIONAL",
                timeout=40, probe_hold_seconds=1,
            )
            assert probe.answered and probe.sip_code == 200, probe.status
            assert probe.media_active, "SIP probe answered but negotiated no active media"
        fake = SyntheticRealtime()

        @asynccontextmanager
        async def factory(**_kwargs):
            assert driver._call.answered, "AI started before SIP answer"
            # Verify actual SIP processing continues through async handshake.
            await asyncio.sleep(0.2)
            yield fake

        # Keep the CI conversation short; production remains 90 seconds.
        original_timeout = lia_voz_call.CONVERSATION_TIMEOUT_SECONDS
        lia_voz_call.CONVERSATION_TIMEOUT_SECONDS = 8
        try:
            with PjSession(account, "loopback-only") as driver:
                assert driver.wait_for_registration()[0] == 200
                outcome = lia_voz_call.perform_lia_test(
                    driver, "5555999990000", None, "synthetic",
                    should_abort=lambda: False, session_factory=factory,
                    on_progress=lambda msg, _code: print(msg, flush=True),
                )
                if outcome.status != "LIA_FINALIZADA":
                    print("Loopback RTP counters:", [
                        (c["confirmed"], c["acked"], c["packets_rx"], c["packets_tx"])
                        for c in peer.calls.values()
                    ], flush=True)
                assert outcome.status == "LIA_FINALIZADA", outcome.message
        finally:
            lia_voz_call.CONVERSATION_TIMEOUT_SECONDS = original_timeout
        calls = list(peer.calls.values())
        assert len(calls) == 2, "Exactly one native INVITE per requested test"
        assert calls[0]["media_offer"] == calls[1]["media_offer"], "Different SDP media offers"
        assert all(c["acked"] and c["ended"] for c in calls), "SIP dialog not closed"
        print("Independent peer RTP counters:", [
            {"rx": c["packets_rx"], "tx": c["packets_tx"],
             "voice_rx": c["voice_packets_rx"]} for c in calls
        ], flush=True)
        assert calls[1]["packets_tx"] > 10 and calls[1]["packets_rx"] > 10
        assert calls[1]["voice_packets_rx"] > 10, "Peer received no audible G.711 samples"
        assert fake.greeted and fake.received > 10 and fake.non_silent > 0
        print("SIP LOOPBACK PASS: same INVITE/SDP, 183/200/ACK/BYE, bidirectional RTP/PCM")


if __name__ == "__main__":
    main()
