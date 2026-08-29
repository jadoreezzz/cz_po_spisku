"""Хранилище пулов этикеток бота: ~/.cz_bot_pools/<chat_id>/"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from pool import Pool, build_pool, load_pool

# На сервере (Railway) путь задаётся переменной CZ_POOL_ROOT и должен указывать
# на подключённый том, иначе пулы пропадут при следующем деплое.
ROOT = Path(os.environ.get("CZ_POOL_ROOT") or Path.home() / ".cz_bot_pools")
STATE = ROOT / "state.json"


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-zА-Яа-яёЁ _-]+", "", name).strip()
    return (s or datetime.now().strftime("pool-%d.%m-%H%M"))[:60]


def chat_dir(chat_id: int) -> Path:
    d = ROOT / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def inbox_dir(chat_id: int) -> Path:
    d = chat_dir(chat_id) / "_inbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def out_dir(chat_id: int) -> Path:
    d = chat_dir(chat_id) / "_out"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------- активный пул (state)

def _state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def _save_state(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_active(chat_id: int, pool_path: Path) -> None:
    data = _state()
    data[str(chat_id)] = str(pool_path)
    _save_state(data)


def get_active(chat_id: int) -> Pool | None:
    path = _state().get(str(chat_id))
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return load_pool(p)


def list_pools(chat_id: int) -> list[Path]:
    d = chat_dir(chat_id)
    return sorted(
        (f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )


def find_pool(chat_id: int, name: str) -> Path | None:
    want = _slug(name).lower().replace(" ", "")
    for p in list_pools(chat_id):
        if p.stem.lower().replace(" ", "") == want:
            return p
    for p in list_pools(chat_id):
        if want in p.stem.lower().replace(" ", ""):
            return p
    return None


# ------------------------------------------------------------ создание пулов

def save_pool(chat_id: int, sources: list[tuple[str, Path]], name: str) -> tuple[Pool, list[str]]:
    target = chat_dir(chat_id) / f"{_slug(name)}.pdf"
    pool, unreadable = build_pool(sources, target, _slug(name))
    set_active(chat_id, pool.path)
    return pool, unreadable


def inbox_files(chat_id: int) -> list[tuple[str, Path]]:
    d = inbox_dir(chat_id)
    files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
    return [(f.name, f) for f in files]


def clear_inbox(chat_id: int) -> None:
    shutil.rmtree(inbox_dir(chat_id), ignore_errors=True)
    inbox_dir(chat_id)


def adopt_pool_file(chat_id: int, src: Path, name: str) -> Pool | None:
    """Пользователь прислал ранее собранный пул — кладём его в хранилище и делаем активным."""
    pool = load_pool(src, name=_slug(name))
    if pool is None:
        return None
    target = chat_dir(chat_id) / f"{_slug(name)}.pdf"
    shutil.copyfile(src, target)
    saved = load_pool(target, name=_slug(name))
    if saved is None:
        return None
    set_active(chat_id, target)
    return saved
