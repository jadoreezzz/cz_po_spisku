"""Пул этикеток: один PDF со всеми ЧЗ-этикетками + индекс «страница -> исходный файл».

Этикетки ЧЗ — картинки, текста в PDF нет, поэтому из «просто склеенного» PDF
невозможно понять, где какой артикул. Индекс зашивается внутрь самого файла
(метаданные + закладки), так что пул самодостаточен: его можно скачать,
хранить и прислать боту снова — папки на диске больше не нужны.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from pdf_io import PdfUnreadable, read_pdf_with_retry

META_KEY = "/CZIndex"
RESULT_MARKER = "_CZ_po_spisku"  # файлы, собранные самим ботом, — не этикетки


def nfc(name: str) -> str:
    """macOS хранит имена в NFD, архивы обычно в NFC — приводим к одному виду,
    иначе одинаковые на вид имена оказываются разными строками."""
    return unicodedata.normalize("NFC", name)


INDEX_VERSION = 1


@dataclass
class Pool:
    """Пул этикеток: имя, путь к единому PDF, имя исходного файла для каждой страницы."""

    name: str
    path: Path
    page_files: list[str] = field(default_factory=list)
    _reader: PdfReader | None = None

    @property
    def filenames(self) -> list[str]:
        seen: dict[str, None] = {}
        for f in self.page_files:
            seen.setdefault(f, None)
        return list(seen)

    @property
    def page_count(self) -> int:
        return len(self.page_files)

    def reader(self) -> PdfReader:
        if self._reader is None:
            self._reader = read_pdf_with_retry(self.path)
        return self._reader

    def pages_of(self, filename: str) -> list:
        pages = self.reader().pages
        return [pages[i] for i, f in enumerate(self.page_files) if f == filename]


# ------------------------------------------------------------------ создание

def _index_payload(page_files: list[str]) -> str:
    uniq: list[str] = []
    pos: dict[str, int] = {}
    for f in page_files:
        if f not in pos:
            pos[f] = len(uniq)
            uniq.append(f)
    return json.dumps(
        {"v": INDEX_VERSION, "files": uniq, "pages": [pos[f] for f in page_files]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_pool(sources: list[tuple[str, Path]], out_path: Path, name: str) -> tuple[Pool, list[str]]:
    """sources — список (имя_файла_этикетки, путь к нему). Возвращает (пул, нечитаемые файлы)."""
    writer = PdfWriter()
    page_files: list[str] = []
    unreadable: list[str] = []
    readers = []

    for filename, path in sources:
        try:
            reader = read_pdf_with_retry(path)
        except PdfUnreadable:
            unreadable.append(filename)
            continue
        readers.append(reader)
        start = len(page_files)
        for page in reader.pages:
            writer.add_page(page)
            page_files.append(filename)
        if len(page_files) > start:
            writer.add_outline_item(filename, start)  # закладка = человекочитаемый индекс

    writer.add_metadata({META_KEY: _index_payload(page_files), "/Title": f"CZ pool: {name}"})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return Pool(name=name, path=out_path, page_files=page_files), unreadable


def collect_from_zip(zip_path: Path, dest_dir: Path) -> list[tuple[str, Path]]:
    """Распаковать PDF-этикетки из архива (включая вложенные папки), сохранив имена файлов."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, Path]] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw = info.filename
            if not raw.lower().endswith(".pdf"):
                continue
            if "__MACOSX" in raw or Path(raw).name.startswith("._"):
                continue
            if RESULT_MARKER in Path(raw).name:  # ранее собранный результат попал в архив
                continue
            name = _decode_zip_name(raw, info)
            base = nfc(Path(name).name)
            target = dest_dir / base
            n = 1
            while target.exists():  # одинаковые имена в разных подпапках архива
                target = dest_dir / nfc(f"{Path(base).stem}~{n}{Path(base).suffix}")
                n += 1
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append((base, target))
    out.sort(key=lambda p: p[0])
    return out


def _decode_zip_name(name: str, info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:  # имя уже в UTF-8
        return name
    # zipfile декодировал байты как cp437; на практике архиваторы кладут UTF-8
    # даже без флага, поэтому UTF-8 пробуем первым — иначе получаются кракозябры.
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return name.encode("cp437").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return name


# ------------------------------------------------------------------ чтение

def load_pool(path: Path, name: str | None = None) -> Pool | None:
    """Прочитать пул из ранее собранного PDF. None — если индекса внутри нет."""
    try:
        reader = read_pdf_with_retry(path)
    except PdfUnreadable:
        return None

    payload = None
    meta = reader.metadata or {}
    if META_KEY in meta:
        payload = str(meta[META_KEY])
    else:
        sidecar = path.with_suffix(".index.json")
        if sidecar.exists():
            payload = sidecar.read_text(encoding="utf-8")
    if not payload:
        return None

    try:
        data = json.loads(payload)
        files = data["files"]
        page_files = [files[i] for i in data["pages"]]
    except (ValueError, KeyError, IndexError, TypeError):
        return None

    if len(page_files) != len(reader.pages):
        return None  # индекс не соответствует файлу — лучше отказаться, чем перепутать страницы

    pool = Pool(name=name or path.stem, path=path, page_files=page_files)
    pool._reader = reader
    return pool


ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".xz")


class ArchiveError(Exception):
    """Архив не удалось распаковать имеющимися средствами."""


def _collect_pdfs(root: Path) -> list[tuple[str, Path]]:
    """Все PDF из распакованного дерева, с уникализацией одинаковых имён."""
    out: list[tuple[str, Path]] = []
    used: set[str] = set()
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() != ".pdf":
            continue
        if "__MACOSX" in f.parts or f.name.startswith("._"):
            continue
        if RESULT_MARKER in f.name:
            continue
        base = nfc(f.name)
        n = 1
        while base in used:
            base = nfc(f"{f.stem}~{n}{f.suffix}")
            n += 1
        used.add(base)
        out.append((base, f))
    out.sort(key=lambda p: p[0])
    return out


def _extract_with_tool(archive: Path, dest: Path) -> None:
    """RAR / 7z / tar — сторонними распаковщиками.

    bsdtar (libarchive) есть в macOS из коробки и умеет читать RAR4/RAR5;
    unar / unrar / 7z используются, если установлены.
    """
    attempts = [
        (tool, args)
        for tool, args in (
            ("unar", ["-quiet", "-force-overwrite", "-output-directory", str(dest), str(archive)]),
            ("7z", ["x", "-y", f"-o{dest}", str(archive)]),
            ("7zz", ["x", "-y", f"-o{dest}", str(archive)]),
            ("unrar", ["x", "-y", "-inul", str(archive), str(dest) + "/"]),
            ("bsdtar", ["-x", "-f", str(archive), "-C", str(dest)]),
        )
        if shutil.which(tool)
    ]
    if not attempts:
        raise ArchiveError("на этой машине нет ни одного распаковщика архивов")

    errors = []
    for tool, args in attempts:
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([tool, *args], capture_output=True, timeout=300)
        if proc.returncode == 0 and any(dest.rglob("*.pdf")):
            return
        errors.append(f"{tool}: {(proc.stderr or proc.stdout).decode(errors='replace').strip()[:200]}")
    raise ArchiveError("; ".join(errors) or "распаковать не удалось")


def collect_from_archive(archive_path: Path, dest_dir: Path) -> list[tuple[str, Path]]:
    """Достать PDF-этикетки из архива: zip — своими силами, rar/7z/tar — распаковщиком."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive_path):
        return collect_from_zip(archive_path, dest_dir)
    _extract_with_tool(archive_path, dest_dir)
    return _collect_pdfs(dest_dir)
