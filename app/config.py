import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Корень проекта
BASE_DIR = Path(__file__).parent.parent


def _load_dotenv() -> None:
    """Загружает .env в os.environ (setdefault)."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                os.environ.setdefault(key, value)


_load_dotenv()

AUDIO_CACHE_DIR = BASE_DIR / "audio_cache"
REPORTS_DIR = BASE_DIR / "reports"


def _read_camera_mapping_json() -> Optional[Union[dict, list]]:
    path = BASE_DIR / "camera_mapping.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(raw, (dict, list)):
        return raw
    return None


def _camera_mapping_list_dicts() -> List[dict]:
    data = _read_camera_mapping_json()
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _env(key: str, default: Any = None, cast: Any = lambda x: x) -> Any:
    """getenv + cast, при ошибке — default."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return cast(value)
    except (ValueError, TypeError):
        return default


# .env — только секреты. Остальное можно переопределить через .env.
# Audio Pipeline. Локально — дефолт ниже; Docker — AUDIO_PIPELINE_URL=http://audio-pipeline:8084
AUDIO_PIPELINE_URL = _env("AUDIO_PIPELINE_URL", "http://localhost:8089")
AUDIO_PIPELINE_POLL_INTERVAL = _env("AUDIO_PIPELINE_POLL_INTERVAL", 2.0, float)

# Записи: API Интерсвязь (platform-vision). Камеры: camera_mapping.json или PAWNSHOP_CAMERA_MAP
INTERSVYAZ_PROJECT_ID = _env("INTERSVYAZ_PROJECT_ID", 126, int)
INTERSVYAZ_USERNAME = os.getenv("INTERSVYAZ_USERNAME", "")
INTERSVYAZ_PASSWORD = os.getenv("INTERSVYAZ_PASSWORD", "")
# Повторных попыток скачать один часовой чанк Интерсвязь (при обрыве / временной ошибке).
INTERSVYAZ_CHUNK_MAX_ATTEMPTS = max(1, _env("INTERSVYAZ_CHUNK_MAX_ATTEMPTS", 3, int))


def _parse_camera_mapping_flat(data: dict) -> Dict[str, int]:
    """Только пары ломбард → camera_id; ключи с префиксом _ (служебные) пропускаются."""
    out: Dict[str, int] = {}
    for k, v in data.items():
        if str(k).startswith("_"):
            continue
        try:
            out[str(k).zfill(3) if str(k).isdigit() else str(k)] = int(v)
        except (ValueError, TypeError):
            continue
    return out


def _pawnshop_key_from_mapping_row(item: dict) -> Optional[str]:
    """Ключ ломбарда: pawnshop_id (строка CRM), иначе pawnshop_number."""
    pid = item.get("pawnshop_id")
    if pid is not None and str(pid).strip() != "":
        s = str(pid).strip()
        return s.zfill(3) if s.isdigit() else s
    pn = item.get("pawnshop_number")
    if pn is None:
        return None
    try:
        return str(int(pn)).zfill(3)
    except (TypeError, ValueError):
        return None


