# Pawnbroker Analyzer Auto

Автоматический контур: смены из PostgreSQL → запись с камеры → транскрипт → LLM-отчёт → email.

## Stack

- Python 3, httpx, APScheduler, python-dotenv, OpenAI SDK (OpenRouter)
- PostgreSQL (смены товароведов)
- API видеонаблюдения (Интерсвязь / platform-vision)
- Внешний Audio Pipeline, FFmpeg
- OpenRouter: `anthropic/claude-sonnet-4.6` + fallback `google/gemini-3.1-pro-preview`
- SMTP, Docker

## Pipeline

1. Планировщик (1-го числа) или CLI выбирает смены.
2. Скачивание записи смены с камеры.
3. Audio Pipeline → 3 LLM-шага (как в ручном анализаторе).
4. Текстовые артефакты + email получателям.

## Run

```bash
pip install -r requirements.txt
# нужен camera_mapping.json (ломбард → камера/получатели) — не в репо
python -m app.cli                              # monthly scheduler
python -m app.cli --db-shifts-test ...         # тест одного ломбарда
python -m app.cli --db-shifts-prod-monthly     # боевой прогон
```

## Config

| Variable | Required | Notes |
|----------|----------|-------|
| `DB_HOST` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | yes | PostgreSQL |
| `INTERSVYAZ_USERNAME` / `INTERSVYAZ_PASSWORD` | yes | видеоархив |
| `OPENROUTER_API_KEY` | yes | |
| `AUDIO_PIPELINE_URL` | yes | |
| `SMTP_*` | yes | рассылка |
| `TEST_REPORT_EMAIL` | for test mode | |
| `PAWNSHOP_CAMERA_MAP` | optional | вместо файла |

## Notes

- `camera_mapping.json` и записи смен в репозиторий не входят.
- Утилиты: `scripts/pg_inspect.py`, `scripts/vision_list_cameras.py`.
