"""Три шага пайплайна: извлечение диалогов, анализ качества, отчёт по смене (LLM)."""

import asyncio
import json
import logging
from typing import Dict, List, Any, Callable, Optional

from .transcription import extract_dialogue_transcript
from ..utils.dialogue_builder import create_dialogue_result

logger = logging.getLogger(__name__)

# Задержка между стартами параллельных запросов к LLM (секунды)
STEP2_TASK_DELAY_SEC = 3.0


def _extract_json_block(text: str) -> Optional[str]:
    """Первый JSON-блок из текста (```json ... ``` или {...})."""
    if not text or not text.strip():
        return None
    json_str = None
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end != -1:
            json_str = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end != -1:
            json_str = text[start:end].strip()
    if json_str:
        return json_str
    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        return text[start:end]
    return None


async def step1_extract_dialogues(
    transcription_text: str,
    prompt_step1: str,
    llm_service: Any,
    extract_fn: Callable[[str, str, str], str] = extract_dialogue_transcript,
) -> Dict:
    """Извлечение диалогов из транскрипции (LLM); при 0 диалогов — один retry."""
    prompt = prompt_step1.replace("[ТЕКСТ_ДЛЯ_АНАЛИЗА]", transcription_text)
    for attempt in range(2):
        if attempt > 0:
            logger.warning("Повторный запрос к LLM (диалогов было 0)")
        result = await llm_service.analyze_with_prompt(
            prompt,
            enable_thinking=False,
            max_tokens=128000,
            response_json=False,
        )
        if not result.get("success"):
            return result
        analysis = result.get("analysis", "")
        logger.info("Step 1: получен ответ длиной %s символов", len(analysis))
        json_to_parse = _extract_json_block(analysis)
        if not json_to_parse:
            logger.warning("JSON не найден в ответе")
            return {"success": False, "error": "JSON не найден"}
        try:
            dialogues_data = json.loads(json_to_parse)
            total_dialogues = dialogues_data.get("total_dialogues_found", 0)
            logger.info("JSON валидный: %s диалогов", total_dialogues)
            if total_dialogues == 0 and attempt == 0:
                logger.warning("Диалогов не найдено, повторяем запрос...")
                continue
            dialogues = dialogues_data.get("client_dialogues", [])
            for dialogue in dialogues:
                st, et = dialogue.get("start", ""), dialogue.get("end", "")
                if st and et:
                    dialogue["full_transcript"] = extract_fn(st, et, transcription_text)
                    logger.debug("Диалог %s: извлечено %s символов", dialogue.get("id"), len(dialogue.get("full_transcript", "")))
                else:
                    dialogue["full_transcript"] = ""
                    logger.warning("Диалог %s: нет временных меток", dialogue.get("id"))
            logger.info("Добавлены full_transcript для всех диалогов")
            return {"success": True, "dialogues_data": dialogues_data}
        except json.JSONDecodeError as e:
            logger.error("Ошибка парсинга JSON: %s", e)
            if json_to_parse:
                logger.error("Попытка парсить: %s...", json_to_parse[:200])
            ob, cb = json_to_parse.count("{"), json_to_parse.count("}")
            if ob != cb:
                logger.error("JSON обрезан: скобки %s/%s", ob, cb)
                return {"success": False, "error": f"JSON обрезан из-за лимита токенов (скобки: {ob}/{cb})"}
            return {"success": False, "error": f"Ошибка парсинга: {e}"}
        except Exception as e:
            logger.error("Критическая ошибка извлечения JSON: %s", e)
            return {"success": False, "error": str(e)}
    return {"success": False, "error": "Все попытки исчерпаны"}


