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
import re
from pathlib import Path
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import pywintypes # Требуется: pip install pywin32
import win32security # Требуется: pip install pywin32
import win32file # Требуется: pip install pywin32
# import ntfsutils.streams as streams # Требуется: pip install ntfsutils
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
        # Проверка на чтение/запись может не всегда отражать блокировку другими процессами.
        # Более надежные методы требуют win32 API.
        # Эта проверка является базовой.
        with open(filepath, 'r+b'):
            pass
        return False
    except (IOError, OSError):
        return True

def safe_copy_file_with_retry(src_path, dst_path, max_retries=3, initial_delay=1, backoff_factor=2, mtime_tolerance=1, verify_hash=False):
    """
    Отказоустойчиво копирует файл с повторными попытками и использованием временного файла.

    Args:
        src_path (Path): Путь к исходному файлу.
        dst_path (str): Путь к целевому файлу (в формате длинного пути).
        max_retries (int): Максимальное количество попыток.
        initial_delay (float): Начальная задержка между попытками.
        backoff_factor (float): Множитель для экспоненциального увеличения задержки.
        mtime_tolerance (float): Допуск для сравнения времени модификации.
        verify_hash (bool): Проверять ли целостность хешем после копирования.

    Returns:
        bool: True, если копирование прошло успешно, иначе False.
    """
    # Преобразуем строку в Path для использования методов Path
    dst_path_obj = Path(dst_path)
    temp_dst_path = dst_path_obj.with_name(dst_path_obj.name + '.NTFSSync_tmp')

    for attempt in range(max_retries):
        try:
            # 1. Копируем данные файла во временный файл
            shutil.copy2(src_path, temp_dst_path)
            logging.debug(f"Скопированы данные в временный файл: {temp_dst_path}")

            # 2. Копируем ACL
            copy_ntfs_acl(src_path, temp_dst_path)

            # 3. Копируем ADS
            copy_ntfs_ads(src_path, temp_dst_path)

            # 4. Проверка целостности (опционально)
            if verify_hash:
                src_hash = calculate_hash(src_path)
                dst_hash = calculate_hash(temp_dst_path)
                if src_hash is None or dst_hash is None or src_hash != dst_hash:
                    raise OSError(f"Проверка хеша не пройдена для временного файла: {temp_dst_path}")

            # 5. Атомарно заменяем целевой файл временным
            # os.replace также работает со строками путей
            os.replace(temp_dst_path, dst_path)
            logging.info(f"Файл успешно скопирован: {src_path} -> {dst_path}")
            return True

        except (IOError, OSError, pywintypes.error) as e:
            logging.warning(f"Ошибка копирования (попытка {attempt + 1}): {src_path} -> {dst_path}, Ошибка: {e}")
            # Удаляем временный файл, если он остался после ошибки
            if temp_dst_path.exists():
                try:
                    temp_dst_path.unlink()
                    logging.debug(f"Временный файл удален после ошибки: {temp_dst_path}")
                except OSError as unlink_err:
                    logging.warning(f"Не удалось удалить временный файл {temp_dst_path}: {unlink_err}")

            if attempt < max_retries - 1:
                delay = initial_delay * (backoff_factor ** attempt)
                logging.debug(f"Ожидание {delay:.2f} секунд перед повторной попыткой...")
                time.sleep(delay)
            else:
                logging.error(f"Не удалось скопировать файл после {max_retries} попыток: {src_path} -> {dst_path}")
        except Exception as e:
            logging.error(f"Неожиданная ошибка при копировании {src_path} -> {dst_path}: {e}")
            # Удаляем временный файл, если он остался
            if temp_dst_path.exists():
                try:
                    temp_dst_path.unlink()
                    logging.debug(f"Временный файл удален после ошибки: {temp_dst_path}")
                except OSError as unlink_err:
                    logging.warning(f"Не удалось удалить временный файл {temp_dst_path}: {unlink_err}")
            return False

    return False

