"""Два режима: тестовый свод БД (один ломбард → TEST_REPORT_EMAIL) и боевой месячный (все точки → report_recipients из маппинга). Планировщик — только месячный свод."""
import argparse
import asyncio
import logging
import random
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

_MONTH_RU_NOM = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from .calendar_month import current_month_yyyy_mm, previous_month_yyyy_mm
from .config import AUDIO_CACHE_DIR, REPORTS_DIR


def _email_body_db_shift(
    fio: str,
    pawnshop_id: str,
    month_label: str,
    target_date: date,
    period_str: Optional[str] = None,
    address: Optional[str] = None,
) -> str:
    date_str = target_date.strftime("%d.%m.%Y")
    addr = (address or "").strip() or "адрес не указан в camera_mapping.json"
    period_line = f"\n\nФрагмент записи (Интерсвязь): {period_str}." if period_str else ""
    return (
        "Добрый день!\n\n"
        f"Во вложении — анализ работы смены за {date_str} товароведа {fio} "
        f"(ломбард №{pawnshop_id}, {addr}).{period_line}\n\n"
        f"Месяц выборки смен по базе данных: {month_label}"
    )


def _delete_artifacts_after_send(artifacts: dict) -> None:
    for key in ("report", "transcript", "zip"):
        p = artifacts.get(key)
        if p is not None:
            path = Path(p)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
    for p in artifacts.get("dialogue_files") or []:
        path = Path(p)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


async def _process_one_pawnshop(
    pawnshop_id: str,
    target_date: date,
    work_dir: Path,
    *,
    keep_video_chunks: bool = False,
) -> Tuple[list, Optional[date], Optional[dict], Optional[str]]:
    from .bootstrap import init_services
    from .sources import get_recording
    from .services.text_reports import build_all_artifacts

    init_services()
    from .bootstrap import pawnbroker_processor

    recording_path, period_str, _ = get_recording(
        pawnshop_id, target_date, keep_video_chunks=keep_video_chunks
    )
    if not recording_path:
        logger.warning("Пропуск ломбарда %s за %s: запись не получена", pawnshop_id, target_date)
        return [], None, None, None

    recording_path = Path(recording_path)
    try:
        result = await pawnbroker_processor.process_audio_async(
            str(recording_path), status_callback=None, priority="low", period_str=period_str
        )
        artifacts = build_all_artifacts(
            result, pawnshop_id, target_date, output_dir=work_dir, period_str=period_str
        )
        paths = [artifacts["report"], artifacts["transcript"], artifacts["zip"]]
        return paths, target_date, artifacts, period_str
    finally:
        if recording_path.exists():
            try:
                recording_path.relative_to(AUDIO_CACHE_DIR)
                recording_path.unlink()
            except (ValueError, OSError):
                pass


async def _try_candidates_for_tovaroved(
    pid: str,
    shift_date: date,
    candidates: List[date],
    work_dir: Path,
    fio: str,
    *,
    keep_video_chunks: bool = False,
) -> Tuple[Optional[list], Optional[date], Optional[dict], Optional[str]]:
    """Перебирает кандидатские даты по порядку; возвращает первый успешный результат."""
    for attempt_i, try_date in enumerate(candidates, start=1):
        paths, used_date, artifacts, period_str = await _process_one_pawnshop(
            pid, try_date, work_dir, keep_video_chunks=keep_video_chunks
        )
        if paths and used_date:
            if try_date != shift_date:
                logger.info(
                    "Товаровед %s: вместо дня %s обработан другой день месяца %s (попытка %s/%s)",
                    fio,
                    shift_date.isoformat(),
                    try_date.isoformat(),
                    attempt_i,
                    len(candidates),
                )
            return paths, used_date, artifacts, period_str
        logger.warning(
            "Товаровед %s: день %s — запись или анализ не получены (%s/%s)",
            fio,
            try_date.isoformat(),
            attempt_i,
            len(candidates),
        )
    return None, None, None, None


def _ref_date_for_db_month() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from .config import get_db_month_calendar_tz

    try:
        return datetime.now(ZoneInfo(get_db_month_calendar_tz())).date()
    except ZoneInfoNotFoundError:
        return date.today()


