from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class MediaProcessingError(RuntimeError):
    pass


AUDIO_PASSTHROUGH_MIMES = {
    "audio/ogg",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/aac",
    "audio/amr",
}


def normalize_audio(
    data: bytes,
    *,
    filename: str,
    mime_type: str,
) -> tuple[bytes, str, str]:
    clean_mime = (mime_type or "").split(";")[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()
    if clean_mime in AUDIO_PASSTHROUGH_MIMES or suffix in {".ogg", ".opus", ".mp3", ".m4a", ".aac", ".amr"}:
        return data, filename or "audio.ogg", clean_mime or "audio/ogg"

    if not shutil.which("ffmpeg"):
        raise MediaProcessingError("Conversao de audio indisponivel no servidor.")

    input_suffix = suffix or ".webm"
    with tempfile.TemporaryDirectory(prefix="sulnet-audio-") as tmp:
        input_path = Path(tmp) / f"input{input_suffix}"
        output_path = Path(tmp) / "audio.ogg"
        input_path.write_bytes(data)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(input_path),
                    "-vn",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "32k",
                    str(output_path),
                ],
                check=True,
                timeout=90,
                capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise MediaProcessingError(
                "Nao foi possivel converter o audio gravado."
            ) from exc
        output = output_path.read_bytes()
        if not output:
            raise MediaProcessingError("O audio convertido ficou vazio.")
        return output, "audio.ogg", "audio/ogg"
