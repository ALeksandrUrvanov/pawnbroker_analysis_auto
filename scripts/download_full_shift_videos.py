#!/usr/bin/env python3
"""
Скачивание полного видео смены (09:00-21:00) по часам из Интерсвязи с автосклейкой в один MP4.

Пример:
  python scripts/download_full_shift_videos.py --pawnshop 039 --dates 2026-03-18,2026-03-24
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfg  # noqa: E402
from app.services.intersvyaz_archive import (  # noqa: E402
    _local_to_utc_iso,
    get_token,
    run_order_and_download,
)


def _parse_dates(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        val = part.strip()
        if not val:
            continue
        try:
            datetime.strptime(val, "%Y-%m-%d")
        except ValueError:
            raise SystemExit(f"Неверная дата: {val!r}, ожидается YYYY-MM-DD") from None
        out.append(val)
    if not out:
        raise SystemExit("После --dates укажите хотя бы одну дату YYYY-MM-DD")
    return out


def _resolve_camera_id(pawnshop: str, camera_id: int | None) -> int:
    if camera_id is not None:
        return int(camera_id)
    mapping = cfg.load_pawnshop_camera_mapping()
    cid = mapping.get(str(pawnshop))
    if cid is None:
        raise SystemExit(
            f"Для ломбарда {pawnshop!r} не найдена камера в camera_mapping.json; "
            "укажите --camera-id явно."
        )
    return int(cid)


def _concat_mp4(chunks: list[Path], out_full: Path) -> bool:
    out_full = out_full.resolve()
    list_file = (out_full.parent / "_concat_list.txt").resolve()
    list_file.write_text("".join([f"file '{p.name}'\n" for p in chunks]), encoding="utf-8")
    try:
        res = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file.name,
                "-c",
                "copy",
                out_full.name,
            ],
            cwd=str(out_full.parent),
        )
        return res.returncode == 0 and out_full.exists()
    finally:
        list_file.unlink(missing_ok=True)


def _download_one_day(
    token: str,
    project_id: int,
    camera_id: int,
    day: str,
    out_dir: Path,
    local_tz: ZoneInfo,
) -> None:
    start = datetime.fromisoformat(day + "T09:00:00").replace(tzinfo=local_tz)
    end = datetime.fromisoformat(day + "T21:00:00").replace(tzinfo=local_tz)
    max_attempts = max(1, int(cfg.INTERSVYAZ_CHUNK_MAX_ATTEMPTS))

    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    cur = start
    idx = 1
    while cur < end:
        nxt = min(cur + timedelta(hours=1), end)
        chunk_path = out_dir / f"{day}_{cur:%H%M}-{nxt:%H%M}.mp4"

        ok = False
        last_err = ""
        for attempt in range(max_attempts):
            ok, last_err = run_order_and_download(
                token,
                project_id,
                camera_id,
                _local_to_utc_iso(cur),
                _local_to_utc_iso(nxt),
                str(chunk_path),
            )
            if ok and chunk_path.exists():
                break
            if attempt + 1 < max_attempts:
                time.sleep(2.0)

        print(f"  чанк {idx:02d} {cur:%H:%M}-{nxt:%H:%M}: {'OK' if ok else f'ERROR: {last_err}'}")
        if ok and chunk_path.exists():
            chunks.append(chunk_path)

        cur = nxt
        idx += 1

    if not chunks:
        print(f"  {day}: ничего не скачано")
        return

    out_full = out_dir / f"full_{day}_0900-2100.mp4"
    if _concat_mp4(chunks, out_full):
        print(f"  full: OK -> {out_full}")
    else:
        print(f"  full: ERROR (склейка не удалась) -> {out_full}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Полные видео смены (09:00-21:00) по часам с автосклейкой.")
    parser.add_argument("--pawnshop", required=True, help="Номер ломбарда (например 039)")
    parser.add_argument("--dates", required=True, help="Даты через запятую: YYYY-MM-DD,YYYY-MM-DD")
    parser.add_argument("--camera-id", type=int, default=None, help="Камера явно (если не брать из camera_mapping)")
    parser.add_argument(
        "--output-root",
        default="audio_cache",
        help="Корневая папка для выгрузки (по умолчанию audio_cache)",
    )
    args = parser.parse_args()

    if not cfg.INTERSVYAZ_USERNAME or not cfg.INTERSVYAZ_PASSWORD:
        raise SystemExit("В .env должны быть INTERSVYAZ_USERNAME и INTERSVYAZ_PASSWORD")

    pawnshop = str(args.pawnshop).strip()
    dates = _parse_dates(args.dates)
    camera_id = _resolve_camera_id(pawnshop, args.camera_id)
    token = get_token(cfg.INTERSVYAZ_USERNAME, cfg.INTERSVYAZ_PASSWORD)

    pawnshop_tz = cfg.load_pawnshop_shift_tz(pawnshop)
    local_tz = ZoneInfo(pawnshop_tz)

    print(
        f"Ломбард: {pawnshop}, камера: {camera_id}, проект: {cfg.INTERSVYAZ_PROJECT_ID}, "
        f"таймзона смены: {pawnshop_tz}"
    )
    for d in dates:
        print(f"\n=== {d} ===")
        out_dir = Path(args.output_root) / f"full_video_{pawnshop}_{d}"
        _download_one_day(token, cfg.INTERSVYAZ_PROJECT_ID, camera_id, d, out_dir, local_tz)

    print("\nГотово.")


if __name__ == "__main__":
    main()
