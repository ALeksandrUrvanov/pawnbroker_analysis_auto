"""Интерсвязь (platform-vision): архив, смены 9–21, скачивание по часу → склейка → MP3. Env: INTERSVYAZ_USERNAME, INTERSVYAZ_PASSWORD."""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time as time_mod
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
import shutil
from typing import Callable, Optional

import httpx

from ..config import INTERSVYAZ_CHUNK_MAX_ATTEMPTS

BASE_URL = "https://platform-vision.is74.ru"
TIMEOUT = 30.0
# Таймаут скачивания файла: connect=30с, read=600с (Интерсвязь стримит до ~4 мин на чанк)
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)

# Интерсвязь требует TLS 1.2 — TLS 1.3 не проходит рукопожатие с OpenSSL
def _tls12_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

_SSL = _tls12_context()
POLL_INTERVAL = 5
ORDER_WAIT_TIMEOUT = 600
MIN_CHUNK_BYTES = 500_000
CHUNK_DELAY_SEC = 5
FFMPEG_CONCAT_TIMEOUT = 3600

_STATUS_URL_TEMPLATES = (
    "{base}/cams/archive/order/status/{oid}",
    "{base}/api/cams/archive/order/status/{oid}",
)


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Неверный формат даты: {s}. Используйте YYYY-MM-DD или DD.MM.YYYY")


def _sanitize_name(name: str, fallback_id: int) -> str:
    bad = '\\/:*?"<>|'
    s = "".join(c if c not in bad else "_" for c in (name or "")).strip()
    return s or f"camera_{fallback_id}"


def _local_to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_local(iso: str, tz: tzinfo) -> datetime:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _camera_name(cameras: list, camera_id: int) -> str:
    return next(
        (c.get("name", "camera") for c in cameras if c.get("id") == camera_id),
        "camera",
    )


def _parse_tz_offset(s: str) -> tzinfo:
    s = s.strip().upper().replace("UTC", "").strip()
    sign = 1
    if s.startswith("-"):
        sign = -1
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()
    return timezone(timedelta(hours=sign * int(s)))


def _get_local_tz(args: argparse.Namespace) -> tzinfo:
    if args.tz:
        return _parse_tz_offset(args.tz)
    return datetime.now().astimezone().tzinfo


def get_token(username: str, password: str) -> str:
    r = httpx.post(
        f"{BASE_URL}/api/login/",
        data={"username": username, "password": password},
        timeout=TIMEOUT,
        verify=_SSL,
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError("В ответе нет access_token")
    return token


def _json_list(data: object) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items") or data.get("results") or []
    return []


def get_projects(token: str) -> list:
    r = httpx.get(f"{BASE_URL}/api/projects/", headers=_auth_headers(token), timeout=TIMEOUT, verify=_SSL)
    r.raise_for_status()
    return _json_list(r.json())


def get_cameras(token: str, project_id: int) -> list:
    r = httpx.get(
        f"{BASE_URL}/api/projects/{project_id}/cameras",
        headers=_auth_headers(token), timeout=TIMEOUT, verify=_SSL,
    )
    r.raise_for_status()
    return _json_list(r.json())


def get_archive_intervals(token: str, project_id: int, camera_id: int) -> list | None:
    r = httpx.get(
        f"{BASE_URL}/api/projects/{project_id}/cameras/{camera_id}/archive_intervals",
        headers=_auth_headers(token), timeout=TIMEOUT, verify=_SSL,
    )
    if r.status_code == 422:
        return None
    r.raise_for_status()
    return r.json()


def create_archive_order(
    token: str, project_id: int, camera_id: int,
    start_utc: str, stop_utc: str,
) -> str:
    r = httpx.post(
        f"{BASE_URL}/api/projects/{project_id}/cameras/{camera_id}/archive/order",
        json={"startDate": start_utc, "stopDate": stop_utc},
        headers={**_auth_headers(token), "Content-Type": "application/json"},
        timeout=TIMEOUT,
        verify=_SSL,
    )
    r.raise_for_status()
    order_id = r.json().get("id")
    if not order_id:
        raise RuntimeError("В ответе нет id задания")
    return order_id


def get_order_status(token: str, order_id: str) -> dict:
    for tpl in _STATUS_URL_TEMPLATES:
        url = tpl.format(base=BASE_URL, oid=order_id)
        r = httpx.get(url, headers=_auth_headers(token), timeout=TIMEOUT, verify=_SSL)
        if r.status_code != 200:
            continue
        raw = (r.text or "").strip()
        if not raw:
            return {"status": "queue", "url": None, "errorCode": None}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"Не удалось получить статус заказа {order_id}")