def run_db_shifts_test_single(
    pawnshop_id: str,
    month_mode: str,
    *,
    only_first_tovaroved: bool = False,
    random_one_tovaroved: bool = False,
    force_date: str | None = None,
    keep_video_chunks: bool = False,
) -> None:
    from .config import get_db_month_calendar_tz, load_camera_mapping_entry_by_pawnshop

    entry = load_camera_mapping_entry_by_pawnshop(pawnshop_id)
    if not entry:
        logger.error("Ломбард %s не найден в camera_mapping с kod_podrazdeleniya", pawnshop_id)
        sys.exit(1)
    ref = _ref_date_for_db_month()
    mode = (month_mode or "previous").strip().lower()
    if mode == "current":
        month_str = current_month_yyyy_mm(ref)
    elif mode == "previous":
        month_str = previous_month_yyyy_mm(ref)
    else:
        logger.error("month_mode: нужно current или previous, получено %s", month_mode)
        sys.exit(1)
    logger.info(
        "Тестовый свод БД: ломбард %s, kod=%s, месяц %s (%s), зона опорной даты %s",
        entry["pawnshop_id"],
        entry["kod_podrazdeleniya"],
        month_str,
        mode,
        get_db_month_calendar_tz(),
    )
    run_db_shifts_analysis(
        entry["kod_podrazdeleniya"],
        month_str,
        entry["pawnshop_id"],
        mail_test=True,
        only_first_tovaroved=only_first_tovaroved,
        random_one_tovaroved=random_one_tovaroved,
        force_date=force_date,
        keep_video_chunks=keep_video_chunks,
    )


def run_db_shifts_prod_single(
    pawnshop_id: str,
    month_mode: str,
    *,
    only_first_tovaroved: bool = False,
    random_one_tovaroved: bool = False,
    force_date: str | None = None,
    keep_video_chunks: bool = False,
) -> None:
    from .config import get_db_month_calendar_tz, load_camera_mapping_entry_by_pawnshop

    entry = load_camera_mapping_entry_by_pawnshop(pawnshop_id)
    if not entry:
        logger.error("Ломбард %s не найден в camera_mapping с kod_podrazdeleniya", pawnshop_id)
        sys.exit(1)
    ref = _ref_date_for_db_month()
    mode = (month_mode or "previous").strip().lower()
    if mode == "current":
        month_str = current_month_yyyy_mm(ref)
    elif mode == "previous":
        month_str = previous_month_yyyy_mm(ref)
    else:
        logger.error("month_mode: нужно current или previous, получено %s", month_mode)
        sys.exit(1)
    logger.info(
        "Боевой прогон (один ломбард): ломбард %s, kod=%s, месяц %s (%s), зона опорной даты %s",
        entry["pawnshop_id"],
        entry["kod_podrazdeleniya"],
        month_str,
        mode,
        get_db_month_calendar_tz(),
    )
    run_db_shifts_analysis(
        entry["kod_podrazdeleniya"],
        month_str,
        entry["pawnshop_id"],
        mail_test=False,
        only_first_tovaroved=only_first_tovaroved,
        random_one_tovaroved=random_one_tovaroved,
        force_date=force_date,
        keep_video_chunks=keep_video_chunks,
    )


def run_db_monthly_production(*, ref_date: Optional[date] = None) -> None:
    from . import config as cfg_mod

    entries = cfg_mod.load_all_camera_mapping_db_entries()
    if not entries:
        logger.error("camera_mapping.json: нет записей с kod_podrazdeleniya")
        sys.exit(1)
    missing_rcp = [
        e["pawnshop_id"]
        for e in entries
        if not cfg_mod.load_report_recipients_for_pawnshop(e["pawnshop_id"])
    ]
    if missing_rcp:
        logger.error(
            "У ломбардов нет report_recipients в camera_mapping: %s",
            ", ".join(missing_rcp),
        )
        sys.exit(1)
    ref = ref_date or _ref_date_for_db_month()
    month_str = previous_month_yyyy_mm(ref)
    logger.info(
        "Боевой месячный свод: ломбардов %s, месяц БД %s, опорная дата %s (%s); письма — на адреса report_recipients каждой точки",
        len(entries),
        month_str,
        ref.isoformat(),
        cfg_mod.get_db_month_calendar_tz(),
    )
    failed: List[str] = []
    for entry in entries:
        pid = entry["pawnshop_id"]
        ok = run_db_shifts_analysis(
            entry["kod_podrazdeleniya"],
            month_str,
            pid,
            mail_test=False,
            only_first_tovaroved=False,
            raise_on_no_sent=False,
        )
        if not ok:
            failed.append(pid)
    if failed:
        logger.error("Месячный свод: нет отправленных отчётов по ломбардам: %s", ", ".join(failed))
        sys.exit(1)


def _scheduled_db_monthly_job() -> None:
    run_db_monthly_production()


