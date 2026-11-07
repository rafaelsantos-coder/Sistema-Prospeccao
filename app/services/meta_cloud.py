"""Cliente da WhatsApp Cloud API oficial (Meta).

Expõe os mesmos métodos de envio do ``ZApiService`` (send_text,
send_button_list, send_image, ...) para que campanhas, disparos, Vendedor IA e
inbox funcionem sem saber qual provedor está por trás da fila. As exceções
herdam de ``ZApiError``/``ZApiNotConfigured`` pelo mesmo motivo: todo código
que já trata falha de envio da Z-API trata a da Meta do mesmo jeito.

O token vai sempre no cabeçalho Authorization, nunca na URL, então os logs do
httpx não expõem credencial.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import settings
from app.services.zapi import ZApiError, ZApiNotConfigured, ZApiService


class MetaCloudError(ZApiError):
    """Falha de envio/consulta na Cloud API da Meta."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MetaNotConfigured(ZApiNotConfigured):
    """Número Meta sem credenciais completas."""


class MetaWindowClosed(MetaCloudError):
    """Fora da janela de 24h: a Meta só aceita template aprovado."""


class MetaTemplateRequired(MetaWindowClosed):
    """Disparo/campanha para quem não falou nas últimas 24h e sem template."""


# Limites da Cloud API.
TEXT_LIMIT = 4096
INTERACTIVE_BODY_LIMIT = 1024
BUTTON_TITLE_LIMIT = 20
MEDIA_DOWNLOAD_LIMIT = 100 * 1024 * 1024

WINDOW_CLOSED_CODES = {131047}
FRIENDLY_ERRORS: dict[int, str] = {
    131047: (
        "Fora da janela de 24h da Meta: o cliente não respondeu nas últimas 24 horas. "
        "Só é possível enviar um template aprovado."
    ),
    131026: "A Meta não conseguiu entregar: o número pode não ter WhatsApp.",
    131049: "A Meta não entregou para preservar o engajamento (limite de marketing por usuário).",
    131050: "O cliente bloqueou mensagens de marketing deste número.",
    131056: "Muitas mensagens para o mesmo cliente em pouco tempo. Aguarde e tente de novo.",
    131063: "Templates de marketing estão desativados na Cloud API desta conta.",
    130429: "Limite de envio da Meta atingido. Aguarde e tente de novo.",
    131048: "A Meta limitou este número por denúncias de spam.",
    132000: "A quantidade de variáveis não confere com o template aprovado.",
    132001: "O template não existe na Meta (nome ou idioma diferente).",
    132005: "O texto do template ficou longo demais depois de preencher as variáveis.",
    132007: "O conteúdo do template viola uma política da Meta.",
    132012: "Formato de variável inválido para este template.",
    132015: "Template pausado pela Meta por baixa qualidade.",
    132016: "Template desativado pela Meta.",
    190: "Token de acesso da Meta inválido ou expirado. Gere um token permanente de System User.",
    200: "O token da Meta não tem permissão whatsapp_business_messaging.",
    368: "A conta foi temporariamente bloqueada pela Meta por violação de política.",
}


def _clean_text(value: str | None) -> str:
    return (value or "").strip()


def template_param_text(value: str | None) -> str:
    """Texto aceito em variável de template: sem quebras, tabs ou 4+ espaços seguidos."""
    clean = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    clean = re.sub(r" {4,}", "   ", clean)
    return clean.strip()