def _download_file(token: str, url: str, save_path: str) -> None:
    full = url if url.startswith("http") else f"{BASE_URL.rstrip('/')}/{url.lstrip('/')}"
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", full, headers=_auth_headers(token), timeout=DOWNLOAD_TIMEOUT, verify=_SSL) as resp:
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)


def run_order_and_download(
    token: str, project_id: int, camera_id: int,
    start_utc: str, stop_utc: str, save_path: str,
    *,
    min_chunk_bytes: Optional[int] = None,
) -> tuple[bool, str]:
    """Возвращает (успех, пустая строка) или (False, краткая причина для лога)."""
    threshold = MIN_CHUNK_BYTES if min_chunk_bytes is None else int(min_chunk_bytes)
    try:
        order_id = create_archive_order(token, project_id, camera_id, start_utc, stop_utc)
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "").strip().replace("\n", " ")[:180]
        # 422 часто бывает на коротких/пограничных интервалах или при отсутствии куска архива.
        msg = f"HTTP {e.response.status_code}"
        if body:
            msg += f": {body}"
        return False, msg
    except Exception as e:
        return False, str(e)[:200]

    deadline = time_mod.monotonic() + ORDER_WAIT_TIMEOUT
    while time_mod.monotonic() < deadline:
        try:
            st = get_order_status(token, order_id)
        except Exception:
            # Transient connection error — retry after pause instead of failing immediately
            time_mod.sleep(POLL_INTERVAL)
            continue
        if st.get("status") == "done":
            url = st.get("url") or f"/cams/archive/download/{order_id}.mp4"
            try:
                _download_file(token, url, save_path)
            except Exception as e:
                Path(save_path).unlink(missing_ok=True)
                return False, f"скачивание: {e!s}"[:200]
            p = Path(save_path)
            if p.stat().st_size < threshold:
                p.unlink(missing_ok=True)
                return False, f"файл < {threshold} байт (пустой/битый ответ)"
            return True, ""
        ec = st.get("errorCode")
        if ec in ("compile", "no_archive"):
            hints = {
                "no_archive": "нет записи в архиве за этот интервал",
                "compile": "ошибка сборки видео (compile)",
            }
            return False, hints.get(ec, str(ec))
        if ec == "have_hops":
            # Файл собран с разрывами, но скачать можно — для транскрипции разрывы не критичны
            url = st.get("url") or f"/cams/archive/download/{order_id}.mp4"
            try:
                _download_file(token, url, save_path)
            except Exception as e:
                Path(save_path).unlink(missing_ok=True)
                return False, f"have_hops, скачивание: {e!s}"[:200]
            p = Path(save_path)
            if p.stat().st_size < threshold:
                p.unlink(missing_ok=True)
                return False, f"have_hops: файл < {threshold} байт"
            return True, ""
        time_mod.sleep(POLL_INTERVAL)
    return False, "таймаут ожидания готовности заказа"


def build_intervals_local(
    intervals: list, local_tz: tzinfo, from_dt: datetime | None,
) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for iv in intervals:
        try:
            start = _to_local(iv.get("startDate") or "", local_tz)
            stop = _to_local(iv.get("stopDate") or "", local_tz)
        except (ValueError, TypeError):
            continue
        if from_dt and stop < from_dt:
            continue
        result.append((max(start, from_dt) if from_dt else start, stop))
    return result


def _segments_by_day_hours(
    intervals: list[tuple[datetime, datetime]],
    tz: tzinfo,
    hour_start: int,
    hour_end: int,
) -> dict[date, list[tuple[datetime, datetime]]]:
    """Пересечение интервалов архива с одним календарным окном [hour_start, hour_end) по дням."""
    if not intervals:
        return {}
    by_date: dict[date, list[tuple[datetime, datetime]]] = defaultdict(list)
    for start, end in intervals:
        cur_d = start.date()
        while cur_d <= end.date():
            win_s = datetime.combine(cur_d, time(hour_start, 0), tzinfo=tz)
            win_e = datetime.combine(cur_d, time(hour_end, 0), tzinfo=tz)
            seg_s, seg_e = max(start, win_s), min(end, win_e)
            if seg_s < seg_e and (seg_e - seg_s).total_seconds() >= 60:
                by_date[cur_d].append((seg_s, seg_e))
            cur_d += timedelta(days=1)
    return {d: sorted(segs) for d, segs in by_date.items() if segs}


