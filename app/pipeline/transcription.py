"""Форматирование транскрипции и вырезка фрагментов по времени."""

from typing import Any, Dict, Optional

from ..utils import format_speaker_label, format_timestamp_range


def _time_to_seconds(time_str: str) -> int:
    """ЧЧ:ММ:СС или ММ:СС → секунды."""
    try:
        parts = time_str.split(":")
        if len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(int, parts)
            return m * 60 + s
        return int(parts[0])
    except (ValueError, IndexError):
        return 0


def period_start_seconds(period_str: Optional[str]) -> int:
    """Из «ЧЧ:ММ:СС–...» возвращает начало периода в секундах от полуночи; для offset меток в транскрипции."""
    if not period_str or not period_str.strip():
        return 0
    part = period_str.replace("\u2013", "-").replace("–", "-").strip().split("-")[0].strip()
    return _time_to_seconds(part)


def format_transcription(pipeline_result: Dict[str, Any], offset_sec: float = 0) -> str:
    """Сегменты pipeline → строки «[ЧЧ:ММ:СС - ЧЧ:ММ:СС] [Спикер N] текст». offset_sec — начало записи по времени суток (чтобы метки совпадали с периодом)."""
    diarization_segments = pipeline_result.get("diarization", {}).get("segments", [])
    transcription_results = pipeline_result.get("transcription_results", [])
    lines = []
    for diar_seg, trans_seg in zip(diarization_segments, transcription_results):
        speaker = diar_seg.get("speaker", "unknown")
        text = trans_seg.get("text", "").strip()
        if not text:
            continue
        start = diar_seg.get("start", 0) + offset_sec
        end = diar_seg.get("end", 0) + offset_sec
        speaker_label = format_speaker_label(speaker)
        time_str = format_timestamp_range(start, end)
        lines.append(f"{time_str} [{speaker_label}] {text}")
    return "\n".join(lines)


def extract_dialogue_transcript(start_time: str, end_time: str, full_transcription: str) -> str:
    """Вырезает из транскрипции реплики в интервале [start_time, end_time] (формат ЧЧ:ММ:СС)."""
    start_seconds = _time_to_seconds(start_time)
    end_seconds = _time_to_seconds(end_time)
    dialogue_lines = []
    for line in full_transcription.split("\n"):
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        try:
            time_part = line[1 : line.find("]")]
            if " - " not in time_part:
                continue
            line_start, line_end = time_part.split(" - ")
            line_start_sec = _time_to_seconds(line_start.strip())
            line_end_sec = _time_to_seconds(line_end.strip())
            if line_start_sec >= start_seconds and line_end_sec <= end_seconds:
                dialogue_lines.append(line)
        except (ValueError, IndexError):
            continue
    return "\n".join(dialogue_lines)
