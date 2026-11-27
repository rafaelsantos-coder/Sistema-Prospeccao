"""One-shot Railway lab: native PCM bench and optional synthetic API probe.

No database, phone numbers, SIP accounts, customer audio, public endpoint,
or persistent transcripts. The image deliberately lacks the dialer worker.
"""
from __future__ import annotations

import asyncio
import os
import time

from app.services.lia_voz_realtime import MAX_INPUT_BYTES, LiaVoiceError, open_realtime


async def probe_realtime(api_key: str, *, session_factory=open_realtime) -> dict:
    started = time.monotonic()
    async with session_factory(enabled=True, api_key=api_key, max_seconds=35) as session:
        handshake_ms = round((time.monotonic() - started) * 1000)
        await session.greet()
        first_audio_ms = None
        samples = 0
        non_silent = False
        # No user voice is sent: a synthetic assistant greeting only.
        while True:
            event = await session.receive_once()
            # The lab is the sole consumer on the session event loop.
            while session.protocol.playback.pending:
                size = min(MAX_INPUT_BYTES, len(session.protocol.playback.pending))
                pcm = session.pull_pcm(size)
                samples += len(pcm) // 2
                non_silent = non_silent or any(pcm)
                if first_audio_ms is None:
                    first_audio_ms = round((time.monotonic() - started) * 1000)
            if event == "response.done":
                break
        if first_audio_ms is None or samples < 2400 or not non_silent:
            raise LiaVoiceError("Synthetic greeting did not return usable audio")
        return {
            "handshake_ms": handshake_ms,
            "first_audio_ms": first_audio_ms,
            "audio_samples": samples,
        }


def native_bench():
    from scripts.lia_voz_native_bench import main
    main()


def main() -> int:
    try:
        native_bench()
        print("LIA_VOZ_LAB: native_pcm=PASS", flush=True)
        if os.getenv("LIA_VOZ_LAB_REALTIME_ENABLED", "").lower() != "true":
            print("LIA_VOZ_LAB: realtime=SKIPPED (explicit opt-in required)", flush=True)
            return 0
        key = os.getenv("LIA_VOZ_OPENAI_API_KEY", "").strip()
        if not key:
            print("LIA_VOZ_LAB: realtime=BLOCKED (test credential missing)", flush=True)
            return 2
        result = asyncio.run(probe_realtime(key))
        print("LIA_VOZ_LAB: realtime=PASS", result, flush=True)
        return 0
    except Exception as exc:
        # Never log raw API responses, transcripts or credentials.
        print("LIA_VOZ_LAB: FAIL", type(exc).__name__, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
