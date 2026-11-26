"""Native PJSIP media/SIP driver for a separate Python 3.12 voice worker.

Unlike the legacy REGISTER/INVITE probe, PJSUA2 owns SIP dialogs, UDP/TCP/TLS
transport, negotiated RTP/RTCP media, ACK, CANCEL, BYE, Digest and REFER/NOTIFY.
Import pjsua2 lazily: web services and CI remain on Python 3.14.
"""
from __future__ import annotations

import importlib
import logging
import socket
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.services.voice_runtime import make_dial_uri, make_transfer_uri, validate_host, validate_port

LOG = logging.getLogger(__name__)

# Tempo disponivel para confirmar DTMF apos a reproducao completa do audio.
DTMF_AFTER_AUDIO_WAIT_SECONDS = 20


def dtmf_response_window_expired(audio_started_at: float | None, audio_duration: float,
                                 current_time: float) -> bool:
    """Considera o tempo de reproducao mais 20s para a tecla de confirmacao."""
    return (audio_started_at is not None and
            current_time - audio_started_at >= audio_duration + DTMF_AFTER_AUDIO_WAIT_SECONDS)


class VoiceEngineError(RuntimeError):
    pass


@dataclass
class VoiceCallResult:
    status: str
    sip_code: int | None
    transferred: bool
    message: str
    answered: bool
    media_active: bool


@dataclass
class VoiceAccountParams:
    username: str
    password: str
    domain: str
    host: str
    port: int = 5060
    transport: str = "UDP"
    proxy_host: str = ""
    proxy_port: int = 5060
    caller_id: str = ""


def parse_params(account, secret: str) -> VoiceAccountParams:
    host = validate_host(account.domain)
    port = validate_port(getattr(account, "sip_port", 5060) or 5060)
    proxy = validate_host(getattr(account, "proxy_host", ""), optional=True)
    return VoiceAccountParams(
        username=account.username, password=secret,
        domain=host, host=host, port=port, transport=account.transport,
        proxy_host=proxy, proxy_port=validate_port(getattr(account, "proxy_port", 5060) or 5060),
        caller_id=getattr(account, "caller_id", "") or "",
    )


def endpoint_name(params: VoiceAccountParams) -> str:
    """Fail before starting native code if neither host nor proxy resolves."""
    target = params.proxy_host or params.host
    port = params.proxy_port if params.proxy_host else params.port
    kind = socket.SOCK_DGRAM if params.transport == "UDP" else socket.SOCK_STREAM
    try:
        result = socket.getaddrinfo(target, port, socket.AF_INET, kind)
        if not result:
            raise VoiceEngineError("Servidor SIP nao possui IPv4 disponivel.")
    except socket.gaierror as exc:
        raise VoiceEngineError(
            "Servidor SIP nao encontrado via DNS. Configure IP/host acessivel e porta no cadastro."
        ) from exc
    return target