def run_db_shifts_analysis(
    kod_podrazdeleniya: str,
    month_yyyy_mm: str,
    pawnshop_id: str,
    *,
    mail_test: bool = False,
    only_first_tovaroved: bool = False,
    random_one_tovaroved: bool = False,
    force_date: str | None = None,
    keep_video_chunks: bool = False,
    raise_on_no_sent: bool = True,
) -> bool:
    from .config import (
        DB_SHIFT_DATE_ATTEMPTS,
        TEST_REPORT_EMAIL,
        load_pawnshop_address,
        load_pawnshop_camera_mapping,
        load_report_recipients_for_pawnshop,
    )
    from .db_pg import get_db_connection
    from .services.email_sender import send_report_email
    from .tovaroved_svod import (
        candidate_shift_dates_for_kod,
        dates_by_tovaroved,
        fetch_month_shifts,
        month_bounds,
        parse_month_yyyy_mm,
        pick_random_shift_per_tovaroved,
    )

    pid = pawnshop_id.zfill(3) if pawnshop_id.isdigit() else pawnshop_id
    if pid not in load_pawnshop_camera_mapping():
        logger.error("Ломбард %s нет в camera_mapping / PAWNSHOP_CAMERA_MAP", pid)
        sys.exit(1)

    kod_sql = (kod_podrazdeleniya or "").strip()
    allowed = set("0123456789.vV")
    if not kod_sql or any(ch not in allowed for ch in kod_sql):
        logger.error(
            "Нужен KodPodrazdeleniya как в БД: например 000000001, 0000276.1, 00000039v"
        )
        sys.exit(1)

    try:
        y, mo = parse_month_yyyy_mm(month_yyyy_mm)
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    d0, d1 = month_bounds(y, mo)
    month_label = f"{_MONTH_RU_NOM[mo - 1]} {y} г."
    address = load_pawnshop_address(pid)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        rows, colnames = fetch_month_shifts(
            cur, kod_sql, d0, d1, include_branch_name=False
        )
    finally:
        cur.close()
        conn.close()

    if not rows:
        logger.warning("За период нет данных свода (проверьте код подразделения и месяц)")
        if raise_on_no_sent:
            sys.exit(1)
        return False

    picks = pick_random_shift_per_tovaroved(rows, colnames)
    if force_date:
        import datetime as _dt
        try:
            fixed = _dt.date.fromisoformat(force_date)
        except ValueError:
            logger.error("--db-test-date: неверный формат даты '%s', ожидается YYYY-MM-DD", force_date)
            sys.exit(1)
        picks = [(kod, fixed, fio) for kod, _, fio in picks]
        logger.info("Зафиксирована дата смены: %s (переопределяет случайный выбор)", fixed.isoformat())
    if only_first_tovaroved:
        picks.sort(key=lambda p: (p[2] or "").lower())
        picks = picks[:1]
        logger.info("Режим одного товароведа: после случайного выбора смены — только %s", picks[0][2] if picks else "—")
    if random_one_tovaroved:
        picks = [random.choice(picks)] if picks else []
        logger.info(
            "Режим одного случайного товароведа: после случайного выбора смен — только %s",
            picks[0][2] if picks else "—",
        )
    logger.info(
        "Случайные смены по товароведам (%s чел.): %s",
        len(picks),
        ", ".join(f"{p[2]} → {p[1].isoformat()}" for p in picks),
    )

    work_dir = REPORTS_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    if mail_test:
        recipients: List[str] = [TEST_REPORT_EMAIL]
    else:
        recipients = load_report_recipients_for_pawnshop(pid)
        if not recipients:
            logger.error("Нет email в report_recipients для ломбарда %s", pid)
            if raise_on_no_sent:
                sys.exit(1)
            return False
    by_dates = dates_by_tovaroved(rows, colnames)

    async def _run_all_picks() -> bool:
        sent = False
        for kod_tov, shift_date, fio in picks:
            candidates = candidate_shift_dates_for_kod(
                kod_tov, shift_date, by_dates, max_dates=DB_SHIFT_DATE_ATTEMPTS
            )
            paths, used_date, artifacts, period_str = await _try_candidates_for_tovaroved(
                pid, shift_date, candidates, work_dir, fio, keep_video_chunks=keep_video_chunks
            )
            if not paths or used_date is None:
                logger.warning(
                    "Пропуск %s: исчерпаны дни (%s), изначально выбран %s",
                    fio,
                    ", ".join(d.isoformat() for d in candidates),
                    shift_date.isoformat(),
                )
                continue
            ds = used_date.strftime("%d.%m.%Y")
            subject = f"Анализ работы смены за {ds} — {fio} — ломбард №{pid}"
            body = _email_body_db_shift(fio, pid, month_label, used_date, period_str, address=address)
            if send_report_email(subject, body, paths, recipients=recipients):
                sent = True
                logger.info("Отправлено: %s (%s)", fio, ds)
                if artifacts:
                    _delete_artifacts_after_send(artifacts)
            else:
                logger.error("Не удалось отправить письмо: %s", fio)
        return sent

    sent_any = asyncio.run(_run_all_picks())

    if not sent_any:
        logger.warning("Ни одного отправленного письма")
        if raise_on_no_sent:
            sys.exit(1)
        return False
    return True