def compute_shift_segments(
    intervals: list[tuple[datetime, datetime]],
    shift_start: int = 9,
    shift_end: int = 21,
) -> dict[date, list[tuple[datetime, datetime]]]:
    if not intervals:
        return {}
    tz = intervals[0][0].tzinfo
    return _segments_by_day_hours(intervals, tz, shift_start, shift_end)


def _intervals_and_shifts(
    token: str,
    project_id: int,
    camera_id: int,
    local_tz: tzinfo,
    from_date: Optional[date] = None,
) -> tuple[Optional[list[tuple[datetime, datetime]]], dict[date, list[tuple[datetime, datetime]]]]:
    """Сырые интервалы → смены 9–21. (None, {}) если архива нет (422/пусто)."""
    from_dt = datetime.combine(from_date, time(0, 0), tzinfo=local_tz) if from_date else None
    intervals_raw = get_archive_intervals(token, project_id, camera_id)
    if not intervals_raw:
        return None, {}
    intervals_local = build_intervals_local(intervals_raw, local_tz, from_dt)
    shifts = compute_shift_segments(intervals_local)
    if from_date:
        shifts = {d: s for d, s in shifts.items() if d >= from_date}
    return intervals_local, shifts


def resolve_shift_segments(
    token: str,
    project_id: int,
    camera_id: int,
    local_tz: tzinfo,
    from_date: Optional[date] = None,
) -> dict[date, list[tuple[datetime, datetime]]]:
    """Смены 9–21 по дням; пустой dict если архива нет."""
    intervals_local, shifts = _intervals_and_shifts(
        token, project_id, camera_id, local_tz, from_date,
    )
    return shifts if intervals_local is not None else {}


def resolve_window_segments(
    token: str,
    project_id: int,
    camera_id: int,
    local_tz: tzinfo,
    window_start_hour: int,
    window_end_hour: int,
    from_date: Optional[date] = None,
) -> dict[date, list[tuple[datetime, datetime]]]:
    """Сегменты по дням внутри окна часов (например 10–12)."""
    intervals_local, _ = _intervals_and_shifts(
        token, project_id, camera_id, local_tz, from_date,
    )
    if intervals_local is None:
        return {}
    return _segments_by_day_hours(
        intervals_local, local_tz, window_start_hour, window_end_hour,
    )


def resolve_dates_with_any_archive(
    token: str,
    project_id: int,
    camera_id: int,
    local_tz: tzinfo,
    from_date: Optional[date] = None,
) -> list[date]:
    """Календарные дни, в которые по архиву есть хоть какая-то запись (без привязки к окну 10–12)."""
    intervals_local, _ = _intervals_and_shifts(
        token, project_id, camera_id, local_tz, from_date,
    )
    if not intervals_local:
        return []
    out: set[date] = set()
    for s, e in intervals_local:
        cur_d = s.date()
        end_d = e.date()
        while cur_d <= end_d:
            out.add(cur_d)
            cur_d += timedelta(days=1)
    return sorted(out)


def _split_to_hour_chunks(
    segments: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    chunk_size = timedelta(hours=1)
    chunks: list[tuple[datetime, datetime]] = []
    for start, end in segments:
        cur = start
        while cur < end:
            nxt = min(cur + chunk_size, end)
            if (nxt - cur).total_seconds() >= 1:
                chunks.append((cur, nxt))
            cur = nxt
    return sorted(chunks)


def _temp_chunk_path(out_dir: Path, d: date, i: int) -> Path:
    return out_dir / f"_tmp_{d.isoformat()}_{i}.mp4"


def _kept_chunk_path(keep_chunks_dir: Path, target_date: date, start: datetime, end: datetime) -> Path:
    keep_chunks_dir.mkdir(parents=True, exist_ok=True)
    return keep_chunks_dir / f"{target_date.isoformat()}_{start:%H%M}-{end:%H%M}.mp4"


def _concat_chunks_to_mp3(inputs: list[str], output_mp3: str) -> tuple[bool, float]:
    if not inputs:
        return False, 0.0
    list_file = Path(output_mp3).parent / "_concat_list.txt"
    list_file.write_text(
        "\n".join(f"file '{Path(p).absolute().as_posix()}'" for p in inputs),
        encoding="utf-8",
    )
    t0 = time_mod.monotonic()
    try:
        ok = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-vn",
                "-c:a", "libmp3lame", "-q:a", "7",
                "-ar", "16000", "-ac", "1",
                output_mp3,
            ],
            capture_output=True, timeout=FFMPEG_CONCAT_TIMEOUT,
        ).returncode == 0
        return ok, (time_mod.monotonic() - t0)
    finally:
        list_file.unlink(missing_ok=True)


