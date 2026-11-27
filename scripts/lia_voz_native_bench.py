"""Local native PCM bridge bench, no SIP transport, API, number or credentials."""
import math
import struct
import time

import pjsua2 as pj

from app.services.lia_voz_media import FRAME_BYTES, MediaBridge, attach_pj_media, create_pj_port

SAMPLES = FRAME_BYTES // 2


class ToneSession:
    ready = True

    def __init__(self, hz):
        self.hz = hz
        self.offset = 0

    def pull_pcm(self, size):
        count = size // 2
        samples = [
            int(4000 * math.sin(2 * math.pi * self.hz * (self.offset + i) / 24000))
            for i in range(count)
        ]
        self.offset += count
        return struct.pack("<" + "h" * count, *samples)


def main():
    ep = pj.Endpoint()
    ep.libCreate()
    cfg = pj.EpConfig()
    cfg.uaConfig.threadCnt = 0
    cfg.uaConfig.mainThreadOnly = True
    cfg.logConfig.level = 2
    cfg.logConfig.consoleLevel = 0
    cfg.medConfig.clockRate = 8000
    ep.libInit(cfg)
    ep.libStart()
    ep.audDevManager().setNullDev()

    a = MediaBridge(ToneSession(400))
    b = MediaBridge(ToneSession(700))
    first = create_pj_port(pj, a)
    second = None
    seen_a = seen_b = 0
    try:
        with attach_pj_media(pj, first, b, enabled=True) as second:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                ep.libHandleEvents(10)
                for bridge, label in ((a, "a"), (b, "b")):
                    while (frame := bridge.next_input()) is not None:
                        if any(frame):
                            if label == "a":
                                seen_a += 1
                            else:
                                seen_b += 1
                if a.failure or b.failure:
                    raise RuntimeError("Native callback failed")
            if seen_a < 10 or seen_b < 10:
                raise RuntimeError("Native bridge did not deliver audio in both directions")
            print("Native PCM bridge: received non-silent frames", seen_a, seen_b)
    finally:
        a.close()
        b.close()
        second = None
        first = None
        ep.libDestroy()


if __name__ == "__main__":
    main()