def _parse_camera_mapping_list(data: list) -> Dict[str, int]:
    """Массив точек: name, address, camera_id; ключ ломбарда — pawnshop_id или pawnshop_number."""
    out: Dict[str, int] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        key = _pawnshop_key_from_mapping_row(item)
        if key is None:
            continue
        try:
            out[key] = int(item["camera_id"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _parse_camera_mapping_file_payload(data: Union[dict, list]) -> Dict[str, int]:
    if isinstance(data, list):
        return _parse_camera_mapping_list(data)
    if isinstance(data, dict):
        return _parse_camera_mapping_flat(data)
    return {}


def _camera_mapping_db_rows() -> List[Dict[str, str]]:
    """Строки camera_mapping.json с полями ломбард + kod_podrazdeleniya (порядок как в файле)."""
    out: List[Dict[str, str]] = []
    for item in _camera_mapping_list_dicts():
        key = _pawnshop_key_from_mapping_row(item)
        kod = item.get("kod_podrazdeleniya")
        if not key or kod is None or str(kod).strip() == "":
            continue
        out.append({"pawnshop_id": key, "kod_podrazdeleniya": str(kod).strip()})
    return out


def load_all_camera_mapping_db_entries() -> List[Dict[str, str]]:
    """Все точки с kod_podrazdeleniya — для боевого месячного прогона по списку."""
    return _camera_mapping_db_rows()


def load_camera_mapping_entry_by_pawnshop(pawnshop_id: str) -> Optional[Dict[str, str]]:
    """Одна точка по номеру ломбарда (как в CRM / camera_mapping)."""
    key = pawnshop_id.zfill(3) if pawnshop_id.isdigit() else pawnshop_id
    for row in _camera_mapping_db_rows():
        if row["pawnshop_id"] == key:
            return row
    return None


def _find_camera_mapping_item(pawnshop_id: str) -> Optional[dict]:
    """Строка camera_mapping.json по номеру ломбарда (None если не найдена)."""
    key = pawnshop_id.zfill(3) if pawnshop_id.isdigit() else pawnshop_id
    for item in _camera_mapping_list_dicts():
        if _pawnshop_key_from_mapping_row(item) == key:
            return item
    return None


def load_pawnshop_address(pawnshop_id: str) -> Optional[str]:
    """Адрес точки из camera_mapping.json по ключу ломбарда (как в CRM, напр. 001)."""
    item = _find_camera_mapping_item(pawnshop_id)
    if item is None:
        return None
    a = item.get("address")
    return str(a).strip() if a else None


def _emails_from_report_recipients(item: dict) -> List[str]:
    raw = item.get("report_recipients")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        email = str(r.get("email", "")).strip()
        if email:
            out.append(email)
    return out


def load_report_recipients_for_pawnshop(pawnshop_id: str) -> List[str]:
    """Email из report_recipients строки camera_mapping для ломбарда."""
    item = _find_camera_mapping_item(pawnshop_id)
    if item is None:
        return []
    return _emails_from_report_recipients(item)


def load_pawnshop_camera_mapping() -> Dict[str, int]:
    """Ключ — id ломбарда (строка 039), значение — id камеры в проекте Интерсвязь."""
    raw = os.getenv("PAWNSHOP_CAMERA_MAP")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return _parse_camera_mapping_flat(data)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    data = _read_camera_mapping_json()
    if data is not None:
        try:
            return _parse_camera_mapping_file_payload(data)
        except (ValueError, TypeError):
            pass
    return {}

TEST_REPORT_EMAIL: str = _env("TEST_REPORT_EMAIL", "test@example.com")
SCHEDULER_TZ = _env("SCHEDULER_TZ", "Europe/Moscow")
# Текущий/пред. месяц для свода БД: при необходимости задать Asia/Yekaterinburg, если граница месяца должна быть по местному времени точки, а не по SCHEDULER_TZ.
DB_MONTH_CALENDAR_TZ = _env("DB_MONTH_CALENDAR_TZ", "").strip()


def get_db_month_calendar_tz() -> str:
    """IANA-зона для календарной даты при расчёте месяца свода БД; иначе SCHEDULER_TZ."""
    return DB_MONTH_CALENDAR_TZ or SCHEDULER_TZ


SCHEDULER_DB_MONTHLY_HOUR = _env("SCHEDULER_DB_MONTHLY_HOUR", "0")
SCHEDULER_DB_MONTHLY_MINUTE = _env("SCHEDULER_DB_MONTHLY_MINUTE", "0")
# run_db_shifts_analysis: сколько различных дней мес. у одного товароведа пробовать при полном провале скачивания/анализа.
DB_SHIFT_DATE_ATTEMPTS = max(1, _env("DB_SHIFT_DATE_ATTEMPTS", 3, int))


def load_pawnshop_shift_tz(pawnshop_id: str) -> str:
    """IANA-таймзона точки для смены 9–21 (поле shift_tz); иначе SCHEDULER_TZ."""
    item = _find_camera_mapping_item(pawnshop_id)
    if item is None:
        return SCHEDULER_TZ
    tz = item.get("shift_tz")
    if tz is not None and str(tz).strip():
        return str(tz).strip()
    return SCHEDULER_TZ


# LLM: OpenRouter (Claude primary + Gemini fallback)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = _env("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.6")
OPENROUTER_MODEL_LABEL = "Claude Sonnet 4.6"
OPENROUTER_FALLBACK_MODEL = _env("OPENROUTER_FALLBACK_MODEL", "google/gemini-3.1-pro-preview")
OPENROUTER_FALLBACK_MODEL_LABEL = "Gemini 3.1 Pro Preview"

# SMTP
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = _env("SMTP_PORT", 587, int)
SMTP_USER = _env("SMTP_USER", "smtp@example.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
