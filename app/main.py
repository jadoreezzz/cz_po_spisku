"""Сборка PDF-этикеток «Честный знак» по списку заказов.

Десктопное приложение: выбираешь папку с этикетками, вставляешь/загружаешь
список артикулов — получаешь один PDF в порядке списка + отчёт по найденным
и отсутствующим позициям. Работает полностью локально.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import ttkbootstrap as tb
from ttkbootstrap import ScrolledText
from ttkbootstrap.constants import *
from tkinterdnd2 import DND_FILES, TkinterDnD

from config_store import load_config, save_config
from lamoda import LamodaBuildReport, build_lamoda_pdf
from matcher import list_pdf_files, parse_any_file, parse_text_list
from pdf_builder import BuildReport, build_pdf, make_output_name

APP_TITLE = "Сборка ЧЗ-этикеток по списку заказов"


def enable_dnd(widget):
    """Динамически подмешивает поддержку drag-and-drop (tkinterdnd2) в произвольный
    tkinter-виджет (нужно для внутреннего Text у ttkbootstrap.ScrolledText, который
    создаётся библиотекой напрямую как tkinter.Text, без миксина DnDWrapper)."""
    cls = widget.__class__
    if TkinterDnD.DnDWrapper not in cls.__mro__:
        widget.__class__ = type(cls.__name__ + "Dnd", (cls, TkinterDnD.DnDWrapper), {})
    return widget


def enable_text_widget_editing(widget) -> None:
    """Гарантирует работу copy/cut/paste/select-all в Text-виджете независимо от
    системных биндингов (на некоторых системах Cmd/Ctrl-V по умолчанию не срабатывает
    в кастомных Tk-сборках) + добавляет контекстное меню по правому клику."""
    menu = tb.Menu(widget, tearoff=0)

    def do_paste(_event=None):
        try:
            clip = widget.clipboard_get()
        except Exception:
            return "break"
        try:
            if widget.tag_ranges("sel"):
                widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        widget.insert("insert", clip)
        return "break"

    def do_copy(_event=None):
        try:
            if widget.tag_ranges("sel"):
                text = widget.get("sel.first", "sel.last")
                widget.clipboard_clear()
                widget.clipboard_append(text)
        except Exception:
            pass
        return "break"

    def do_cut(_event=None):
        do_copy()
        try:
            if widget.tag_ranges("sel"):
                widget.delete("sel.first", "sel.last")
        except Exception:
            pass
        return "break"

    def do_select_all(_event=None):
        widget.tag_add("sel", "1.0", "end-1c")
        return "break"

    menu.add_command(label="Вырезать", command=do_cut)
    menu.add_command(label="Копировать", command=do_copy)
    menu.add_command(label="Вставить", command=do_paste)
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=do_select_all)

    def show_menu(event):
        widget.focus_set()
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    widget.bind("<Button-3>", show_menu)
    widget.bind("<Button-2>", show_menu)
    widget.bind("<Control-Button-1>", show_menu)

    # На macOS (Aqua) физический Cmd-V/Cmd-C/Cmd-X/Cmd-A транслируется Tk'ом сразу
    # в виртуальные события <<Paste>>/<<Copy>>/<<Cut>>/<<SelectAll>>, а не в буквальные
    # сочетания клавиш — поэтому привязка нужна именно к ним, буквальные сочетания
    # добавлены как страховка для Linux/Windows-сборок, где это не так.
    for seq in ("<<Paste>>", "<Command-v>", "<Control-v>"):
        widget.bind(seq, do_paste)
    for seq in ("<<Copy>>", "<Command-c>", "<Control-c>"):
        widget.bind(seq, do_copy)
    for seq in ("<<Cut>>", "<Command-x>", "<Control-x>"):
        widget.bind(seq, do_cut)
    for seq in ("<<SelectAll>>", "<Command-a>", "<Control-a>"):
        widget.bind(seq, do_select_all)


def open_in_file_manager(path: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        pass


class ListSlot(tb.Frame):
    """Один список артикулов: маркетплейс + метка + источник (текст или файл)."""

    def __init__(self, master, title: str, default_marketplace: str, closable: bool = False, on_toggle=None, on_folder_drop=None):
        super().__init__(master, padding=14)
        self.on_toggle = on_toggle
        self.on_folder_drop = on_folder_drop
        self.enabled_var = tb.BooleanVar(value=True)
        self.source_var = tb.StringVar(value="text")
        self.loaded_file_path: str | None = None
        self.loaded_codes: list[str] = []

        row = 0
        header = tb.Frame(self)
        header.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        header.columnconfigure(1, weight=1)

        if closable:
            tb.Checkbutton(
                header, text="Собирать этот список", variable=self.enabled_var,
                bootstyle="round-toggle", command=self._toggle
            ).grid(row=0, column=0, sticky="w")
        else:
            tb.Label(header, text=title, font=("-size", 12, "-weight", "bold")).grid(row=0, column=0, sticky="w")

        row += 1
        form = tb.Frame(self)
        form.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        for c in (1, 3):
            form.columnconfigure(c, weight=1)

        tb.Label(form, text="Площадка:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.marketplace_var = tb.StringVar(value=default_marketplace)
        tb.Combobox(
            form, textvariable=self.marketplace_var,
            values=["OZON", "YANDEX", "LAMODA", "OTHER"], width=12,
        ).grid(row=0, column=1, sticky="w", padx=(0, 20))

        tb.Label(form, text="Метка (папка/дата):").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.label_var = tb.StringVar(value="")
        tb.Entry(form, textvariable=self.label_var, width=16).grid(row=0, column=3, sticky="w")

        row += 1
        src_frame = tb.Frame(self)
        src_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        tb.Radiobutton(
            src_frame, text="Вставить текст", variable=self.source_var, value="text",
            bootstyle="info-toolbutton",
        ).pack(side=LEFT, padx=(0, 6))
        tb.Radiobutton(
            src_frame, text="Загрузить из файла (csv/xlsx)", variable=self.source_var, value="file",
            bootstyle="info-toolbutton",
        ).pack(side=LEFT)
        tb.Button(
            src_frame, text="Выбрать файл…", bootstyle="secondary-outline",
            command=self._pick_file,
        ).pack(side=LEFT, padx=(12, 0))
        tb.Button(
            src_frame, text="Вставить из буфера", bootstyle="primary-outline",
            command=self._paste_from_clipboard,
        ).pack(side=LEFT, padx=(12, 0))
        self.file_label = tb.Label(src_frame, text="", bootstyle="secondary")
        self.file_label.pack(side=LEFT, padx=(10, 0))

        row += 1
        self.text_widget = ScrolledText(self, height=10, autohide=True)
        self.text_widget.grid(row=row, column=0, columnspan=2, sticky="nsew")
        self.rowconfigure(row, weight=1)
        self.columnconfigure(0, weight=1)
        enable_text_widget_editing(self.text_widget.text)

        enable_dnd(self.text_widget.text)
        self.text_widget.text.drop_target_register(DND_FILES)
        self.text_widget.text.dnd_bind("<<Drop>>", self._on_drop)

        self._toggle()

    def _on_drop(self, event):
        try:
            paths = self.text_widget.text.tk.splitlist(event.data)
        except Exception:
            return
        if not paths:
            return
        p = Path(paths[0])
        if p.is_dir():
            if self.on_folder_drop:
                self.on_folder_drop(str(p))
            return
        if p.is_file() and p.suffix.lower() in (".csv", ".xlsx"):
            self.loaded_file_path = str(p)
            self.source_var.set("file")
            self.file_label.config(text=p.name)

    def _toggle(self):
        state = "normal" if self.enabled_var.get() else "disabled"
        for child in self.winfo_children():
            pass  # оставляем управление доступностью на уровне сборки, не блокируя ввод визуально
        if self.on_toggle:
            self.on_toggle()

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Выберите файл со списком артикулов",
            filetypes=[("CSV/XLSX", "*.csv *.xlsx"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        self.loaded_file_path = path
        self.source_var.set("file")
        self.file_label.config(text=Path(path).name)

    def _paste_from_clipboard(self):
        try:
            clip = self.clipboard_get()
        except Exception:
            messagebox.showwarning(APP_TITLE, "Буфер обмена пуст или не содержит текста.")
            return
        self.source_var.set("text")
        self.text_widget.text.delete("1.0", "end")
        self.text_widget.text.insert("1.0", clip)

    def get_codes(self) -> tuple[list[str], list[str]]:
        """Возвращает (codes, warnings)."""
        if self.source_var.get() == "file":
            if not self.loaded_file_path:
                return [], ["Файл не выбран."]
            parsed = parse_any_file(self.loaded_file_path)
            return parsed.codes, parsed.warnings
        text = self.text_widget.get("1.0", "end")
        parsed = parse_text_list(text)
        return parsed.codes, parsed.warnings

    def is_enabled(self) -> bool:
        return self.enabled_var.get()

    def label_for_filename(self) -> str:
        return self.label_var.get().strip() or "spisok"

    def marketplace(self) -> str:
        return (self.marketplace_var.get().strip() or "SPISOK").upper()


class LamodaSlot(tb.Frame):
    """Опциональный режим Lamoda: сопоставление по номеру заказа + артикулу через текст PDF."""

    def __init__(self, master):
        super().__init__(master, padding=14)
        self.enabled_var = tb.BooleanVar(value=False)
        self.include_pvz_var = tb.BooleanVar(value=True)
        self.label_var = tb.StringVar(value="")

        row = 0
        header = tb.Frame(self)
        header.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        tb.Checkbutton(
            header, text="Собирать список Lamoda (по номеру заказа + артикулу из текста PDF)",
            variable=self.enabled_var, bootstyle="round-toggle",
        ).pack(side=LEFT)

        row += 1
        form = tb.Frame(self)
        form.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        tb.Label(form, text="Метка (папка/дата):").pack(side=LEFT, padx=(0, 6))
        tb.Entry(form, textvariable=self.label_var, width=16).pack(side=LEFT, padx=(0, 20))
        tb.Checkbutton(form, text="Включать этикетку ПВЗ", variable=self.include_pvz_var, bootstyle="round-toggle").pack(side=LEFT)
        tb.Button(
            form, text="Вставить из буфера", bootstyle="primary-outline",
            command=self._paste_from_clipboard,
        ).pack(side=LEFT, padx=(20, 0))

        row += 1
        tb.Label(
            self,
            text="По одной строке на позицию заказа, в нужном порядке: НОМЕР_ЗАКАЗА;АРТИКУЛ\n"
                 "Пример: RU260812-003269;R-SHORT003F-XXL",
            bootstyle="secondary",
        ).grid(row=row, column=0, sticky="w", pady=(0, 6))

        row += 1
        self.text_widget = ScrolledText(self, height=8, autohide=True)
        self.text_widget.grid(row=row, column=0, sticky="nsew")
        self.rowconfigure(row, weight=1)
        self.columnconfigure(0, weight=1)
        enable_text_widget_editing(self.text_widget.text)

    def _paste_from_clipboard(self):
        try:
            clip = self.clipboard_get()
        except Exception:
            messagebox.showwarning(APP_TITLE, "Буфер обмена пуст или не содержит текста.")
            return
        self.text_widget.text.delete("1.0", "end")
        self.text_widget.text.insert("1.0", clip)

    def is_enabled(self) -> bool:
        return self.enabled_var.get()

    def label_for_filename(self) -> str:
        return self.label_var.get().strip() or "spisok"

    def get_pairs(self) -> tuple[list[tuple[str, str]], list[str]]:
        pairs = []
        warnings = []
        text = self.text_widget.get("1.0", "end")
        for i, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            if ";" not in line:
                warnings.append(f"Строка {i}: нет разделителя ';' — пропущена: {line}")
                continue
            order, article = line.split(";", 1)
            order = order.strip()
            article = article.strip()
            if not order or not article:
                warnings.append(f"Строка {i}: пустой номер заказа или артикул — пропущена")
                continue
            pairs.append((order, article))
        return pairs, warnings


class App(tb.Window, TkinterDnD.DnDWrapper):
    def __init__(self):
        super().__init__(title=APP_TITLE, themename="flatly", size=(980, 760), resizable=(True, True))
        self.minsize(860, 620)
        self.TkdndVersion = TkinterDnD._require(self)

        self.config_data = load_config()
        self.folder_var = tb.StringVar(value=self.config_data.get("last_folder", ""))

        self._build_ui()
        self._refresh_folder_stats()

        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_global_drop)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = tb.Frame(self, padding=18)
        outer.pack(fill=BOTH, expand=YES)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # Заголовок
        title_frame = tb.Frame(outer)
        title_frame.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        tb.Label(title_frame, text=APP_TITLE, font=("-size", 18, "-weight", "bold")).pack(anchor="w")
        tb.Label(
            title_frame,
            text="Собирает один PDF из готовых этикеток ЧЗ строго в порядке списка заказов, включая повторы.",
            bootstyle="secondary",
        ).pack(anchor="w")

        # Папка с этикетками
        folder_card = tb.Labelframe(outer, text="Папка с этикетками", padding=14)
        folder_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        folder_card.columnconfigure(0, weight=1)

        entry_row = tb.Frame(folder_card)
        entry_row.grid(row=0, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        tb.Entry(entry_row, textvariable=self.folder_var, state="readonly").grid(row=0, column=0, sticky="ew", padx=(0, 10))
        tb.Button(entry_row, text="Выбрать папку…", bootstyle="primary-outline", command=self._pick_folder).grid(row=0, column=1)

        self.folder_stats_label = tb.Label(folder_card, text="", bootstyle="secondary")
        self.folder_stats_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

        # Списки артикулов
        lists_card = tb.Labelframe(outer, text="Списки артикулов", padding=10)
        lists_card.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        lists_card.columnconfigure(0, weight=1)

        self.notebook = tb.Notebook(lists_card, bootstyle="primary")
        self.notebook.pack(fill=BOTH, expand=YES)

        self.slot1 = ListSlot(self.notebook, "Список 1", "OZON", closable=False, on_folder_drop=self._set_folder)
        self.slot2 = ListSlot(self.notebook, "Список 2", "YANDEX", closable=True, on_folder_drop=self._set_folder)
        self.lamoda_slot = LamodaSlot(self.notebook)
        self.notebook.add(self.slot1, text="  Список 1  ")
        self.notebook.add(self.slot2, text="  Список 2 (опционально)  ")
        self.notebook.add(self.lamoda_slot, text="  Lamoda (опционально)  ")

        # Действие + прогресс
        action_row = tb.Frame(outer)
        action_row.grid(row=3, column=0, sticky="new", pady=(0, 10))
        self.build_btn = tb.Button(
            action_row, text="Собрать PDF", bootstyle="success", width=20,
            command=self._on_build_clicked,
        )
        self.build_btn.pack(side=LEFT)
        self.progress = tb.Progressbar(action_row, mode="indeterminate", bootstyle="success-striped")
        self.progress.pack(side=LEFT, fill=X, expand=YES, padx=(14, 0))

        # Результат
        result_card = tb.Labelframe(outer, text="Результат", padding=14)
        result_card.grid(row=4, column=0, sticky="nsew")
        outer.rowconfigure(4, weight=1)
        result_card.columnconfigure(0, weight=1)
        result_card.rowconfigure(1, weight=1)

        summary_row = tb.Frame(result_card)
        summary_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.summary_label = tb.Label(summary_row, text="Ещё ничего не собрано.", font=("-size", 11))
        self.summary_label.pack(side=LEFT)

        btns_row = tb.Frame(summary_row)
        btns_row.pack(side=RIGHT)
        self.copy_btn = tb.Button(btns_row, text="Скопировать отсутствующие", bootstyle="secondary-outline", command=self._copy_missing, state="disabled")
        self.copy_btn.pack(side=LEFT, padx=(0, 8))
        self.open_folder_btn = tb.Button(btns_row, text="Открыть папку с результатом", bootstyle="secondary-outline", command=self._open_result_folder, state="disabled")
        self.open_folder_btn.pack(side=LEFT)

        self.result_text = ScrolledText(result_card, height=12, autohide=True)
        self.result_text.grid(row=1, column=0, sticky="nsew")
        self.result_text.text.config(state="disabled")

        self._last_result_folder: str | None = None
        self._last_missing: list[str] = []

    # ------------------------------------------------------------ handlers
    def _pick_folder(self):
        path = filedialog.askdirectory(title="Выберите папку с PDF-этикетками")
        if not path:
            return
        self._set_folder(path)

    def _set_folder(self, path: str):
        self.folder_var.set(path)
        self.config_data["last_folder"] = path
        save_config(self.config_data)
        self._refresh_folder_stats()

    def _on_global_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            return
        if not paths:
            return
        p = Path(paths[0])
        if p.is_dir():
            self._set_folder(str(p))
        else:
            messagebox.showinfo(
                APP_TITLE,
                f"Перетащите папку с PDF-этикетками, а не файл.\nПолучено: {p.name}",
            )

    def _refresh_folder_stats(self):
        folder = self.folder_var.get()
        if not folder or not Path(folder).is_dir():
            self.folder_stats_label.config(text="Папка не выбрана. Можно перетащить папку в любое место окна.")
            return
        try:
            n = len(list_pdf_files(folder))
            self.folder_stats_label.config(text=f"PDF-файлов в папке: {n}   (можно перетащить другую папку в окно, чтобы сменить)")
        except Exception as e:
            self.folder_stats_label.config(text=f"Не удалось прочитать папку: {e}")

    def _on_build_clicked(self):
        folder = self.folder_var.get()
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning(APP_TITLE, "Сначала выберите папку с этикетками.")
            return

        slots = [self.slot1]
        if self.slot2.is_enabled():
            slots.append(self.slot2)

        jobs = []
        warnings: list[str] = []
        for slot in slots:
            codes, warns = slot.get_codes()
            warnings.extend(warns)
            if not codes:
                continue
            jobs.append((slot, codes))

        lamoda_job = None
        if self.lamoda_slot.is_enabled():
            pairs, warns = self.lamoda_slot.get_pairs()
            warnings.extend(warns)
            if pairs:
                lamoda_job = pairs

        if not jobs and not lamoda_job:
            msg = "Не найдено ни одного артикула для сборки."
            if warnings:
                msg += "\n\n" + "\n".join(warnings)
            messagebox.showwarning(APP_TITLE, msg)
            return

        self.build_btn.config(state="disabled")
        self.progress.start(12)
        self._set_result_text("Идёт сборка…")

        thread = threading.Thread(
            target=self._run_build, args=(folder, jobs, lamoda_job, warnings), daemon=True
        )
        thread.start()

    def _run_build(self, folder: str, jobs, lamoda_job, warnings: list[str]):
        reports: list[tuple[ListSlot, BuildReport]] = []
        lamoda_report: LamodaBuildReport | None = None
        error: str | None = None
        try:
            for slot, codes in jobs:
                out_name = make_output_name(slot.marketplace(), slot.label_for_filename())
                out_path = Path(folder) / out_name
                report = build_pdf(codes, folder, out_path)
                reports.append((slot, report))

            if lamoda_job:
                out_name = make_output_name("LAMODA", self.lamoda_slot.label_for_filename())
                out_path = Path(folder) / out_name
                lamoda_report = build_lamoda_pdf(
                    lamoda_job, folder, out_path,
                    include_pvz=self.lamoda_slot.include_pvz_var.get(),
                )
        except Exception:
            error = traceback.format_exc()

        self.after(0, lambda: self._on_build_done(reports, lamoda_report, warnings, error))

    def _on_build_done(self, reports, lamoda_report: LamodaBuildReport | None, warnings: list[str], error: str | None):
        self.progress.stop()
        self.build_btn.config(state="normal")

        if error:
            messagebox.showerror(APP_TITLE, f"Ошибка при сборке:\n\n{error}")
            self._set_result_text(f"Ошибка:\n{error}")
            return

        lines = []
        total_found = 0
        total_all = 0
        all_missing: list[str] = []
        last_folder = None

        for slot, report in reports:
            total_found += report.found
            total_all += report.total
            last_folder = str(Path(report.output_path).parent)
            lines.append(f"=== {slot.marketplace()} / {slot.label_for_filename()} ===")
            lines.append(f"Файл: {report.output_path}")
            lines.append(f"Всего позиций: {report.total}    Найдено: {report.found}    Не найдено: {len(report.missing_codes)}")
            if report.fallback_used:
                lines.append(f"Найдено через фолбэк (проверьте вручную): {len(report.fallback_used)}")
                for code, method in report.fallback_used.items():
                    lines.append(f"    {code}  —  {method}")
            if report.missing_codes:
                lines.append("Не найдены:")
                for code in report.missing_codes:
                    lines.append(f"    {code}")
                    all_missing.append(code)
            lines.append("")

        if lamoda_report:
            total_found += lamoda_report.found_items
            total_all += lamoda_report.total_items
            last_folder = str(Path(lamoda_report.output_path).parent)
            lines.append(f"=== LAMODA / {self.lamoda_slot.label_for_filename()} ===")
            lines.append(f"Файл: {lamoda_report.output_path}")
            lines.append(f"Заказов: {lamoda_report.total_orders}    Позиций: {lamoda_report.total_items}    Найдено: {lamoda_report.found_items}")
            if lamoda_report.missing:
                lines.append("Не найдены (заказ / артикул):")
                for m in lamoda_report.missing:
                    lines.append(f"    {m}")
                    all_missing.append(m)
            lines.append("")

        if warnings:
            lines.append("Предупреждения:")
            for w in warnings:
                lines.append(f"    {w}")
            lines.append("")

        self._set_result_text("\n".join(lines))
        self.summary_label.config(
            text=f"Готово: {total_found} из {total_all} найдено, {len(all_missing)} не найдено."
        )

        self._last_result_folder = last_folder
        self._last_missing = all_missing
        self.copy_btn.config(state="normal" if all_missing else "disabled")
        self.open_folder_btn.config(state="normal" if last_folder else "disabled")

    def _set_result_text(self, text: str):
        self.result_text.text.config(state="normal")
        self.result_text.text.delete("1.0", "end")
        self.result_text.text.insert("1.0", text)
        self.result_text.text.config(state="disabled")

    def _copy_missing(self):
        if not self._last_missing:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(self._last_missing))
        messagebox.showinfo(APP_TITLE, "Список отсутствующих артикулов скопирован в буфер обмена.")

    def _open_result_folder(self):
        if self._last_result_folder:
            open_in_file_manager(self._last_result_folder)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
