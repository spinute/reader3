"""Provider adapters for reader3's in-page AI chat."""

from __future__ import annotations

import importlib.util
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator


ProviderName = Literal["openai", "anthropic", "openai-compatible", "apple-foundation"]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=120_000)


class LLMChatRequest(BaseModel):
    provider: ProviderName
    model: str = Field(default="", max_length=160)
    api_token: str = Field(default="", max_length=1_000)
    base_url: str = Field(default="", max_length=500)
    instructions: str = Field(default="You are a thoughtful reading assistant.", max_length=10_000)
    messages: list[ChatMessage] = Field(min_length=1, max_length=60)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Base URL must be an http:// or https:// URL")
        return value.rstrip("/")


class LLMProviderError(RuntimeError):
    pass


def apple_foundation_status() -> dict[str, object]:
    if importlib.util.find_spec("apple_fm_sdk") is None:
        return {
            "available": False,
            "reason": "apple_fm_sdk is unavailable (requires a supported Apple Silicon Mac and newer macOS)",
        }
    try:
        import apple_fm_sdk as fm

        model = fm.SystemLanguageModel()
        available, reason = model.is_available()
        return {"available": bool(available), "reason": "" if available else str(reason)}
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


def provider_status() -> dict[str, object]:
    return {
        "apple_foundation": apple_foundation_status(),
        "providers": ["openai", "anthropic", "openai-compatible", "apple-foundation"],
    }


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message") or payload.get("message")
    except Exception:
        message = response.text
    return str(message or f"Provider returned HTTP {response.status_code}")[:500]


def _openai_output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


async def _call_openai(request: LLMChatRequest) -> str:
    if not request.api_token:
        raise LLMProviderError("OpenAI API token is required")
    payload = {
        "model": request.model or "gpt-5.6-terra",
        "instructions": request.instructions,
        "input": [message.model_dump() for message in request.messages],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {request.api_token}"},
            json=payload,
        )
    if response.is_error:
        raise LLMProviderError(_error_message(response))
    text = _openai_output_text(response.json())
    if not text:
        raise LLMProviderError("OpenAI returned no text")
    return text


async def _call_anthropic(request: LLMChatRequest) -> str:
    if not request.api_token:
        raise LLMProviderError("Anthropic API token is required")
    payload = {
        "model": request.model or "claude-sonnet-5",
        "max_tokens": 4_096,
        "system": request.instructions,
        "messages": [message.model_dump() for message in request.messages],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": request.api_token,
                "anthropic-version": "2023-06-01",
            },
            json=payload,
        )
    if response.is_error:
        raise LLMProviderError(_error_message(response))
    text = "\n".join(
        item.get("text", "")
        for item in response.json().get("content", [])
        if item.get("type") == "text"
    ).strip()
    if not text:
        raise LLMProviderError("Anthropic returned no text")
    return text


async def _call_openai_compatible(request: LLMChatRequest) -> str:
    base_url = request.base_url or "http://127.0.0.1:11434/v1"
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {request.api_token}"} if request.api_token else {}
    messages = [{"role": "system", "content": request.instructions}]
    messages.extend(message.model_dump() for message in request.messages)
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            endpoint,
            headers=headers,
            json={"model": request.model or "llama3.2", "messages": messages},
        )
    if response.is_error:
        raise LLMProviderError(_error_message(response))
    try:
        return response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMProviderError("Compatible provider returned no chat message") from exc


async def _call_apple_foundation(request: LLMChatRequest) -> str:
    status = apple_foundation_status()
    if not status["available"]:
        raise LLMProviderError(str(status["reason"]))
    import apple_fm_sdk as fm

    transcript = "\n\n".join(
        f"{message.role.title()}: {message.content}" for message in request.messages
    )
    session = fm.LanguageModelSession(instructions=request.instructions)
    response = await session.respond(transcript)
    return str(response).strip()


async def call_llm(request: LLMChatRequest) -> str:
    try:
        if request.provider == "openai":
            return await _call_openai(request)
        if request.provider == "anthropic":
            return await _call_anthropic(request)
        if request.provider == "openai-compatible":
            return await _call_openai_compatible(request)
        return await _call_apple_foundation(request)
    except httpx.RequestError as exc:
        raise LLMProviderError(f"Could not connect to the provider: {exc}") from exc