def copy_ntfs_ads(src_path, dst_path):
    """
    Копирует альтернативные потоки данных (ADS) из исходного файла в целевой, используя win32file.
    """
    try:
        # Используем str() для передачи пути в win32file, если dst_path - строка
        # Если dst_path - Path, str() всё равно преобразует его в строку
        src_str = str(src_path)
        dst_str = str(dst_path)

        # Используем FindFirstStreamW для перечисления потоков
        handle = win32file.FindFirstStreamW(src_str, win32file.StreamInfoTypes.FindStreamInfoStandard)
        streams_found = []
        while True:
            stream_name, stream_size = handle[0], handle[1]
            # Основной поток имеет имя ':$DATA', его пропускаем
            if stream_name != ':$DATA':
                clean_name = stream_name
                streams_found.append(clean_name)
            try:
                handle = win32file.FindNextStreamW(handle)
            except pywintypes.error:
                break
        win32file.FindClose(handle)

        logging.debug(f"ADS для {src_path}: {streams_found}")
        for stream_name in streams_found:
            # Формируем путь к потоку для чтения и записи, используя синтаксис ':stream_name'
            src_stream_path = f"{src_str}{stream_name}" # e.g., C:\path\file.txt:stream_name
            dst_stream_path = f"{dst_str}{stream_name}" # e.g., D:\path\file.txt:stream_name
            # Читаем содержимое потока
            with open(src_stream_path, 'rb') as src_stream:
                data = src_stream.read()
            # Записываем содержимое потока в целевой файл
            with open(dst_stream_path, 'wb') as dst_stream:
                dst_stream.write(data)
            logging.debug(f"Скопирован ADS: {src_stream_path} -> {dst_stream_path}")
    except pywintypes.error as e:
        # win32file может генерировать pywintypes.error
        logging.error(f"Ошибка win32file при работе с ADS для {src_path}: {e}")
    except Exception as e:
        logging.error(f"Ошибка копирования ADS для {src_path}: {e}")

def copy_ntfs_acl(src_path, dst_path):
    """
    Копирует списки контроля доступа (ACL) из исходного файла/папки в целевой.

    Args:
        src_path (Path): Путь к исходному файлу/папке.
        dst_path (Path): Путь к целевому файлу/папке.
    """
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
        logging.debug(f"Скопированы ACL: {src_path} -> {dst_path}")
    except Exception as e:
        logging.error(f"Ошибка копирования ACL для {src_path}: {e}")

def get_long_path(path_str):
    """
    Преобразует путь в формат, поддерживающий длинные пути.

    Args:
        path_str (str): Входной путь.

    Returns:
        str: Путь с префиксом для длинных имен.
    """
    # Проверяем, является ли путь UNC
    if path_str.startswith('\\\\'):
        # UNC путь: \\server\share\path -> \\?\UNC\server\share\path
        return f"\\\\?\\UNC\\{path_str[2:]}"
    else:
        # Обычный путь: C:\path -> \\?\C:\path
        return f"\\\\?\\{path_str}"

def matches_any_pattern(path, patterns):
    """
    Проверяет, соответствует ли путь хотя бы одному из шаблонов.

    Args:
        path (Path): Путь к файлу или папке.
        patterns (list): Список шаблонов (glob-паттернов).

    Returns:
        bool: True, если путь соответствует хотя бы одному шаблону.
    """
    for pattern in patterns:
        # Используем PurePath.match для проверки паттерна относительно конца пути
        if path.match(pattern):
            return True
    return False

