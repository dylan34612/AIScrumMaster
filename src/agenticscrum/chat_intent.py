"""Chat intent classification for single-composer board management."""

from __future__ import annotations

import re
from enum import Enum

from agenticscrum.config import Settings


class ChatIntent(str, Enum):
    ANSWER = "answer"
    PROPOSE = "propose"
    CLARIFY = "clarify"
    REVIEW = "review"


_PROPOSE_HINTS = re.compile(
    r"\b("
    r"move|assign|create|add|update|close|done|comment|set|promote|transition|"
    r"estimate|story\s*points|acceptance|mark|put|change\s+state|new\s+(pbi|bug|task|feature)"
    r")\b",
    re.IGNORECASE,
)
_REVIEW_HINTS = re.compile(
    r"\b(review\s+the\s+board|daily\s+review|board\s+hygiene|what'?s\s+blocked|"
    r"cleanup\s+the\s+board|scan\s+the\s+board)\b",
    re.IGNORECASE,
)
_QUESTION_HINTS = re.compile(
    r"^\s*(what|who|when|where|why|how|which|status|show|list|tell|explain|summarize)\b",
    re.IGNORECASE,
)


def classify_intent_heuristic(text: str) -> ChatIntent:
    """Fast rule-based intent classification."""

    cleaned = (text or "").strip()
    if not cleaned:
        return ChatIntent.CLARIFY
    if _REVIEW_HINTS.search(cleaned):
        return ChatIntent.REVIEW
    if _PROPOSE_HINTS.search(cleaned) and (
        re.search(r"#?\d{2,}", cleaned) or len(cleaned.split()) >= 4
    ):
        return ChatIntent.PROPOSE
    if _QUESTION_HINTS.search(cleaned) and not _PROPOSE_HINTS.search(cleaned):
        return ChatIntent.ANSWER
    if _PROPOSE_HINTS.search(cleaned):
        return ChatIntent.PROPOSE
    if len(cleaned.split()) <= 3 and not cleaned.endswith("?"):
        return ChatIntent.CLARIFY
    return ChatIntent.ANSWER


async def classify_chat_intent(settings: Settings, text: str) -> ChatIntent:
    """Classify user chat intent; falls back to heuristics on LLM failure."""

    heuristic = classify_intent_heuristic(text)
    # Short / obvious cases skip the LLM round-trip.
    if heuristic in {ChatIntent.REVIEW, ChatIntent.CLARIFY}:
        return heuristic
    if heuristic == ChatIntent.PROPOSE and re.search(r"#?\d{3,}", text or ""):
        return ChatIntent.PROPOSE

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from agenticscrum.llm.client import build_chat_model

        model = build_chat_model(settings)
        response = await model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Classify the user's message for an AI scrum master chat. "
                        "Reply with exactly one word: answer, propose, clarify, or review.\n"
                        "- answer: questions about board status, risks, explanations\n"
                        "- propose: requests to create/update/move/assign/comment on work items\n"
                        "- clarify: too vague; need 1-2 clarifying questions\n"
                        "- review: ask for a board hygiene / daily review scan"
                    )
                ),
                HumanMessage(content=text.strip()),
            ]
        )
        raw = str(response.content or "").strip().lower()
        token = re.split(r"[\s,.;:]+", raw)[0] if raw else ""
        for intent in ChatIntent:
            if token == intent.value:
                return intent
    except Exception:
        pass
    return heuristic
