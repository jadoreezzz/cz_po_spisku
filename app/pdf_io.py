"""Чтение PDF-файлов с повторными попытками.

В папках, синхронизируемых через iCloud/облако, периодически возникает
`OSError: [Errno 35] Resource deadlock avoided` — это блокировка на стороне ОС,
а не повреждение данных, поэтому попытку надо повторить.
"""
from __future__ import annotations

import io
import time
from pathlib import Path

from pypdf import PdfReader


class PdfUnreadable(Exception):
    """Файл не удалось прочитать после всех повторов."""

    def __init__(self, path: str, original: Exception):
        super().__init__(f"{path}: {original}")
        self.path = path
        self.original = original


def read_bytes_with_retry(path: str | Path, retries: int = 6, delay: float = 1.0) -> io.BytesIO:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with open(path, "rb") as fh:
                return io.BytesIO(fh.read())
        except OSError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(delay)
    raise PdfUnreadable(str(path), last_err)  # type: ignore[arg-type]


def read_pdf_with_retry(path: str | Path, retries: int = 6, delay: float = 1.0) -> PdfReader:
    """PdfReader поверх байтов файла — файл не остаётся открытым после чтения."""
    buf = read_bytes_with_retry(path, retries=retries, delay=delay)
    try:
        return PdfReader(buf)
    except Exception as e:  # битый/недокачанный файл — тоже «нечитаемый», но не «не найден»
        raise PdfUnreadable(str(path), e)
