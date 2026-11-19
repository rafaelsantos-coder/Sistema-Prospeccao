from __future__ import annotations

import logging
import os
import time

from app.database import SessionLocal
from app.services.paced_outbound import process_one_pending
from app.services.campaign_queue import claim_next_recipient, process_recipient
from app.services.dispatch_queue import claim_next_dispatch_recipient, process_dispatch_recipient
from app.workers.graceful_shutdown import GracefulShutdown


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_campaign_worker")
SEND_INTERVAL = max(5, int(os.getenv("CAMPAIGN_SEND_INTERVAL_SECONDS", "20")))
IDLE_INTERVAL = max(2, int(os.getenv("CAMPAIGN_IDLE_INTERVAL_SECONDS", "5")))


def run() -> None:
    shutdown = GracefulShutdown(LOG)
    shutdown.install()
    # Bloqueio global de emergencia solicitado pelo gestor em 17/11/2026.
    # O atendimento receptivo de WhatsApp e os agentes IA usam outro worker.
    # A retomada exige remover a pausa explicitamente na configuracao.
    campaigns_paused = os.getenv("CAMPAIGN_DISPATCH_PAUSED", "true").strip().lower() not in {"0", "false", "off", "no"}
    if campaigns_paused:
        LOG.warning("Campanhas pausadas; somente mensagens manuais agendadas serao processadas.")
    LOG.info("Worker iniciado. Campanhas pausadas=%s; intervalo=%ss", campaigns_paused, SEND_INTERVAL)
    while not shutdown.requested:
        # Outbox manual independente das campanhas e da IA, inclusive durante pausa.
        try:
            pacing_result = process_one_pending()
        except Exception:
            LOG.exception("Falha do worker de mensagens manuais")
            pacing_result = "error"
        if campaigns_paused:
            shutdown.wait(1 if pacing_result == "sent" else 3)
            continue
        db = SessionLocal()
        outcome = "idle"
        used_dispatch = False
        try:
            dispatch_recipient = claim_next_dispatch_recipient(db)
            if dispatch_recipient:
                used_dispatch = True
                # Reserva e tentativa devem estar confirmadas ANTES de acessar a Z-API.
                # SIGKILL após ACK nunca devolve um envio ambíguo a PENDENTE.
                db.commit()
                outcome = process_dispatch_recipient(db, dispatch_recipient)
                db.commit()
                LOG.info(
                    "dispatch_recipient=%s dispatch_id=%s outcome=%s attempts=%s",
                    dispatch_recipient.id,
                    dispatch_recipient.dispatch_id,
                    outcome,
                    dispatch_recipient.attempts,
                )
            else:
                recipient = claim_next_recipient(db)
                if not recipient:
                    db.commit()
                else:
                    db.commit()  # confirma ENVIANDO antes da requisição externa
                    outcome = process_recipient(db, recipient)
                    db.commit()
                    LOG.info(
                        "campaign_recipient=%s outcome=%s attempts=%s",
                        recipient.id,
                        outcome,
                        recipient.attempts,
                    )
        except Exception:
            db.rollback()
            LOG.exception("Falha inesperada no campaign worker")
            outcome = "error"
        finally:
            db.close()

        if used_dispatch:
            shutdown.wait(IDLE_INTERVAL)
        elif outcome == "sent":
            shutdown.wait(SEND_INTERVAL)
        elif outcome == "limit":
            shutdown.wait(60)
        else:
            shutdown.wait(IDLE_INTERVAL)

    LOG.info("Worker encerrado apos a operacao em curso.")


if __name__ == "__main__":
    run()
