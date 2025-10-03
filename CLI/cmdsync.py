#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
cmdsync.py

Скрипт синхронизации каталогов для Windows с поддержкой NTFS.
Цель: Зеркалирование или обновление каталогов с сохранением атрибутов NTFS (ACL, ADS, длинные пути).
"""

import os
import sys
import shutil
import argparse
import hashlib
import time
import threading
from pathlib import Path
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import pywintypes # Требуется: pip install pywin32
import win32security # Требуется: pip install pywin32
import win32file # Требуется: pip install pywin32
import ntfsutils.streams as streams # Требуется: pip install ntfsutils
# import pyshadowcopy # Комментарий: библиотека может потребовать ручной установки или быть устаревшей. Пример VSS ниже использует win32com.client.

# --- Вспомогательные функции ---

def get_file_info(filepath):
    """Получает размер и время модификации файла."""
    stat = filepath.stat()
    return stat.st_size, stat.st_mtime

def calculate_hash(filepath, chunk_size=8192):
    """Вычисляет SHA256 хеш файла."""
    hash_sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()
    except (IOError, OSError):
        logging.error(f"Не удалось прочитать файл для хеширования: {filepath}")
        return None

def is_file_locked(filepath):
    """Проверяет, заблокирован ли файл (простая проверка)."""
    try:
        with open(filepath, 'r+b'):
            pass
        return False
    except (IOError, OSError):
        return True

def copy_file_with_retry(src, dst, retries=3, delay=1):
    """Копирует файл с повторными попытками при ошибках."""
    for attempt in range(retries):
        try:
            # Копирование данных файла
            shutil.copy2(src, dst)
            logging.info(f"Скопирован файл: {src} -> {dst}")
            return True
        except (IOError, OSError) as e:
            logging.warning(f"Ошибка копирования (попытка {attempt + 1}): {src} -> {dst}, Ошибка: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                logging.error(f"Не удалось скопировать файл после {retries} попыток: {src}")
                return False
    return False

def copy_ntfs_ads(src_path, dst_path):
    """Копирует альтернативные потоки данных (ADS) из исходного файла в целевой."""
    try:
        # Получаем список потоков для исходного файла
        stream_list = list(streams.list_streams(src_path))
        logging.debug(f"ADS для {src_path}: {stream_list}")

        for stream_name in stream_list:
            if stream_name == ':$DATA':  # Пропускаем основной поток данных
                continue
            # Формируем путь к потоку для чтения
            src_stream_path = f"{src_path}{stream_name}"
            # Формируем путь к потоку для записи
            dst_stream_path = f"{dst_path}{stream_name}"

            # Читаем содержимое потока
            with open(src_stream_path, 'rb') as src_stream:
                data = src_stream.read()

            # Записываем содержимое потока в целевой файл
            with open(dst_stream_path, 'wb') as dst_stream:
                dst_stream.write(data)
            logging.info(f"Скопирован ADS: {src_stream_path} -> {dst_stream_path}")

    except Exception as e:
        logging.error(f"Ошибка копирования ADS для {src_path}: {e}")

def copy_ntfs_acl(src_path, dst_path):
    """Копирует списки контроля доступа (ACL) из исходного файла/папки в целевой."""
    try:
        # Получаем дескриптор безопасности исходного объекта
        sd_src = win32security.GetFileSecurity(
            str(src_path), 
            win32security.DACL_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION
        )
        # Устанавливаем дескриптор безопасности на целевой объект
        win32security.SetFileSecurity(
            str(dst_path),
            win32security.DACL_SECURITY_INFORMATION | win32security.GROUP_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION,
            sd_src
        )
        logging.info(f"Скопированы ACL: {src_path} -> {dst_path}")
    except Exception as e:
        logging.error(f"Ошибка копирования ACL для {src_path}: {e}")

def get_long_path(path_str):
    """Преобразует путь в формат, поддерживающий длинные пути."""
    # Проверяем, является ли путь UNC
    if path_str.startswith('\\\\'):
        # UNC путь: \\server\share\path -> \\?\UNC\server\share\path
        return f"\\\\?\\UNC\\{path_str[2:]}"
    else:
        # Обычный путь: C:\path -> \\?\C:\path
        return f"\\\\?\\{path_str}"

# --- Основная логика синхронизации ---

class NTFSSync:
    def __init__(self, source, destination, mode='update', threads=1, dry_run=False):
        self.source = Path(source).resolve()
        self.destination = Path(destination).resolve()
        self.mode = mode
        self.threads = threads
        self.dry_run = dry_run
        self.changed_files = []
        self.deleted_files = []
        self.locked_files = []

        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )

        if self.dry_run:
            logging.info("РЕЖИМ ПРОБНОГО ЗАПУСКА (dry-run) - изменения не будут применены.")

        # Проверка путей
        if not self.source.exists():
            logging.critical(f"Исходный каталог не существует: {self.source}")
            sys.exit(1)
        if not self.destination.exists():
            if not self.dry_run:
                self.destination.mkdir(parents=True, exist_ok=True)
            logging.info(f"Целевой каталог создан: {self.destination}")

    def compare_directories(self):
        """Сравнивает содержимое каталогов и определяет файлы для синхронизации."""
        logging.info(f"Сравнение каталогов '{self.source}' и '{self.destination}'...")
        source_files = {f.relative_to(self.source): f for f in self.source.rglob('*') if f.is_file()}
        dest_files = {f.relative_to(self.destination): f for f in self.destination.rglob('*') if f.is_file()}

        for rel_path, src_file in source_files.items():
            dst_file = self.destination / rel_path

            if rel_path not in dest_files:
                # Файл существует в источнике, но отсутствует в цели
                self.changed_files.append((src_file, dst_file))
                logging.debug(f"Новый файл: {rel_path}")
            else:
                # Файл существует и там, и там - проверяем размер и время модификации
                src_size, src_mtime = get_file_info(src_file)
                dst_size, dst_mtime = get_file_info(dest_files[rel_path])
                if src_size != dst_size or abs(src_mtime - dst_mtime) > 1: # Допуск 1 секунда
                    self.changed_files.append((src_file, dst_file))
                    logging.debug(f"Измененный файл: {rel_path}")

        if self.mode == 'mirror':
            # В режиме зеркалирования определяем файлы для удаления
            for rel_path, dst_file in dest_files.items():
                if rel_path not in source_files:
                    self.deleted_files.append(dst_file)
                    logging.debug(f"Файл для удаления: {rel_path}")

    def sync_file(self, src_file, dst_file):
        """Однопоточная функция для синхронизации одного файла."""
        try:
            # Преобразуем пути для поддержки длинных имен
            long_dst_file = get_long_path(str(dst_file))
            long_dst_parent = get_long_path(str(dst_file.parent))

            if not self.dry_run:
                # Создаем родительские каталоги, если не существуют
                os.makedirs(long_dst_parent, exist_ok=True)

            # Проверяем, заблокирован ли файл
            if is_file_locked(src_file):
                logging.warning(f"Файл заблокирован, пропуск: {src_file}")
                self.locked_files.append(src_file)
                return # Пока пропускаем, VSS не реализован

            # Копируем файл
            if not self.dry_run:
                success = copy_file_with_retry(src_file, long_dst_file)
                if not success:
                    return # Ошибка копирования, прерываем обработку этого файла
            else:
                logging.info(f"[DRY-RUN] Будет скопирован файл: {src_file} -> {long_dst_file}")

            # Копируем NTFS ACL
            if not self.dry_run:
                copy_ntfs_acl(src_file, long_dst_file)
            else:
                logging.info(f"[DRY-RUN] Будут скопированы ACL: {src_file} -> {long_dst_file}")

            # Копируем NTFS ADS
            if not self.dry_run:
                copy_ntfs_ads(src_file, long_dst_file)
            else:
                logging.info(f"[DRY-RUN] Будут скопированы ADS: {src_file} -> {long_dst_file}")

        except Exception as e:
            logging.error(f"Ошибка синхронизации файла {src_file}: {e}")

    def run_sync(self):
        """Выполняет основной процесс синхронизации."""
        self.compare_directories()

        logging.info(f"Найдено {len(self.changed_files)} файлов для синхронизации.")
        logging.info(f"Найдено {len(self.deleted_files)} файлов для удаления (режим mirror).")
        logging.info(f"Найдено {len(self.locked_files)} заблокированных файлов.")

        if self.dry_run:
            logging.info("Пробный запуск завершен.")
            return

        # Синхронизация файлов
        if self.changed_files:
            logging.info("Начало копирования файлов...")
            with ThreadPoolExecutor(max_workers=self.threads) as executor:
                # Создаем задачи для пула потоков
                future_to_file = {
                    executor.submit(self.sync_file, src, dst): (src, dst)
                    for src, dst in self.changed_files
                }
                # Ждем завершения всех задач
                for future in as_completed(future_to_file):
                    src, dst = future_to_file[future]
                    try:
                        future.result() # Проверяем исключения
                    except Exception as exc:
                        logging.error(f'Файл {src} вызвал ошибку: {exc}')

        # Удаление файлов (только в режиме mirror)
        if self.mode == 'mirror' and self.deleted_files:
            logging.info("Начало удаления файлов...")
            for file_to_delete in self.deleted_files:
                long_file_to_delete = get_long_path(str(file_to_delete))
                try:
                    os.remove(long_file_to_delete)
                    logging.info(f"Удален файл: {long_file_to_delete}")
                except Exception as e:
                    logging.error(f"Ошибка удаления файла {long_file_to_delete}: {e}")

        logging.info("Синхронизация завершена.")

def main():
    parser = argparse.ArgumentParser(description="Скрипт синхронизации каталогов с поддержкой NTFS (ntfs_sync)")
    parser.add_argument("source", help="Путь к исходному каталогу")
    parser.add_argument("destination", help="Путь к целевому каталогу")
    parser.add_argument("--mode", choices=['update', 'mirror'], default='update',
                        help="Режим синхронизации: update (по умолчанию) или mirror")
    parser.add_argument("--threads", type=int, default=1,
                        help="Количество потоков для копирования файлов (по умолчанию 1)")
    parser.add_argument("--dry-run", action='store_true',
                        help="Выполнить пробный запуск без внесения изменений")

    args = parser.parse_args()

    sync_engine = NTFSSync(args.source, args.destination, mode=args.mode, threads=args.threads, dry_run=args.dry_run)
    sync_engine.run_sync()

    # Возвращаем код завершения
    # 0 - успех, 1 - ошибка, 2 - есть заблокированные файлы
    if sync_engine.locked_files:
        sys.exit(2)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()