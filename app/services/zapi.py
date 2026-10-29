from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import settings


class ZApiError(RuntimeError):
    """Erro seguro da integracao Z-API, sem expor credenciais em logs."""


class ZApiNotConfigured(ZApiError):
    pass


class ZApiService:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        instance_id: str | None = None,
        instance_token: str | None = None,
        client_token: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.zapi_base_url).rstrip("/")
        self.instance_id = instance_id or settings.zapi_instance_id
        self.instance_token = instance_token or settings.zapi_instance_token
        self.client_token = client_token if client_token is not None else settings.zapi_client_token

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.instance_id and self.instance_token)

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise ZApiNotConfigured("A integracao com a Z-API ainda nao foi configurada.")

    def _headers(self, *, json_request: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {}
        if json_request:
            headers["Content-Type"] = "application/json"
        if self.client_token:
            headers["Client-Token"] = self.client_token
        return headers

    def _url(self, endpoint: str) -> str:
        self._ensure_configured()
        return (
            f"{self.base_url}/instances/{self.instance_id}"
            f"/token/{self.instance_token}/{endpoint.lstrip('/')}"
        )

    @staticmethod
    def normalize_phone(phone: str | None) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) in {10, 11}:
            digits = "55" + digits
        if not 12 <= len(digits) <= 15:
            raise ZApiError("Informe um telefone valido com DDI e DDD.")
        return digits

    @staticmethod
    def _safe_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message")
            if message:
                return str(message)[:240]
        return f"HTTP {response.status_code}"

    def status(self) -> dict[str, Any]:
        self._ensure_configured()
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(self._url("status"), headers=self._headers())
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel consultar o status da Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(f"Falha ao consultar Z-API: {self._safe_error(response)}")
        payload = response.json()
        return {
            "configured": True,
            "connected": bool(payload.get("connected")),
            "smartphone_connected": bool(payload.get("smartphoneConnected")),
            "error": payload.get("error"),
        }

    def device(self) -> dict[str, Any]:
        self._ensure_configured()
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(self._url("device"), headers=self._headers())
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel consultar o dispositivo da Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(
                f"Falha ao consultar dispositivo Z-API: {self._safe_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZApiError("A Z-API retornou dados invalidos do dispositivo.") from exc
        if not isinstance(payload, dict):
            raise ZApiError("A Z-API retornou dados invalidos do dispositivo.")
        device = payload.get("device")
        return {
            "phone": str(payload.get("phone") or "").strip(),
            "name": str(payload.get("name") or "").strip(),
            "img_url": str(payload.get("imgUrl") or "").strip(),
            "about": str(payload.get("about") or "").strip(),
            "is_business": bool(payload.get("isBusiness")),
            "original_device": str(payload.get("originalDevice") or "").strip(),
            "session_name": (
                str(device.get("sessionName") or "").strip()
                if isinstance(device, dict)
                else ""
            ),
            "device_model": (
                str(device.get("device_model") or "").strip()
                if isinstance(device, dict)
                else ""
            ),
        }

    def send_text(self, *, phone: str, message: str) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        clean_message = message.strip()
        if not clean_message:
            raise ZApiError("A mensagem nao pode ficar vazia.")
        if len(clean_message) > 4096:
            raise ZApiError("A mensagem deve ter no maximo 4.096 caracteres.")
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self._url("send-text"),
                    headers=self._headers(json_request=True),
                    json={"phone": normalized_phone, "message": clean_message},
                )
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel enviar a mensagem pela Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(f"Falha no envio pela Z-API: {self._safe_error(response)}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ZApiError("A Z-API retornou uma resposta inesperada.")
        return payload

    def send_button_list(
        self,
        *,
        phone: str,
        message: str,
        buttons: list[dict[str, str]],
    ) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        clean_message = message.strip()
        if not clean_message:
            raise ZApiError("A mensagem nao pode ficar vazia.")
        if len(clean_message) > 4096:
            raise ZApiError("A mensagem deve ter no maximo 4.096 caracteres.")

        clean_buttons: list[dict[str, str]] = []
        for index, button in enumerate(buttons[:3], start=1):
            label = str(button.get("label") or "").strip()
            if not label:
                continue
            if len(label) > 20:
                raise ZApiError("Cada botao deve ter no maximo 20 caracteres.")
            button_id = str(button.get("id") or f"reply_{index}").strip()
            clean_buttons.append({"id": button_id[:64], "label": label})
        if not clean_buttons:
            raise ZApiError("Informe ao menos um botao de resposta.")

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self._url("send-button-list"),
                    headers=self._headers(json_request=True),
                    json={
                        "phone": normalized_phone,
                        "message": clean_message,
                        "buttonList": {"buttons": clean_buttons},
                    },
                )
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel enviar os botoes pela Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(f"Falha no envio pela Z-API: {self._safe_error(response)}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZApiError("A Z-API retornou uma resposta inesperada.") from exc
        if not isinstance(payload, dict):
            raise ZApiError("A Z-API retornou uma resposta inesperada.")
        return payload

    def _send_media(
        self,
        *,
        endpoint: str,
        phone: str,
        media_field: str,
        media_url: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_phone = self.normalize_phone(phone)
        clean_url = media_url.strip()
        if not clean_url:
            raise ZApiError("A midia nao pode ficar vazia.")
        payload: dict[str, Any] = {
            "phone": normalized_phone,
            media_field: clean_url,
        }
        if extra:
            payload.update({key: value for key, value in extra.items() if value not in {None, ""}})
        try:
            with httpx.Client(timeout=90.0) as client:
                response = client.post(
                    self._url(endpoint),
                    headers=self._headers(json_request=True),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel enviar a midia pela Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(f"Falha no envio pela Z-API: {self._safe_error(response)}")
        try:
            result = response.json()
        except ValueError as exc:
            raise ZApiError("A Z-API retornou uma resposta inesperada.") from exc
        if not isinstance(result, dict):
            raise ZApiError("A Z-API retornou uma resposta inesperada.")
        return result

    def send_image(
        self,
        *,
        phone: str,
        image_url: str,
        caption: str = "",
    ) -> dict[str, Any]:
        return self._send_media(
            endpoint="send-image",
            phone=phone,
            media_field="image",
            media_url=image_url,
            extra={"caption": caption.strip(), "viewOnce": False},
        )

    def send_video(
        self,
        *,
        phone: str,
        video_url: str,
        caption: str = "",
    ) -> dict[str, Any]:
        return self._send_media(
            endpoint="send-video",
            phone=phone,
            media_field="video",
            media_url=video_url,
            extra={"caption": caption.strip(), "viewOnce": False},
        )

    def send_audio(
        self,
        *,
        phone: str,
        audio_url: str,
    ) -> dict[str, Any]:
        return self._send_media(
            endpoint="send-audio",
            phone=phone,
            media_field="audio",
            media_url=audio_url,
            extra={"viewOnce": False, "waveform": True},
        )

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
            raise ZApiError("Extensao de documento invalida.")
        return self._send_media(
            endpoint=f"send-document/{clean_extension}",
            phone=phone,
            media_field="document",
            media_url=document_url,
            extra={
                "fileName": file_name.strip() or f"arquivo.{clean_extension}",
                "caption": caption.strip(),
            },
        )

    def webhook_state(self) -> dict[str, Any]:
        self._ensure_configured()
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(self._url("me"), headers=self._headers())
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel consultar os webhooks da Z-API.") from exc
        if response.status_code >= 400:
            raise ZApiError(
                f"Falha ao consultar webhooks Z-API: {self._safe_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ZApiError("A Z-API retornou dados invalidos da instancia.") from exc
        if not isinstance(payload, dict):
            raise ZApiError("A Z-API retornou dados invalidos da instancia.")
        return {
            "received_callback_url": str(payload.get("receivedCallbackUrl") or "").strip(),
            "received_and_delivery_callback_url": str(
                payload.get("receivedAndDeliveryCallbackUrl") or ""
            ).strip(),
            "delivery_callback_url": str(payload.get("deliveryCallbackUrl") or "").strip(),
            "presence_callback_url": str(payload.get("presenceChatCallbackUrl") or "").strip(),
            "message_status_callback_url": str(
                payload.get("messageStatusCallbackUrl") or ""
            ).strip(),
            "receive_sent_by_me": bool(payload.get("receiveCallbackSentByMe")),
            "connected": bool(payload.get("connected")),
        }


    def configure_webhooks(self, webhook_url: str) -> dict[str, Any]:
        clean_url = webhook_url.strip()
        if not clean_url.startswith("https://"):
            raise ZApiError("O webhook da Z-API precisa usar HTTPS.")
        try:
            with httpx.Client(timeout=20.0) as client:
                all_response = client.put(
                    self._url("update-every-webhooks"),
                    headers=self._headers(json_request=True),
                    json={"value": clean_url, "notifySentByMe": True},
                )
                if all_response.status_code >= 400:
                    raise ZApiError(
                        f"Falha ao configurar webhooks: {self._safe_error(all_response)}"
                    )

                # Reforça explicitamente o webhook de mensagens recebidas.
                # A Z-API oferece um endpoint dedicado para esse callback e ele
                # é crítico para transformar a resposta do cliente em AIJob.
                received_response = client.put(
                    self._url("update-webhook-received"),
                    headers=self._headers(json_request=True),
                    json={"value": clean_url},
                )
                if received_response.status_code >= 400:
                    raise ZApiError(
                        "Falha ao configurar webhook de mensagens recebidas: "
                        f"{self._safe_error(received_response)}"
                    )
        except httpx.HTTPError as exc:
            raise ZApiError("Nao foi possivel configurar os webhooks da Z-API.") from exc

        try:
            all_payload = all_response.json()
        except ValueError:
            all_payload = {"ok": True}
        try:
            received_payload = received_response.json()
        except ValueError:
            received_payload = {"ok": True}

        state = self.webhook_state()
        configured_url = state.get("received_callback_url") or ""
        if configured_url.rstrip("/") != clean_url.rstrip("/"):
            raise ZApiError(
                "Webhook de mensagens recebidas nao confirmou a URL configurada."
            )

        return {
            "all": all_payload,
            "received": received_payload,
            "notify_sent_by_me": True,
            "webhook_state": state,
        }

