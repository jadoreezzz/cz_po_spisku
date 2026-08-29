"""Сборка итогового PDF в порядке списка артикулов, с курсором по страницам.

Источник этикеток — либо пул (единый PDF, присланный в бота), либо папка на диске.
Правила матчинга и курсора одни и те же для обоих источников.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfWriter

from matcher import find_with_fallbacks
from pdf_io import PdfUnreadable, read_pdf_with_retry


@dataclass
class BuildReport:
    total: int = 0
    found: int = 0
    missing_codes: list[str] = field(default_factory=list)
    unreadable_codes: list[str] = field(default_factory=list)  # файл найден, но не прочитался
    unreadable_files: list[str] = field(default_factory=list)
    short_pages: dict[str, tuple[int, int]] = field(default_factory=dict)  # code -> (нужно, есть)
    fallback_used: dict[str, str] = field(default_factory=dict)  # code -> method
    output_path: str | None = None


class FolderSource:
    """Этикетки лежат отдельными файлами в папке на диске."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self._unreadable: list[str] = []

    @property
    def filenames(self) -> list[str]:
        from pool import nfc
        return [
            nfc(f.name) for f in self.folder.iterdir()
            if f.is_file() and f.suffix.lower() == ".pdf"
        ]

    def pages_of(self, filename: str) -> list:
        try:
            reader = read_pdf_with_retry(self.folder / filename)
        except PdfUnreadable:
            self._unreadable.append(filename)
            return []
        self._keep = getattr(self, "_keep", [])
        self._keep.append(reader)  # держим ридер живым, пока страницы не записаны
        return list(reader.pages)

    @property
    def unreadable(self) -> list[str]:
        return self._unreadable


def build_from_source(codes: list[str], source, output_path: str | Path) -> BuildReport:
    files = source.filenames
    report = BuildReport(total=len(codes))
    writer = PdfWriter()

    cursor: dict[str, int] = defaultdict(int)
    pages_pool_cache: dict[str, list] = {}
    candidates_cache: dict[str, list[str]] = {}

    for code in codes:
        if code not in candidates_cache:
            candidates, method = find_with_fallbacks(code, files)
            candidates_cache[code] = candidates
            if candidates and method != "exact":
                report.fallback_used[code] = method

        candidates = candidates_cache[code]
        if not candidates:
            report.missing_codes.append(code)
            continue

        if code not in pages_pool_cache:
            pool = []
            for fn in sorted(candidates):
                pool.extend(source.pages_of(fn))
            pages_pool_cache[code] = pool

        pool = pages_pool_cache[code]
        if not pool:
            # файл нашёлся, но не прочитался — причина отличается от «не найден»
            report.unreadable_codes.append(code)
            continue

        idx = cursor[code]
        if idx >= len(pool):
            # страниц меньше, чем позиций в списке: дублируем последнюю, но это ОБЯЗАТЕЛЬНО
            # показать в отчёте — один и тот же код ЧЗ нельзя клеить на два товара
            report.short_pages[code] = (idx + 1, len(pool))
            idx = len(pool) - 1
        writer.add_page(pool[idx])
        cursor[code] += 1
        report.found += 1

    for fn in getattr(source, "unreadable", []):
        if fn not in report.unreadable_files:
            report.unreadable_files.append(fn)

    output_path = Path(output_path)
    with open(output_path, "wb") as f:
        writer.write(f)
    report.output_path = str(output_path)
    return report


def build_pdf(codes: list[str], folder: str | Path, output_path: str | Path) -> BuildReport:
    """Сборка из папки на диске (используется десктопным GUI)."""
    return build_from_source(codes, FolderSource(folder), output_path)


def make_output_name(marketplace: str, label: str) -> str:
    safe_label = "".join(c for c in label if c.isalnum() or c in ("-", "_")) or "list"
    return f"{marketplace}_{safe_label}_CZ_po_spisku.pdf"
