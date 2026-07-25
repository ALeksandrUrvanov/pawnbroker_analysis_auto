"""Свод товаровед × день по SD_Dogovora ∪ SD_Oplaty_po_Dogovoram (не онлайн)."""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
import random
from typing import Any, Optional


def fio_sql_expr(cur) -> str:
    """Выражение для ФИО из SD_FizicheskieLitsa (по фактическим колонкам)."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'SD_FizicheskieLitsa'
        """
    )
    cols = {r[0] for r in cur.fetchall()}
    if "FIO" in cols:
        return 'fl."FIO"'
    if "Familiya" in cols and "Imya" in cols:
        if "Otchestvo" in cols:
            return (
                "TRIM(CONCAT_WS(' ', NULLIF(TRIM(fl.\"Familiya\"), ''), "
                "NULLIF(TRIM(fl.\"Imya\"), ''), NULLIF(TRIM(fl.\"Otchestvo\"), '')))"
            )
        return (
            "TRIM(CONCAT_WS(' ', NULLIF(TRIM(fl.\"Familiya\"), ''), NULLIF(TRIM(fl.\"Imya\"), '')))"
        )
    for n in ("Naimenovanie", "Fio", "FI"):
        if n in cols:
            return f'fl."{n}"'
    return "NULL::text"


