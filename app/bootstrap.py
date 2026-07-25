"""Инициализация сервисов для CLI: Audio Pipeline, OpenRouter LLM, процессор."""
import logging
from typing import Any

logger = logging.getLogger(__name__)

audio_pipeline_client: Any = None
llm_service: Any = None
pawnbroker_processor: Any = None


def init_services() -> None:
    """Поднимает глобальные сервисы (один раз). LLM: OpenRouter."""
    global audio_pipeline_client, llm_service, pawnbroker_processor

    if pawnbroker_processor is not None and llm_service is not None:
        return

    from .audio_pipeline_client import audio_pipeline_client as _ap
    from .openrouter_service import openrouter_service as _openrouter
    from .pawnbroker_processor import pawnbroker_processor as _pp

    audio_pipeline_client = _ap
    pawnbroker_processor = _pp

    llm_service = _openrouter
    if not llm_service.is_ready():
        logger.warning("OpenRouter API ключ не установлен (OPENROUTER_API_KEY)")
    llm_label = getattr(llm_service, "model_label", "OpenRouter")

    if audio_pipeline_client and not audio_pipeline_client.is_ready():
        audio_pipeline_client.initialize()
    logger.info("Bootstrap: сервисы инициализированы (LLM: %s)", llm_label)
