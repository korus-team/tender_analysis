# -*- coding: utf-8 -*-
"""Небольшие общие проверки для данных, поступающих извне приложения."""
from __future__ import annotations

import os
import secrets
import time
import zipfile
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(ValueError):
    """Архив отклонён до передачи библиотеке-парсеру."""


def load_or_create_secret_key(project_root: Path) -> str:
    """Возвращает стабильный секрет Flask, не храня его в исходном коде."""
    configured = os.getenv("FLASK_SECRET_KEY", "").strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("FLASK_SECRET_KEY должен содержать не менее 32 символов.")
        return configured

    secret_path = project_root / "data" / ".flask_secret_key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = secret_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    if len(existing) >= 32:
        return existing

    generated = secrets.token_urlsafe(48)
    try:
        with secret_path.open("x", encoding="ascii") as secret_file:
            secret_file.write(generated)
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        return generated
    except FileExistsError:
        # При одновременном старте нескольких worker-процессов первый процесс
        # может уже создать файл, но ещё не успеть закончить запись.
        for _ in range(50):
            existing = secret_path.read_text(encoding="ascii").strip()
            if len(existing) >= 32:
                return existing
            time.sleep(0.02)
        raise RuntimeError(
            "Файл data/.flask_secret_key повреждён. Удалите его или задайте FLASK_SECRET_KEY."
        )


def validate_zip_archive(
    source,
    *,
    max_files: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: int = 300,
) -> None:
    """Проверяет ZIP-контейнер до распаковки DOCX/XLSX библиотекой."""
    stream = getattr(source, "stream", source)
    position = None
    if hasattr(stream, "tell"):
        try:
            position = stream.tell()
        except (OSError, ValueError):
            position = None

    try:
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if len(members) > max_files:
                raise UnsafeArchiveError("В архиве слишком много файлов.")

            total_uncompressed = 0
            for member in members:
                if member.flag_bits & 0x1:
                    raise UnsafeArchiveError("Зашифрованные архивы не поддерживаются.")

                normalized = member.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts:
                    raise UnsafeArchiveError("Архив содержит небезопасные пути.")

                total_uncompressed += member.file_size
                if total_uncompressed > max_uncompressed_bytes:
                    raise UnsafeArchiveError("Архив слишком большой после распаковки.")

                if member.file_size:
                    if member.compress_size <= 0:
                        raise UnsafeArchiveError("Некорректный сжатый файл в архиве.")
                    if member.file_size / member.compress_size > max_compression_ratio:
                        raise UnsafeArchiveError("Обнаружена подозрительно высокая степень сжатия.")
    except zipfile.BadZipFile as exc:
        raise UnsafeArchiveError("Файл не является корректным ZIP-контейнером.") from exc
    finally:
        if position is not None and hasattr(stream, "seek"):
            try:
                stream.seek(position)
            except (OSError, ValueError):
                pass
