#!/usr/bin/env python3
"""PostgreSQL: просмотр таблиц / SQL и свод «товаровед × день» за месяц (даты и ФИО; опционально название точки)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import app.config  # noqa: F401 — загружает .env проекта

from app.db_pg import get_db_connection as _db_connect


def get_db_connection():
    """Подключение к PostgreSQL (DB_* через app)."""
    print(
        f"Подключение к PostgreSQL {os.getenv('DB_HOST', 'localhost')}:"
        f"{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'postgres')}..."
    )
    try:
        return _db_connect()
    except Exception as e:
        print(f"\n[ОШИБКА] Подключение: {e}")
        sys.exit(1)


def list_tables() -> None:
    conn = get_db_connection()
    print("[OK] Подключение установлено\n")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
        """
    )
    tables = cur.fetchall()
    print(f"Таблиц: {len(tables)}\n")
    for t in tables:
        print(f"  - {t[0]}")
    cur.close()
    conn.close()


def _schema_table(name: str) -> tuple[str, str]:
    if "." in name:
        a, b = name.split(".", 1)
        return a, b
    return "public", name


def show_table_structure(table_name: str) -> None:
    conn = get_db_connection()
    print("[OK] Подключение установлено\n")
    schema, table = _schema_table(table_name)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    columns = cur.fetchall()
    print(f"Структура '{table_name}':\n")
    print(f"{'Колонка':<30} {'Тип':<20} {'NULL':<10} {'По умолчанию'}")
    print("-" * 80)
    for col_name, data_type, is_nullable, col_default in columns:
        print(f"{col_name:<30} {data_type:<20} {is_nullable:<10} {str(col_default) or ''}")
    cur.close()
    conn.close()


def show_table_data(table_name: str, limit: int = 100, columns_filter: list | None = None) -> None:
    conn = get_db_connection()
    print("[OK] Подключение установлено\n")
    schema, table = _schema_table(table_name)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    all_columns = [row[0] for row in cur.fetchall()]
    if not all_columns:
        print(f"[ОШИБКА] Таблица '{table_name}' не найдена")
        cur.close()
        conn.close()
        return
    if columns_filter:
        columns = [c for c in columns_filter if c in all_columns]
        for c in columns_filter:
            if c not in all_columns:
                print(f"[ПРЕДУПРЕЖДЕНИЕ] Колонка '{c}' не найдена")
        if not columns:
            print("[ОШИБКА] Ни одна из указанных колонок не найдена")
            cur.close()
            conn.close()
            return
    else:
        columns = all_columns
    cols_quoted = ", ".join(f'"{c}"' for c in columns)
    query = f'SELECT {cols_quoted} FROM "{schema}"."{table}" LIMIT %s'
    cur.execute(query, (limit,))
    rows = cur.fetchall()
    print(f"Данные '{table_name}' (строк: {len(rows)}, колонок: {len(columns)}):\n")
    print(" | ".join(columns))
    print("-" * (sum(len(str(c)) for c in columns) + len(columns) * 3))
    for row in rows:
        print(" | ".join(str(val) if val is not None else "NULL" for val in row))
    cur.close()
    conn.close()


def execute_query(query: str) -> None:
    conn = get_db_connection()
    print("[OK] Подключение установлено\n")
    cur = conn.cursor()
    try:
        cur.execute(query)
        if cur.description:
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            print(f"Строк: {len(rows)}\n")
            print(" | ".join(columns))
            print("-" * (sum(len(str(c)) for c in columns) + len(columns) * 3))
            for row in rows:
                print(" | ".join(str(v) if v is not None else "NULL" for v in row))
        else:
            conn.commit()
            print(f"Выполнено. Затронуто строк: {cur.rowcount}")
    except Exception as e:
        print(f"ОШИБКА: {e}")
        conn.rollback()
    cur.close()
    conn.close()


def test_connection() -> bool:
    print("Параметры (пароль скрыт):")
    print(
        f"  {os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}  "
        f"БД={os.getenv('DB_NAME', 'postgres')}  Пользователь={os.getenv('DB_USER', 'postgres')}"
    )
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT version(), current_database(), current_user, now()")
        ver, db, user, now = cur.fetchone()
        print("[OK] Подключение установлено")
        print(f"  База: {db}, пользователь: {user}")
        print(f"  Время сервера: {now}")
        print(f"  Версия: {ver[:80]}...")
        cur.close()
        conn.close()
        return True
    except SystemExit:
        return False


def _parse_kod_podrazdeleniya(s: str) -> str:
    t = (s or "").strip()
    if not t:
        raise SystemExit(f"--kod-podrazdeleniya: пустое значение: {s!r}")
    allowed = set("0123456789.vV")
    if any(ch not in allowed for ch in t):
        raise SystemExit(
            f"--kod-podrazdeleniya: ожидается код вида 000000001 / 0000276.1 / 00000039v, получено: {s!r}"
        )
    return t


