"""Формирование результатов анализа диалогов."""

from typing import Dict, Optional


def create_dialogue_result(
    dialogue_id: int,
    dialogue: Dict,
    success: bool = True,
    analysis: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict:
    """Словарь результата: dialogue_id, success, analysis или error."""
    result: Dict = {
        'dialogue_id': dialogue_id,
        'success': success,
    }
    if success and analysis:
        result['analysis'] = analysis
    elif not success and error:
        result['error'] = error
    return result