def _cleanup_day_chunks(out_dir: Path, d: date, n_chunks: int) -> None:
    for i in range(n_chunks):
        _temp_chunk_path(out_dir, d, i).unlink(missing_ok=True)


def _period_and_duration(segs: list[tuple[datetime, datetime]]) -> tuple[str, float]:
    if not segs:
        return "", 0.0
    first, last = segs[0][0], segs[-1][1]
    period = f"{first:%H:%M:%S}–{last:%H:%M:%S}"
    dur = sum((e - s).total_seconds() for s, e in segs)
    return period, dur


def download_preview_window_two_mp4(
    token: str,
    project_id: int,
    camera_id: int,
    target_date: date,
    out_dir: Path,
    local_tz: tzinfo,
    *,
    cli_progress: bool = False,
) -> list[Path]:
    """
    Два отдельных MP4 за день: 10:00–11:00 и 11:00–12:00. Без склейки и без MP3.
    Имена: YYYY-MM-DD_HHMM-HHMM.mp4

    Env (только превью): PREVIEW_CHUNK_RETRIES — повторов при неудаче (по умолчанию 1);
    PREVIEW_MIN_CHUNK_BYTES — мин. размер файла (по умолчанию как у смены, 500000).
    """
    preview_retries = max(0, int(os.getenv("PREVIEW_CHUNK_RETRIES", "1")))
    raw_min = (os.getenv("PREVIEW_MIN_CHUNK_BYTES") or "").strip()
    min_b: Optional[int] = int(raw_min) if raw_min.isdigit() else None

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, (h1, m1, h2, m2) in enumerate(
        ((10, 0, 11, 0), (11, 0, 12, 0)),
        start=1,
    ):
        s = datetime.combine(target_date, time(h1, m1), tzinfo=local_tz)
        e = datetime.combine(target_date, time(h2, m2), tzinfo=local_tz)
        fname = f"{target_date.isoformat()}_{h1:02d}{m1:02d}-{h2:02d}{m2:02d}.mp4"
        out_path = out_dir / fname
        if cli_progress:
            print(f"  {target_date} видео {i}/2 {s:%H:%M}–{e:%H:%M} … ", end="", flush=True)
        ok, err = False, ""
        last_err = ""
        for attempt in range(preview_retries + 1):
            ok, err = run_order_and_download(
                token, project_id, camera_id,
                _local_to_utc_iso(s), _local_to_utc_iso(e), str(out_path),
                min_chunk_bytes=min_b,
            )
            last_err = err
            if ok:
                break
            if attempt < preview_retries:
                time_mod.sleep(CHUNK_DELAY_SEC * 2)
        if cli_progress:
            print("OK" if ok else f"ошибка ({last_err})")
        if ok and out_path.exists():
            saved.append(out_path)
        if i < 2:
            time_mod.sleep(CHUNK_DELAY_SEC)
    return saved


