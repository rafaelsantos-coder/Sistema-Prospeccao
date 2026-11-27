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


async def perform_lia_test(driver, phone, prefix, api_key, *,
                           should_abort, session_factory=open_realtime):
    """Call only after explicit worker authorization and a ready Realtime session.

    Own the native endpoint on this thread; no automatic retry/transfer.
    SIP 200 or RTP alone is not proof of a successful conversation.
    """
    if not api_key or should_abort():
        raise LiaVoiceError("Lia Voz pilot not authorized")
    pj = driver.pj
    call = None
    bridge = None
    media_context = None
    task = None

    async with session_factory(enabled=True, api_key=api_key, max_seconds=155) as session:
        # Realtime handshake precedes the only INVITE.
        if should_abort():
            raise LiaVoiceError("Lia Voz pilot authorization revoked")

        class PilotCall(pj.Call):
            def __init__(self, account):
                super().__init__(account)
                self.answered = False
                self.disconnected = False
                self.sip_code = None
                self.callback_failed = False

            def onCallState(self, _prm):
                try:
                    info = self.getInfo()
                    self.sip_code = int(info.lastStatusCode)
                    if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
                        self.answered = True
                    elif info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                        self.disconnected = True
                except Exception:
                    self.callback_failed = True

        try:
            target = make_dial_uri(driver.account_model, phone,
                                   native_techprefix=prefix, native_local_ddd="55")
            call = PilotCall(driver.acc)
            call.makeCall(target, pj.CallOpParam(True))
            ring_deadline = time.monotonic() + 25
            answered_at = None
            next_guard = 0.0
            while True:
                driver.pump(0)
                now = time.monotonic()
                if call.callback_failed:
                    raise LiaVoiceError("SIP callback failed")
                if now >= next_guard:
                    if should_abort():
                        return PilotOutcome("LIA_INTERROMPIDA", call.sip_code,
                                            "Teste interrompido por controle de seguranca.")
                    next_guard = now + 1
                if task is not None and task.done():
                    task.result()
                    raise LiaVoiceError("Media session ended before SIP call")
                if call.disconnected:
                    if bridge and not bridge.failure:
                        counters = bridge.snapshot()
                        return PilotOutcome(
                            "LIA_FINALIZADA", call.sip_code,
                            "SIP encerrado. Quadros recebidos: %s; solicitados: %s. "
                            "Validar a conversa no aparelho." %
                            (counters["received_frames"], counters["requested_frames"]),
                        )
                    return PilotOutcome(
                        "LIA_FALHOU" if call.answered else "LIA_SEM_ATENDIMENTO",
                        call.sip_code, "Chamada encerrada sem ponte de audio validada.",
                    )
                if not call.answered:
                    if now >= ring_deadline:
                        return PilotOutcome("LIA_SEM_ATENDIMENTO", call.sip_code,
                                            "Tempo de toque encerrado.")
                else:
                    if answered_at is None:
                        answered_at = now
                    if bridge is None:
                        for index, media in enumerate(call.getInfo().media):
                            if (media.type == pj.PJMEDIA_TYPE_AUDIO
                                    and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE):
                                bridge = MediaBridge(session)
                                context = attach_pj_media(
                                    pj, call.getAudioMedia(index), bridge, enabled=True,
                                )
                                context.__enter__()
                                media_context = context
                                task = asyncio.create_task(run_media_bridge(session, bridge))
                                break
                        if bridge is None and now - answered_at >= 12:
                            raise LiaVoiceError("SIP answered without active RTP")
                    if bridge and bridge.failure:
                        raise LiaVoiceError("Native audio bridge failed")
                    if now - answered_at >= 90:
                        return PilotOutcome("LIA_FINALIZADA", call.sip_code,
                                            "Limite do teste atingido; validar voz no aparelho.")
                await asyncio.sleep(0.01)
        finally:
            if bridge is not None:
                bridge.close()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Disconnect the media port before hangup/endpoint destruction.
            try:
                if media_context is not None:
                    media_context.__exit__(None, None, None)
            finally:
                if call is not None:
                    try:
                        if not call.disconnected:
                            end = pj.CallOpParam()
                            end.statusCode = 200
                            call.hangup(end)
                    finally:
                        for _ in range(4):
                            driver.pump(100)