class MetaCloudService:
    provider = "META"

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        waba_id: str = "",
        verify_token: str = "",
        graph_version: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.phone_number_id = _clean_text(phone_number_id)
        self.access_token = _clean_text(access_token)
        self.waba_id = _clean_text(waba_id)
        self.verify_token = verify_token or ""
        self.graph_version = _clean_text(graph_version) or settings.meta_graph_api_version
        self.base_url = (base_url or settings.meta_graph_base_url).rstrip("/")

    # ------------------------------------------------------------------ infra

    @property
    def configured(self) -> bool:
        return bool(self.phone_number_id and self.access_token)

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise MetaNotConfigured("O número da API oficial da Meta não está configurado.")

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.graph_version}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    normalize_phone = staticmethod(ZApiService.normalize_phone)

    @staticmethod
    def _error_from_response(response: httpx.Response) -> MetaCloudError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        code: int | None = None
        detail = ""
        if isinstance(error, dict):
            try:
                code = int(error.get("code")) if error.get("code") is not None else None
            except (TypeError, ValueError):
                code = None
            data = error.get("error_data")
            if isinstance(data, dict) and data.get("details"):
                detail = str(data["details"])
            detail = detail or str(error.get("error_user_msg") or error.get("message") or "")
        friendly = FRIENDLY_ERRORS.get(code or -1)
        message = friendly or (detail[:240] if detail else f"HTTP {response.status_code}")
        if code is not None:
            message = f"{message} (Meta {code})"
        cls = MetaWindowClosed if code in WINDOW_CLOSED_CODES else MetaCloudError
        return cls(f"Falha na API oficial da Meta: {message}", code=code)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
        action: str = "falar com a Meta",
    ) -> dict[str, Any]:
        self._ensure_configured()
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method,
                    self._url(path),
                    headers=self._headers(),
                    json=json,
                    params=params,
                )
        except httpx.HTTPError as exc:
            raise MetaCloudError(f"Não foi possível {action}.") from exc
        if response.status_code >= 400:
            raise self._error_from_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MetaCloudError("A Meta retornou uma resposta inesperada.") from exc
        if not isinstance(payload, dict):
            raise MetaCloudError("A Meta retornou uma resposta inesperada.")
        return payload

    def _send(self, phone: str, body: dict[str, Any]) -> dict[str, Any]:
        to = self.normalize_phone(phone)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            **body,
        }
        result = self._request(
            "POST",
            f"{self.phone_number_id}/messages",
            json=payload,
            timeout=60.0,
            action="enviar a mensagem pela Meta",
        )
        messages = result.get("messages")
        message_id = ""
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            message_id = str(messages[0].get("id") or "")
        contacts = result.get("contacts")
        wa_id = ""
        if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
            wa_id = str(contacts[0].get("wa_id") or "")
        if not message_id:
            raise MetaCloudError("A Meta aceitou a requisição mas não devolveu o ID da mensagem.")
        # Mesmo formato que os chamadores já leem da Z-API.
        return {"messageId": message_id, "id": message_id, "provider": "META", "wa_id": wa_id}

    # ----------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        info = self.phone_info()
        return {
            "configured": True,
            "connected": True,
            "smartphone_connected": True,
            "error": None,
            **info,
        }

    def phone_info(self) -> dict[str, Any]:
        payload = self._request(
            "GET",
            self.phone_number_id,
            params={"fields": "display_phone_number,verified_name,quality_rating,platform_type"},
            timeout=15.0,
            action="consultar o número na Meta",
        )
        return {
            "display_phone_number": str(payload.get("display_phone_number") or "").strip(),
            "verified_name": str(payload.get("verified_name") or "").strip(),
            "quality_rating": str(payload.get("quality_rating") or "").strip(),
            "platform_type": str(payload.get("platform_type") or "").strip(),
        }

    def device(self) -> dict[str, Any]:
        info = self.phone_info()
        return {
            "phone": re.sub(r"\D", "", info["display_phone_number"]),
            "name": info["verified_name"],
            "is_business": True,
            "quality_rating": info["quality_rating"],
        }

    def configure_webhooks(self, webhook_url: str) -> dict[str, Any]:
        """Inscreve o app na WABA apontando o callback para o Prospecta.

        A Meta valida o callback na hora (GET com hub.challenge), então o número
        precisa já estar salvo no banco antes desta chamada.
        """
        if not self.waba_id:
            raise MetaNotConfigured("Informe o ID da conta do WhatsApp Business (WABA).")
        body: dict[str, Any] = {}
        if webhook_url:
            body = {"override_callback_uri": webhook_url, "verify_token": self.verify_token}
        return self._request(
            "POST",
            f"{self.waba_id}/subscribed_apps",
            json=body or None,
            timeout=30.0,
            action="inscrever o webhook na Meta",
        )

    # ------------------------------------------------------------------ envio

    def send_text(self, *, phone: str, message: str) -> dict[str, Any]:
        clean = _clean_text(message)
        if not clean:
            raise MetaCloudError("A mensagem não pode ficar vazia.")
        if len(clean) > TEXT_LIMIT:
            raise MetaCloudError("A mensagem deve ter no máximo 4.096 caracteres.")
        return self._send(phone, {"type": "text", "text": {"preview_url": True, "body": clean}})

    def _interactive_body(self, phone: str, message: str) -> tuple[str, list[str]]:
        """Corpo de mensagem interativa (máx. 1.024). Texto maior vai antes, como texto."""
        clean = _clean_text(message)
        if not clean:
            raise MetaCloudError("A mensagem não pode ficar vazia.")
        if len(clean) <= INTERACTIVE_BODY_LIMIT:
            return clean, []
        sent = self.send_text(phone=phone, message=clean)
        return "Escolha uma opção:", [sent["messageId"]]

    def send_button_list(
        self,
        *,
        phone: str,
        message: str,
        buttons: list[dict[str, str]],
    ) -> dict[str, Any]:
        clean_buttons: list[dict[str, Any]] = []
        for index, button in enumerate(buttons[:3], start=1):
            label = _clean_text(str(button.get("label") or ""))
            if not label:
                continue
            if len(label) > BUTTON_TITLE_LIMIT:
                raise MetaCloudError("Cada botão deve ter no máximo 20 caracteres.")
            button_id = _clean_text(str(button.get("id") or f"reply_{index}"))[:256]
            clean_buttons.append({"type": "reply", "reply": {"id": button_id, "title": label}})
        if not clean_buttons:
            raise MetaCloudError("Informe ao menos um botão de resposta.")
        body, previous_ids = self._interactive_body(phone, message)
        result = self._send(
            phone,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {"buttons": clean_buttons},
                },
            },
        )
        if previous_ids:
            # O texto longo é a mensagem "principal" para histórico e status.
            result = {**result, "messageId": previous_ids[0], "id": previous_ids[0],
                      "extra_message_ids": [result["messageId"]]}
        return result

    def send_link_button(
        self,
        *,
        phone: str,
        message: str,
        label: str,
        url: str,
    ) -> dict[str, Any]:
        clean_url = _clean_text(url)
        if not clean_url.startswith(("https://", "http://")):
            raise MetaCloudError("O botão precisa de um link http(s) válido.")
        body, previous_ids = self._interactive_body(phone, message)
        result = self._send(
            phone,
            {
                "type": "interactive",
                "interactive": {
                    "type": "cta_url",
                    "body": {"text": body},
                    "action": {
                        "name": "cta_url",
                        "parameters": {
                            "display_text": (_clean_text(label) or "Abrir")[:BUTTON_TITLE_LIMIT],
                            "url": clean_url,
                        },
                    },
                },
            },
        )
        if previous_ids:
            result = {**result, "messageId": previous_ids[0], "id": previous_ids[0],
                      "extra_message_ids": [result["messageId"]]}
        return result

    def _send_media(
        self,
        *,
        phone: str,
        kind: str,
        media_url: str,
        caption: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        clean_url = _clean_text(media_url)
        if not clean_url:
            raise MetaCloudError("A mídia não pode ficar vazia.")
        media: dict[str, Any] = {"link": clean_url}
        if caption.strip() and kind in {"image", "video", "document"}:
            media["caption"] = caption.strip()[:1024]
        if filename and kind == "document":
            media["filename"] = filename
        return self._send(phone, {"type": kind, kind: media})

    def send_image(self, *, phone: str, image_url: str, caption: str = "") -> dict[str, Any]:
        return self._send_media(phone=phone, kind="image", media_url=image_url, caption=caption)

    def send_video(self, *, phone: str, video_url: str, caption: str = "") -> dict[str, Any]:
        return self._send_media(phone=phone, kind="video", media_url=video_url, caption=caption)

    def send_audio(self, *, phone: str, audio_url: str) -> dict[str, Any]:
        return self._send_media(phone=phone, kind="audio", media_url=audio_url)

    def send_document(
        self,
        *,
        phone: str,
        document_url: str,
        extension: str,
        file_name: str,
        caption: str = "",
    ) -> dict[str, Any]:
        clean_extension = re.sub(r"[^a-zA-Z0-9]", "", extension or "").lower()
        if not clean_extension or len(clean_extension) > 12:
            raise MetaCloudError("Extensão de documento inválida.")
        name = file_name.strip() or f"arquivo.{clean_extension}"
        return self._send_media(
            phone=phone,
            kind="document",
            media_url=document_url,
            caption=caption,
            filename=name,
        )

    def send_template(
        self,
        *,
        phone: str,
        name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not _clean_text(name) or not _clean_text(language):
            raise MetaCloudError("Template sem nome ou idioma.")
        template: dict[str, Any] = {"name": name.strip(), "language": {"code": language.strip()}}
        if components:
            template["components"] = components
        return self._send(phone, {"type": "template", "template": template})

    # -------------------------------------------------------------- templates

    def list_templates(self, *, max_pages: int = 20) -> list[dict[str, Any]]:
        if not self.waba_id:
            raise MetaNotConfigured("Informe o ID da conta do WhatsApp Business (WABA).")
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "fields": "id,name,language,status,category,components",
            "limit": 100,
        }
        after = ""
        for _ in range(max_pages):
            if after:
                params["after"] = after
            payload = self._request(
                "GET",
                f"{self.waba_id}/message_templates",
                params=params,
                timeout=30.0,
                action="buscar os templates na Meta",
            )
            data = payload.get("data")
            if isinstance(data, list):
                items.extend(item for item in data if isinstance(item, dict))
            paging = payload.get("paging") if isinstance(payload.get("paging"), dict) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
            after = str(cursors.get("after") or "")
            if not after or not paging.get("next"):
                break
        return items

    # ------------------------------------------------------------------ mídia

    def media_info(self, media_id: str) -> dict[str, Any]:
        clean = re.sub(r"[^0-9A-Za-z_-]", "", media_id or "")
        if not clean:
            raise MetaCloudError("Mídia sem identificador.")
        return self._request(
            "GET",
            clean,
            params={"phone_number_id": self.phone_number_id},
            timeout=20.0,
            action="consultar a mídia na Meta",
        )

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        info = self.media_info(media_id)
        url = str(info.get("url") or "")
        if not url.startswith("https://"):
            raise MetaCloudError("A Meta não devolveu um link de mídia válido.")
        try:
            size = int(info.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MEDIA_DOWNLOAD_LIMIT:
            raise MetaCloudError("Mídia recebida maior que o limite permitido.")
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                response = client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MetaCloudError("Não foi possível baixar a mídia da Meta.") from exc
        if response.status_code >= 400:
            raise MetaCloudError(f"Não foi possível baixar a mídia da Meta (HTTP {response.status_code}).")
        data = response.content
        if len(data) > MEDIA_DOWNLOAD_LIMIT:
            raise MetaCloudError("Mídia recebida maior que o limite permitido.")
        mime = str(info.get("mime_type") or response.headers.get("content-type") or "")
        return data, mime.split(";")[0].strip().lower()
