"""Поиск папки с этикетками по короткому имени из сообщения («br 29» -> ~/Desktop/BR 29)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOTS = [Path.home() / "Desktop"]


def get_roots() -> list[Path]:
    env = os.environ.get("CZ_LABEL_ROOTS")
    if env:
        roots = [Path(p).expanduser() for p in env.split(":") if p.strip()]
        return [r for r in roots if r.is_dir()]
    return [r for r in DEFAULT_ROOTS if r.is_dir()]


def normalize(name: str) -> str:
    return re.sub(r"[^0-9A-ZА-Я]+", "", name.upper())


@dataclass
class FolderMatch:
    path: Path
    pdf_count: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ResolveResult:
    status: str          # "ok" | "ambiguous" | "not_found" | "no_query"
    match: Path | None = None
    candidates: list[FolderMatch] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.candidates is None:
            self.candidates = []


def _count_pdfs(p: Path) -> int:
    try:
        return sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
    except OSError:
        return 0


def _all_dirs() -> list[Path]:
    dirs: list[Path] = []
    for root in get_roots():
        try:
            dirs.extend(d for d in root.iterdir() if d.is_dir())
        except OSError:
            continue
    return dirs


def resolve_folder(query: str | None) -> ResolveResult:
    if not query:
        return ResolveResult(status="no_query")

    # Явный путь
    p = Path(query).expanduser()
    if p.is_dir():
        return ResolveResult(status="ok", match=p)

    q = normalize(query)
    if not q:
        return ResolveResult(status="no_query")

    dirs = _all_dirs()
    exact = [d for d in dirs if normalize(d.name) == q]
    prefix = [d for d in dirs if normalize(d.name).startswith(q)]
    substr = [d for d in dirs if q in normalize(d.name)]

    for group in (exact, prefix, substr):
        if not group:
            continue
        matches = sorted(
            (FolderMatch(d, _count_pdfs(d)) for d in group),
            key=lambda m: (-m.pdf_count, m.name),
        )
        if len(matches) == 1:
            return ResolveResult(status="ok", match=matches[0].path, candidates=matches)
        with_pdfs = [m for m in matches if m.pdf_count > 0]
        if len(with_pdfs) == 1:
            return ResolveResult(status="ok", match=with_pdfs[0].path, candidates=matches)
        return ResolveResult(status="ambiguous", candidates=(with_pdfs or matches)[:10])

    return ResolveResult(status="not_found")