async def step2_quality_check_parallel(
    dialogues: List[Dict],
    prompt_step2: str,
    llm_service: Any,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> List[Dict]:
    """Параллельный анализ качества диалогов (LLM); задержка между стартами — STEP2_TASK_DELAY_SEC."""
    logger.info("Запуск анализа %s диалогов параллельно...", len(dialogues))

    async def analyze_one(dialogue: Dict, index: int, delay: float) -> Dict:
        if delay > 0:
            await asyncio.sleep(delay)
        if progress_callback:
            progress_callback(index + 1, len(dialogues))
        dialogue_id = index + 1
        logger.info("Запуск анализа диалога #%s...", dialogue_id)
        transcript = dialogue.get("full_transcript", "")
        preliminary = dialogue.get("operation_type", "не указан")
        prompt = prompt_step2.replace("[ПРЕДВАРИТЕЛЬНЫЙ_СЦЕНАРИЙ]", preliminary).replace(
            "[ТЕКСТ_ДЛЯ_АНАЛИЗА]", transcript
        )
        try:
            result = await llm_service.analyze_with_prompt(
                prompt,
                enable_thinking=True,
                max_tokens=128000,
                response_json=False,
            )
            if result.get("success"):
                logger.info("Диалог #%s проанализирован", dialogue_id)
                return create_dialogue_result(
                    dialogue_id=dialogue_id, dialogue=dialogue, success=True, analysis=result.get("analysis", "")
                )
            logger.error("Диалог #%s: %s", dialogue_id, result.get("error", "Неизвестная ошибка"))
            return create_dialogue_result(
                dialogue_id=dialogue_id, dialogue=dialogue, success=False, error=result.get("error", "Неизвестная ошибка")
            )
        except Exception as e:
            logger.error("Диалог #%s: исключение - %s", dialogue_id, e)
            return create_dialogue_result(
                dialogue_id=dialogue_id, dialogue=dialogue, success=False, error=f"Исключение при анализе: {str(e)}"
            )

    tasks = [analyze_one(d, i, i * STEP2_TASK_DELAY_SEC) for i, d in enumerate(dialogues)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_results = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.error("Диалог #%s: критическое исключение - %s", i + 1, res)
            all_results.append(
                create_dialogue_result(
                    dialogue_id=i + 1, dialogue=dialogues[i], success=False, error=f"Критическая ошибка: {str(res)}"
                )
            )
        elif isinstance(res, dict):
            all_results.append(res)
        else:
            logger.warning("Диалог #%s: неожиданный тип результата", i + 1)
            all_results.append(
                create_dialogue_result(dialogue_id=i + 1, dialogue=dialogues[i], success=False, error="Неожиданный результат")
            )
    ok = sum(1 for r in all_results if r.get("success"))
    logger.info("Успешно: %s, ошибки: %s", ok, len(all_results) - ok)
    return all_results


async def step3_shift_report(
    dialogues_analyses: List[Dict],
    prompt_step3: str,
    llm_service: Any,
) -> Dict:
    """Итоговый отчёт по смене из JSON-блоков успешных анализов (LLM)."""
    successful = [d for d in dialogues_analyses if d.get("success")]
    failed = [d for d in dialogues_analyses if not d.get("success")]
    failed_ids = [str(d.get("dialogue_id")) for d in failed] if failed else []
    if failed:
        logger.warning("Пропущено %s диалогов с ошибками при формировании отчета", len(failed))
    if not successful:
        logger.error("Нет успешных анализов для формирования отчета")
        return {"success": False, "error": "Все диалоги завершились с ошибкой"}
    json_blocks = []
    for d in successful:
        did = d.get("dialogue_id")
        analysis = d.get("analysis", "")
        json_match = _extract_json_block(analysis)
        if json_match:
            json_blocks.append(f"Диалог #{did}:\n```json\n{json_match}\n```")
    analyses_text = "\n\n".join(json_blocks)
    if failed:
        analyses_text += f"\n\nПРИМЕЧАНИЕ: Диалоги {', '.join(failed_ids)} не были проанализированы из-за ошибок.\n"
    prompt = prompt_step3.replace("[ЗДЕСЬ БУДУТ JSON-БЛОКИ ИЗ КАЖДОГО ДИАЛОГА]", analyses_text)
    try:
        result = await llm_service.analyze_with_prompt(
            prompt, enable_thinking=True, max_tokens=128000, response_json=False
        )
        if not result.get("success"):
            return result
        report_text = result.get("analysis", "")
        if failed:
            report_text += f"\n\n---\n\n**ВНИМАНИЕ:** {len(failed)} диалогов не проанализированы из-за технических ошибок (диалоги: {', '.join(failed_ids)})"
        return {"success": True, "report": report_text}
    except Exception as e:
        logger.error("Ошибка формирования отчета: %s", e)
        return {"success": False, "error": str(e)}