def safe_remove_with_retry(file_path, max_retries=3, initial_delay=1, backoff_factor=2):
    """
    Безопасно удаляет файл с повторными попытками при ошибках блокировки.

    Args:
        file_path (str): Путь к файлу для удаления (в формате длинного пути).
        max_retries (int): Максимальное количество попыток.
        initial_delay (float): Начальная задержка между попытками.
        backoff_factor (float): Множитель для экспоненциального увеличения задержки.

    Returns:
        bool: True, если удаление прошло успешно, иначе False.
    """
    # os.remove работает со строками, так что Path не обязателен здесь,
    # но если вы хотите использовать Path для каких-то проверок, можно:
    # file_path_obj = Path(file_path)
    # или просто используйте file_path как строку, как раньше.

    for attempt in range(max_retries):
        try:
            # os.remove принимает строку
            os.remove(file_path)
            logging.info(f"Файл успешно удален: {file_path}")
            return True
        except (OSError, pywintypes.error) as e:
            logging.warning(f"Ошибка удаления файла (попытка {attempt + 1}): {file_path}, Ошибка: {e}")
            if attempt < max_retries - 1:
                delay = initial_delay * (backoff_factor ** attempt)
                logging.debug(f"Ожидание {delay:.2f} секунд перед повторной попыткой удаления...")
                time.sleep(delay)
            else:
                logging.error(f"Не удалось удалить файл после {max_retries} попыток: {file_path}")
                return False
        except Exception as e:
            logging.error(f"Неожиданная ошибка при удалении файла {file_path}: {e}")
            return False
    return False

# --- Основная логика синхронизации ---