def podrazdelenie_name_sql_expr(cur) -> str:
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'SD_Podrazdeleniya'
        """
    )
    cols = {r[0] for r in cur.fetchall()}
    for n in ("Naimenovanie", "PodrazdelenieNaimenovanie", "Nazvanie"):
        if n in cols:
            return f'pod."{n}"'
    return "NULL::text"


def onlayn_offline_sql(table_alias: str) -> str:
    """Фильтр «не онлайн»: Onlayn == False (boolean: f/t; NULL считаем офлайн)."""
    a = table_alias
    return (
        f"(LOWER(TRIM(COALESCE({a}.\"Onlayn\"::text, 'false'))) IN ('f', 'false', '0'))"
    )


def kod_podrazdeleniya_match_sql(a: str, b: str) -> str:
    """Совпадение кода подразделения: строка или число без ведущих нулей."""
    return f"""(
                TRIM(BOTH FROM {a}) = TRIM(BOTH FROM {b})
                OR (
                    TRIM(BOTH FROM {a}) ~ '^[0-9]+$'
                    AND TRIM(BOTH FROM {b}) ~ '^[0-9]+$'
                    AND TRIM(BOTH FROM {a})::bigint = TRIM(BOTH FROM {b})::bigint
                )
            )"""


def tovaroved_enriched_cte(
    fio_x: str,
    pod_x: str,
    off_d: str,
    off_o: str,
    *,
    kod_podrazdeleniya_eq: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    include_branch_name: bool = False,
) -> str:
    extra_d = ""
    extra_o = ""
    if kod_podrazdeleniya_eq is not None:
        extra_d = f' AND TRIM(d."KodPodrazdeleniya"::text) = \'{kod_podrazdeleniya_eq}\''
        extra_o = f' AND TRIM(o."KodPodrazdeleniyaOplaty"::text) = \'{kod_podrazdeleniya_eq}\''
    if date_from is not None and date_to is not None:
        extra_d += f' AND (d."DataVremya"::date) BETWEEN \'{date_from.isoformat()}\'::date AND \'{date_to.isoformat()}\'::date'
        extra_o += f' AND (o."DataVremyaPlatezha"::date) BETWEEN \'{date_from.isoformat()}\'::date AND \'{date_to.isoformat()}\'::date'

    base = f"""
        dogov AS (
            SELECT DISTINCT
                d."KodTovaroveda" AS kod_tovaroveda,
                d."KodPodrazdeleniya" AS kod_podrazdeleniya,
                (d."DataVremya"::date) AS den_operatsii
            FROM public."SD_Dogovora" d
            WHERE {off_d}
              AND d."KodTovaroveda" IS NOT NULL
              {extra_d}
        ),
        oplaty AS (
            SELECT DISTINCT
                o."KodTovarovedaPrinyavshegoOplatu" AS kod_tovaroveda,
                o."KodPodrazdeleniyaOplaty" AS kod_podrazdeleniya,
                (o."DataVremyaPlatezha"::date) AS den_operatsii
            FROM public."SD_Oplaty_po_Dogovoram" o
            WHERE {off_o}
              AND o."KodTovarovedaPrinyavshegoOplatu" IS NOT NULL
              {extra_o}
        ),
        combined AS (
            SELECT * FROM dogov
            UNION
            SELECT * FROM oplaty
        ),"""

    if not include_branch_name:
        return base + f"""
        enriched AS (
            SELECT DISTINCT
                c.kod_tovaroveda,
                c.kod_podrazdeleniya,
                c.den_operatsii,
                ({fio_x}) AS fio_tovaroveda
            FROM combined c
            LEFT JOIN public."SD_FizicheskieLitsa" fl ON fl."SysID" = c.kod_tovaroveda
        )
    """

    return base + f"""
        enriched AS (
            SELECT DISTINCT
                c.kod_tovaroveda,
                c.kod_podrazdeleniya,
                c.den_operatsii,
                ({fio_x}) AS fio_tovaroveda,
                COALESCE(
                    ({pod_x}),
                    (
                        SELECT t."PodrazdelenieNaimenovanie"
                        FROM public."TerritorialnyeUpravlyayushchie" t
                        WHERE {kod_podrazdeleniya_match_sql('t."PodrazdelenieKod"::text', 'c.kod_podrazdeleniya::text')}
                        ORDER BY t."ParametrDataZaprosa" DESC NULLS LAST
                        LIMIT 1
                    )
                ) AS naimenovanie_podrazdeleniya
            FROM combined c
            LEFT JOIN public."SD_FizicheskieLitsa" fl ON fl."SysID" = c.kod_tovaroveda
            LEFT JOIN public."SD_Podrazdeleniya" pod ON {kod_podrazdeleniya_match_sql('pod."KodPodrazdeleniya"::text', 'c.kod_podrazdeleniya::text')}
        )
    """


def month_bounds(y: int, m: int) -> tuple[date, date]:
    last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


def parse_month_yyyy_mm(s: str) -> tuple[int, int]:
    try:
        y, mo = (int(x) for x in s.strip().split("-", 1))
        if not (1 <= mo <= 12):
            raise ValueError
        return y, mo
    except ValueError as e:
        raise ValueError(f"month: нужен формат YYYY-MM, получено: {s!r}") from e


def fetch_month_shifts(
    cur,
    kod_podrazdeleniya_eq: str,
    date_from: date,
    date_to: date,
    *,
    include_branch_name: bool,
) -> tuple[list[tuple[Any, ...]], list[str]]:
    fio_x = fio_sql_expr(cur)
    pod_x = podrazdelenie_name_sql_expr(cur) if include_branch_name else ""
    off_d = onlayn_offline_sql("d")
    off_o = onlayn_offline_sql("o")
    cte = tovaroved_enriched_cte(
        fio_x,
        pod_x,
        off_d,
        off_o,
        kod_podrazdeleniya_eq=kod_podrazdeleniya_eq,
        date_from=date_from,
        date_to=date_to,
        include_branch_name=include_branch_name,
    )
    cur.execute(
        f"""
        WITH {cte}
        SELECT * FROM enriched
        ORDER BY den_operatsii ASC, kod_tovaroveda
        """
    )
    rows = cur.fetchall()
    colnames = [d[0] for d in cur.description] if cur.description else []
    return rows, colnames


def pick_random_shift_per_tovaroved(
    rows: list[tuple[Any, ...]], colnames: list[str]
) -> list[tuple[str, date, str]]:
    """По каждому уникальному kod_tovaroveda — одна случайная дата смены и ФИО."""
    try:
        i_kod = colnames.index("kod_tovaroveda")
        i_day = colnames.index("den_operatsii")
        i_fio = colnames.index("fio_tovaroveda")
    except ValueError as e:
        raise ValueError("ожидаются колонки kod_tovaroveda, den_operatsii, fio_tovaroveda") from e
    by_kod: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for row in rows:
        kod = str(row[i_kod]).strip()
        day = row[i_day]
        fio = row[i_fio] if row[i_fio] is not None else ""
        if not isinstance(day, date):
            continue
        by_kod[kod].append((day, str(fio).strip()))
    out: list[tuple[str, date, str]] = []
    for kod, pairs in by_kod.items():
        d, fio = random.choice(pairs)
        out.append((kod, d, fio))
    out.sort(key=lambda x: x[1])
    return out


def dates_by_tovaroved(
    rows: list[tuple[Any, ...]], colnames: list[str]
) -> dict[str, list[date]]:
    """Все календарные дни смен по каждому kod_tovaroveda (уникальные, по возрастанию)."""
    try:
        i_kod = colnames.index("kod_tovaroveda")
        i_day = colnames.index("den_operatsii")
    except ValueError as e:
        raise ValueError("ожидаются колонки kod_tovaroveda, den_operatsii") from e
    by_kod: dict[str, set[date]] = defaultdict(set)
    for row in rows:
        kod = str(row[i_kod]).strip()
        day = row[i_day]
        if isinstance(day, date):
            by_kod[kod].add(day)
    return {k: sorted(v) for k, v in by_kod.items()}


def candidate_shift_dates_for_kod(
    kod: str,
    primary: date,
    by_dates: dict[str, list[date]],
    *,
    max_dates: int,
) -> list[date]:
    """
    Порядок обработки: primary, затем до max_dates−1 других случайных дней того же товароведа.
    """
    pool = list(by_dates.get(kod, []))
    max_dates = max(1, int(max_dates))
    if not pool:
        return [primary]
    if primary not in pool:
        alt = list(pool)
        random.shuffle(alt)
        return alt[:max_dates]
    others = [d for d in pool if d != primary]
    random.shuffle(others)
    out = [primary]
    for d in others:
        if len(out) >= max_dates:
            break
        out.append(d)
    return out
