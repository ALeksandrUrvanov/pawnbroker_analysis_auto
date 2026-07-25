#!/usr/bin/env python3
"""
Ручной отбор камер: скачивание видео (MP4) по окну 10:00–12:00.

По умолчанию: **последние даты, где в архиве вообще есть запись** (не обязательно 10–12),
на каждую дату — **2 часовых MP4** 10:00–11:00 и 11:00–12:00.

Если нужны только дни с пересечением 10–12 в архиве: PREVIEW_DATE_POLICY=window

Повтор при битом/крошечном MP4 (по умолчанию 1 повтор на чанк): PREVIEW_CHUNK_RETRIES=1|0.
Порог размера файла: PREVIEW_MIN_CHUNK_BYTES (по умолчанию 500000).

Отдельно по списку id (без обхода всех групп из JSON):

  python scripts/download_camera_previews.py --cameras 24974,24975 --point "Чел. Комс пр 105"

За вчера (календарный день в SCHEDULER_TZ) или за конкретный день:

  python scripts/download_camera_previews.py --yesterday
  python scripts/download_camera_previews.py --date 2026-03-30

Структура:
audio_cache/camera_previews/<точка>/<camera_id>/YYYY-MM-DD_1000-1100.mp4
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfg  # noqa: E402
from app.services.intersvyaz_archive import (  # noqa: E402
    download_preview_window_two_mp4,
    get_token,
    resolve_dates_with_any_archive,
    resolve_window_segments,
)

# Сколько разных дат на камеру (каждая дата = 2 MP4-чанка 10–11 и 11–12).
FILES_PER_CAMERA = int(os.getenv("PREVIEW_FILES_PER_CAMERA", "1"))
# any — последние дни, где в архиве вообще есть запись (по умолчанию).
# window — только дни, где в архиве пересекается окно 10:00–12:00.
PREVIEW_DATE_POLICY = os.getenv("PREVIEW_DATE_POLICY", "any").strip().lower()


def _safe_dir(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    return s or "unknown_point"


def _load_groups(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Файл конфигурации должен быть списком объектов")
    groups: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        cams = item.get("camera_ids", [])
        if not name or not isinstance(cams, list):
            continue
        cam_ids = []
        for v in cams:
            try:
                cam_ids.append(int(v))
            except (TypeError, ValueError):
                continue
        groups.append(
            {
                "name": name,
                "pawnshop_number": item.get("pawnshop_number"),
                "address": item.get("address"),
                "camera_ids": cam_ids,
            }
        )
    return groups


def _parse_camera_ids_arg(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            raise SystemExit(f"Неверный id камеры: {part!r}") from None
    if not out:
        raise SystemExit("После --cameras укажите хотя бы один числовой id")
    return out


def _today_local(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


def _parse_dates_arg(raw: str) -> list[date]:
    out: list[date] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(datetime.strptime(part, "%Y-%m-%d").date())
        except ValueError:
            raise SystemExit(f"Неверная дата: {part!r}, нужен YYYY-MM-DD") from None
    if not out:
        raise SystemExit("После --date укажите хотя бы одну дату YYYY-MM-DD")
    return out


def _print_done(total_ok_mp4: int, total_fail: int) -> None:
    print("\nГотово.")
    print(f"Скачано MP4-файлов: {total_ok_mp4}, проблемных сессий: {total_fail}")


def _process_one_camera(
    token: str,
    camera_id: int,
    cam_dir: Path,
    local_tz: ZoneInfo,
    pinned_dates: Optional[list[date]] = None,
) -> tuple[int, int]:
    """Одна камера: (число скачанных MP4, число проблемных сессий)."""
    ok_mp4 = 0
    fail = 0
    cam_dir.mkdir(parents=True, exist_ok=True)
    if pinned_dates:
        days = sorted(pinned_dates)[: max(1, FILES_PER_CAMERA)]
        print(
            f"Камера {camera_id}: фикс. даты (без запроса списка архива): "
            + ", ".join(str(d) for d in days)
        )
    else:
        print(f"Камера {camera_id}: выбор дат для превью ...")
        try:
            if PREVIEW_DATE_POLICY == "window":
                by_day = resolve_window_segments(
                    token=token,
                    project_id=cfg.INTERSVYAZ_PROJECT_ID,
                    camera_id=camera_id,
                    local_tz=local_tz,
                    window_start_hour=10,
                    window_end_hour=12,
                )
                if not by_day:
                    print("  нет дней с пересечением 10:00–12:00 в архиве")
                    return 0, 1
                days = sorted(by_day.keys(), reverse=True)[: max(1, FILES_PER_CAMERA)]
            else:
                all_days = resolve_dates_with_any_archive(
                    token=token,
                    project_id=cfg.INTERSVYAZ_PROJECT_ID,
                    camera_id=camera_id,
                    local_tz=local_tz,
                )
                if not all_days:
                    print("  нет ни одного дня с записью в архиве")
                    return 0, 1
                days = sorted(all_days, reverse=True)[: max(1, FILES_PER_CAMERA)]
        except Exception as e:
            print(f"  ошибка API: {e}")
            return 0, 1
        print("  даты: " + ", ".join(str(d) for d in sorted(days)))
    for day in sorted(days):
        paths = download_preview_window_two_mp4(
            token=token,
            project_id=cfg.INTERSVYAZ_PROJECT_ID,
            camera_id=camera_id,
            target_date=day,
            out_dir=cam_dir,
            local_tz=local_tz,
            cli_progress=True,
        )
        for p in paths:
            mb = p.stat().st_size / (1024 * 1024)
            print(f"  → {p.name} ({mb:.1f} МБ)")
        ok_mp4 += len(paths)
        if len(paths) < 2:
            fail += 1
    return ok_mp4, fail


def main() -> None:
    parser = argparse.ArgumentParser(description="Превью MP4 10:00–12:00 по камерам Интерсвязь")
    parser.add_argument(
        "--cameras",
        type=str,
        default=None,
        metavar="IDS",
        help="Только эти id камер через запятую (отдельный прогон, без всех групп из JSON)",
    )
    parser.add_argument(
        "--point",
        type=str,
        default="_manual",
        help='Имя подпапки под camera_previews (по умолчанию "_manual")',
    )
    dg = parser.add_mutually_exclusive_group()
    dg.add_argument(
        "--yesterday",
        action="store_true",
        help="Скачать за вчера (календарный день в зоне SCHEDULER_TZ), без выбора даты из архива",
    )
    dg.add_argument(
        "--date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Одна или несколько дат через запятую; без опроса API о доступных днях",
    )
    args = parser.parse_args()

    if not cfg.INTERSVYAZ_USERNAME or not cfg.INTERSVYAZ_PASSWORD:
        raise SystemExit("В .env должны быть INTERSVYAZ_USERNAME и INTERSVYAZ_PASSWORD")

    local_tz = ZoneInfo(cfg.SCHEDULER_TZ)
    token = get_token(cfg.INTERSVYAZ_USERNAME, cfg.INTERSVYAZ_PASSWORD)
    root = cfg.AUDIO_CACHE_DIR / "camera_previews"
    root.mkdir(parents=True, exist_ok=True)

    print(f"Папка выгрузки: {root}")
    print(
        f"Формат: MP4 (2 часа на дату: 10–11 и 11–12), проект={cfg.INTERSVYAZ_PROJECT_ID}, "
        f"выбор даты: {PREVIEW_DATE_POLICY} (PREVIEW_DATE_POLICY=any|window)\n",
    )

    pinned_dates: Optional[list[date]] = None
    if args.yesterday:
        d = _today_local(local_tz) - timedelta(days=1)
        pinned_dates = [d]
        print(f"Режим даты: за вчера → {d} ({cfg.SCHEDULER_TZ})\n")
    elif args.date:
        pinned_dates = _parse_dates_arg(args.date)
        print(f"Режим даты: фикс. {', '.join(str(x) for x in sorted(pinned_dates))}\n")

    total_ok_mp4 = 0
    total_fail = 0

    if args.cameras is not None:
        cam_ids = _parse_camera_ids_arg(args.cameras)
        point_dir = root / _safe_dir(args.point)
        point_dir.mkdir(parents=True, exist_ok=True)
        print(f"Режим: только камеры {cam_ids}, папка: {_safe_dir(args.point)}\n")
        for i, camera_id in enumerate(cam_ids):
            if i:
                print()
            cam_dir = point_dir / str(camera_id)
            o, f = _process_one_camera(token, camera_id, cam_dir, local_tz, pinned_dates=pinned_dates)
            total_ok_mp4 += o
            total_fail += f
        _print_done(total_ok_mp4, total_fail)
        return

    config_path = cfg.BASE_DIR / "camera_preview_groups.json"
    if not config_path.is_file():
        raise SystemExit(
            "Нет camera_preview_groups.json. Скопируйте camera_preview_groups.json.example и заполните камеры."
        )

    groups = _load_groups(config_path)
    if not groups:
        raise SystemExit("В camera_preview_groups.json нет валидных групп")

    for g in groups:
        point_name = g["name"]
        point_dir = root / _safe_dir(point_name)
        point_dir.mkdir(parents=True, exist_ok=True)
        number = g.get("pawnshop_number")
        address = g.get("address")
        suffix = f" [№{number}]" if number is not None else ""
        print(f"\n=== {point_name}{suffix} ===")
        if address:
            print(f"Адрес: {address}")

        for camera_id in g["camera_ids"]:
            cam_dir = point_dir / str(camera_id)
            o, f = _process_one_camera(token, camera_id, cam_dir, local_tz, pinned_dates=pinned_dates)
            total_ok_mp4 += o
            total_fail += f

    _print_done(total_ok_mp4, total_fail)


if __name__ == "__main__":
    main()
