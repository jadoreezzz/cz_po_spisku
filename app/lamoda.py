"""Опциональный режим: транспортные/товарные этикетки Lamoda.

Файлы имеют случайные имена, поэтому номер заказа и артикул достаются
из текста PDF, а не из имени файла.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ORDER_RE = re.compile(r'RU\d{6}-\d+(?:-\d+)?')
ORDER_ITEM_RE = re.compile(r'RU\d{6}-\d+-\d+')
ARTICLE_LABEL_RE = re.compile(r'Артикул\s*\n?\s*([^\n]+)')


@dataclass
class LamodaLabel:
    filename: str
    text: str
    order_item: str | None  # RU######-#-# (позиция) — есть только у товарной этикетки
    order_base: str | None  # RU######-# — есть у обоих типов
    article: str | None  # артикул — есть только у товарной этикетки


@dataclass
class LamodaIndex:
    labels: list[LamodaLabel] = field(default_factory=list)

    def find_item_label(self, order_base: str, article: str, used: set[str]) -> LamodaLabel | None:
        for lbl in self.labels:
            if lbl.filename in used:
                continue
            if lbl.article == article and lbl.order_base == order_base:
                return lbl
        return None

    def find_pvz_label(self, order_base: str, used: set[str]) -> LamodaLabel | None:
        for lbl in self.labels:
            if lbl.filename in used:
                continue
            if lbl.article is None and lbl.order_base == order_base:
                return lbl
        return None


def scan_folder(folder: str | Path) -> LamodaIndex:
    folder = Path(folder)
    index = LamodaIndex()
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() != ".pdf":
            continue
        try:
            reader = PdfReader(str(f))
            text = reader.pages[0].extract_text() or ""
        except Exception:
            continue

        item_match = ORDER_ITEM_RE.search(text)
        art_match = ARTICLE_LABEL_RE.search(text)

        order_base = None
        m = ORDER_RE.search(text)
        if m:
            full = m.group(0)
            parts = full.split("-")
            if len(parts) >= 2:
                order_base = f"{parts[0]}-{parts[1]}"

        article = art_match.group(1).strip() if art_match else None

        index.labels.append(
            LamodaLabel(
                filename=f.name,
                text=text,
                order_item=item_match.group(0) if item_match else None,
                order_base=order_base,
                article=article,
            )
        )
    return index


@dataclass
class LamodaBuildReport:
    total_orders: int = 0
    total_items: int = 0
    found_items: int = 0
    missing: list[str] = field(default_factory=list)
    output_path: str | None = None


def build_lamoda_pdf(
    order_article_pairs: list[tuple[str, str]],
    folder: str | Path,
    output_path: str | Path,
    include_pvz: bool = True,
) -> LamodaBuildReport:
    """order_article_pairs: список (номер_заказа_base 'RU######-#', артикул), в нужном порядке."""
    folder = Path(folder)
    index = scan_folder(folder)
    report = LamodaBuildReport(total_items=len(order_article_pairs))

    writer = PdfWriter()
    used: set[str] = set()
    seen_orders: set[str] = set()

    for order_base, article in order_article_pairs:
        if order_base not in seen_orders and include_pvz:
            pvz = index.find_pvz_label(order_base, used)
            if pvz:
                reader = PdfReader(str(folder / pvz.filename))
                for page in reader.pages:
                    writer.add_page(page)
                used.add(pvz.filename)
            seen_orders.add(order_base)

        item = index.find_item_label(order_base, article, used)
        if item:
            reader = PdfReader(str(folder / item.filename))
            for page in reader.pages:
                writer.add_page(page)
            used.add(item.filename)
            report.found_items += 1
        else:
            report.missing.append(f"{order_base} / {article}")

    report.total_orders = len(seen_orders)

    output_path = Path(output_path)
    with open(output_path, "wb") as f:
        writer.write(f)
    report.output_path = str(output_path)
    return report
