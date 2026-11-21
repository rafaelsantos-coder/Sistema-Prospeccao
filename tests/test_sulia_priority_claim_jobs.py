"""Os quatro resgates urgentes sao processados antes do backlog comum."""
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import AIJob, Contact, Conversation, Message
from app.services.ai_sdr import claim_ai_job
from app.timeutils import utcnow
from test_ai_all_queues_coverage import _database, _setup


def test_priority_rescue_job_claimed_ahead_of_older_normal_job():
    engine = _database()
    try:
        with Session(engine) as db:
            agent_id, queue_id, _, _ = _setup(db)
            jobs = []
            for cid, phone, minutes in (
                (30, "5555988000030", 10),
                (4701, "5555988004701", 1),
            ):
                contact = Contact(name="Cliente de teste", phone=phone)
                db.add(contact)
                db.flush()
                conv = Conversation(
                    id=cid, contact_id=contact.id,
                    source="WHATSAPP", team="DIGITAL", status="EM_FILA",
                )
                db.add(conv)
                db.flush()
                inbound = Message(
                    conversation_id=cid, contact_id=contact.id,
                    direction="INBOUND", message_type="TEXT",
                    status="RECEIVED", body="Quero contratar",
                )
                db.add(inbound)
                db.flush()
                job = AIJob(
                    agent_id=agent_id, queue_id=queue_id,
                    conversation_id=cid, message_id=inbound.id,
                    status="PENDENTE",
                    available_at=utcnow() - timedelta(minutes=minutes),
                )
                db.add(job)
                db.flush()
                jobs.append(job.id)
            db.commit()
            chosen = claim_ai_job(db)
            assert chosen is not None
            assert chosen.conversation_id == 4701
            assert chosen.status == "PROCESSANDO"
            assert chosen.id == jobs[1]
            assert len(jobs) == 2
    finally:
        engine.dispose()
