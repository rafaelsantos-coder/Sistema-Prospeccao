"""Lia Voz: one native dialer; connect AI only to an answered call."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.services.lia_voz_media import MediaBridge, attach_pj_media, run_media_bridge
from app.services.lia_voz_realtime import LiaVoiceError, open_realtime, safe_realtime_failure
from app.services.voice_pjsua2 import VoiceCallResult

RING_TIMEOUT_SECONDS = 40
CONVERSATION_TIMEOUT_SECONDS = 90


@dataclass(frozen=True)
class PilotOutcome:
    status: str
    sip_code: int | None
    message: str


def _route_options(prefix):
    if prefix is None:
        return {"dial_format": "NATIONAL"}
    return {"native_techprefix": prefix, "native_local_ddd": "55"}


def _active_audio_index(call, pj):
    """Keep SWIG's native owner alive while reading its borrowed media vector.

    Call.getInfo().media may refer to a member of a temporary C++ CallInfo.
    Return only the copied stream index, never a borrowed SWIG object.
    """
    info = call.getInfo()
    for index, media in enumerate(info.media):
        if (media.type == pj.PJMEDIA_TYPE_AUDIO
                and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE):
            return int(getattr(media, "index", index))
    return None


def _rtp_stats(call, pj):
    """Read actual network RTP counters, not local PCM callback counters."""
    if call.disconnected:
        return None
    try:
        index = _active_audio_index(call, pj)
        if index is not None:
            # The RTCP stats are also nested members of a native owner.
            stats = call.getStreamStat(index)
            rtcp = stats.rtcp
            return int(rtcp.rxStat.pkt), int(rtcp.txStat.pkt)
    except Exception:
        pass
    return None


async def _converse_connected(driver, call, api_key, *, should_abort,
                              session_factory, on_progress):
    """Same native owner thread; process SIP during handshake and voice."""
    pj = driver.pj
    bridge = None
    realtime = None
    last_rtp = None
    stage = "conexao Realtime apos atendimento"
    deadline = (call.confirmed_at if call.confirmed_at is not None else time.monotonic()
                ) + CONVERSATION_TIMEOUT_SECONDS

    def sample_rtp():
        nonlocal last_rtp
        current = _rtp_stats(call, pj)
        if current is not None:
            last_rtp = current

    async def pump_sip():
        next_guard = next_sample = 0.0
        while True:
            now = time.monotonic()
            if now >= next_sample:
                sample_rtp()
                next_sample = now + 0.25
            driver.pump(10)
            call.flush_trace()
            now = time.monotonic()
            if call.disconnected:
                return "remote"
            if call.callback_failed:
                return "callback"
            if now >= deadline:
                return "timeout"
            if now >= next_guard:
                if should_abort():
                    return "abort"
                next_guard = now + 1.0
            await asyncio.sleep(0.01)

    async def converse():
        nonlocal bridge, realtime, stage
        on_progress("SIP 200 confirmado; conectando Realtime.", call.sip_code)
        async with session_factory(enabled=True, api_key=api_key, max_seconds=105) as session:
            realtime = session
            if call.disconnected or should_abort():
                return "remote" if call.disconnected else "abort"
            if not session.ready:
                raise LiaVoiceError("Realtime session not ready")
            stage = "negociacao RTP"
            media_deadline = min(deadline, time.monotonic() + 12)
            while not call.disconnected:
                index = _active_audio_index(call, pj)
                if index is not None:
                    bridge = MediaBridge(session)
                    stage = "ponte RTP"
                    with attach_pj_media(
                        pj, call.getAudioMedia(index),
                        bridge, enabled=True, remote_ended=lambda: call.disconnected,
                    ):
                        stage = "transporte Realtime"
                        on_progress("Ponte RTP conectada apos atendimento; iniciando voz.", call.sip_code)
                        await run_media_bridge(session, bridge)
                    return "bridge_ended"
                if time.monotonic() >= media_deadline:
                    return "no_media"
                await asyncio.sleep(0.02)
            return "remote"

    # Both tasks execute on the original PJSUA2 thread. No asyncio.to_thread
    # and no blocking network handshake before the SIP dialog is confirmed.
    pump_task = asyncio.create_task(pump_sip())
    voice_task = asyncio.create_task(converse())
    stop = "failure"
    failure = None
    try:
        done, _ = await asyncio.wait((pump_task, voice_task), return_when=asyncio.FIRST_COMPLETED)
        if voice_task in done and not voice_task.cancelled() and voice_task.exception() is not None:
            failure = safe_realtime_failure(voice_task.exception())
        elif pump_task in done:
            stop = pump_task.result()
        else:
            stop = voice_task.result()
    except Exception as exc:
        failure = safe_realtime_failure(exc)
    finally:
        sample_rtp()
        # Remove the conference links before the shared dialer hangs up.
        # Continue pumping while an ordinary WebSocket close is in flight.
        voice_task.cancel()
        voice_results = await asyncio.gather(voice_task, return_exceptions=True)
        if isinstance(voice_results[0], Exception):
            failure = safe_realtime_failure(voice_results[0])
        pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)
        if bridge is not None:
            bridge.close()

    counters = bridge.snapshot() if bridge is not None else {}
    rx = counters.get("received_frames", 0)
    tx = counters.get("non_silent_rendered_frames", 0)
    rtp = "?" if last_rtp is None else str(last_rtp[0]) + "/" + str(last_rtp[1])
    diagnostic = ("SIP " + "/".join(map(str, call.sip_codes or [call.sip_code]))
                  + "; RTP pacotes RX/TX=" + rtp
                  + "; PCM RX/TX voz=" + str(rx) + "/" + str(tx) + ".")
    if realtime is not None and callable(getattr(realtime, "quality_snapshot", None)):
        q = realtime.quality_snapshot()
        on_progress(
            "Qualidade voz: fala inicio/fim=" + str(q["speech_starts"]) + "/" + str(q["speech_ends"])
            + "; interrupcoes=" + str(q["interruptions"])
            + "; respostas=" + str(q["audio_responses"])
            + "; geracao ms media/max=" + str(q["generation_avg_ms"]) + "/" + str(q["generation_max_ms"])
            + "; amostras=" + str(q["generation_samples"])
            + "; recuperacoes buffer=" + str(q.get("buffer_recoveries", 0))
            + "; turnos limitados=" + str(q.get("token_limited_responses", 0))
            + "; pico fila ms=" + str(q.get("peak_pending_ms", 0)) + ".",
            call.sip_code,
        )
    exchanged = bool(last_rtp and min(last_rtp) > 0 and rx > 0 and tx > 0)
    if failure:
        status, message = "LIA_FALHOU", "Falha: " + stage + " [" + failure + "]."
    elif stop == "abort":
        status, message = "LIA_INTERROMPIDA", "Teste interrompido por seguranca."
    elif stop in {"remote", "timeout"} and exchanged:
        status, message = "LIA_FINALIZADA", "Audio RTP e PCM validados; confirmar conversa no aparelho."
    elif stop == "no_media":
        status, message = "LIA_FALHOU", "SIP confirmou sem midia RTP ativa."
    elif stop in {"remote", "timeout"}:
        status, message = "LIA_FALHOU", "Encerrado sem audio RTP/PCM bidirecional comprovado."
    else:
        status, message = "LIA_FALHOU", "Falha na conversacao (" + stop + ")."
    on_progress((message + " " + diagnostic)[:230], call.sip_code)
    return VoiceCallResult(status, call.sip_code, False, (message + " " + diagnostic)[:230],
                           True, bool(call.media_active))


def perform_lia_test(driver, phone, prefix, api_key, *,
                     should_abort, session_factory=open_realtime,
                     on_progress=lambda _message, _sip_code: None):
    """Use exactly the native originate/ring/answer path of the SIP-only probe."""
    if not api_key or not api_key.strip() or driver._registration != 200 or should_abort():
        raise LiaVoiceError("Lia Voz pilot not authorized")
    codes = []
    last_event = None
    last_guard = float("-inf")
    aborted = False

    def guard():
        nonlocal last_guard, aborted
        now = time.monotonic()
        if not aborted and now - last_guard >= 1:
            last_guard = now
            aborted = should_abort()
        return aborted

    def observe(status, code):
        nonlocal last_event
        if isinstance(code, int) and 100 <= code <= 699 and code not in codes:
            codes.append(code)
        if (status, code) != last_event:
            last_event = (status, code)
            on_progress("Discagem SIP: " + status + "; SIP " + ("/".join(map(str, codes)) or "-"), code)

    def connected(call):
        return asyncio.run(_converse_connected(
            driver, call, api_key, should_abort=should_abort,
            session_factory=session_factory, on_progress=on_progress,
        ))

    on_progress("Iniciando discagem pelo mesmo motor do teste SIP sem IA.", None)
    outcome = driver.perform_call(
        phone, mode="LIA", **_route_options(prefix), timeout=RING_TIMEOUT_SECONDS,
        should_abort=guard, on_status=observe, on_trace=on_progress,
        on_connected=connected,
    )
    if outcome.status.startswith("LIA_"):
        return PilotOutcome(outcome.status, outcome.sip_code, outcome.message)
    if outcome.status == "INTERROMPIDA":
        status = "LIA_INTERROMPIDA"
    elif not outcome.answered:
        status = "LIA_SEM_ATENDIMENTO"
    else:
        status = "LIA_FALHOU"
    if outcome.sip_code and outcome.sip_code not in codes:
        codes.append(outcome.sip_code)
    message = ("SIP " + ("/".join(map(str, codes)) or "-")
               + "; sem confirmacao do aparelho. " + outcome.message)
    return PilotOutcome(status, outcome.sip_code, message[:230])


def perform_native_probe(driver, phone, prefix, *, should_abort,
                         on_progress=lambda _message, _sip_code: None):
    """One manually authorized comparison call on the explicitly selected route.

    No API session, speech, recording, transfer, retry, or customer lookup.
    The shared worker has already validated the number, account, and SIP slot.
    Central confirmation is not evidence that the handset actually rang.
    """
    if driver._registration != 200 or should_abort():
        raise LiaVoiceError("SIP probe not authorized")
    codes = []
    last_check = 0.0
    aborted = False

    def guard():
        nonlocal last_check, aborted
        now = time.monotonic()
        if not aborted and now - last_check >= 1:
            last_check = now
            aborted = should_abort()
        return aborted

    def observe(status, code):
        if isinstance(code, int) and 100 <= code <= 699 and code not in codes:
            codes.append(code)
        on_progress("Teste SIP sem IA: " + status + "; SIP " +
                    ("/".join(map(str, codes)) or "-"), code)

    on_progress("Teste SIP sem IA; rota padrao DDD+numero." if prefix is None
                else "Teste SIP sem IA; usando o techprefix Native selecionado.", None)
    outcome = driver.perform_call(
        phone, mode="PROBE", **_route_options(prefix),
        timeout=RING_TIMEOUT_SECONDS, probe_hold_seconds=12, should_abort=guard, on_status=observe,
        on_trace=on_progress,
    )
    if outcome.sip_code and outcome.sip_code not in codes:
        codes.append(outcome.sip_code)
    message = ("Teste sem IA. SIP " + ("/".join(map(str, codes)) or "-")
               + "; 180=" + ("sim" if 180 in codes else "nao")
               + "; central confirmou=" + ("sim" if outcome.answered else "nao")
               + "; midia negociada=" + ("sim" if outcome.media_active else "nao")
               + ". Confirme o toque no aparelho.")
    if outcome.status == "INTERROMPIDA":
        status = "LIA_PROBE_INTERROMPIDA"
    elif outcome.answered:
        status = "LIA_PROBE_FINALIZADO"
    else:
        status = "LIA_PROBE_SEM_ATENDIMENTO"
    on_progress(message, outcome.sip_code)
    return PilotOutcome(status, outcome.sip_code, message[:230])
