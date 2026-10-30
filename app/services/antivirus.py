from __future__ import annotations

import socket
import struct

from app.config import settings


class AntivirusError(RuntimeError):
    pass


class MalwareDetectedError(AntivirusError):
    pass


def scan_bytes(data: bytes) -> None:
    if not settings.clamav_host:
        if settings.clamav_required:
            raise AntivirusError("Antivírus obrigatório não está configurado.")
        return

    try:
        with socket.create_connection(
            (settings.clamav_host, settings.clamav_port),
            timeout=5,
        ) as sock:
            sock.settimeout(20)
            sock.sendall(b"zINSTREAM\0")
            for offset in range(0, len(data), 64 * 1024):
                chunk = data[offset : offset + 64 * 1024]
                sock.sendall(struct.pack("!I", len(chunk)))
                sock.sendall(chunk)
            sock.sendall(struct.pack("!I", 0))

            response = bytearray()
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                response.extend(part)
                if b"\0" in part:
                    break
    except OSError as exc:
        if settings.clamav_required:
            raise AntivirusError(
                "Não foi possível validar o arquivo no antivírus."
            ) from exc
        return

    result = response.decode("utf-8", errors="replace").strip("\0\r\n ")
    if " FOUND" in result:
        signature = result.rsplit(" FOUND", 1)[0].split(": ", 1)[-1]
        raise MalwareDetectedError(
            f"Arquivo bloqueado pelo antivírus: {signature[:120]}"
        )
    if not result.endswith(" OK"):
        if settings.clamav_required:
            raise AntivirusError(
                "O antivírus não confirmou o arquivo como seguro."
            )
