from __future__ import annotations

from sqlalchemy import select

from agenticscrum.config import Settings
from agenticscrum.db import init_db, session_scope
from agenticscrum.models import ChatMessage, ChatRole, ChatSession


def test_chat_session_and_message_round_trip(tmp_path) -> None:
    settings = Settings(app_db_path=str(tmp_path / "test.db"))
    init_db(settings)
    with session_scope(settings) as session:
        chat = ChatSession(title="Test Chat")
        session.add(chat)
        session.flush()
        session.add(ChatMessage(session_id=chat.id, role=ChatRole.USER, content="Hello"))
        session.flush()

    with session_scope(settings) as session:
        chat = session.scalar(select(ChatSession).where(ChatSession.title == "Test Chat"))
        assert chat is not None
        messages = list(session.scalars(select(ChatMessage).where(ChatMessage.session_id == chat.id)))
        assert len(messages) == 1
        assert messages[0].role == ChatRole.USER
        assert messages[0].content == "Hello"

