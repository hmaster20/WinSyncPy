#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winsync.py
Улучшенный скрипт синхронизации с GUI на основе tkinter.
Поддерживает:
- Множественные пары папок
- Фильтрацию (исключения)
- Предварительное сравнение (dry-run)
- Сохранение/загрузку конфигурации в XML (*.ws)
- Удаление в корзину (требуется Send2Trash)
- Контекстное меню копирования в логе
- Режим синхронизации в вкладке "Пары папок"
"""
import os
import sys
import shutil
import threading
import stat
import re
import xml.etree.ElementTree as ET
from pathlib import Path
import win32security
import win32file
import pywintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Попытка импорта Send2Trash для корзины
try:
    from send2trash import send2trash
    SEND2TRASH_AVAILABLE = True
except ImportError:
    SEND2TRASH_AVAILABLE = False

# --- Глобальные переменные ---
ERRORS = []
ERRORS_LOCK = threading.Lock()

# --- Вспомогательные функции ---
def normalize_path(path):
    """Нормализует путь для поддержки длинных имен."""
    path = os.path.abspath(path)
    if not path.startswith('\\\\?\\'):
        path = '\\\\?\\' + path
    return path

def get_file_streams(filepath):
    """Получает список всех альтернативных потоков данных (ADS) для файла."""
    # Проверяем наличие необходимого атрибута
    if not hasattr(win32file, 'FindFirstStreamW'):
        with ERRORS_LOCK:
            ERRORS.append(f"Функция FindFirstStreamW недоступна. Пропуск ADS для {filepath}.")
        return []

    try:
        streams = []
        handle = win32file.FindFirstStreamW(filepath, win32file.StreamInfoTypes.FindStreamInfoStandard)
        while True:
            stream_name, stream_size = handle[0], handle[1]
            if stream_name != ':$DATA':
                clean_name = stream_name.replace(':$DATA', '')
                streams.append(clean_name)
            try:
                handle = win32file.FindNextStreamW(handle)
            except pywintypes.error:
                break
        win32file.FindClose(handle)
        return streams
    except Exception as e:
        with ERRORS_LOCK:
            ERRORS.append(f"Ошибка при получении потоков для {filepath}: {e}")
        return []

def copy_ads(source_file, dest_file):
    """Копирует все альтернативные потоки данных (ADS)."""
    streams = get_file_streams(source_file)
    for stream in streams:
        try:
            src_stream_path = f"{source_file}:{stream}"
            dst_stream_path = f"{dest_file}:{stream}"
            with open(src_stream_path, 'rb') as src, open(dst_stream_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        except Exception as e:
            with ERRORS_LOCK:
                ERRORS.append(f"Ошибка копирования ADS '{stream}' для {source_file}: {e}")

def copy_acl(source_path, dest_path):
    """Копирует ACL (права доступа NTFS)."""
    try:
        security_descriptor = win32security.GetFileSecurity(
            source_path,
            win32security.DACL_SECURITY_INFORMATION | 
            win32security.OWNER_SECURITY_INFORMATION | 
            win32security.GROUP_SECURITY_INFORMATION
        )
        win32security.SetFileSecurity(
            dest_path,
            win32security.DACL_SECURITY_INFORMATION | 
            win32security.OWNER_SECURITY_INFORMATION | 
            win32security.GROUP_SECURITY_INFORMATION,
            security_descriptor
        )
    except Exception as e:
        with ERRORS_LOCK:
            ERRORS.append(f"Ошибка копирования ACL для {source_path}: {e}")

def should_copy(src_stat, dst_stat):
    """Определяет, нужно ли копировать файл."""
    return (src_stat.st_size != dst_stat.st_size or
            abs(src_stat.st_mtime - dst_stat.st_mtime) > 1)

def match_filter(path, exclude_patterns):
    """Проверяет, должен ли путь быть исключён."""
    if not exclude_patterns:
        return False
    path_str = str(path).replace('\\', '/')
    for pattern in exclude_patterns:
        pattern = pattern.replace('\\', '/').replace('*', '.*').replace('?', '.')
        if pattern.startswith('/'):
            pattern = pattern[1:]
        if re.search(pattern, path_str, re.IGNORECASE):
            return True
    return False

def analyze_sync(source, destination, mode, exclude_patterns):
    """Анализирует изменения без их применения. Возвращает список действий."""
    actions = []
    source_path = Path(normalize_path(source))
    dest_path = Path(normalize_path(destination))
    # Проход по источнику
    for root, dirs, files in os.walk(source_path):
        rel_path = os.path.relpath(root, source_path)
        if rel_path == '.':
            rel_path = ''
        dirs[:] = [d for d in dirs if not match_filter(os.path.join(root, d), exclude_patterns)]
        for d in dirs:
            src_dir = os.path.join(root, d)
            dst_dir = os.path.join(destination, rel_path, d) if rel_path else os.path.join(destination, d)
            dst_dir_norm = normalize_path(dst_dir)
            if not os.path.exists(dst_dir_norm):
                actions.append(('create_dir', src_dir, dst_dir))
        files = [f for f in files if not match_filter(os.path.join(root, f), exclude_patterns)]
        for f in files:
            src_file = os.path.join(root, f)
            dst_file = os.path.join(destination, rel_path, f) if rel_path else os.path.join(destination, f)
            src_file_norm = normalize_path(src_file)
            dst_file_norm = normalize_path(dst_file)
            if os.path.exists(dst_file_norm):
                if should_copy(os.stat(src_file_norm), os.stat(dst_file_norm)):
                    actions.append(('copy_file', src_file, dst_file))
            else:
                actions.append(('copy_file', src_file, dst_file))
    # Удаление в режиме mirror
    if mode == 'mirror' and dest_path.exists():
        for root, dirs, files in os.walk(dest_path, topdown=False):
            rel_path = os.path.relpath(root, dest_path)
            if rel_path == '.':
                rel_path = ''
            for f in files:
                full_path = os.path.join(root, f)
                if not match_filter(full_path, exclude_patterns):
                    src_equiv = os.path.join(source, rel_path, f) if rel_path else os.path.join(source, f)
                    if not os.path.exists(src_equiv):
                        actions.append(('delete_file', full_path, None))
            for d in dirs:
                full_path = os.path.join(root, d)
                if not match_filter(full_path, exclude_patterns):
                    src_equiv = os.path.join(source, rel_path, d) if rel_path else os.path.join(source, d)
                    if not os.path.exists(src_equiv):
                        actions.append(('delete_dir', full_path, None))
    return actions

def apply_sync(actions, progress_callback=None):
    """Применяет список действий."""
    global ERRORS
    ERRORS = []
    total = len(actions)
    for i, (action, src, dst) in enumerate(actions, 1):
        try:
            if action == 'create_dir':
                os.makedirs(normalize_path(dst), exist_ok=True)
                copy_acl(normalize_path(src), normalize_path(dst))
            elif action == 'copy_file':
                os.makedirs(os.path.dirname(normalize_path(dst)), exist_ok=True)
                shutil.copy2(normalize_path(src), normalize_path(dst))
                copy_acl(normalize_path(src), normalize_path(dst))
                copy_ads(normalize_path(src), normalize_path(dst))
            elif action == 'delete_file':
                if SEND2TRASH_AVAILABLE:
                    send2trash(normalize_path(src))
                else:
                    os.remove(normalize_path(src))
            elif action == 'delete_dir':
                if SEND2TRASH_AVAILABLE:
                    send2trash(normalize_path(src))
                else:
                    os.rmdir(normalize_path(src))
        except Exception as e:
            with ERRORS_LOCK:
                ERRORS.append(f"Ошибка при {action} {src}: {e}")
        if progress_callback:
            progress_callback(i, total)

# --- Класс приложения ---
class SyncApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Windows синхронизация")
        self.root.geometry("800x600")
        self.root.resizable(True, True)
        self.exclude_patterns = [
            r'\System Volume Information\\',
            r'\$Recycle\.Bin\\',
            r'\RECYCLER\\',
            r'\RECYCLED\\',
            r'.*\\desktop\.ini$',
            r'.*\\thumbs\.db$'
        ]
        self.create_widgets()

    def create_widgets(self):
        style = ttk.Style()
        style.theme_use('vista')

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)

        # Верхнее меню
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Файл", menu=file_menu)
        file_menu.add_command(label="Сохранить конфигурацию", command=self.save_config)
        file_menu.add_command(label="Загрузить конфигурацию", command=self.load_config)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", command=self.root.quit)

        # Notebook (вкладки)
        notebook = ttk.Notebook(main_frame)
        notebook.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        main_frame.rowconfigure(0, weight=1)

        # Вкладка "Пары папок"
        pairs_frame = ttk.Frame(notebook, padding="10")
        notebook.add(pairs_frame, text="Пары папок")
        self.setup_pairs_tab(pairs_frame)

        # Вкладка "Фильтры"
        filter_frame = ttk.Frame(notebook, padding="10")
        notebook.add(filter_frame, text="Фильтры")
        self.setup_filter_tab(filter_frame)

        # Кнопки управления
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=1, column=0, pady=10)

        ttk.Button(btn_frame, text="Сравнить", command=self.compare_sync).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Запустить синхронизацию", command=self.start_sync).pack(side=tk.LEFT, padx=5)

        # Прогресс и лог
        self.progress = ttk.Progressbar(main_frame, orient="horizontal", mode="determinate")
        self.progress.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=(0, 10))
        self.progress.grid_remove()

        self.log_text = tk.Text(main_frame, height=10, state='disabled')
        self.log_text.grid(row=3, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=3, column=1, sticky=(tk.N, tk.S))
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid_remove()

        # Контекстное меню для лога
        self.log_context_menu = tk.Menu(self.log_text, tearoff=0)
        self.log_context_menu.add_command(label="Копировать", command=self.copy_selected_log)
        self.log_text.bind("<Button-3>", self.show_log_context_menu)  # ПКМ на Windows

        main_frame.rowconfigure(3, weight=1)

    def setup_pairs_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # Кнопки и режим синхронизации
        top_frame = ttk.Frame(parent)
        top_frame.grid(row=0, column=0, sticky=tk.W, pady=(0, 10))

        btn_frame = ttk.Frame(top_frame)
        btn_frame.pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Добавить пару", command=self.add_pair).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Удалить выбранное", command=self.remove_pair).pack(side=tk.LEFT)

        # Режим синхронизации
        mode_frame = ttk.Frame(top_frame)
        mode_frame.pack(side=tk.RIGHT)
        ttk.Label(mode_frame, text="Режим:").pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value="update")
        ttk.Radiobutton(mode_frame, text="Обновление", variable=self.mode_var, value="update").pack(side=tk.LEFT, padx=(5, 10))
        ttk.Radiobutton(mode_frame, text="Зеркало", variable=self.mode_var, value="mirror").pack(side=tk.LEFT, padx=(0, 10))

        # Treeview для пар
        columns = ('source', 'dest')
        self.tree = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        self.tree.heading('source', text='Исходная папка')
        self.tree.heading('dest', text='Целевая папка')
        self.tree.column('source', width=300)
        self.tree.column('dest', width=300)
        self.tree.grid(row=2, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        vsb.grid(row=2, column=1, sticky=(tk.N, tk.S))
        self.tree.configure(yscrollcommand=vsb.set)

    def setup_filter_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        ttk.Label(parent, text="Исключения (по одному на строку, поддержка * и ?):").grid(row=0, column=0, sticky=tk.W, pady=(0, 5))
        self.filter_text = tk.Text(parent, height=10)
        self.filter_text.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.filter_text.yview)
        vsb.grid(row=1, column=1, sticky=(tk.N, tk.S))
        self.filter_text.configure(yscrollcommand=vsb.set)
        self.filter_text.insert('1.0', '\n'.join(self.exclude_patterns))

    def add_pair(self):
        src = filedialog.askdirectory(title="Выберите исходную папку")
        if not src:
            return
        dst = filedialog.askdirectory(title="Выберите целевую папку")
        if not dst:
            return
        self.tree.insert('', 'end', values=(src, dst))

    def remove_pair(self):
        selected = self.tree.selection()
        if selected:
            self.tree.delete(selected)

    def get_pairs(self):
        pairs = []
        for item in self.tree.get_children():
            src, dst = self.tree.item(item, 'values')
            pairs.append((src, dst))
        return pairs

    def get_filters(self):
        text = self.filter_text.get('1.0', tk.END).strip()
        return [line.strip() for line in text.split('\n') if line.strip()]

    def log_message(self, message):
        self.log_text.configure(state='normal')
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.configure(state='disabled')
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def update_progress(self, current, total):
        if total > 0:
            percent = int((current / total) * 100)
            self.progress['value'] = percent
            self.root.update_idletasks()

    def compare_sync(self):
        pairs = self.get_pairs()
        if not pairs:
            messagebox.showerror("Ошибка", "Добавьте хотя бы одну пару папок.")
            return
        exclude_patterns = self.get_filters()
        self.log_text.grid()
        self.log_text.configure(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state='disabled')
        all_actions = []
        for src, dst in pairs:
            if not os.path.exists(src):
                self.log_message(f"Исходная папка не существует: {src}")
                continue
            actions = analyze_sync(src, dst, self.mode_var.get(), exclude_patterns)
            all_actions.extend(actions)
            for act in actions:
                op, s, d = act
                if op == 'copy_file':
                    self.log_message(f"→ Копировать: {s} → {d}")
                elif op == 'create_dir':
                    self.log_message(f"📁 Создать папку: {d}")
                elif op == 'delete_file':
                    self.log_message(f"× Удалить файл: {s}")
                elif op == 'delete_dir':
                    self.log_message(f"🗑 Удалить папку: {s}")
        if not all_actions:
            self.log_message("Нет изменений для синхронизации.")
        else:
            self.log_message(f"\nВсего операций: {len(all_actions)}")

    def start_sync(self):
        pairs = self.get_pairs()
        if not pairs:
            messagebox.showerror("Ошибка", "Добавьте хотя бы одну пару папок.")
            return
        exclude_patterns = self.get_filters()
        self.start_button_state(False)
        self.progress.grid()
        self.log_text.grid()
        self.log_text.configure(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state='disabled')
        thread = threading.Thread(target=self.run_sync, args=(pairs, exclude_patterns), daemon=True)
        thread.start()

    def run_sync(self, pairs, exclude_patterns):
        all_actions = []
        for src, dst in pairs:
            if not os.path.exists(src):
                self.root.after(0, lambda s=src: self.log_message(f"Исходная папка не существует: {s}"))
                continue
            actions = analyze_sync(src, dst, self.mode_var.get(), exclude_patterns)
            all_actions.extend(actions)
        if all_actions:
            apply_sync(all_actions, self.update_progress)
        self.root.after(0, self.sync_finished)

    def sync_finished(self):
        global ERRORS
        if ERRORS:
            self.log_message(f"Синхронизация завершена с ошибками ({len(ERRORS)}).")
            for err in ERRORS[:5]:
                self.log_message(f"  - {err}")
        else:
            self.log_message("Синхронизация успешно завершена!")
        self.start_button_state(True)
        messagebox.showinfo("Готово", "Синхронизация завершена!")

    def start_button_state(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for child in self.root.winfo_children():
            if isinstance(child, ttk.Frame):
                for grand in child.winfo_children():
                    if isinstance(grand, ttk.Button) and grand.cget('text') in ["Сравнить", "Запустить синхронизацию"]:
                        grand.config(state=state)
        if enabled:
            self.progress.grid_remove()

    def save_config(self):
        pairs = self.get_pairs()
        if not pairs:
            messagebox.showwarning("Предупреждение", "Нет пар для сохранения.")
            return

        initial_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        filepath = filedialog.asksaveasfilename(
            initialdir=initial_dir,
            defaultextension=".ws",
            filetypes=[("Workstation Sync Config", "*.ws"), ("All files", "*.*")]
        )
        if not filepath:
            return

        root = ET.Element("SyncConfig")
        pairs_el = ET.SubElement(root, "FolderPairs")
        for src, dst in pairs:
            pair = ET.SubElement(pairs_el, "Pair")
            ET.SubElement(pair, "Left").text = src
            ET.SubElement(pair, "Right").text = dst
        filter_el = ET.SubElement(root, "Filter")
        exclude_el = ET.SubElement(filter_el, "Exclude")
        for pat in self.get_filters():
            ET.SubElement(exclude_el, "Item").text = pat
        mode_el = ET.SubElement(root, "Mode")
        mode_el.text = self.mode_var.get()

        tree = ET.ElementTree(root)
        tree.write(filepath, encoding='utf-8', xml_declaration=True)
        self.log_message(f"Конфигурация сохранена: {filepath}")

    def load_config(self):
        initial_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        filepath = filedialog.askopenfilename(
            initialdir=initial_dir,
            filetypes=[("Workstation Sync Config", "*.ws"), ("All files", "*.*")]
        )
        if not filepath:
            return

        try:
            tree = ET.parse(filepath)
            root = tree.getroot()

            # Очистка текущих пар
            for item in self.tree.get_children():
                self.tree.delete(item)

            # Загрузка пар
            folder_pairs = root.find("FolderPairs")
            if folder_pairs is not None:
                for pair in folder_pairs.findall("Pair"):
                    left = pair.find("Left").text or ""
                    right = pair.find("Right").text or ""
                    self.tree.insert('', 'end', values=(left, right))

            # Загрузка фильтров
            exclude_el = root.find("Filter/Exclude")
            if exclude_el is not None:
                patterns = [item.text for item in exclude_el.findall("Item") if item.text]
                self.filter_text.delete('1.0', tk.END)
                self.filter_text.insert('1.0', '\n'.join(patterns))
            else:
                self.filter_text.delete('1.0', tk.END)

            # Загрузка режима
            mode_el = root.find("Mode")
            if mode_el is not None and mode_el.text in ("update", "mirror"):
                self.mode_var.set(mode_el.text)

            self.log_message(f"Конфигурация загружена: {filepath}")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось загрузить конфигурацию:\n{e}")

    def show_log_context_menu(self, event):
        try:
            self.log_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_context_menu.grab_release()

    def copy_selected_log(self):
        try:
            selected_text = self.log_text.selection_get()
            self.root.clipboard_clear()
            self.root.clipboard_append(selected_text)
        except tk.TclError:
            # Ничего не выделено
            pass

def main():
    root = tk.Tk()
    app = SyncApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()