def main() -> None:
    print("pawnbroker_analyzer_auto: старт CLI", flush=True)
    parser = argparse.ArgumentParser(
        description="Свод БД по сменам товароведов: --db-shifts-test | --db-shifts-prod-monthly | без аргументов — планировщик 1-го числа",
    )
    parser.add_argument(
        "--db-shifts-test",
        action="store_true",
        help="Тест: один ломбард, месяц current|previous, письма только на TEST_REPORT_EMAIL",
    )
    parser.add_argument(
        "--db-test-pawnshop",
        type=str,
        default=None,
        metavar="ID",
        help="Номер ломбарда как в camera_mapping (039, 626)",
    )
    parser.add_argument(
        "--db-test-month",
        type=str,
        choices=["current", "previous"],
        default="previous",
        help="Календарный месяц (опорная дата: DB_MONTH_CALENDAR_TZ / SCHEDULER_TZ)",
    )
    parser.add_argument(
        "--db-shifts-prod-monthly",
        action="store_true",
        help="Боевой прогон вручную: все ломбарды из camera_mapping по очереди, прошлый месяц",
    )
    parser.add_argument(
        "--db-shifts-prod-single",
        action="store_true",
        help="Боевой прогон одного ломбарда: письма на реальные report_recipients из camera_mapping",
    )
    parser.add_argument(
        "--db-only-first-tovaroved",
        action="store_true",
        help="Только один товаровед (мин. ФИО после случайного выбора смены)",
    )
    parser.add_argument(
        "--db-random-one-tovaroved",
        action="store_true",
        help="Только один случайный товаровед из списка случайных смен по всем товароведам точки",
    )
    parser.add_argument(
        "--db-test-date",
        type=str,
        metavar="YYYY-MM-DD",
        help="Зафиксировать конкретную дату смены вместо случайной (для воспроизводимых тестов)",
    )
    parser.add_argument(
        "--keep-video-chunks",
        action="store_true",
        help="Сохранить часовые MP4-чанки смены в audio_cache/kept_chunks/<ломбард>/<дата>",
    )
    args = parser.parse_args()

    if args.db_only_first_tovaroved and args.db_random_one_tovaroved:
        parser.error("Выберите один режим одного товароведа: --db-only-first-tovaroved или --db-random-one-tovaroved")

    if args.db_shifts_test:
        if args.db_shifts_prod_monthly or args.db_shifts_prod_single:
            parser.error("Выберите один режим: --db-shifts-test, --db-shifts-prod-single или --db-shifts-prod-monthly")
        if not args.db_test_pawnshop:
            parser.error("--db-shifts-test требует --db-test-pawnshop")
        run_db_shifts_test_single(
            args.db_test_pawnshop,
            args.db_test_month,
            only_first_tovaroved=args.db_only_first_tovaroved,
            random_one_tovaroved=args.db_random_one_tovaroved,
            force_date=args.db_test_date,
            keep_video_chunks=args.keep_video_chunks,
        )
        return

    if args.db_shifts_prod_single:
        if args.db_shifts_prod_monthly:
            parser.error("Выберите один режим: --db-shifts-prod-single или --db-shifts-prod-monthly")
        if not args.db_test_pawnshop:
            parser.error("--db-shifts-prod-single требует --db-test-pawnshop")
        run_db_shifts_prod_single(
            args.db_test_pawnshop,
            args.db_test_month,
            only_first_tovaroved=args.db_only_first_tovaroved,
            random_one_tovaroved=args.db_random_one_tovaroved,
            force_date=args.db_test_date,
            keep_video_chunks=args.keep_video_chunks,
        )
        return

    if args.db_shifts_prod_monthly:
        run_db_monthly_production()
        return

    from apscheduler.schedulers.blocking import BlockingScheduler

    from .config import (
        SCHEDULER_DB_MONTHLY_HOUR,
        SCHEDULER_DB_MONTHLY_MINUTE,
        SCHEDULER_TZ,
        load_all_camera_mapping_db_entries,
    )

    hour = int(SCHEDULER_DB_MONTHLY_HOUR) if SCHEDULER_DB_MONTHLY_HOUR is not None else 0
    minute = int(SCHEDULER_DB_MONTHLY_MINUTE) if SCHEDULER_DB_MONTHLY_MINUTE is not None else 0
    scheduler = BlockingScheduler(timezone=SCHEDULER_TZ)
    scheduler.add_job(
        _scheduled_db_monthly_job,
        "cron",
        day=1,
        hour=hour,
        minute=minute,
        id="db_monthly_shifts",
    )
    nloc = len(load_all_camera_mapping_db_entries())
    logger.info(
        "Планировщик: 1-е число %02d:%02d (%s), свод БД за предыдущий месяц, ломбардов: %s",
        hour,
        minute,
        SCHEDULER_TZ,
        nloc,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
