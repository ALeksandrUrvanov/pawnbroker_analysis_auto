"""OpenRouter: Gemini с reasoning, Claude как фолбэк. Настройки только в app.config (OPENROUTER_*)."""

import asyncio
import logging
from typing import Any, Dict

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)
_client: AsyncOpenAI | None = None


def get_client(api_key: str, base_url: str) -> AsyncOpenAI:
    """Получить глобальный клиент OpenRouter (создаётся один раз)."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=600.0,
            default_headers={
                "HTTP-Referer": "http://pawnbroker_auto",
                "X-Title": "Pawnbroker Auto",
            },
        )
    return _client


class OpenRouterService:
    """Запросы к Gemini через OpenRouter API с включённым reasoning."""

    MAX_RETRIES = 3
    RETRY_DELAY = 5

    def __init__(self):
        from .config import (
            OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_MODEL_LABEL,
            OPENROUTER_FALLBACK_MODEL, OPENROUTER_FALLBACK_MODEL_LABEL,
        )
        self.api_key = OPENROUTER_API_KEY or ""
        self.ready = bool(self.api_key)
        if not self.ready:
            logger.warning("OpenRouter: API Key не задан в конфиге (OPENROUTER_API_KEY)")
        self.base_url = "https://openrouter.ai/api/v1"
        self.model = OPENROUTER_MODEL
        self.model_label = OPENROUTER_MODEL_LABEL
        self.fallback_model = OPENROUTER_FALLBACK_MODEL
        self.fallback_model_label = OPENROUTER_FALLBACK_MODEL_LABEL
        if self.ready:
            logger.info("OpenRouter primary: %s | fallback: %s — Ready", self.model_label, self.fallback_model_label)

    def is_ready(self) -> bool:
        return self.ready

    async def _call_openrouter(
        self,
        user_content: str,
        enable_thinking: bool = True,
        max_tokens: int = 128000,
        response_json: bool = False,
        model: str | None = None,
    ) -> str:
        """Один запрос к Chat Completions; возвращает content ответа."""
        client = get_client(self.api_key, self.base_url)
        used_model = model or self.model
        messages = [
            {"role": "system", "content": "Ты — профессиональный аналитик аудиозаписей. Твоя задача — точно следовать инструкциям и выводить результат в указанном формате."},
            {"role": "user", "content": user_content},
        ]
        kwargs = {
            "model": used_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        # temperature несовместим с reasoning у Claude — добавляем только без thinking
        if not enable_thinking:
            kwargs["temperature"] = 0.7
        if response_json:
            kwargs["response_format"] = {"type": "json_object"}
        if enable_thinking:
            kwargs["extra_body"] = {"reasoning": {"max_tokens": 80000}}
        response = await client.chat.completions.create(**kwargs)
        if response is None or not response.choices:
            raise ValueError("OpenRouter API вернул пустой ответ")
        msg = response.choices[0].message
        content = msg.content if msg.content else ""
        return content.strip()

    async def analyze_with_prompt(
        self,
        prompt: str,
        enable_thinking: bool = True,
        max_tokens: int = 128000,
        response_json: bool = False,
    ) -> Dict[str, Any]:
        """Готовый промпт → ответ LLM. Подстановку плейсхолдеров выполняет вызывающий код."""
        if not self.ready:
            return {"success": False, "error": "OpenRouter API ключ не установлен. Задайте OPENROUTER_API_KEY."}
        try:
            for attempt in range(self.MAX_RETRIES):
                try:
                    analysis_text = await self._call_openrouter(
                        prompt,
                        enable_thinking=enable_thinking,
                        max_tokens=max_tokens,
                        response_json=response_json,
                    )
                    if not analysis_text:
                        if attempt < self.MAX_RETRIES - 1:
                            logger.warning("Пустой ответ, retry %s/%s", attempt + 1, self.MAX_RETRIES)
                            await asyncio.sleep(self.RETRY_DELAY)
                            continue
                        return {"success": False, "error": "Пустой ответ после всех попыток"}
                    return {"success": True, "analysis": analysis_text, "model": self.model_label}
                except Exception as e:
                    if attempt < self.MAX_RETRIES - 1:
                        logger.warning("Ошибка %s, retry %s/%s", e, attempt + 1, self.MAX_RETRIES)
                        await asyncio.sleep(self.RETRY_DELAY)
                    else:
                        raise
        except Exception as e:
            logger.error("Ошибка OpenRouter API: %s", e)
            logger.warning("Переключаемся на фолбэк: %s", self.fallback_model_label)
            fallback_attempts = 2
            last_fallback_exc: Exception | None = None
            for fb_attempt in range(fallback_attempts):
                try:
                    analysis_text = await self._call_openrouter(
                        prompt,
                        enable_thinking=False,
                        max_tokens=min(max_tokens, 65536),  # Gemini max output
                        response_json=response_json,
                        model=self.fallback_model,
                    )
                    if analysis_text:
                        logger.info("Фолбэк %s: ответ получен", self.fallback_model_label)
                        return {"success": True, "analysis": analysis_text, "model": self.fallback_model_label}
                    logger.warning("Фолбэк пустой ответ, retry %s/%s", fb_attempt + 1, fallback_attempts)
                    await asyncio.sleep(self.RETRY_DELAY)
                except Exception as fallback_e:
                    last_fallback_exc = fallback_e
                    if fb_attempt < fallback_attempts - 1:
                        logger.warning("Фолбэк ошибка %s, retry %s/%s", fallback_e, fb_attempt + 1, fallback_attempts)
                        await asyncio.sleep(self.RETRY_DELAY)
                    else:
                        logger.error("Фолбэк %s тоже упал: %s", self.fallback_model_label, fallback_e)
            return {"success": False, "error": f"Основная модель: {e}; Фолбэк: {last_fallback_exc}"}


openrouter_service = OpenRouterService()
