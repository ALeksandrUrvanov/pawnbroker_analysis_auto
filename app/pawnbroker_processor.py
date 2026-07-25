"""Пайплайн: транскрипция → извлечение диалогов (LLM) → анализ качества (LLM) → отчёт по смене (LLM)."""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import format_duration_readable
from .pipeline import (
    format_transcription,
    period_start_seconds,
    step1_extract_dialogues,
    step2_quality_check_parallel,
    step3_shift_report,
)

logger = logging.getLogger(__name__)

audio_pipeline_client = None
llm_service = None


def _get_services():
    """Подключение к bootstrap (Audio Pipeline, LLM) при первом вызове."""
    global audio_pipeline_client, llm_service
    if audio_pipeline_client is None or llm_service is None:
        from . import bootstrap
        bootstrap.init_services()
        audio_pipeline_client = bootstrap.audio_pipeline_client
        llm_service = bootstrap.llm_service


class PawnbrokerProcessor:
    """Аудио → транскрипция → диалоги → анализ качества → итоговый отчёт."""

    def __init__(self):
        self.prompts_dir = Path(__file__).parent.parent / "prompts"
        self.prompt_step1 = self._load_prompt("PROMPT_STEP_1_EXTRACTION.md")
        self.prompt_step2 = self._load_prompt("PROMPT_STEP_2_QUALITY_CHECK.md")
        self.prompt_step3 = self._load_prompt("PROMPT_STEP_3_SHIFT_REPORT.md")

    def _load_prompt(self, filename: str) -> str:
        with open(self.prompts_dir / filename, "r", encoding="utf-8") as f:
            return f.read()

    async def process_audio_async(
        self,
        audio_path: str,
        status_callback=None,
        priority: str = "low",
        period_str: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Полный пайплайн: Pipeline → извлечение диалогов → анализ диалогов → отчёт по смене. period_str — период записи (09:00:00–21:59:19) для смещения меток времени в транскрипции."""
        def update_status(status: str, message: str, progress: int):
            if status_callback:
                status_callback(status, message, progress)

        start_time = time.time()
        transcription_text = None
        pipeline_result = None
        try:
            _get_services()
            if not llm_service or not llm_service.is_ready():
                raise RuntimeError("LLM сервис недоступен. Задайте OPENROUTER_API_KEY (Gemini 3.1 Pro Preview).")

            if not audio_pipeline_client.is_ready():
                logger.info("Инициализируем Audio Pipeline клиент...")
                audio_pipeline_client.initialize()
                if not audio_pipeline_client.is_ready():
                    raise RuntimeError("Audio Pipeline сервис не готов")

            from .config import AUDIO_PIPELINE_POLL_INTERVAL
            job_id = await audio_pipeline_client.submit_job(audio_path, priority=priority)
            pipeline_result = await audio_pipeline_client.wait_for_job(
                job_id, status_callback=update_status, poll_interval=AUDIO_PIPELINE_POLL_INTERVAL
            )
            offset_sec = period_start_seconds(period_str)
            transcription_text = format_transcription(pipeline_result, offset_sec=offset_sec)
            segments = pipeline_result.get("diarization", {}).get("segments", [])
            total = len(segments)
            with_text = sum(1 for line in transcription_text.split("\n") if line.strip())
            logger.info(
                "Пайплайн завершен за %s: %s/%s сегментов с текстом",
                format_duration_readable(time.time() - start_time), with_text, total,
            )
            model_display = getattr(llm_service, "model_label", None) or llm_service.model
            logger.info(
                "Отправка запроса в LLM (%s), размер текста: %s символов",
                model_display, len(transcription_text),
            )

            update_status("step1", "Извлекаем диалоги из транскрипции...", 40)
            try:
                step1_result = await step1_extract_dialogues(
                    transcription_text, self.prompt_step1, llm_service
                )
            except Exception as e:
                logger.error("Критическая ошибка на Шаге 1: %s", e)
                return _partial_result(transcription_text, pipeline_result, {"error": str(e)})
            if not step1_result or not step1_result.get("success"):
                return _partial_result(transcription_text, pipeline_result, step1_result)
            dialogues_data = step1_result.get("dialogues_data", {})
            dialogues = dialogues_data.get("client_dialogues", [])
            logger.info("Извлечение диалогов завершено: найдено %s диалогов", len(dialogues))
            if not dialogues:
                return _success_result(transcription_text, pipeline_result, [], [], "Диалоги с клиентами не найдены в записи.")

            update_status("step2", "Анализируем диалоги...", 50)
            try:
                dialogues_analyses = await step2_quality_check_parallel(
                    dialogues,
                    self.prompt_step2,
                    llm_service,
                    lambda c, t: update_status("step2", f"Анализируем диалог {c} из {t}...", 50 + int(30 * c / t)),
                )
            except Exception as e:
                logger.error("Критическая ошибка на Шаге 2: %s", e)
                return {
                    "status": "partial_success",
                    "transcript": transcription_text,
                    "dialogues": dialogues,
                    "dialogues_analyses": [],
                    "final_report": f"Критическая ошибка анализа диалогов: {str(e)}",
                    "audio_info": pipeline_result.get("audio_info", {}),
                    "error": f"Шаг 2 exception: {str(e)}",
                    "message": "Обработка частично завершена: диалоги извлечены, но анализ не выполнен",
                }
            successful = sum(1 for d in dialogues_analyses if d.get("success"))
            logger.info(
                "Анализ диалогов завершен: %s успешно, %s ошибок",
                successful, len(dialogues_analyses) - successful,
            )

            update_status("step3", "Формируем итоговый отчет по смене...", 90)
            try:
                step3_result = await step3_shift_report(dialogues_analyses, self.prompt_step3, llm_service)
            except Exception as e:
                logger.error("Критическая ошибка на Шаге 3: %s", e)
                step3_result = {"success": False, "error": str(e)}
            final_report = ""
            if step3_result and step3_result.get("success"):
                final_report = step3_result.get("report", "")
                logger.info("Отчет сформирован: %s символов", len(final_report))
            else:
                err = step3_result.get("error", "Нет ответа") if step3_result else "Нет ответа"
                logger.warning("Шаг 3: %s", err)
                final_report = f"Ошибка формирования итогового отчета: {err}"

            return _success_result(
                transcription_text, pipeline_result, dialogues, dialogues_analyses, final_report, time.time() - start_time
            )

        except Exception as e:
            logger.error("Критическая ошибка обработки: %s", e)
            import traceback
            logger.error(traceback.format_exc())
            if transcription_text is not None:
                return _error_result(transcription_text, pipeline_result, str(e))
            raise


def _partial_result(transcription_text: str, pipeline_result: dict, step1_result: dict) -> Dict[str, Any]:
    err = step1_result.get("error", "Неизвестная ошибка") if step1_result else "Нет ответа"
    return {
        "status": "partial_success",
        "transcript": transcription_text,
        "dialogues": [],
        "dialogues_analyses": [],
        "final_report": f"Ошибка извлечения диалогов: {err}",
        "audio_info": (pipeline_result or {}).get("audio_info", {}),
        "error": f"Шаг 1 failed: {err}",
        "message": "Обработка частично завершена: транскрипция получена, но анализ не выполнен",
    }


def _success_result(
    transcript: str,
    pipeline_result: dict,
    dialogues: List[Any],
    dialogues_analyses: List[Any],
    final_report: str,
    processing_time: Optional[float] = None,
) -> Dict[str, Any]:
    ok = sum(1 for d in dialogues_analyses if d.get("success"))
    return {
        "status": "success",
        "transcript": transcript,
        "dialogues": dialogues,
        "dialogues_analyses": dialogues_analyses,
        "final_report": final_report,
        "audio_info": (pipeline_result or {}).get("audio_info", {}),
        "processing_time": processing_time,
        "stats": {
            "total_dialogues": len(dialogues),
            "successful_analyses": ok,
            "failed_analyses": len(dialogues_analyses) - ok,
        },
        "message": "Обработка завершена успешно",
    }


def _error_result(transcription_text: str, pipeline_result: dict, error: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "transcript": transcription_text,
        "dialogues": [],
        "dialogues_analyses": [],
        "final_report": f"Критическая ошибка: {error}",
        "audio_info": (pipeline_result or {}).get("audio_info", {}),
        "error": error,
        "message": "Обработка завершена с ошибкой, доступна только транскрипция",
    }


pawnbroker_processor = PawnbrokerProcessor()
