"""Разбор свободного текстового сообщения из Telegram:
списки артикулов Ozon / Yandex, имя папки, флаг «только эта папка».

Формат ввода нестрогий: маркер маркетплейса может стоять как ДО списка,
так и ПОСЛЕ него (как обычно пишет пользователь), порядок блоков любой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

OZON_MARKERS = {"ozon", "озон", "ozn", "озн", "ozon.ru"}
YANDEX_MARKERS = {"yandex", "яндекс", "yand", "ym", "ya", "яндексмаркет", "yandexmarket"}
FOLDER_WORDS = {"папка", "папке", "папку", "folder", "из"}
STRICT_WORDS = ("только", "строго", "лишь")

_PUNCT = " \t:—–-=*#>.,;!()[]\"'"


@dataclass
class ParsedMessage:
    ozon: list[str] = field(default_factory=list)
    yandex: list[str] = field(default_factory=list)
    folder_query: str | None = None
    strict_folder: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.ozon and not self.yandex


def _norm_marker(line: str) -> str:
    return re.sub(r"[^0-9a-zA-Zа-яА-ЯёЁ]+", "", line).lower()


def _marker_of(line: str) -> str | None:
    """OZON / YANDEX, если строка целиком является маркером-разделителем."""
    key = _norm_marker(line)
    if not key:
        return None
    if key in OZON_MARKERS:
        return "OZON"
    if key in YANDEX_MARKERS:
        return "YANDEX"
    return None


def _is_folder_line(line: str) -> bool:
    low = line.lower()
    return any(re.search(r"\b" + w + r"\b", low) for w in ("папка", "папке", "папку", "folder"))


def _extract_folder(line: str) -> tuple[str, bool]:
    """Из строки вида «папка BR 29», «br 29 папка», «папка BR 29 и только она»
    достаём имя папки и флаг строгого поиска."""
    low = line.lower()
    strict = any(w in low for w in STRICT_WORDS)
    tokens = re.split(r"\s+", line.strip(_PUNCT))
    keep: list[str] = []
    for t in tokens:
        tl = t.strip(_PUNCT).lower()
        if not tl:
            continue
        if tl in FOLDER_WORDS or tl in ("и", "она", "он", "оно", "этой", "эта", "это", "с", "в", "только", "строго", "лишь", "искать", "бери", "брать"):
            continue
        keep.append(t.strip(_PUNCT))
    return " ".join(keep).strip(), strict


CODE_TOKEN_RE = re.compile(r"^[A-Za-zА-Яа-я0-9]+(?:[-/][A-Za-zА-Яа-я0-9]+)+$")


def _looks_like_codes(tokens: list[str]) -> bool:
    if not tokens:
        return False
    return any(CODE_TOKEN_RE.match(t) for t in tokens)


def _tokens(line: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", line.strip()) if t]


def parse_message(text: str) -> ParsedMessage:
    result = ParsedMessage()
    blocks: dict[str, list[str]] = {"OZON": [], "YANDEX": []}

    pending: list[str] = []          # список, для которого маркер ещё не встречен
    active: str | None = None        # маркер, объявленный ПЕРЕД списком
    tail_lines: list[tuple[str, list[str]]] = []  # строки без кодов — кандидаты в имя папки

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("/"):  # команда бота — не часть списка
            continue

        marker = _marker_of(line)
        if marker:
            if pending:
                blocks[marker].extend(pending)
                pending = []
                active = None
            else:
                active = marker
            continue

        if _is_folder_line(line):
            folder, strict = _extract_folder(line)
            if folder:
                result.folder_query = folder
            result.strict_folder = result.strict_folder or strict
            continue

        toks = _tokens(line)
        if not _looks_like_codes(toks):
            # строка без похожих на артикул токенов — возможно, это имя папки
            tail_lines.append((line, toks))
            continue

        if active:
            blocks[active].extend(toks)
        else:
            pending.extend(toks)

    # Остаток без маркера
    if pending:
        if not blocks["OZON"] and not blocks["YANDEX"]:
            blocks["OZON"] = pending
            result.warnings.append(
                "Маркетплейс не указан — считаю список как OZON. "
                "Добавьте строку «ozon» или «yandex», если нужно иначе."
            )
        elif not blocks["OZON"]:
            blocks["OZON"] = pending
        elif not blocks["YANDEX"]:
            blocks["YANDEX"] = pending
        else:
            blocks["OZON"].extend(pending)
            result.warnings.append(
                "Часть строк оказалась без маркера маркетплейса — добавил их в список OZON."
            )

    # Имя папки, если не было строки со словом «папка»
    if result.folder_query is None and tail_lines:
        # берём последнюю «некодовую» строку — обычно это короткое имя папки
        cand_line, cand_toks = tail_lines[-1]
        if 1 <= len(cand_toks) <= 4 and len(cand_line) <= 40:
            folder, strict = _extract_folder(cand_line)
            if folder:
                result.folder_query = folder
                result.strict_folder = result.strict_folder or strict
        if result.folder_query is None:
            result.warnings.append(f"Не понял строку: «{cand_line}»")

    result.ozon = blocks["OZON"]
    result.yandex = blocks["YANDEX"]
    return result
