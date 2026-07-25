"""Источник записей: API Интерсвязь (platform-vision) — архив камеры, смена 9–21, MP3 16 kHz моно."""
import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import (
    AUDIO_CACHE_DIR,
    INTERSVYAZ_PASSWORD,
    INTERSVYAZ_PROJECT_ID,
    INTERSVYAZ_USERNAME,
    load_pawnshop_shift_tz,
    load_pawnshop_camera_mapping,
)
from ..services.intersvyaz_archive import download_shift_day_as_mp3, get_token

logger = logging.getLogger(__name__)


def _local_tz_for_pawnshop(pawnshop_id: str):
    """Границы 9–21 в локальном времени точки (см. camera_mapping.shift_tz)."""
    name = load_pawnshop_shift_tz(pawnshop_id)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Ломбард %s: неверная shift_tz %r, используем системную TZ",
            pawnshop_id,
            name,
        )
        return datetime.now().astimezone().tzinfo


def _camera_id_for_pawnshop(pawnshop_id: str) -> Optional[int]:
    m = load_pawnshop_camera_mapping()
    pid = pawnshop_id.zfill(3) if pawnshop_id.isdigit() else pawnshop_id
    return m.get(pid)


def get_recording(
    pawnshop_id: str,
    target_date: date,
    *,
    keep_video_chunks: bool = False,
) -> Tuple[Optional[Path], Optional[str], Optional[float]]:
    """Путь к MP3, период (HH:MM:SS–HH:MM:SS) и длительность смены в секундах."""
    date_str = target_date.isoformat()
    logger.info(
        "Запрос записи: ломбард=%s, дата=%s, shift_tz=%s",
        pawnshop_id,
        date_str,
        load_pawnshop_shift_tz(pawnshop_id),
    )

    if not INTERSVYAZ_USERNAME or not INTERSVYAZ_PASSWORD:
        logger.warning(
            "Интерсвязь не настроена (INTERSVYAZ_USERNAME, INTERSVYAZ_PASSWORD). Ломбард %s, дата %s.",
            pawnshop_id,
            date_str,
        )
        return None, None, None

    cam = _camera_id_for_pawnshop(pawnshop_id)
    if cam is None:
        logger.warning(
            "Ломбард %s: задайте соответствие камеры в camera_mapping.json или PAWNSHOP_CAMERA_MAP",
            pawnshop_id,
        )
        return None, None, None

    token = None
    for _attempt in range(3):
        try:
            token = get_token(INTERSVYAZ_USERNAME, INTERSVYAZ_PASSWORD)
            break
        except Exception as e:
            logger.warning("Интерсвязь: не удалось получить токен (попытка %s/3): %s", _attempt + 1, e)
            if _attempt < 2:
                import time as _time; _time.sleep(5)
    if token is None:
        return None, None, None

    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    work = AUDIO_CACHE_DIR / "_intersvyaz_work"
    work.mkdir(parents=True, exist_ok=True)
    keep_chunks_dir = None
    if keep_video_chunks:
        keep_chunks_dir = AUDIO_CACHE_DIR / "kept_chunks" / pawnshop_id.zfill(3) / date_str
        keep_chunks_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Часовые MP4-чанки будут сохранены в %s", keep_chunks_dir)

    def _log_chunk(msg: str) -> None:
        logger.info("%s", msg)

    mp3, period_str, dur = download_shift_day_as_mp3(
        token,
        INTERSVYAZ_PROJECT_ID,
        cam,
        target_date,
        work,
        _local_tz_for_pawnshop(pawnshop_id),
        keep_chunks_dir=keep_chunks_dir,
        log_chunk=_log_chunk,
    )
    if not mp3 or not mp3.exists():
        logger.warning(
            "Интерсвязь: за %s нет смены в архиве или не удалось скачать (ломбард %s, камера %s)",
            date_str,
            pawnshop_id,
            cam,
        )
        return None, None, None

    final_name = AUDIO_CACHE_DIR / f"merged_{pawnshop_id.zfill(3)}_{date_str}.mp3"
    try:
        shutil.move(str(mp3), final_name)
    except OSError:
        shutil.copy2(mp3, final_name)
        mp3.unlink(missing_ok=True)

    return final_name, period_str, dur
