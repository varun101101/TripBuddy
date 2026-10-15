"""
The chat model every TripBuddy agent uses, served through OpenRouter.

Settings in .env:
    OPENROUTER_API_KEY=...   required
    LLM_MODEL=...            optional, defaults to openai/gpt-oss-120b
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv(Path(__file__).resolve().parent / ".env")

DEFAULT_MODEL = "openai/gpt-oss-120b"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# A rate-limit (429) reply is retried after a short wait instead of failing
# the whole plan straight away.
MAX_RETRIES = 6


def make_llm() -> ChatOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY is missing. Add OPENROUTER_API_KEY=your_key to the project .env file."
        )
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL") or DEFAULT_MODEL,
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        max_retries=MAX_RETRIES,
        timeout=180,
    )
