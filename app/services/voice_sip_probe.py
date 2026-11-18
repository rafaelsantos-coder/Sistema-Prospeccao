"""Bounded SIP/UDP signaling probe.

Designed for a dedicated, single-replica diagnostic worker, NEVER for bulk dialing.
Checks SIP REGISTER followed by one INVITE, interprets 180 Ringing or 200 OK,
then terminates the attempt. This is not an RTP/audio quality test or PBX transfer test.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import socket
import time
from dataclasses import dataclass
from typing import Callable


class SipProbeError(RuntimeError):
    def __init__(self, message: str, register_code: int | None = None,
                 invite_code: int | None = None):
        super().__init__(message)
        self.register_code = register_code
        self.invite_code = invite_code


@dataclass
class SipDiagnostic:
    status: str
    register_code: int | None
    invite_code: int | None
    message: str


def _parse_response(payload: bytes) -> tuple[int, dict[str, str]]:
    try:
        message = payload.decode("utf-8", "replace")
    except Exception as exc:
        raise SipProbeError("Resposta SIP invalida.") from exc
    first, *headers = message.split("\r\n\r\n", 1)[0].split("\r\n")
    matched = re.match(r"^SIP/2\.0\s+(\d{3})\b", first)
    if not matched:
        raise SipProbeError("Resposta SIP sem codigo valido.")
    result: dict[str, str] = {}
    for line in headers:
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return int(matched.group(1)), result


def _digest(challenge: str, method: str, uri: str, username: str, password: str) -> str:
    if not challenge.lower().startswith("digest "):
        raise SipProbeError("Central SIP usa autenticacao nao suportada.")
    pairs = {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', challenge[7:])
    }
    algorithm = pairs.get("algorithm", "MD5").upper()
    hashes = {"MD5": hashlib.md5, "SHA-256": hashlib.sha256}
    if algorithm not in hashes:
        raise SipProbeError("Algoritmo de autenticacao SIP nao suportado.")
    if not pairs.get("nonce") or not pairs.get("realm"):
        raise SipProbeError("Desafio SIP incompleto.")
    h = lambda v: hashes[algorithm](v.encode()).hexdigest()
    realm = pairs["realm"]
    nonce = pairs["nonce"]
    a1 = h(f"{username}:{realm}:{password}")
    a2 = h(f"{method}:{uri}")
    qops = {v.strip().lower() for v in pairs.get("qop", "").split(",") if v.strip()}
    cnonce = secrets.token_hex(8)
    if qops and "auth" not in qops:
        raise SipProbeError("Central exige modo Digest SIP nao suportado.")
    if "auth" in qops:
        response = h(f"{a1}:{nonce}:00000001:{cnonce}:auth:{a2}")
    else:
        response = h(f"{a1}:{nonce}:{a2}")
    fields = [
        f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
        f'uri="{uri}"', f'response="{response}"', f'algorithm={algorithm}',
    ]
    if "auth" in qops:
        fields += ['qop=auth', 'nc=00000001', f'cnonce="{cnonce}"']
    if "opaque" in pairs:
        fields.append(f'opaque="{pairs["opaque"]}"')
    return "Digest " + ", ".join(fields)


class SipProbe:
    def __init__(self, host: str, username: str, password: str, phone: str | None,
                 notify: Callable[[str, int | None, int | None, str], None],
                 port: int = 5060, proxy_host: str = "", proxy_port: int = 5060):
        if not re.fullmatch(r"[A-Za-z0-9.-]{3,190}", host):
            raise SipProbeError("Servidor SIP invalido.")
        if not re.fullmatch(r"[0-9*#]{1,20}", username):
            raise SipProbeError("Usuario SIP invalido.")
        if phone is not None and not re.fullmatch(r"\d{10,14}", phone):
            raise SipProbeError("Numero de teste invalido.")
        self.host = host
        self.port = int(port)
        self.proxy_host = proxy_host.strip()
        self.proxy_port = int(proxy_port)
        if not 1 <= self.port <= 65535 or not 1 <= self.proxy_port <= 65535:
            raise SipProbeError("Porta SIP fora do intervalo valido.")
        if self.proxy_host and not re.fullmatch(r"[A-Za-z0-9.-]{3,190}", self.proxy_host):
            raise SipProbeError("Proxy SIP invalido.")
        self.username = username
        self.password = password
        self.phone = phone
        self.notify = notify
        self.sock: socket.socket | None = None
        self.media: socket.socket | None = None
        self.from_tag = secrets.token_hex(7)
        self.callid_prefix = secrets.token_hex(12)
        self.local_ip = "127.0.0.1"
        self.local_port = 0

    def _request(self, method: str, target: str, callid: str, seq: int, branch: str,
                 to_header: str, *, expires: int | None = None, auth: str = "",
                 auth_header: str = "Authorization", body: str = "") -> bytes:
        addr = f"sip:{self.username}@{self.host}"
        heads = [
            f"{method} {target} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <{addr}>;tag={self.from_tag}",
            f"To: {to_header}",
            f"Call-ID: {callid}",
            f"CSeq: {seq} {method}",
            f"Contact: <sip:{self.username}@{self.local_ip}:{self.local_port};transport=udp>",
            "User-Agent: SulnetProspecta-VOZ-Diagnostico/1.0",
            "Allow: INVITE, ACK, CANCEL, BYE, REGISTER",
        ]
        if expires is not None:
            heads.append(f"Expires: {expires}")
        if auth:
            heads.append(f"{auth_header}: {auth}")
        if body:
            heads.append("Content-Type: application/sdp")
        heads.append(f"Content-Length: {len(body.encode('utf-8'))}")
        return ("\r\n".join(heads) + "\r\n\r\n" + body).encode("utf-8")

    def _send(self, data: bytes) -> None:
        assert self.sock is not None
        self.sock.send(data)

    def _next(self, callid: str, method: str, limit: float) -> tuple[int, dict[str, str]]:
        assert self.sock is not None
        while time.monotonic() < limit:
            remaining = max(0.05, min(2.0, limit - time.monotonic()))
            self.sock.settimeout(remaining)
            try:
                response = self.sock.recv(65535)
            except socket.timeout:
                continue
            try:
                code, headers = _parse_response(response)
            except SipProbeError:
                continue
            if headers.get("call-id", "") != callid:
                continue
            if not headers.get("cseq", "").upper().endswith(" " + method):
                continue
            return code, headers
        raise SipProbeError("Tempo limite de resposta SIP esgotado.")

    def _exchange(self, method: str, target: str, callid: str,
                  to_header: str, expires: int | None = None, body: str = "",
                  timeout: float = 10.0) -> tuple[int, dict[str, str], int, str, str, str]:
        seq = 1
        branch = "z9hG4bK" + secrets.token_hex(9)
        auth = ""
        auth_header = "Authorization"
        request = self._request(method, target, callid, seq, branch, to_header,
                                expires=expires, auth=auth, body=body)
        self._send(request)
        deadline = time.monotonic() + timeout
        for _ in range(2):
            while True:
                code, headers = self._next(callid, method, deadline)
                if code == 100:
                    continue
                if code in (401, 407):
                    challenge = headers.get("www-authenticate" if code == 401 else "proxy-authenticate", "")
                    auth = _digest(challenge, method, target, self.username, self.password)
                    auth_header = "Proxy-Authorization" if code == 407 else "Authorization"
                    seq += 1
                    branch = "z9hG4bK" + secrets.token_hex(9)
                    request = self._request(method, target, callid, seq, branch, to_header,
                                            expires=expires, auth=auth, auth_header=auth_header, body=body)
                    self._send(request)
                    break
                return code, headers, seq, branch, auth, auth_header
        raise SipProbeError("Central SIP recusou autenticacao da conta.")

    def _register(self, expires: int = 75) -> None:
        callid = f"{self.callid_prefix}-{'reg' if expires else 'unreg'}@{self.host}"
        target = f"sip:{self.host}:{self.port}"
        to = f"<sip:{self.username}@{self.host}>"
        code, _, _, _, _, _ = self._exchange(
            "REGISTER", target, callid, to, expires=expires, timeout=12,
        )
        if code != 200:
            raise SipProbeError(f"Registro SIP recusado pela central (SIP {code}).", register_code=code)
        if expires:
            self.notify("REGISTRADO", 200, None, "Conta autenticada na central SIP.")

    def _sdp(self) -> str:
        assert self.media is not None
        port = self.media.getsockname()[1]
        return (
            "v=0\r\n"
            f"o=- {int(time.time())} 1 IN IP4 {self.local_ip}\r\n"
            "s=Prospecta VOZ teste de chamada\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {port} RTP/AVP 0 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            "a=sendrecv\r\n"
        )

    def _finalize_invite(self, code: int, headers: dict[str, str],
                         target: str, callid: str, seq: int, branch: str,
                         to_default: str) -> None:
        to = headers.get("to", to_default)
        if code >= 300:
            ack = self._request("ACK", target, callid, seq, branch, to)
            self._send(ack)
        elif code == 200:
            ack = self._request("ACK", target, callid, seq, "z9hG4bK" + secrets.token_hex(7), to)
            self._send(ack)
            time.sleep(0.35)
            bye = self._request("BYE", target, callid, seq + 1,
                                "z9hG4bK" + secrets.token_hex(7), to)
            self._send(bye)

    def _call(self) -> SipDiagnostic:
        if self.phone is None:
            raise SipProbeError("Teste sem telefone nao pode originar chamada.")
        self.media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.media.bind((self.local_ip, 0))
        target = f"sip:{self.phone}@{self.host}:{self.port}"
        to_header = f"<{target}>"
        callid = f"{self.callid_prefix}-call@{self.host}"
        self.notify("CHAMANDO", 200, None, "Central SIP autenticada. Originando chamada de teste.")
        # INVITE may receive 401 or 407; exchange handles one Digest challenge.
        code, headers, seq, branch, _auth, _auth_header = self._exchange(
            "INVITE", target, callid, to_header, body=self._sdp(), timeout=12,
        )
        if code == 200:
            self._finalize_invite(code, headers, target, callid, seq, branch, to_header)
            return SipDiagnostic("ATENDIDA", 200, code, "Chamada atendida segundo SIP 200 OK. Audio nao verificado.")
        if code >= 300:
            self._finalize_invite(code, headers, target, callid, seq, branch, to_header)
            if code == 486:
                return SipDiagnostic("OCUPADO", 200, code, "Numero ocupado (SIP 486).")
            return SipDiagnostic("FALHOU", 200, code, f"Central recusou a chamada (SIP {code}).")
        ringing = code == 180
        if ringing:
            self.notify("TOCANDO", 200, code, "Sinal SIP 180 Ringing recebido. Telefone pode estar tocando.")
        deadline = time.monotonic() + 8.0
        last = code
        latest_headers = headers
        while time.monotonic() < deadline:
            try:
                code, headers = self._next(callid, "INVITE", deadline)
            except SipProbeError:
                break
            if code == 100:
                continue
            last = code
            latest_headers = headers
            if code == 180:
                ringing = True
                self.notify("TOCANDO", 200, 180, "Sinal SIP 180 Ringing recebido.")
            elif code == 200:
                self._finalize_invite(code, headers, target, callid, seq, branch, to_header)
                return SipDiagnostic("ATENDIDA", 200, 200, "Chamada atendida segundo SIP 200 OK. Audio nao verificado.")
            elif code >= 300:
                self._finalize_invite(code, headers, target, callid, seq, branch, to_header)
                if code == 486:
                    return SipDiagnostic("OCUPADO", 200, code, "Destino ocupado (SIP 486).")
                return SipDiagnostic("FALHOU", 200, code, f"Central respondeu SIP {code}.")
        # Do not leave the user's device ringing after test duration.
        cancel = self._request("CANCEL", target, callid, seq, branch, to_header)
        self._send(cancel)
        if ringing:
            return SipDiagnostic("TOCOU", 200, 180, "Central sinalizou toque SIP 180. Teste encerrado automaticamente.")
        return SipDiagnostic("SEM_RESPOSTA", 200, last, "Nenhuma confirmacao SIP 180/200 de chamada dentro do limite.")

    def run_registration_only(self) -> SipDiagnostic:
        """Check REGISTER response without INVITE, phone number or campaign traffic."""
        self.notify("REGISTRANDO", None, None, "Validando autenticacao SIP sem realizar ligacao.")
        registered = False
        try:
            resolved = socket.getaddrinfo(self.proxy_host or self.host,
                                           self.proxy_port if self.proxy_host else self.port,
                                           socket.AF_INET, socket.SOCK_DGRAM)
            if not resolved:
                raise SipProbeError("Dominio SIP sem endereco IPv4.")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(2)
            self.sock.connect(resolved[0][4])
            self.local_ip, self.local_port = self.sock.getsockname()
            self._register(expires=60)
            registered = True
            return SipDiagnostic("SIP_VALIDADO", 200, None,
                                 "SIP REGISTER 200 OK. Autenticacao confirmada neste teste; "
                                 "ramal nao esta permanentemente conectado.")
        except (socket.gaierror, OSError) as exc:
            raise SipProbeError("Falha de rede com central SIP; revise dominio, porta 5060 e UDP.") from exc
        finally:
            if registered:
                try:
                    self._register(expires=0)
                except (SipProbeError, OSError):
                    pass
            if self.sock:
                self.sock.close()


    def run(self) -> SipDiagnostic:
        self.notify("REGISTRANDO", None, None, "Registrando temporariamente o ramal SIP.")
        try:
            resolved = socket.getaddrinfo(self.proxy_host or self.host,
                                           self.proxy_port if self.proxy_host else self.port,
                                           socket.AF_INET, socket.SOCK_DGRAM)
            if not resolved:
                raise SipProbeError("Dominio SIP sem endereco IPv4.")
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.settimeout(2)
            self.sock.connect(resolved[0][4])
            self.local_ip, self.local_port = self.sock.getsockname()
            self._register()
            return self._call()
        except (socket.gaierror, OSError) as exc:
            raise SipProbeError("Falha de rede ao acessar a central SIP; verifique DNS, porta 5060 e UDP.") from exc
        finally:
            if self.sock:
                try:
                    self._register(expires=0)
                except (SipProbeError, OSError):
                    pass
            if self.media:
                self.media.close()
            if self.sock:
                self.sock.close()
