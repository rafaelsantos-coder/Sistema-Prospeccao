from __future__ import annotations

import logging
import os
import time

from app.database import SessionLocal
from app.workers.graceful_shutdown import GracefulShutdown
from app.services.campaign_queue import claim_next_recipient, process_recipient
from app.services.dispatch_queue import claim_next_dispatch_recipient, process_dispatch_recipient


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_campaign_worker")
SEND_INTERVAL = max(5, int(os.getenv("CAMPAIGN_SEND_INTERVAL_SECONDS", "20")))
IDLE_INTERVAL = max(2, int(os.getenv("CAMPAIGN_IDLE_INTERVAL_SECONDS", "5")))


def run() -> None:
    shutdown = GracefulShutdown(LOG)
    shutdown.install()

    # O bloqueio global não deve manter um worker pendurado durante deploy.
    if os.getenv("CAMPAIGN_DISPATCH_PAUSED", "true").strip().lower() not in {"0", "false", "off", "no"}:
        LOG.warning("SULNET_DISPAROS_PAUSADOS_POR_SOLICITACAO: nenhum envio de campanha sera processado")
        while not shutdown.wait(60):
            pass
        LOG.info("Campaign worker encerrado sem processar novos contatos.")
        return

    LOG.info("Campaign worker iniciado. Intervalo entre envios: %ss", SEND_INTERVAL)
    while not shutdown.requested:
        db = SessionLocal()
        outcome = "idle"
        used_dispatch = False
        try:
            dispatch_recipient = claim_next_dispatch_recipient(db)
            if shutdown.requested:
                # Um claim não enviado deve voltar a PENDENTE sem consumir o contato.
                db.rollback()
                break
            if dispatch_recipient:
                used_dispatch = True
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
                if shutdown.requested:
                    db.rollback()
                    break
                if not recipient:
                    db.commit()
                else:
                    outcome = process_recipient(db, recipient)
                    # Se SIGTERM chegou durante o envio, gravar o resultado antes de sair.
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
            delay = IDLE_INTERVAL
        elif outcome == "sent":
            delay = SEND_INTERVAL
        elif outcome == "limit":
            delay = 60
        else:
            delay = IDLE_INTERVAL
        if shutdown.wait(delay):
            break

    LOG.info("Campaign worker encerrado graciosamente.")


if __name__ == "__main__":
    run()