def analyze_tovaroved_month_one_pawnshop(
    kod_podrazdeleniya: str,
    month_yyyy_mm: str | None,
    ref_today: date | None = None,
    *,
    default_month: str = "prev",
    with_branch_name: bool = False,
) -> None:
    """Свод за месяц: по умолчанию только даты и ФИО (быстро). С --with-branch-name — + название точки (медленнее)."""
    from app.calendar_month import previous_month_y_m
    from app.tovaroved_svod import fetch_month_shifts, month_bounds, parse_month_yyyy_mm

    ref = ref_today or date.today()
    if month_yyyy_mm:
        try:
            y, m = parse_month_yyyy_mm(month_yyyy_mm)
        except ValueError as e:
            raise SystemExit(str(e)) from e
    else:
        if default_month == "current":
            y, m = ref.year, ref.month
        else:
            y, m = previous_month_y_m(ref)

    d0, d1 = month_bounds(y, m)
    kod_sql = _parse_kod_podrazdeleniya(kod_podrazdeleniya)

    conn = get_db_connection()
    print("[OK] Подключение установлено\n")
    cur = conn.cursor()
    try:
        rows, colnames = fetch_month_shifts(
            cur, kod_sql, d0, d1, include_branch_name=with_branch_name
        )
    finally:
        cur.close()
        conn.close()

    print("=" * 100)
    mode = "даты и ФИО" if not with_branch_name else "даты, ФИО и наименование точки (ТУ/справочник)"
    print(f"Смены (товаровед × день) — {mode}")
    print(f"  KodPodrazdeleniya = {kod_sql}")
    print(f"  Период: {d0.isoformat()} … {d1.isoformat()} ({y}-{m:02d})")
    if month_yyyy_mm is None:
        label = "текущий" if default_month == "current" else "предыдущий"
        print(f"  (месяц по умолчанию: {label} от даты «сегодня» {ref.isoformat()})")
    print("=" * 100)

    total = len(rows)
    print(f"Строк в своде за период: {total}\n")
    if total == 0:
        return

    print(" | ".join(colnames))
    print("-" * (sum(len(str(c)) for c in colnames) + len(colnames) * 3))
    for row in rows:
        print(" | ".join(str(v) if v is not None else "NULL" for v in row))
    print()


def main() -> None:
    p = argparse.ArgumentParser(
        description="PostgreSQL: --test, --list-tables, --table, --structure, --query, --analyze-tovaroved-month"
    )
    p.add_argument("--test", action="store_true", help="Проверить подключение")
    p.add_argument("--list-tables", action="store_true", help="Список таблиц public")
    p.add_argument("--table", type=str, help="Данные из таблицы")
    p.add_argument("--structure", type=str, help="Структура таблицы")
    p.add_argument("--query", type=str, help="Произвольный SQL")
    p.add_argument("--limit", type=int, default=100, help="Лимит строк")
    p.add_argument("--columns", type=str, help="Колонки через запятую")
    p.add_argument(
        "--analyze-tovaroved-month",
        action="store_true",
        help="Свод по одному KodPodrazdeleniya за месяц (даты и ФИО; см. --with-branch-name)",
    )
    p.add_argument(
        "--with-branch-name",
        action="store_true",
        help="С --analyze-tovaroved-month: добавить наименование точки (медленнее)",
    )
    p.add_argument(
        "--kod-podrazdeleniya",
        type=str,
        default=None,
        metavar="КОД",
        help="Код подразделения в БД (напр. 000000001)",
    )
    p.add_argument(
        "--month",
        type=str,
        default=None,
        dest="month_yyyy_mm",
        metavar="YYYY-MM",
        help="Месяц. Без параметра — предыдущий календарный месяц от --as-of или от сегодня",
    )
    p.add_argument(
        "--as-of",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Опорная дата для «предыдущего месяца» (если не задан --month)",
    )
    p.add_argument(
        "--current-month",
        action="store_true",
        help="Если не задан --month: текущий календарный месяц (вместо предыдущего)",
    )
    args = p.parse_args()

    if args.test:
        test_connection()
    elif args.analyze_tovaroved_month:
        if not args.kod_podrazdeleniya:
            p.error("Для --analyze-tovaroved-month укажите --kod-podrazdeleniya")
        ref: date | None = None
        if args.as_of:
            try:
                ref = datetime.strptime(args.as_of.strip(), "%Y-%m-%d").date()
            except ValueError:
                p.error("--as-of: формат YYYY-MM-DD")
        analyze_tovaroved_month_one_pawnshop(
            args.kod_podrazdeleniya,
            args.month_yyyy_mm,
            ref_today=ref,
            default_month=("current" if args.current_month else "prev"),
            with_branch_name=args.with_branch_name,
        )
    elif args.list_tables:
        list_tables()
    elif args.table:
        cols = [c.strip() for c in args.columns.split(",")] if args.columns else None
        show_table_data(args.table, args.limit, cols)
    elif args.structure:
        show_table_structure(args.structure)
    elif args.query:
        execute_query(args.query)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
