"""Model-provider boundary for Knowledge final responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from openai import OpenAI

from core.knowledge_response_config import KnowledgeResponseConfig


DeltaCallback = Callable[[str], None]


class KnowledgeResponseClient:
    """Generate one Knowledge answer through Terra or an OpenAI-compatible model."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        max_output_tokens: int,
        reasoning_effort: str,
        sdk_client: Any,
    ) -> None:
        self.provider = provider
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.sdk_client = sdk_client

    @classmethod
    def from_config(
        cls,
        *,
        config: KnowledgeResponseConfig,
        openai_api_key: str,
    ) -> "KnowledgeResponseClient":
        if config.provider == "terra":
            sdk_client = OpenAI(
                api_key=openai_api_key,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
        else:
            sdk_client = OpenAI(
                api_key=config.taide_api_key,
                base_url=config.taide_api_base,
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            )
        return cls(
            provider=config.provider,
            model=config.model,
            max_output_tokens=config.max_output_tokens,
            reasoning_effort=config.reasoning_effort,
            sdk_client=sdk_client,
        )

    def generate(
        self,
        *,
        system_prompt: str,
        messages: Sequence[Mapping[str, str]],
        on_delta: DeltaCallback | None = None,
    ) -> str:
        if self.provider == "terra":
            return self._generate_terra(
                system_prompt=system_prompt,
                messages=messages,
                on_delta=on_delta,
            )
        return self._generate_chat(messages=messages, on_delta=on_delta)

    def _generate_terra(
        self,
        *,
        system_prompt: str,
        messages: Sequence[Mapping[str, str]],
        on_delta: DeltaCallback | None,
    ) -> str:
        response_kwargs = {
            "model": self.model,
            "instructions": system_prompt,
            "input": [messages[-1]],
            "reasoning": {"effort": self.reasoning_effort},
            "text": {"verbosity": "low"},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        if on_delta is None:
            response = self.sdk_client.responses.create(**response_kwargs)
            return (response.output_text or "").strip()

        chunks = []
        stream = self.sdk_client.responses.create(
            **response_kwargs,
            stream=True,
        )
        for event in stream:
            if getattr(event, "type", "") != "response.output_text.delta":
                continue
            delta = getattr(event, "delta", "") or ""
            if not delta:
                continue
            chunks.append(delta)
            on_delta(delta)
        return "".join(chunks).strip()

    def _generate_chat(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        on_delta: DeltaCallback | None,
    ) -> str:
        response_kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        if on_delta is None:
            response = self.sdk_client.chat.completions.create(
                **response_kwargs
            )
            return (
                response.choices[0].message.content or ""
            ).strip()

        chunks = []
        stream = self.sdk_client.chat.completions.create(
            **response_kwargs,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if not delta:
                continue
            chunks.append(delta)
            on_delta(delta)
        return "".join(chunks).strip()
