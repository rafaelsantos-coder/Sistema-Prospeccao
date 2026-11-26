"""Object-level WhatsApp instance ACL. Role, queue and manager rights never bypass it."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import Conversation, ConversationZApiInstance, Message, User, ZApiInstance


def may_access_instance(user: User, instance: ZApiInstance | None) -> bool:
    return bool(user and getattr(user, "active", False) and (
        instance is None or not instance.exclusive_access
        or (instance.exclusive_user_id is not None and instance.exclusive_user_id == user.id)
    ))


def require_instance_access(user: User, instance: ZApiInstance | None) -> None:
    if not may_access_instance(user, instance):
        raise HTTPException(status_code=404, detail="Conversa indisponivel.")


def instance_for_conversation(db: Session, conversation_id: int) -> ZApiInstance | None:
    # Do not filter inactive instances; archived conversations remain private.
    return db.scalar(
        select(ZApiInstance)
        .join(ConversationZApiInstance, ConversationZApiInstance.zapi_instance_id == ZApiInstance.id)
        .where(ConversationZApiInstance.conversation_id == conversation_id)
    )


def require_conversation_instance_access(db: Session, user: User, conversation_id: int) -> None:
    conversation = db.get(Conversation, conversation_id)
    if (
        conversation is not None
        and conversation.private_to_assignee
        and conversation.assigned_user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="Conversa indisponivel.")
    instance = instance_for_conversation(db, conversation_id)
    if may_access_instance(user, instance):
        return
    # Quem já é responsável pelo atendimento mantém acesso à própria conversa.
    if conversation is not None and conversation.assigned_user_id == user.id:
        return
    raise HTTPException(status_code=404, detail="Conversa indisponivel.")


def visible_message_clause(user: User):
    """Never blend protected messages into another conversation with same phone."""
    exclusive_restricted = (
        select(ConversationZApiInstance.conversation_id)
        .join(ZApiInstance, ConversationZApiInstance.zapi_instance_id == ZApiInstance.id)
        .join(Conversation, Conversation.id == ConversationZApiInstance.conversation_id)
        .where(
            ZApiInstance.exclusive_access.is_(True),
            or_(
                ZApiInstance.exclusive_user_id.is_(None),
                and_(
                    ZApiInstance.exclusive_user_id != user.id,
                    or_(
                        Conversation.assigned_user_id.is_(None),
                        Conversation.assigned_user_id != user.id,
                    ),
                ),
            ),
        )
    )
    private_restricted = select(Conversation.id).where(
        Conversation.private_to_assignee.is_(True),
        or_(
            Conversation.assigned_user_id.is_(None),
            Conversation.assigned_user_id != user.id,
        ),
    )
    return and_(
        Message.conversation_id.not_in(exclusive_restricted),
        Message.conversation_id.not_in(private_restricted),
    )


def protected_conversation(db: Session, conversation_id: int) -> bool:
    conversation = db.get(Conversation, conversation_id)
    if conversation is not None and conversation.private_to_assignee:
        return True
    inst = instance_for_conversation(db, conversation_id)
    return bool(inst and inst.exclusive_access)
