from __future__ import annotations

import logging
import os
import time

from app.database import Base, SessionLocal, engine
from app.services.campaign_queue import claim_next_recipient, process_recipient


logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("sulnet_campaign_worker")
SEND_INTERVAL = max(5, int(os.getenv("CAMPAIGN_SEND_INTERVAL_SECONDS", "20")))
IDLE_INTERVAL = max(2, int(os.getenv("CAMPAIGN_IDLE_INTERVAL_SECONDS", "5")))


def run() -> None:
    Base.metadata.create_all(bind=engine)
    LOG.info("Campaign worker iniciado. Intervalo entre envios: %ss", SEND_INTERVAL)
    while True:
        db = SessionLocal()
        outcome = "idle"
        try:
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

        if outcome == "sent":
            time.sleep(SEND_INTERVAL)
        elif outcome == "limit":
            time.sleep(60)
        else:
            time.sleep(IDLE_INTERVAL)


if __name__ == "__main__":
    run()
