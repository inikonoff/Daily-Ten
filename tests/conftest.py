"""
Общая настройка тестов. Настройки читаются при импорте src.config, поэтому
переменные окружения задаём до любых импортов проекта.

Интеграционные тесты (tests/test_flow.py) идут на реальный Postgres и
запускаются только при заданной TEST_DATABASE_URL. Имя базы должно содержать
"test": тесты очищают таблицы.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:test")
os.environ.setdefault("GROQ_API_KEYS", "test-key")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or "postgresql://test@localhost/unused_test"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
