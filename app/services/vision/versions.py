"""Immutable prompt/agent versions carried by durable conversation jobs."""

from app.services.vision.conversation_adapter import CHAT_PROMPT_VERSION

AGENT_PROMPT_VERSION = "agent-v1"


def new_conversation_version(mode: str) -> str:
    return AGENT_PROMPT_VERSION if mode == "smart" else CHAT_PROMPT_VERSION


def supported_conversation_version(mode: str, version: str) -> bool:
    return version == CHAT_PROMPT_VERSION or (mode == "smart" and version == AGENT_PROMPT_VERSION)
