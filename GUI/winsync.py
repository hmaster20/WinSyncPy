#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winsync.py

Скрипт синхронизации с GUI на основе tkinter.
Полностью бесплатный и не требует внешних зависимостей, кроме pywin32.
"""

import os
import sys
import shutil
import threading
import queue
import stat
from pathlib import Path
import win32security
import win32file
import pywintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --- Основная логика синхронизации ---

ERRORS = []
ERRORS_LOCK = threading.Lock()

def normalize_path(path):
    """Нормализует путь для поддержки длинных имен."""
    path = os.path.abspath(path)
    if not path.startswith('\\\\?\\'):
        path = '\\\\?\\' + path
    return path

def get_file_streams(filepath):
    """Получает список всех альтернативных потоков данных (ADS) для файла."""
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

def walk_and_sync(source, destination, mode, progress_callback=None):
    """Основная функция синхронизации."""
    global ERRORS
    ERRORS = []
    
    source_path = Path(normalize_path(source))
    dest_path = Path(normalize_path(destination))
    
    files_to_process = []
    total_files = 0
    
    for root, dirs, files in os.walk(source_path):
        rel_path = os.path.relpath(root, source_path)
        if rel_path == '.':
            rel_path = ''
            
        for d in dirs:
            src_dir = os.path.join(root, d)
            dst_dir = os.path.join(destination, rel_path, d) if rel_path else os.path.join(destination, d)
            files_to_process.append((normalize_path(src_dir), normalize_path(dst_dir), True))
            total_files += 1
            
        for f in files:
            src_file = os.path.join(root, f)
            dst_file = os.path.join(destination, rel_path, f) if rel_path else os.path.join(destination, f)
            src_file_norm = normalize_path(src_file)
            dst_file_norm = normalize_path(dst_file)
            
            if os.path.exists(dst_file_norm):
                if should_copy(os.stat(src_file_norm), os.stat(dst_file_norm)):
                    files_to_process.append((src_file_norm, dst_file_norm, False))
                    total_files += 1
            else:
                files_to_process.append((src_file_norm, dst_file_norm, False))
                total_files += 1

    processed = 0
    for src, dst, is_dir in files_to_process:
        try:
            if is_dir:
                os.makedirs(dst, exist_ok=True)
                copy_acl(src, dst)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copy_acl(src, dst)
                copy_ads(src, dst)
        except Exception as e:
            with ERRORS_LOCK:
                ERRORS.append(f"Ошибка копирования {src} -> {dst}: {e}")
        
        processed += 1
        if progress_callback:
            progress_callback(processed, total_files)

    if mode == 'mirror' and dest_path.exists():
        existing_dest = set()
        for root, dirs, files in os.walk(dest_path):
            for d in dirs:
                existing_dest.add(os.path.join(root, d))
            for f in files:
                existing_dest.add(os.path.join(root, f))
        
        for item in sorted(existing_dest, reverse=True):
            try:
                if os.path.isfile(item) or os.path.islink(item):
                    os.remove(item)
                elif os.path.isdir(item):
                    os.rmdir(item)
            except Exception as e:
                with ERRORS_LOCK:
                    ERRORS.append(f"Ошибка удаления {item}: {e}")

# --- Графический интерфейс на tkinter ---

class SyncApp:
    def __init__(self, root):
        self.root = root
        self.root.title("NTFS Синхронизация")
        self.root.geometry("600x400")
        self.root.resizable(True, True)
        
        # Создаем стиль для ttk
        style = ttk.Style()
        style.theme_use('vista')  # Используем нативную тему Windows
        
        self.create_widgets()
        
    def create_widgets(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Настройка сетки
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        
        # Исходная папка
        ttk.Label(main_frame, text="Исходная папка:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.source_var = tk.StringVar()
        ttk.Entry(main_frame, textvariable=self.source_var).grid(row=0, column=1, sticky=(tk.W, tk.E), padx=(5, 5), pady=5)
        ttk.Button(main_frame, text="Обзор...", command=self.browse_source).grid(row=0, column=2, sticky=tk.W, pady=5)
        
        # Целевая папка
        ttk.Label(main_frame, text="Целевая папка:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.dest_var = tk.StringVar()
        ttk.Entry(main_frame, textvariable=self.dest_var).grid(row=1, column=1, sticky=(tk.W, tk.E), padx=(5, 5), pady=5)
        ttk.Button(main_frame, text="Обзор...", command=self.browse_dest).grid(row=1, column=2, sticky=tk.W, pady=5)
        
        # Режим синхронизации
        ttk.Label(main_frame, text="Режим:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.mode_var = tk.StringVar(value="update")
        mode_frame = ttk.Frame(main_frame)
        mode_frame.grid(row=2, column=1, sticky=tk.W, pady=5)
        ttk.Radiobutton(mode_frame, text="Обновление", variable=self.mode_var, value="update").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="Зеркало", variable=self.mode_var, value="mirror").pack(side=tk.LEFT, padx=(10, 0))
        
        # Кнопка запуска
        self.start_button = ttk.Button(main_frame, text="Запустить синхронизацию", command=self.start_sync)
        self.start_button.grid(row=3, column=0, columnspan=3, pady=20)
        
        # Прогресс-бар
        self.progress = ttk.Progressbar(main_frame, orient="horizontal", mode="determinate")
        self.progress.grid(row=4, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 10))
        self.progress.grid_remove()  # Скрываем изначально
        
        # Лог-окно
        self.log_text = tk.Text(main_frame, height=10, state='disabled')
        self.log_text.grid(row=5, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=(0, 10))
        scrollbar = ttk.Scrollbar(main_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=5, column=3, sticky=(tk.N, tk.S))
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid_remove()  # Скрываем изначально
        
        # Настройка растягивания
        main_frame.rowconfigure(5, weight=1)
        
    def browse_source(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_var.set(folder)
            
    def browse_dest(self):
        folder = filedialog.askdirectory()
        if folder:
            self.dest_var.set(folder)
            
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
            
    def start_sync(self):
        source = self.source_var.get()
        dest = self.dest_var.get()
        mode = self.mode_var.get()
        
        if not source or not dest:
            messagebox.showerror("Ошибка", "Пожалуйста, выберите обе папки.")
            return
            
        if not os.path.exists(source):
            messagebox.showerror("Ошибка", f"Исходная папка не существует:\n{source}")
            return
            
        # Подготавливаем UI для синхронизации
        self.start_button.config(state='disabled')
        self.progress.grid()
        self.log_text.grid()
        self.log_text.configure(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state='disabled')
        self.root.update_idletasks()
        
        # Запускаем синхронизацию в отдельном потоке
        thread = threading.Thread(target=self.run_sync, args=(source, dest, mode), daemon=True)
        thread.start()
        
    def run_sync(self, source, dest, mode):
        try:
            walk_and_sync(source, dest, mode, self.update_progress)
            self.root.after(0, self.sync_finished)
        except Exception as e:
            self.root.after(0, lambda: self.sync_error(str(e)))
            
    def sync_finished(self):
        global ERRORS
        if ERRORS:
            self.log_message(f"Синхронизация завершена с ошибками ({len(ERRORS)}).")
            for err in ERRORS[:5]:
                self.log_message(f"  - {err}")
        else:
            self.log_message("Синхронизация успешно завершена!")
        self.start_button.config(state='normal')
        messagebox.showinfo("Готово", "Синхронизация завершена!")
        
    def sync_error(self, error_msg):
        self.log_message(f"Критическая ошибка: {error_msg}")
        self.start_button.config(state='normal')
        messagebox.showerror("Ошибка", f"Синхронизация прервана:\n{error_msg}")

def main():
    root = tk.Tk()
    app = SyncApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()