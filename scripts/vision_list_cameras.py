"""
Печать проектов и камер из Интерсвязь API.

Запуск:
  python scripts/vision_list_cameras.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.intersvyaz_archive import get_cameras, get_projects, get_token  # noqa: E402


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip().strip('"').strip("'")
    if not value:
        raise RuntimeError(f"Не задано {name} в .env")
    return value


def main() -> None:
    from app import config as _config  # noqa: F401 — загрузка .env

    user = _require_env("INTERSVYAZ_USERNAME")
    pwd = _require_env("INTERSVYAZ_PASSWORD")

    token = get_token(user, pwd)
    projects = get_projects(token)

    print(f"Проектов: {len(projects)}")
    for p in projects:
        pid = p.get("id")
        pname = p.get("name") or p.get("title") or "(без названия)"
        print(f"\n[{pid}] {pname}")
        try:
            cameras = get_cameras(token, pid)
        except Exception as e:
            print(f"  Ошибка получения камер: {e}")
            continue
        print(f"  Камер: {len(cameras)}")
        for c in cameras:
            cid = c.get("id")
            cname = c.get("name") or c.get("title") or c.get("cameraName") or "(без названия)"
            print(f"    - {cid}: {cname}")


if __name__ == "__main__":
    main()
