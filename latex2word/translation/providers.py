from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .prompts import build_batch_prompt, make_system, parse_batch_response

log = logging.getLogger(__name__)


class BaseProvider(ABC):
    """Translate a flat list of texts in a single API call."""

    MAX_RETRIES = 5

    async def translate_batch(self, texts: List[str], system: Optional[str] = None) -> List[str]:
        if len(texts) == 1:
            sys_prompt = system if system is not None else make_system(batch=False)
            result = await self._call_with_retry(sys_prompt, texts[0])
            return [result]

        sys_prompt = system if system is not None else make_system(batch=True)
        prompt = build_batch_prompt(texts)
        response = await self._call_with_retry(sys_prompt, prompt)
        return parse_batch_response(response, len(texts))

    async def _call_with_retry(self, system: str, user: str) -> str:
        for attempt in range(self.MAX_RETRIES):
            try:
                return await self._call_api(system, user)
            except Exception as exc:
                if not self._is_retryable(exc):
                    raise
                wait = (2 ** attempt) + random.random()
                log.warning(
                    "Retryable error (attempt %d/%d): %s -- waiting %.1fs",
                    attempt + 1,
                    self.MAX_RETRIES,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
        return await self._call_api(system, user)

    @abstractmethod
    async def _call_api(self, system: str, user: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__
        msg = str(exc)
        retryable_names = {
            "RateLimitError", "APIStatusError", "APIConnectionError",
            "APITimeoutError", "InternalServerError", "ServiceUnavailableError",
            "Timeout", "ConnectionError",
        }
        if name in retryable_names:
            return True
        return any(code in msg for code in ("429", "500", "502", "503", "504", "529"))


class AnthropicProvider(BaseProvider):
    def __init__(self, model: str, api_key: Optional[str] = None):
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            sys.exit("[ERROR] anthropic package not installed. Run: pip install anthropic")
        self.client = AsyncAnthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model

    async def _call_api(self, system: str, user: str) -> str:
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text


class OpenAICompatibleProvider(BaseProvider):
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider_name: str = "openai",
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            sys.exit("[ERROR] openai package not installed. Run: pip install openai")

        if api_key is None:
            env_key = {
                "openai": "OPENAI_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
                "kimi": "MOONSHOT_API_KEY",
            }.get(provider_name, "OPENAI_API_KEY")
            api_key = os.environ.get(env_key)

        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**kwargs)
        self.model = model
        self.provider_name = provider_name
        self._token_limit_param = self._default_token_limit_param(provider_name, model)

    @staticmethod
    def _default_token_limit_param(provider_name: str, model: str) -> str:
        model_lower = (model or "").lower()
        if provider_name == "openai" and model_lower.startswith(("gpt-5", "o1", "o3", "o4")):
            return "max_completion_tokens"
        return "max_tokens"

    @staticmethod
    def _unsupported_param(exc: Exception, param: str) -> bool:
        msg = str(exc)
        return "Unsupported parameter" in msg and param in msg

    async def _call_api(self, system: str, user: str) -> str:
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            self._token_limit_param: 4096,
        }
        try:
            resp = await self.client.chat.completions.create(**request)
        except Exception as exc:
            if self._unsupported_param(exc, self._token_limit_param):
                old_param = self._token_limit_param
                self._token_limit_param = (
                    "max_completion_tokens"
                    if old_param == "max_tokens"
                    else "max_tokens"
                )
                log.info(
                    "Retrying %s request with %s instead of %s.",
                    self.provider_name,
                    self._token_limit_param,
                    old_param,
                )
                request.pop(old_param, None)
                request[self._token_limit_param] = 4096
                resp = await self.client.chat.completions.create(**request)
            else:
                raise
        return resp.choices[0].message.content or ""


def build_provider(
    provider_name: str,
    model: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> BaseProvider:
    provider = provider_name.lower()
    if provider == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key)
    if provider in ("openai", "deepseek", "kimi"):
        resolved_base_url = base_url
        if provider == "deepseek" and not resolved_base_url:
            resolved_base_url = "https://api.deepseek.com"
        if provider == "kimi" and not resolved_base_url:
            resolved_base_url = "https://api.moonshot.cn/v1"
        return OpenAICompatibleProvider(
            model=model,
            api_key=api_key,
            base_url=resolved_base_url,
            provider_name=provider,
        )
    sys.exit(f"[ERROR] Unknown provider: {provider_name!r}.")
