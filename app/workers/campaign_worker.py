from __future__ import annotations

import logging
import os
import time

from app.database import SessionLocal
from app.services.campaign_queue import claim_next_recipient, process_recipient
from app.services.dispatch_queue import claim_next_dispatch_recipient, process_dispatch_recipient


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_campaign_worker")
SEND_INTERVAL = max(5, int(os.getenv("CAMPAIGN_SEND_INTERVAL_SECONDS", "20")))
IDLE_INTERVAL = max(2, int(os.getenv("CAMPAIGN_IDLE_INTERVAL_SECONDS", "5")))


def run() -> None:
    LOG.info("Campaign worker iniciado. Intervalo entre envios: %ss", SEND_INTERVAL)
    while True:
        db = SessionLocal()
        outcome = "idle"
        used_dispatch = False
        try:
            dispatch_recipient = claim_next_dispatch_recipient(db)
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
                if not recipient:
                    db.commit()
                else:
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
            time.sleep(IDLE_INTERVAL)
        elif outcome == "sent":
            time.sleep(SEND_INTERVAL)
        elif outcome == "limit":
            time.sleep(60)
        else:
            time.sleep(IDLE_INTERVAL)


if __name__ == "__main__":
    run()
