from sqlalchemy import (
    Column, Integer, BigInteger, String, Text,
    Date, DateTime, Boolean, ForeignKey
)
from sqlalchemy.sql import func
from app.db import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_user_id = Column(BigInteger, unique=True, nullable=False)
    tg_chat_id = Column(BigInteger, nullable=False)
    consent = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class ScheduleMessage(Base):
    __tablename__ = "schedule_messages"

    id = Column(Integer, primary_key=True)
    day_index = Column(Integer)
    send_date = Column(Date)
    send_at = Column(DateTime, nullable=True)
    type = Column(String)
    text = Column(Text)
    sent_at = Column(DateTime, nullable=True)
    attempts = Column(Integer, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    tg_message_id = Column(BigInteger)
    text = Column(Text, nullable=True)
    media_type = Column(String, nullable=True)
    media_file_id = Column(String, nullable=True)
    raw = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ActionRule(Base):
    __tablename__ = "action_rules"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    days_to_extend = Column(Integer, nullable=False)
    active = Column(Boolean, default=True)


class ActionEvent(Base):
    __tablename__ = "action_events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    rule_id = Column(Integer, ForeignKey("action_rules.id"))
    raw_text = Column(Text, nullable=True)
    old_expires_at = Column(DateTime, nullable=True)
    new_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
