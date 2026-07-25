"""Форматирование меток спикеров для транскрипции."""

def format_speaker_label(speaker: str) -> str:
    """SPEAKER_XX → «Спикер XX», unknown → «Неизвестный»."""
    if speaker == 'unknown' or not speaker:
        return 'Неизвестный'
    if speaker.startswith('SPEAKER_'):
        number = speaker.replace('SPEAKER_', '')
        return f'Спикер {number}'
    
    return speaker