def download_shift_segments_as_mp3(
    token: str,
    project_id: int,
    camera_id: int,
    target_date: date,
    segs: list[tuple[datetime, datetime]],
    out_dir: Path,
    *,
    keep_chunks_dir: Optional[Path] = None,
    log_chunk: Optional[Callable[[str], None]] = None,
    cli_progress: bool = False,
) -> tuple[Optional[Path], Optional[str], Optional[float]]:
    """
    Скачивает переданные отрезки смены и сразу собирает MP3 в out_dir / {date}.mp3.
    На каждый часовой чанк — до INTERSVYAZ_CHUNK_MAX_ATTEMPTS попыток; после исчерпания
    чанк пропускается, остальные склеиваются (частичный MP3). cli_progress — вывод в терминал.
    """
    chunks = _split_to_hour_chunks(segs)
    if not chunks:
        return None, None, None
    n_chunks = len(chunks)
    max_attempts = INTERSVYAZ_CHUNK_MAX_ATTEMPTS
    out_mp3 = out_dir / f"{target_date.isoformat()}.mp3"
    temps: list[str] = []
    ok_segments: list[tuple[datetime, datetime]] = []

    def _log(msg: str) -> None:
        if log_chunk:
            log_chunk(msg)

    t_dl_start = time_mod.monotonic()
    for i, (s, e) in enumerate(chunks):
        tp = _temp_chunk_path(out_dir, target_date, i)
        if cli_progress:
            print(f"  {target_date} чанк {i+1}/{n_chunks} {s:%H:%M}–{e:%H:%M} … ", end="", flush=True)
        else:
            _log(f"Интерсвязь: {target_date} фрагмент {i + 1}/{n_chunks} {s:%H:%M}–{e:%H:%M}")
        ok = False
        last_err = ""
        for attempt in range(max_attempts):
            tp.unlink(missing_ok=True)
            ok, last_err = run_order_and_download(
                token, project_id, camera_id,
                _local_to_utc_iso(s), _local_to_utc_iso(e), str(tp),
            )
            if ok:
                break
            if attempt + 1 < max_attempts:
                msg = f"чанк {i + 1}/{n_chunks}: повтор {attempt + 2}/{max_attempts} ({last_err})"
                if cli_progress:
                    print(f"retry… ", end="", flush=True)
                else:
                    _log(f"Интерсвязь: {msg}")
                time_mod.sleep(CHUNK_DELAY_SEC * 2)
        if cli_progress:
            print("OK" if ok else f"ошибка ({last_err}), пропуск")
        elif not ok:
            _log(f"Интерсвязь: чанк {i + 1}/{n_chunks} пропущен после {max_attempts} попыток: {last_err}")
        if ok:
            temps.append(str(tp))
            ok_segments.append((s, e))
            if keep_chunks_dir is not None and tp.exists():
                kept_path = _kept_chunk_path(keep_chunks_dir, target_date, s, e)
                shutil.copy2(tp, kept_path)
        if i < n_chunks - 1:
            time_mod.sleep(CHUNK_DELAY_SEC)

    t_dl = time_mod.monotonic() - t_dl_start
    if not temps:
        _cleanup_day_chunks(out_dir, target_date, n_chunks)
        if cli_progress:
            print(f"  → за {target_date} ничего не скачано")
        return None, None, None

    if len(ok_segments) < n_chunks:
        if cli_progress:
            print(f"  → частично: {len(ok_segments)}/{n_chunks} чанков")
        else:
            _log(
                f"Интерсвязь: {target_date} собрано {len(ok_segments)}/{n_chunks} чанков — склейка частичная"
            )

    period_str, audio_duration_sec = _period_and_duration(ok_segments)
    if len(ok_segments) < n_chunks:
        period_str = f"[частично {len(ok_segments)}/{n_chunks}] {period_str}"

    if cli_progress:
        print("  MP3 … ", end="", flush=True)
    ok_concat, t_mp3 = _concat_chunks_to_mp3(temps, str(out_mp3))
    if not ok_concat or not out_mp3.exists():
        _cleanup_day_chunks(out_dir, target_date, n_chunks)
        if cli_progress:
            print("ошибка")
        return None, None, None

    _cleanup_day_chunks(out_dir, target_date, n_chunks)
    if cli_progress:
        print(f"OK  (скач. {t_dl:.0f} с, MP3 {t_mp3:.0f} с)")
        print(f"  → {out_mp3.name}")
    return out_mp3, period_str, audio_duration_sec


def download_shift_day_as_mp3(
    token: str,
    project_id: int,
    camera_id: int,
    target_date: date,
    out_dir: Path,
    local_tz: tzinfo,
    keep_chunks_dir: Optional[Path] = None,
    log_chunk: Optional[Callable[[str], None]] = None,
) -> tuple[Optional[Path], Optional[str], Optional[float]]:
    """
    Скачивает смену 9–21 за target_date, склеивает, извлекает MP3 в out_dir / {date}.mp3.
    Возвращает (путь к mp3, период, длительность сек) или (None, None, None).
    """
    shifts = resolve_shift_segments(token, project_id, camera_id, local_tz, from_date=target_date)
    if target_date not in shifts or not shifts[target_date]:
        return None, None, None
    return download_shift_segments_as_mp3(
        token, project_id, camera_id, target_date, shifts[target_date], out_dir,
        keep_chunks_dir=keep_chunks_dir, log_chunk=log_chunk, cli_progress=False,
    )


# --- CLI ---

def _resolve_shifts(
    token: str, args: argparse.Namespace, local_tz: tzinfo,
) -> tuple[list[tuple[datetime, datetime]], dict[date, list[tuple[datetime, datetime]]], date | None]:
    from_date = _parse_date(args.from_date) if args.from_date else None
    intervals_local, shifts = _intervals_and_shifts(
        token, args.project_id, args.camera_id, local_tz, from_date,
    )
    if intervals_local is None:
        print("У камеры нет видеоархива или ошибка 422.")
        sys.exit(1)
    return intervals_local, shifts, from_date


