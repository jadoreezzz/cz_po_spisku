"""Этикетки Lamoda: транспортные (ПВЗ) и товарные, собранные по списку.

Файлы Lamoda приходят двумя многостраничными PDF: «большие» — по одной
транспортной этикетке на заказ, «маленькие» — по одной товарной на позицию.
Имена страниц ничего не значат, поэтому заказ и артикул достаются из текста
страницы. Индекс зашивается внутрь собранного пула — как и у пула ЧЗ.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfWriter

from pdf_io import PdfUnreadable, read_pdf_with_retry

META_KEY = "/LamodaIndex"
INDEX_VERSION = 1

ORDER_ITEM_RE = re.compile(r"RU\d{6}-\d+-\d+")
ORDER_BASE_RE = re.compile(r"RU\d{6}-\d+")
ARTICLE_RE = re.compile(r"Артикул\s+([^\n]+)")

KIND_PVZ = "pvz"      # транспортная этикетка заказа
KIND_ITEM = "item"    # товарная этикетка позиции


def _normalize(text: str) -> str:
    """В тексте страницы номер часто разорван переносом: «RU261013-782856-\\n001»."""
    return re.sub(r"-\s*\n\s*", "-", text or "")


@dataclass
class LamodaPage:
    kind: str
    order_base: str | None = None
    order_item: str | None = None
    article: str | None = None


def describe_page(text: str) -> LamodaPage | None:
    """Что на странице: транспортная этикетка, товарная или ничего из этого."""
    t = _normalize(text)
    base_m = ORDER_BASE_RE.search(t)
    if not base_m:
        return None
    base = base_m.group(0)
    art_m = ARTICLE_RE.search(t)
    if art_m:
        item_m = ORDER_ITEM_RE.search(t)
        item = item_m.group(0) if item_m else None
        if item:
            base = "-".join(item.split("-")[:2])
        return LamodaPage(KIND_ITEM, base, item, art_m.group(1).strip())
    return LamodaPage(KIND_PVZ, base)


def looks_like_lamoda(path: str | Path, probe_pages: int = 3) -> bool:
    try:
        reader = read_pdf_with_retry(path)
    except PdfUnreadable:
        return False
    for page in reader.pages[:probe_pages]:
        if describe_page(page.extract_text() or "") is not None:
            return True
    return False


@dataclass
class LamodaPool:
    name: str
    path: Path
    pages: list[LamodaPage] = field(default_factory=list)
    _reader: object | None = None

    kind = "lamoda"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def orders(self) -> list[str]:
        """Заказы в том порядке, в каком они идут в файле."""
        seen: dict[str, None] = {}
        for p in self.pages:
            if p.order_base:
                seen.setdefault(p.order_base, None)
        return list(seen)

    @property
    def item_count(self) -> int:
        return sum(1 for p in self.pages if p.kind == KIND_ITEM)

    @property
    def pvz_count(self) -> int:
        return sum(1 for p in self.pages if p.kind == KIND_PVZ)

    def reader(self):
        if self._reader is None:
            self._reader = read_pdf_with_retry(self.path)
        return self._reader

    def page(self, i: int):
        return self.reader().pages[i]

    def indexes_of_order(self, order_base: str, kind: str) -> list[int]:
        return [i for i, p in enumerate(self.pages)
                if p.order_base == order_base and p.kind == kind]

    def find_item(self, article: str, used: set[int]) -> int | None:
        for i, p in enumerate(self.pages):
            if i in used or p.kind != KIND_ITEM:
                continue
            if p.article == article:
                return i
        return None


# ------------------------------------------------------------------ создание

def _payload(pages: list[LamodaPage]) -> str:
    return json.dumps(
        {"v": INDEX_VERSION,
         "pages": [[p.kind, p.order_base, p.order_item, p.article] for p in pages]},
        ensure_ascii=False, separators=(",", ":"),
    )


def build_lamoda_pool(sources: list[tuple[str, Path]], out_path: Path,
                      name: str) -> tuple[LamodaPool, list[str]]:
    writer = PdfWriter()
    pages: list[LamodaPage] = []
    unreadable: list[str] = []
    readers = []

    for filename, path in sources:
        try:
            reader = read_pdf_with_retry(path)
        except PdfUnreadable:
            unreadable.append(filename)
            continue
        readers.append(reader)
        for page in reader.pages:
            info = describe_page(page.extract_text() or "")
            if info is None:
                continue  # страница без номера заказа — не этикетка Lamoda
            writer.add_page(page)
            pages.append(info)

    writer.add_metadata({META_KEY: _payload(pages), "/Title": f"Lamoda pool: {name}"})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return LamodaPool(name=name, path=out_path, pages=pages), unreadable


def load_lamoda_pool(path: Path, name: str | None = None) -> LamodaPool | None:
    try:
        reader = read_pdf_with_retry(path)
    except PdfUnreadable:
        return None
    meta = reader.metadata or {}
    if META_KEY not in meta:
        return None
    try:
        data = json.loads(str(meta[META_KEY]))
        pages = [LamodaPage(k, b, i, a) for k, b, i, a in data["pages"]]
    except (ValueError, KeyError, TypeError):
        return None
    if len(pages) != len(reader.pages):
        return None
    pool = LamodaPool(name=name or path.stem, path=path, pages=pages)
    pool._reader = reader
    return pool


# ------------------------------------------------------------------ сборка

@dataclass
class LamodaReport:
    total: int = 0
    found: int = 0
    orders: int = 0
    missing: list[str] = field(default_factory=list)
    output_path: str | None = None
    mode: str = ""


def build_lamoda(items: list[str], pool: LamodaPool, output_path: str | Path,
                 include_pvz: bool = True, include_items: bool = True) -> LamodaReport:
    """items — артикулы и/или номера заказов (RU######-#) в нужном порядке.

    Пустой список = все заказы в порядке файла. Для каждого заказа сначала
    идёт транспортная этикетка, затем его товарные — так посылку удобно собирать.
    """
    report = LamodaReport(total=len(items))
    writer = PdfWriter()
    used: set[int] = set()
    pvz_done: set[str] = set()

    def add(i: int) -> None:
        writer.add_page(pool.page(i))
        used.add(i)

    def add_pvz(order_base: str | None) -> None:
        if not include_pvz or not order_base or order_base in pvz_done:
            return
        pvz_done.add(order_base)
        for i in pool.indexes_of_order(order_base, KIND_PVZ):
            add(i)
            break

    if not items:  # весь файл: заказ за заказом
        report.mode = "по заказам (весь файл)"
        for order in pool.orders:
            add_pvz(order)
            if include_items:
                for i in pool.indexes_of_order(order, KIND_ITEM):
                    add(i)
        report.total = report.found = len(pool.orders)
        report.orders = len(pvz_done)
        output_path = Path(output_path)
        with open(output_path, "wb") as f:
            writer.write(f)
        report.output_path = str(output_path)
        return report

    orders_mode = all(ORDER_BASE_RE.fullmatch(x.strip()) for x in items)
    report.mode = "по номерам заказов" if orders_mode else "по артикулам"

    for raw in items:
        code = raw.strip()
        if orders_mode:
            idxs = pool.indexes_of_order(code, KIND_ITEM)
            if not idxs and not pool.indexes_of_order(code, KIND_PVZ):
                report.missing.append(code)
                continue
            add_pvz(code)
            if include_items:
                for i in idxs:
                    if i not in used:
                        add(i)
            report.found += 1
            continue

        i = pool.find_item(code, used)
        if i is None:
            report.missing.append(code)
            continue
        add_pvz(pool.pages[i].order_base)
        if include_items:
            add(i)
        else:
            used.add(i)
        report.found += 1

    report.orders = len(pvz_done)
    output_path = Path(output_path)
    with open(output_path, "wb") as f:
        writer.write(f)
    report.output_path = str(output_path)
    return report
