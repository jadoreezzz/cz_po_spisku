"""Быстрый нерегрессионный тест логики без GUI: создаёт тестовые PDF/CSV/XLSX и прогоняет сборку."""
import csv
import shutil
import tempfile
from pathlib import Path

import openpyxl
from pypdf import PdfWriter

from matcher import find_with_fallbacks, list_pdf_files, parse_any_file, parse_text_list
from pdf_builder import build_pdf, make_output_name


def make_pdf(path: Path, n_pages: int):
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=200, height=300)
    with open(path, "wb") as f:
        w.write(f)


def run():
    tmp = Path(tempfile.mkdtemp(prefix="cz_test_"))
    print("Тестовая папка:", tmp)

    # Файлы этикеток
    make_pdf(tmp / "04630354081712_Шорты спортивные FIZULI MILITARY, R-SHORT003F-XXL, цвет камуфляж, размер XXL_1.pdf", 1)
    make_pdf(tmp / "04630354081713_Шорты спортивные легкие, FIZULI, ST-SHORT002F-M, цвет черный, размер M_3.pdf", 3)
    # Ловушка: похожий код-подстрока, не должен матчиться на SHORT002F-M
    make_pdf(tmp / "04630354081714_Шорты, FIZULI, SHORT002F-M, цвет синий, размер M_1.pdf", 1)
    # Фолбэк: код без размера + отдельное поле размер
    make_pdf(tmp / "04630354081715_Шорты, FIZULI, L-SHORT002M, цвет черный, размер M_1.pdf", 1)
    # Фолбэк: кириллическая М
    make_pdf(tmp / "04630354081716_Жилет, BARRACUDA, BR-VEST002B-XXL, цвет хаки, размер XXL_1.pdf", 1)
    make_pdf(tmp / "04630354081717_Рубашка, HURACAN, S2-SHIRT001H-L, цвет белый, размер L_1.pdf", 1)
    # Кириллическая M вместо латинской для теста фолбэка 3
    make_pdf(tmp / "04630354081718_Кепка, FIZULI, CAP001-М, цвет черный, размер М_2.pdf", 2)

    files = list_pdf_files(tmp)
    print(f"\nНайдено PDF файлов: {len(files)}")

    # --- Тест 1: точное совпадение с защитой от ложной подстроки ---
    cands, method = find_with_fallbacks("SHORT002F-M", files)
    assert len(cands) == 1 and "SHORT002F-M" in cands[0] and "ST-SHORT002F-M" not in cands[0], cands
    print("OK: точное совпадение без ложной подстроки:", cands, method)

    cands, method = find_with_fallbacks("ST-SHORT002F-M", files)
    assert len(cands) == 1 and cands[0].startswith("04630354081713"), cands
    print("OK: точное совпадение с префиксом:", cands, method)

    # --- Тест 2: фолбэк "размер отдельным полем" ---
    cands, method = find_with_fallbacks("L-SHORT002M-M", files)
    assert cands and method == "fallback_size_field", (cands, method)
    print("OK: фолбэк размер отдельным полем:", cands, method)

    # --- Тест 3: фолбэк кириллическая М ---
    cands, method = find_with_fallbacks("CAP001-M", files)
    assert cands and method == "fallback_cyrillic_m", (cands, method)
    print("OK: фолбэк кириллическая М:", cands, method)

    # --- Тест 4: не найдено ---
    cands, method = find_with_fallbacks("NOPE-XXX-L", files)
    assert not cands and method == "not_found"
    print("OK: не найдено обрабатывается корректно")

    # --- Тест 5: сборка PDF с повторами и курсором по страницам ---
    codes = [
        "R-SHORT003F-XXL",
        "ST-SHORT002F-M",
        "ST-SHORT002F-M",   # второе вхождение -> вторая страница из пула на этот код
        "ST-SHORT002F-M",   # третье вхождение -> третья страница
        "BR-VEST002B-XXL",
        "S2-SHIRT001H-L",
        "NOPE-CODE-L",       # намеренно отсутствующий
    ]
    out = tmp / make_output_name("OZON", "BR9")
    report = build_pdf(codes, tmp, out)
    print(f"\nОтчёт сборки: total={report.total} found={report.found} missing={report.missing_codes}")
    assert report.total == 7
    assert report.found == 6
    assert report.missing_codes == ["NOPE-CODE-L"]
    assert out.exists()

    from pypdf import PdfReader
    reader = PdfReader(str(out))
    assert len(reader.pages) == 6, f"Ожидалось 6 страниц (по числу найденных), получено {len(reader.pages)}"
    print(f"OK: итоговый PDF содержит {len(reader.pages)} страниц, файл: {out.name}")

    # --- Тест 6: 4-е вхождение того же кода при пуле из 3 страниц -> дублирует последнюю, не падает ---
    codes2 = ["ST-SHORT002F-M"] * 4
    out2 = tmp / "extra_test.pdf"
    report2 = build_pdf(codes2, tmp, out2)
    assert report2.found == 4
    reader2 = PdfReader(str(out2))
    assert len(reader2.pages) == 4
    print("OK: превышение пула страниц не приводит к падению (дублирование последней страницы)")

    # --- Тест 7: парсинг текстового списка с повторами ---
    text = "R-SHORT003F-XXL\nST-SHORT002F-M ST-SHORT002F-M\nBR-VEST002B-XXL"
    parsed = parse_text_list(text)
    assert parsed.codes == ["R-SHORT003F-XXL", "ST-SHORT002F-M", "ST-SHORT002F-M", "BR-VEST002B-XXL"], parsed.codes
    print("OK: парсинг текстового списка с сохранением порядка и повторов")

    # --- Тест 8: парсинг CSV Ozon ---
    csv_path = tmp / "ozon_postings.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Номер отправления", "Артикул", "Прочее"])
        writer.writerow(["12345", "R-SHORT003F-XXL", "x"])
        writer.writerow(["12346", "ST-SHORT002F-M", "y"])
    parsed = parse_any_file(csv_path)
    assert parsed.codes == ["R-SHORT003F-XXL", "ST-SHORT002F-M"], parsed.codes
    print("OK: парсинг CSV Ozon (utf-8-sig, ';')")

    # --- Тест 9: парсинг XLSX Yandex (лист "Список заказов", колонка D, с 3 строки) ---
    xlsx_path = tmp / "yandex_orders.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Список заказов"
    ws.append(["служебная строка 1", "", "", ""])
    ws.append(["Номер", "Товар", "Кол-во", "Ваш SKU"])
    ws.append(["1", "Шорты", "1", "R-SHORT003F-XXL"])
    ws.append(["2", "Жилет", "1", "BR-VEST002B-XXL"])
    wb.save(xlsx_path)
    parsed = parse_any_file(xlsx_path)
    assert parsed.codes == ["R-SHORT003F-XXL", "BR-VEST002B-XXL"], parsed.codes
    print("OK: парсинг XLSX Yandex (лист/колонка D/данные с 3-й строки)")

    print("\nВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    run()