def _process_one_day(
    token: str, args: argparse.Namespace, out_dir: Path, d: date,
    segs: list[tuple[datetime, datetime]],
) -> None:
    download_shift_segments_as_mp3(
        token, args.project_id, args.camera_id, d, segs, out_dir, cli_progress=True,
    )


def _cmd_download_shifts(token: str, args: argparse.Namespace) -> None:
    local_tz = _get_local_tz(args)
    _, shifts, _ = _resolve_shifts(token, args, local_tz)
    if not shifts:
        print("Нет смен для скачивания.")
        sys.exit(0)

    if args.day:
        day = _parse_date(args.day)
        if day not in shifts:
            print(f"За {day} нет смены в архиве.")
            sys.exit(1)
        shifts = {day: shifts[day]}

    cameras = get_cameras(token, args.project_id)
    folder = (args.folder or "").strip() or _sanitize_name(
        _camera_name(cameras, args.camera_id), args.camera_id,
    )
    out_dir = Path(args.output_dir) / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Папка: {out_dir.absolute()}")
    print(f"Дат к выгрузке: {len(shifts)}\n")
    for d in sorted(shifts):
        _process_one_day(token, args, out_dir, d, shifts[d])
    print("\nГотово.")


def _cmd_view_intervals(token: str, args: argparse.Namespace) -> None:
    local_tz = _get_local_tz(args)
    intervals_local, shifts, from_date = _resolve_shifts(token, args, local_tz)

    fmt = "%d.%m.%Y %H:%M"
    print(f"\nИнтервалы архива (камера {args.camera_id}):")
    if from_date:
        print(f"  С {from_date:%d.%m.%Y}.\n")
    tz_label = f"--tz {args.tz}" if args.tz else "системный"
    print(f"  Время — местное ({tz_label}), API отдаёт UTC.\n")

    total = 0.0
    for i, (s, e) in enumerate(intervals_local, 1):
        dur = (e - s).total_seconds()
        total += dur
        print(f"  [{i:2}] {s:{fmt}} — {e:{fmt}}  ({dur / 3600:.1f} ч)")

    print(f"\n  Интервалов: {len(intervals_local)}")
    if total:
        print(f"  Всего: {total / 3600:.1f} ч ({total / 86400:.1f} сут)")

    if not shifts:
        return

    print(f"\n{'=' * 60}")
    print("Смены 9:00–21:00")
    if from_date:
        print(f"  С {from_date:%d.%m.%Y}.\n")
    for d in sorted(shifts):
        segs = shifts[d]
        parts = ", ".join(f"{s:%H:%M}–{e:%H:%M}" for s, e in segs)
        n = len(segs)
        w = "отрывок" if n == 1 else ("отрывка" if n < 5 else "отрывков")
        print(f"  {d:%d.%m.%Y}:  {parts}  ({n} {w})")
    print("=" * 60)


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    p = argparse.ArgumentParser(
        description="Интерсвязь: проекты / камеры / интервалы / смены",
    )
    p.add_argument("--username", default=os.getenv("INTERSVYAZ_USERNAME"))
    p.add_argument("--password", default=os.getenv("INTERSVYAZ_PASSWORD"))
    p.add_argument("--project-id", type=int, default=126)
    p.add_argument("--camera-id", type=int)
    p.add_argument("--from-date", default="2026-03-01")
    p.add_argument("--tz", help="Таймзона смены (UTC+5, +5, UTC-2). Без — системная")
    p.add_argument("--download-shifts", action="store_true")
    p.add_argument("--output-dir", default="intersvyaz_archive")
    p.add_argument("--folder")
    p.add_argument("--day")
    args = p.parse_args()

    if not args.username or not args.password:
        p.error("Укажите логин/пароль (--username/--password или env)")

    print("Получение токена…")
    token = get_token(args.username, args.password)
    print("Токен получен.")

    if args.project_id is None:
        for proj in get_projects(token):
            print(f"  id={proj.get('id')}  name={proj.get('name', proj)}")
        return

    if args.camera_id is None:
        for cam in get_cameras(token, args.project_id):
            print(f"  id={cam.get('id')}  name={cam.get('name', cam)}")
        return

    if args.download_shifts:
        _cmd_download_shifts(token, args)
    else:
        _cmd_view_intervals(token, args)


if __name__ == "__main__":
    main()
