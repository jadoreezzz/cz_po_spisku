"""Хранение настроек между запусками (последняя папка и т.п.)."""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".cz_pdf_builder_config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(data: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
