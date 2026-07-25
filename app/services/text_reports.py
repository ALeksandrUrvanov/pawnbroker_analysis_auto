"""Текстовые отчёты для рассылки: итог (report_*), стенограмма (transcript_*), по диалогам (*_01_*, ...), ZIP."""
import logging
import zipfile
from datetime import date
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


def _strip_trailing_json_block(text: str) -> str:
    """Убирает из конца текста последний блок ```json ... ``` или ``` ... ``` (в письмо не включаем JSON)."""
    if not text or not text.strip():
        return text
    text = text.strip()
    last_close = text.rfind("```")  # закрывающие ```
    if last_close == -1:
        return text
    # открывающие ``` последнего блока — ищем последние ``` до last_close
    last_open = text.rfind("```", 0, last_close)
    if last_open == -1:
        return text
    return text[:last_open].rstrip()


def _filename_report(pawnshop_id: str, d: date) -> str:
    return f"report_{pawnshop_id}_{d.isoformat()}.txt"


def _filename_transcript(pawnshop_id: str, d: date) -> str:
    return f"transcript_{pawnshop_id}_{d.isoformat()}.txt"


def _filename_dialogue(pawnshop_id: str, num: int, d: date) -> str:
    return f"{pawnshop_id}_{num:02d}_{d.isoformat()}.txt"


def build_report_txt(
    result: Dict[str, Any], pawnshop_id: str, target_date: date, period_str: Optional[str] = None
) -> str:
    """Текст итогового отчёта по смене; при period_str — первая строка «Период: ...»."""
    body = result.get("final_report", "") or "Итоговый отчёт не сформирован."
    if period_str:
        body = f"Период: {period_str}\n\n{body}"
    return body


def build_transcript_txt(result: Dict[str, Any]) -> str:
    """Полный текст стенограммы."""
    return result.get("transcript", "") or "Стенограмма отсутствует."


def build_dialogue_txt(
    result: Dict[str, Any],
    dialogue_index: int,
) -> str:
    """Текст одного диалога: фрагмент стенограммы + анализ (dialogue_index 0-based)."""
    dialogues = result.get("dialogues", [])
    analyses = result.get("dialogues_analyses", [])

    if dialogue_index >= len(dialogues) or dialogue_index >= len(analyses):
        return ""

    dialogue = dialogues[dialogue_index]
    analysis_item = analyses[dialogue_index]
    transcript_chunk = dialogue.get("full_transcript", "")
    raw_analysis = analysis_item.get("analysis") if analysis_item.get("success") else analysis_item.get("error", "Ошибка анализа")
    analysis_text = _strip_trailing_json_block(raw_analysis or "")

    parts = [
        "=== Стенограмма диалога ===",
        "",
        transcript_chunk,
        "",
        "=== Анализ ===",
        "",
        analysis_text or "",
    ]
    return "\n".join(parts)


def build_all_artifacts(
    result: Dict[str, Any],
    pawnshop_id: str,
    target_date: date,
    output_dir: Optional[Path] = None,
    period_str: Optional[str] = None,
) -> Dict[str, Any]:
    """Пишет report, transcript, файлы диалогов и ZIP; возвращает словарь с путями (report, transcript, zip, dialogue_files)."""
    if output_dir is None:
        output_dir = Path.cwd()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = target_date.isoformat()

    report_content = build_report_txt(result, pawnshop_id, target_date, period_str)
    report_name = _filename_report(pawnshop_id, target_date)
    report_path = output_dir / report_name
    report_path.write_text(report_content, encoding="utf-8")
    logger.info("Записан отчёт: %s", report_path)

    transcript_content = build_transcript_txt(result)
    transcript_name = _filename_transcript(pawnshop_id, target_date)
    transcript_path = output_dir / transcript_name
    transcript_path.write_text(transcript_content, encoding="utf-8")
    logger.info("Записана стенограмма: %s", transcript_path)

    n_dialogues = len(result.get("dialogues", []))
    dialogue_paths: List[Path] = []
    for i in range(n_dialogues):
        content = build_dialogue_txt(result, i)
        name = _filename_dialogue(pawnshop_id, i + 1, target_date)
        path = output_dir / name
        path.write_text(content, encoding="utf-8")
        dialogue_paths.append(path)
    if dialogue_paths:
        logger.info("Записано файлов диалогов: %s", len(dialogue_paths))

    zip_name = f"dialogues_{pawnshop_id}_{date_str}.zip"
    zip_path = output_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in dialogue_paths:
            zf.write(p, p.name)
    logger.info("Создан архив: %s", zip_path)

    return {
        "report": report_path,
        "transcript": transcript_path,
        "zip": zip_path,
        "dialogue_files": dialogue_paths,
    }
