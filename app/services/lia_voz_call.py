"""Single-call SIP orchestration for the isolated, opt-in Lia Voz pilot."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.services.lia_voz_media import MediaBridge, attach_pj_media, run_media_bridge
from app.services.lia_voz_realtime import LiaVoiceError, open_realtime
from app.services.voice_runtime import make_dial_uri


@dataclass(frozen=True)
class PilotOutcome:
    status: str
    sip_code: int | None
    message: str


def _route_options(prefix):
    # Match the previous production campaign dial format exactly when the
    # administrator explicitly selects ACCOUNT. No DDI, no added techprefix.
    if prefix is None:
        return {"dial_format": "NATIONAL"}
    return {"native_techprefix": prefix, "native_local_ddd": "55"}


async def perform_lia_test(driver, phone, prefix, api_key, *,
                           should_abort, session_factory=open_realtime,
                           on_progress=lambda _message, _sip_code: None):
    """Call only after explicit worker authorization and a ready Realtime session.

    Own the native endpoint on this thread; no automatic retry/transfer.
    SIP 200 or RTP alone is not proof of a successful conversation.
    """
    if not api_key or driver._registration != 200 or should_abort():
        raise LiaVoiceError("Lia Voz pilot not authorized")
    pj = driver.pj
    call = None
    bridge = None
    media_context = None
    task = None

    on_progress("Conectando ao OpenAI Realtime.", None)
    async with session_factory(enabled=True, api_key=api_key, max_seconds=155) as session:
        on_progress("Realtime conectado; preparando INVITE SIP.", None)
        # Realtime handshake precedes the only INVITE.
        if not session.ready or should_abort():
            raise LiaVoiceError("Lia Voz pilot authorization revoked or session unavailable")

        class PilotCall(pj.Call):
            def __init__(self, account):
                super().__init__(account)
                self.answered = False
                self.disconnected = False
                self.sip_code = None
                self.callback_failed = False
                self.sip_codes = []
                self.confirmed_at = None
                self.media_states = []

            def observe_media(self, info):
                for media in info.media:
                    if media.type == pj.PJMEDIA_TYPE_AUDIO:
                        state = int(media.status)
                        if state not in self.media_states and len(self.media_states) < 8:
                            self.media_states.append(state)

            def onCallMediaState(self, _prm):
                try:
                    self.observe_media(self.getInfo())
                except Exception:
                    self.callback_failed = True

            def onCallState(self, _prm):
                try:
                    info = self.getInfo()
                    self.sip_code = int(info.lastStatusCode)
                    if self.sip_code and self.sip_code not in self.sip_codes and len(self.sip_codes) < 8:
                        self.sip_codes.append(self.sip_code)
                    if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
                        self.answered = True
                        if self.confirmed_at is None:
                            self.confirmed_at = time.monotonic()
                    elif info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                        self.disconnected = True
                    self.observe_media(info)
                except Exception:
                    self.callback_failed = True

        stage = "preparacao SIP"
        invite_started = False
        last_outcome = None
        last_reported_code = None

        def diagnostic():
            if call is None:
                return "INVITE nao iniciado."
            codes = "/".join(str(c) for c in call.sip_codes) or "-"
            states = "/".join(str(s) for s in call.media_states) or "-"
            elapsed = (str(round(time.monotonic() - call.confirmed_at, 1)) + "s"
                       if call.confirmed_at is not None else "nao")
            counters = bridge.snapshot() if bridge is not None else {}
            return ("SIP " + codes + "; 180=" + ("sim" if 180 in call.sip_codes else "nao")
                    + "; confirmado=" + elapsed + "; audio=" + states
                    + "; quadros RX/TX=" + str(counters.get("received_frames", 0))
                    + "/" + str(counters.get("requested_frames", 0))
                    + "; TX nao silencioso=" + str(counters.get("non_silent_rendered_frames", 0)) + ".")

        def result(status, message):
            nonlocal last_outcome
            last_outcome = PilotOutcome(status, call.sip_code if call else None,
                                        (message + " " + diagnostic())[:230])
            on_progress(last_outcome.message, last_outcome.sip_code)
            return last_outcome

        def media_exchanged():
            if bridge is None or bridge.failure:
                return False
            counters = bridge.snapshot()
            # Requested frames may consist solely of silence before the model
            # produces its first sample. Do not treat those as a validated voice.
            return counters["received_frames"] > 0 and counters["non_silent_rendered_frames"] > 0

        try:
            target = make_dial_uri(driver.account_model, phone, **_route_options(prefix))
            call = PilotCall(driver.acc)
            # Use an explicit audio-only offer, preserving the authorized Native route.
            params = pj.CallOpParam(True)
            params.opt.audioCount = 1
            params.opt.videoCount = 0
            stage = "INVITE"
            on_progress("Enviando INVITE SIP (somente audio).", None)
            call.makeCall(target, params)
            invite_started = True
            stage = "aguardando resposta SIP"
            on_progress("INVITE enviado; aguardando central.", None)
            ring_deadline = time.monotonic() + 25
            answered_at = None
            next_guard = 0.0
            while True:
                # Same event pump used by the native audio bench, without blocking
                # the asynchronous Realtime bridge for long intervals.
                driver.pump(10)
                now = time.monotonic()
                if call.sip_code is not None and call.sip_code != last_reported_code:
                    last_reported_code = call.sip_code
                    on_progress("Resposta SIP " + str(call.sip_code) + ". " + diagnostic(),
                                call.sip_code)
                if call.callback_failed:
                    stage = "callback SIP"
                    raise LiaVoiceError("SIP callback failed")
                if now >= next_guard:
                    if should_abort():
                        return result("LIA_INTERROMPIDA", "Teste interrompido por seguranca.")
                    next_guard = now + 1
                if call.disconnected:
                    if media_exchanged():
                        return result("LIA_FINALIZADA", "SIP encerrado. Validar a conversa no aparelho.")
                    if not call.answered:
                        return result("LIA_SEM_ATENDIMENTO", "Central encerrou antes da confirmacao.")
                    return result("LIA_FALHOU", "SIP confirmou; encerrou sem audio bidirecional.")
                if task is not None and task.done():
                    stage = "transporte Realtime"
                    task.result()
                    raise LiaVoiceError("Media session ended before SIP call")
                if not call.answered:
                    if now >= ring_deadline:
                        return result("LIA_SEM_ATENDIMENTO", "Tempo de toque encerrado.")
                else:
                    if answered_at is None:
                        answered_at = call.confirmed_at or now
                        on_progress("SIP confirmado; aguardando midia RTP. " + diagnostic(),
                                    call.sip_code)
                    if bridge is None:
                        stage = "negociacao RTP"
                        info = call.getInfo()
                        call.observe_media(info)
                        for index, media in enumerate(info.media):
                            if (media.type == pj.PJMEDIA_TYPE_AUDIO
                                    and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE):
                                stage = "conexao ponte de audio"
                                bridge = MediaBridge(session)
                                context = attach_pj_media(
                                    pj, call.getAudioMedia(int(getattr(media, "index", index))),
                                    bridge, enabled=True,
                                    remote_ended=lambda: call.disconnected,
                                )
                                context.__enter__()
                                media_context = context
                                task = asyncio.create_task(run_media_bridge(session, bridge))
                                stage = "audio Realtime"
                                on_progress("Ponte RTP conectada; iniciando audio Realtime. " + diagnostic(),
                                            call.sip_code)
                                break
                        if bridge is None and now - answered_at >= 12:
                            return result("LIA_FALHOU", "SIP confirmou sem midia RTP ativa.")
                    if bridge and bridge.failure:
                        return result("LIA_FALHOU", "Ponte de audio interrompida.")
                    if now - answered_at >= 90:
                        if not media_exchanged():
                            return result("LIA_FALHOU", "Limite atingido sem quadros bidirecionais.")
                        return result("LIA_FINALIZADA", "Limite atingido; validar voz no aparelho.")
                await asyncio.sleep(0.01)
        except Exception:
            # Stage names and numeric SIP/media counters only. Never expose raw
            # exceptions, SDP, SIP URIs, credentials or conversation content.
            return result("LIA_FALHOU", "Falha na etapa: " + stage + ".")
        finally:
            # Do not erase SIP diagnostics when a native cleanup operation races
            # with remote BYE or an unsuccessful makeCall. Endpoint destruction in
            # PjSession still runs after this coroutine exits.
            if bridge is not None:
                bridge.close()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            cleanup_failed = False
            try:
                if media_context is not None:
                    media_context.__exit__(None, None, None)
            except Exception:
                cleanup_failed = True
            if call is not None:
                try:
                    if not call.disconnected and (invite_started or call.isActive()):
                        end = pj.CallOpParam()
                        end.statusCode = 200
                        call.hangup(end)
                except Exception:
                    cleanup_failed = True
                finally:
                    try:
                        for _ in range(4):
                            driver.pump(100)
                    except Exception:
                        cleanup_failed = True
            if cleanup_failed:
                previous = last_outcome.message if last_outcome else diagnostic()
                on_progress(("Falha ao encerrar SIP/midia. " + previous)[:230],
                            call.sip_code if call is not None else None)
                # Never report success if native cleanup could not be confirmed.
                raise LiaVoiceError("Native cleanup failed")


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
        timeout=40, probe_hold_seconds=12, should_abort=guard, on_status=observe,
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