class PjSession:
    """One account and one outgoing call per session; no predictive dialling."""

    def __init__(self, account, secret: str):
        self.account_model = account
        self.params = parse_params(account, secret)
        self.pj = None
        self.ep = None
        self.acc = None
        self._registration = None
        self._registration_message = ""
        self._call = None
        self._player = None

    def __enter__(self):
        endpoint_name(self.params)
        try:
            pj = importlib.import_module("pjsua2")
        except ImportError as exc:
            raise VoiceEngineError(
                "Biblioteca PJSUA2 nao esta instalada no worker; verificar imagem de telefonia."
            ) from exc
        self.pj = pj
        ep = pj.Endpoint()
        self.ep = ep
        try:
            ep.libCreate()
            cfg = pj.EpConfig()
            cfg.uaConfig.threadCnt = 0
            cfg.uaConfig.mainThreadOnly = True
            cfg.logConfig.level = 2
            cfg.logConfig.consoleLevel = 0
            cfg.medConfig.clockRate = 8000
            ep.libInit(cfg)

            transport = self.params.transport.upper()
            constant = {
                "UDP": pj.PJSIP_TRANSPORT_UDP,
                "TCP": pj.PJSIP_TRANSPORT_TCP,
                "TLS": pj.PJSIP_TRANSPORT_TLS,
            }.get(transport)
            if constant is None:
                raise VoiceEngineError("Transporte SIP invalido.")
            tcp = pj.TransportConfig()
            tcp.port = 0  # Ephemeral local port: avoids collision with customer phone.
            ep.transportCreate(constant, tcp)
            ep.libStart()
            # Virtual sound device keeps native G.711 RTP timing; no physical soundcard.
            ep.audDevManager().setNullDev()

            parent = self
            class RegisteredAccount(pj.Account):
                def onRegState(self, prm):
                    try:
                        info = self.getInfo()
                        parent._registration = int(info.regStatus)
                        parent._registration_message = str(info.regStatusText or "")
                    except Exception:
                        parent._registration = int(getattr(prm, "code", 0))
                        parent._registration_message = "Registro SIP retornou resposta."

                def onIncomingCall(self, prm):
                    # Outbound-only worker. Never auto-answer customer-originated calls.
                    try:
                        incoming = pj.Call(self, prm.callId)
                        status = pj.CallOpParam()
                        status.statusCode = 486
                        incoming.answer(status)
                    except Exception:
                        LOG.warning("SIP chamada entrante rejeitada no worker de saida")

            acc_cfg = pj.AccountConfig()
            # Caller ID is a requested display name; the PBX/provider controls the
            # effective CLI displayed to the callee.
            aor = f"sip:{self.params.username}@{self.params.domain}"
            acc_cfg.idUri = (f'"{self.params.caller_id}" <{aor}>' if self.params.caller_id else aor)
            acc_cfg.regConfig.registrarUri = (
                f"sip:{self.params.domain}:{self.params.port};transport={transport.lower()}"
            )
            creds = pj.AuthCredInfo("digest", "*", self.params.username, 0, self.params.password)
            acc_cfg.sipConfig.authCreds.append(creds)
            if self.params.proxy_host:
                acc_cfg.sipConfig.proxies.append(
                    f"sip:{self.params.proxy_host}:{self.params.proxy_port};lr;transport={transport.lower()}"
                )
            self.acc = RegisteredAccount()
            self.acc.create(acc_cfg)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def pump(self, milliseconds: int = 100) -> None:
        if self.ep:
            self.ep.libHandleEvents(milliseconds)

    def wait_for_registration(self, seconds: int = 15) -> tuple[int, str]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
            if self._registration == 200:
                return 200, "Registro SIP 200 OK confirmado."
            if self._registration and self._registration >= 300:
                return self._registration, (
                    "Central recusou registro SIP; verifique credenciais, realm, proxy e rede."
                )
        return 0, "Timeout de autenticacao SIP; central nao respondeu."

    def perform_call(
        self, phone: str, *, mode: str, group=None,
        audio_wav: str | None = None, dtmf_digit: str = "1", timeout: int = 55,
        dial_format: str | None = None,
        native_techprefix: str | None = None,
        native_local_ddd: str = "55",
        on_status: Callable[[str, int | None], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> VoiceCallResult:
        if self._registration != 200:
            raise VoiceEngineError("Conta SIP nao esta autenticada para originar chamada.")
        pj = self.pj
        session = self
        call_mode = mode.upper()
        if call_mode not in {"DIRECT", "AUDIO", "PROBE"}:
            raise VoiceEngineError("Modo de chamada invalido.")
        if call_mode == "DIRECT" and group is None:
            raise VoiceEngineError("A transferencia direta exige grupo de destino.")
        if call_mode == "AUDIO" and (not audio_wav or not Path(audio_wav).is_file()):
            raise VoiceEngineError("Audio WAV indisponivel no motor de chamadas.")
        audio_seconds = 0.0
        if call_mode == "AUDIO":
            # Usar a duracao real do WAV: nao limitar campanhas informativas a 55 s.
            try:
                with wave.open(str(audio_wav), "rb") as wav:
                    audio_seconds = wav.getnframes() / wav.getframerate()
            except (OSError, wave.Error, ZeroDivisionError) as exc:
                raise VoiceEngineError("Nao foi possivel ler a duracao do audio.") from exc
        target_uri = make_dial_uri(
            self.account_model, phone, dial_format=dial_format,
            native_techprefix=native_techprefix, native_local_ddd=native_local_ddd,
        )
        transfer_uri = make_transfer_uri(self.account_model, group) if group is not None else None

        class ManagedCall(pj.Call):
            def __init__(self, account):
                super().__init__(account)
                self.answered = False
                self.ringing = False
                self.disconnected = False
                self.media_active = False
                self.sip_code = None
                self.digit_confirmed = False
                self.transfer_started = False
                self.transfer_confirmed = False
                self.transfer_failed = False
                self.transfer_code = None
                self._audio_connected = False
                self.audio_started_at = None

            def _start_audio_if_ready(self):
                if call_mode != "AUDIO" or self._audio_connected or not self.answered:
                    return
                info = self.getInfo()
                for index, media in enumerate(info.media):
                    if media.type != pj.PJMEDIA_TYPE_AUDIO or media.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                        continue
                    if session._player is None:
                        session._player = pj.AudioMediaPlayer()
                        session._player.createPlayer(audio_wav, pj.PJMEDIA_FILE_NO_LOOP)
                    session._player.startTransmit(self.getAudioMedia(index))
                    self._audio_connected = True
                    self.audio_started_at = time.monotonic()
                    if on_status:
                        on_status("REPRODUZINDO_AUDIO", self.sip_code)
                    return

            def onCallState(self, prm):
                try:
                    info = self.getInfo()
                    self.sip_code = int(info.lastStatusCode)
                    if self.sip_code == 180:
                        self.ringing = True
                    if info.state == pj.PJSIP_INV_STATE_CONFIRMED:
                        self.answered = True
                        if on_status:
                            on_status("ATENDIDA", self.sip_code)
                        self._start_audio_if_ready()
                    elif info.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                        self.disconnected = True
                    elif on_status and self.sip_code in {180, 183}:
                        on_status("TOCANDO", self.sip_code)
                except Exception:
                    LOG.exception("Falha ao interpretar estado de chamada SIP")

            def onCallMediaState(self, prm):
                try:
                    info = self.getInfo()
                    for media in info.media:
                        if media.type != pj.PJMEDIA_TYPE_AUDIO:
                            continue
                        if media.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                            self.media_active = True
                    self._start_audio_if_ready()
                except Exception:
                    LOG.exception("Audio RTP indisponivel em chamada SIP")

            def onDtmfDigit(self, prm):
                if group is None:
                    return
                self.digit_confirmed = bool(str(getattr(prm, "digit", "")) == dtmf_digit)
                if self.digit_confirmed and on_status:
                    on_status("CONFIRMADO_DTMF", None)

            def onCallTransferStatus(self, prm):
                self.transfer_code = int(prm.statusCode)
                if prm.finalNotify:
                    self.transfer_confirmed = 200 <= self.transfer_code < 300
                    self.transfer_failed = not self.transfer_confirmed
                    if on_status:
                        on_status("TRANSFERIDA" if self.transfer_confirmed else "FALHA_TRANSFERENCIA",
                                  self.transfer_code)

        call = ManagedCall(self.acc)
        self._call = call
        try:
            prm = pj.CallOpParam(True)
            call.makeCall(target_uri, prm)
            if on_status:
                on_status("CHAMANDO", None)
            # Aguarda toque, arquivo inteiro e eventual confirmacao/transferencia.
            call_timeout = (max(55, audio_seconds + 80)
                            if call_mode == "AUDIO" else min(90, max(15, timeout)))
            deadline = time.monotonic() + call_timeout
            answered_at = None
            transfer_at = None
            while time.monotonic() < deadline:
                self.pump(150)
                if should_abort and should_abort():
                    return VoiceCallResult("INTERROMPIDA", call.sip_code, False,
                                           "Chamada interrompida pelo administrador.", call.answered,
                                           call.media_active)
                if call.disconnected:
                    if call.transfer_confirmed:
                        return VoiceCallResult("TRANSFERIDA", call.transfer_code, True,
                                               "PABX confirmou transferencia SIP REFER.",
                                               call.answered, call.media_active)
                    if call.transfer_started:
                        return VoiceCallResult("FALHA_TRANSFERENCIA", call.transfer_code or call.sip_code,
                                               False, "Chamada encerrada sem confirmacao de transferencia.",
                                               call.answered, call.media_active)
                    if call_mode == "AUDIO" and group is None and call.answered:
                        complete = (call.audio_started_at is not None
                                    and time.monotonic() - call.audio_started_at >= audio_seconds)
                        return VoiceCallResult(
                            "AUDIO_CONCLUIDO" if complete else "AUDIO_INTERROMPIDO",
                            call.sip_code, False,
                            ("Reproducao local do audio encerrada; recepcao no aparelho nao confirmada."
                             if complete else "Destino encerrou a chamada antes do fim do audio."),
                            True, call.media_active,
                        )
                    return VoiceCallResult("FINALIZADA" if call.answered else "SEM_ATENDIMENTO",
                                           call.sip_code, False,
                                           "Chamada encerrada pelo destino ou pela central.",
                                           call.answered, call.media_active)
                if call.answered:
                    if answered_at is None:
                        answered_at = time.monotonic()
                    if call_mode == "PROBE" and time.monotonic() - answered_at >= 1:
                        return VoiceCallResult("ATENDIDA", call.sip_code, False,
                                               "Chamada atendida, sem transferencia.",
                                               True, call.media_active)
                    if call_mode == "AUDIO" and not call._audio_connected and (
                        time.monotonic() - answered_at >= 12
                    ):
                        return VoiceCallResult("FALHA_AUDIO", call.sip_code, False,
                                               "Midia RTP indisponivel para reproduzir o audio.",
                                               True, call.media_active)
                    if call_mode == "AUDIO" and group is None:
                        # Campanha informativa: toca o WAV integralmente e encerra,
                        # sem solicitar tecla, enviar REFER ou encaminhar para grupo.
                        if call.audio_started_at is not None and (
                            time.monotonic() - call.audio_started_at >= audio_seconds + 0.25
                        ):
                            if on_status:
                                on_status("AUDIO_CONCLUIDO", call.sip_code)
                            return VoiceCallResult(
                                "AUDIO_CONCLUIDO", call.sip_code, False,
                                "Reproducao local do audio concluida; nenhuma transferencia solicitada.",
                                True, call.media_active,
                            )
                        continue
                    ready_for_transfer = (
                        call_mode == "DIRECT" and time.monotonic() - answered_at >= 0.35
                    ) or (call_mode == "AUDIO" and call.digit_confirmed)
                    if ready_for_transfer and not call.transfer_started:
                        call.transfer_started = True
                        transfer_at = time.monotonic()
                        try:
                            call.xfer(transfer_uri, pj.CallOpParam())
                            if on_status:
                                on_status("TRANSFERINDO", None)
                        except Exception as exc:
                            return VoiceCallResult("FALHA_TRANSFERENCIA", None, False,
                                                   "PABX nao aceitou o REFER para o grupo.",
                                                   True, call.media_active)
                    if call.transfer_confirmed:
                        return VoiceCallResult("TRANSFERIDA", call.transfer_code, True,
                                               "Transferencia confirmada pela central SIP.",
                                               True, call.media_active)
                    if call.transfer_failed:
                        return VoiceCallResult("FALHA_TRANSFERENCIA", call.transfer_code, False,
                                               "Central negou a transferencia do grupo.",
                                               True, call.media_active)
                    if transfer_at and time.monotonic() - transfer_at > 20:
                        return VoiceCallResult("FALHA_TRANSFERENCIA", call.transfer_code, False,
                                               "Timeout da confirmacao da transferencia SIP REFER.",
                                               True, call.media_active)
                    if call_mode == "AUDIO" and not call.digit_confirmed:
                        if dtmf_response_window_expired(
                            call.audio_started_at, audio_seconds, time.monotonic()
                        ):
                            return VoiceCallResult("SEM_INTERESSE", call.sip_code, False,
                                                   "Nao houve confirmacao DTMF apos o audio.",
                                                   True, call.media_active)
            return VoiceCallResult("TIMEOUT", call.sip_code, False,
                                   "Limite de duracao da chamada atingido.", call.answered, call.media_active)
        finally:
            try:
                if not call.disconnected and not call.transfer_confirmed:
                    end = pj.CallOpParam()
                    end.statusCode = 200
                    call.hangup(end)
            except Exception:
                pass
            for _ in range(4):
                self.pump(100)
            self._player = None
            self._call = None

    def __exit__(self, _typ, _val, _tb):
        if self.acc:
            try:
                self.acc.setRegistration(False)
                for _ in range(4):
                    self.pump(100)
            except Exception:
                pass
            try:
                self.acc.shutdown()
            except Exception:
                pass
            self.acc = None
        self._call = None
        self._player = None
        if self.ep:
            try:
                self.ep.libDestroy()
            except Exception:
                LOG.warning("Erro na finalizacao da biblioteca PJSUA2")
            self.ep = None