class NTFSSync:
    """
    Класс для синхронизации каталогов с поддержкой NTFS.
    """
    def __init__(self, source, destination, mode='update', threads=1, dry_run=False, log_file=None, use_vss=False, max_retries=3, initial_delay=1, backoff_factor=2, mtime_tolerance=1, exclude_patterns=None, verify_hash=False):
        """
        Инициализирует объект синхронизации.

        Args:
            source (str): Путь к исходному каталогу.
            destination (str): Путь к целевому каталогу.
            mode (str): Режим синхронизации ('update' или 'mirror').
            threads (int): Количество потоков для копирования.
            dry_run (bool): Режим пробного запуска.
            log_file (str, optional): Путь к файлу лога.
            use_vss (bool): Использовать VSS для заблокированных файлов (заглушка).
            max_retries (int): Максимальное количество попыток копирования/удаления.
            initial_delay (float): Начальная задержка между попытками.
            backoff_factor (float): Множитель для экспоненциального увеличения задержки.
            mtime_tolerance (float): Допуск для сравнения времени модификации.
            exclude_patterns (list): Список шаблонов для исключения файлов/папок.
            verify_hash (bool): Проверять ли целостность хешем после копирования.
        """
        self.source = Path(source).resolve()
        self.destination = Path(destination).resolve()
        self.mode = mode
        self.threads = threads
        self.dry_run = dry_run
        self.use_vss = use_vss
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.backoff_factor = backoff_factor
        self.mtime_tolerance = mtime_tolerance
        self.verify_hash = verify_hash
        self.exclude_patterns = exclude_patterns or []

        self.changed_files = []
        self.deleted_files = []
        self.locked_files = []
        self.error_files = []

        # Настройка логирования
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        handlers = [logging.StreamHandler(sys.stdout)] # Логирование в консоль по умолчанию

        if log_file:
            # Добавляем FileHandler, если указан путь к файлу
            file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
            file_handler.setFormatter(logging.Formatter(log_format))
            handlers.append(file_handler)

        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=handlers,
            force=True  # Перезаписывает предыдущие настройки логирования
        )

        if self.dry_run:
            logging.info("РЕЖИМ ПРОБНОГО ЗАПУСКА (dry-run) - изменения не будут применены.")
        if self.use_vss:
            logging.info("Опциональная поддержка VSS включена (реализация отсутствует).")

        # Проверка путей
        if not self.source.exists():
            logging.critical(f"Исходный каталог не существует: {self.source}")
            sys.exit(1)
        if not self.destination.exists():
            if not self.dry_run:
                self.destination.mkdir(parents=True, exist_ok=True)
            logging.info(f"Целевой каталог создан: {self.destination}")

    def compare_directories(self):
        """
        Сравнивает содержимое каталогов и определяет файлы для синхронизации и удаления.
        """
        logging.info(f"Сравнение каталогов '{self.source}' и '{self.destination}'...")
        # Собираем файлы из источника
        source_files = {}
        for f in self.source.rglob('*'):
            if f.is_file():
                rel_path = f.relative_to(self.source)
                if not matches_any_pattern(rel_path, self.exclude_patterns):
                    source_files[rel_path] = f
                else:
                    logging.debug(f"Исключен из синхронизации: {f}")

        # Собираем файлы из цели
        dest_files = {}
        if self.destination.exists():
            for f in self.destination.rglob('*'):
                if f.is_file():
                    rel_path = f.relative_to(self.destination)
                    if not matches_any_pattern(rel_path, self.exclude_patterns):
                        dest_files[rel_path] = f
                    else:
                        logging.debug(f"Исключен из цели: {f}")

        # Определяем файлы для копирования/обновления
        for rel_path, src_file in source_files.items():
            dst_file = self.destination / rel_path
            if rel_path not in dest_files:
                # Файл существует в источнике, но отсутствует в цели
                self.changed_files.append((src_file, dst_file))
                logging.debug(f"Новый файл: {rel_path}")
            else:
                # Файл существует и там, и там - проверяем размер и время модификации
                try:
                    src_size, src_mtime = get_file_info(src_file)
                    dst_size, dst_mtime = get_file_info(dest_files[rel_path])
                    if src_size != dst_size or abs(src_mtime - dst_mtime) > self.mtime_tolerance:
                        self.changed_files.append((src_file, dst_file))
                        logging.debug(f"Измененный файл: {rel_path}")
                except OSError as e:
                    logging.warning(f"Не удалось получить информацию о файле, будет скопирован: {src_file} или {dest_files[rel_path]}. Ошибка: {e}")
                    self.changed_files.append((src_file, dst_file))

        if self.mode == 'mirror':
            # В режиме зеркалирования определяем файлы для удаления
            for rel_path, dst_file in dest_files.items():
                if rel_path not in source_files:
                    self.deleted_files.append(dst_file)
                    logging.debug(f"Файл для удаления: {rel_path}")

    def sync_file(self, src_file, dst_file):
        """
        Однопоточная функция для синхронизации одного файла.

        Args:
            src_file (Path): Путь к исходному файлу.
            dst_file (Path): Путь к целевому файлу.
        """
        try:
            # Проверка, что src_file - файл
            if not src_file.is_file():
                 logging.warning(f"Источник не является файлом, пропуск: {src_file}")
                 return

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

            # Копируем файл с повторными попытками и безопасностью
            if not self.dry_run:
                success = safe_copy_file_with_retry(
                    src_file, long_dst_file,
                    max_retries=self.max_retries,
                    initial_delay=self.initial_delay,
                    backoff_factor=self.backoff_factor,
                    mtime_tolerance=self.mtime_tolerance,
                    verify_hash=self.verify_hash
                )
                if not success:
                    logging.error(f"Файл не был скопирован после всех попыток: {src_file}")
                    self.error_files.append(src_file)
                    return # Ошибка копирования, прерываем обработку этого файла
            else:
                logging.info(f"[DRY-RUN] Будет скопирован файл: {src_file} -> {long_dst_file}")

        except Exception as e:
            logging.error(f"Ошибка синхронизации файла {src_file}: {e}")
            self.error_files.append(src_file)

    def run_sync(self):
        """
        Выполняет основной процесс синхронизации.
        """
        self.compare_directories()
        logging.info(f"Найдено {len(self.changed_files)} файлов для синхронизации.")
        logging.info(f"Найдено {len(self.deleted_files)} файлов для удаления (режим mirror).")
        logging.info(f"Найдено {len(self.locked_files)} заблокированных файлов.")
        logging.info(f"Найдено {len(self.error_files)} файлов с ошибками копирования.")

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
                        self.error_files.append(src) # Добавляем в список ошибок

        # Удаление файлов (только в режиме mirror)
        if self.mode == 'mirror' and self.deleted_files:
            logging.info("Начало удаления файлов...")
            for file_to_delete in self.deleted_files:
                long_file_to_delete = get_long_path(str(file_to_delete))
                try:
                    if not self.dry_run:
                        success = safe_remove_with_retry(
                            long_file_to_delete,
                            max_retries=self.max_retries,
                            initial_delay=self.initial_delay,
                            backoff_factor=self.backoff_factor
                        )
                        if not success:
                            self.error_files.append(file_to_delete)
                    else:
                        logging.info(f"[DRY-RUN] Будет удален файл: {long_file_to_delete}")
                except Exception as e:
                    logging.error(f"Ошибка удаления файла {long_file_to_delete}: {e}")
                    self.error_files.append(file_to_delete) # Считаем это ошибкой

        # Удаление пустых директорий (дополнительно для mirror)
        if self.mode == 'mirror' and not self.dry_run:
            logging.info("Начало очистки пустых директорий в целевом каталоге...")
            # Обход в обратном порядке (от листьев к корню)
            # Сначала собираем все подкаталоги цели
            all_dirs = []
            for root, dirs, files in os.walk(self.destination, topdown=False):
                 for dir_name in dirs:
                     dir_path = Path(root) / dir_name
                     all_dirs.append(dir_path)

            # Теперь удаляем пустые директории
            for dir_path in all_dirs:
                try:
                    # Проверяем, пуста ли директория (только файлы/папки, игнорируя системные)
                    if not any(dir_path.iterdir()):
                        dir_path.rmdir()
                        logging.info(f"Удалена пустая директория: {dir_path}")
                    else:
                        logging.debug(f"Директория не пуста, пропущена: {dir_path}")
                except OSError as e:
                    logging.warning(f"Не удалось удалить пустую директорию {dir_path}: {e}")

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
    parser.add_argument("--log-file", type=str,
                        help="Путь к файлу для записи лога операций")
    parser.add_argument("--use-vss", action='store_true',
                        help="Попытаться использовать VSS для заблокированных файлов (реализация отсутствует)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Максимальное количество попыток копирования/удаления файла (по умолчанию 3)")
    parser.add_argument("--initial-delay", type=float, default=1.0,
                        help="Начальная задержка между попытками копирования/удаления (по умолчанию 1.0 с)")
    parser.add_argument("--backoff-factor", type=float, default=2.0,
                        help="Множитель для экспоненциального увеличения задержки (по умолчанию 2.0)")
    parser.add_argument("--mtime-tolerance", type=float, default=1.0,
                        help="Допуск для сравнения времени модификации файлов (по умолчанию 1.0 с)")
    parser.add_argument("--exclude", action='append', default=[],
                        help="Шаблон для исключения файлов/папок (например, '*.tmp'). Можно указать несколько раз.")
    parser.add_argument("--verify-hash", action='store_true',
                        help="Проверять целостность скопированных файлов с помощью хеша SHA256.")

    args = parser.parse_args()

    sync_engine = NTFSSync(
        source=args.source,
        destination=args.destination,
        mode=args.mode,
        threads=args.threads,
        dry_run=args.dry_run,
        log_file=args.log_file,
        use_vss=args.use_vss,
        max_retries=args.max_retries,
        initial_delay=args.initial_delay,
        backoff_factor=args.backoff_factor,
        mtime_tolerance=args.mtime_tolerance,
        exclude_patterns=args.exclude,
        verify_hash=args.verify_hash
    )
    sync_engine.run_sync()

    # Возвращаем код завершения
    # 0 - успех, 1 - ошибка (включая удаление), 2 - есть заблокированные файлы
    if sync_engine.error_files:
        sys.exit(1)
    elif sync_engine.locked_files:
        sys.exit(2)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()