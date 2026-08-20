# -*- coding: utf-8 -*-
"""Извлечение и анализ пользовательских документов к тендеру.

В SQLite сохраняется структурированный итог; сами файлы хранятся локально,
чтобы пользователь мог открыть источник каждой рекомендации.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree as ET

from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()


ALLOWED_EXTENSIONS = {"txt", "md", "csv", "docx", "pdf"}
MAX_FILES = 20
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_TOTAL_CHARS = 160_000
UPLOAD_ROOT = Path("uploads") / "document_analysis"


class DocumentAnalysisError(ValueError):
    """Ошибка загрузки."""


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1251", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
    except (KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise DocumentAnalysisError("Не удалось прочитать файл DOCX.") from exc
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(f"{ns}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{ns}t"))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise DocumentAnalysisError(
            "Поддержка PDF не установлена. Выполните: pip install -r requirements.txt"
        ) from exc
    try:
        reader = PdfReader(BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pypdf имеет разные исключения для повреждённых PDF
        raise DocumentAnalysisError("Не удалось извлечь текст из PDF. Возможно, это скан без текстового слоя.") from exc


def read_uploads(files) -> tuple[list[dict], str]:
    """Проверяет загруженные файлы и возвращает метаданные и объединённый текст."""
    files = [item for item in files if item and item.filename]
    if not files:
        raise DocumentAnalysisError("Добавьте хотя бы один текстовый документ.")
    if len(files) > MAX_FILES:
        raise DocumentAnalysisError(f"Можно загрузить не более {MAX_FILES} файлов за раз.")

    documents, text_parts, used, total_bytes = [], [], 0, 0
    for item in files:
        filename = item.filename.strip()
        ext = _extension(filename)
        if ext not in ALLOWED_EXTENSIONS:
            raise DocumentAnalysisError(
                "Поддерживаются только форматы TXT, MD, CSV, DOCX и PDF."
            )
        raw = item.read()
        if not raw:
            raise DocumentAnalysisError(f"Файл «{filename}» пустой.")
        if len(raw) > MAX_FILE_BYTES:
            raise DocumentAnalysisError(f"Файл «{filename}» больше 5 МБ.")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_BYTES:
            raise DocumentAnalysisError("Суммарный размер документов не должен превышать 20 МБ.")
        text = _docx_text(raw) if ext == "docx" else _pdf_text(raw) if ext == "pdf" else _decode_text(raw)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            raise DocumentAnalysisError(f"В файле «{filename}» не найден текст.")
        remaining = MAX_TOTAL_CHARS - used
        if remaining <= 0:
            break
        text = text[:remaining]
        used += len(text)
        # _raw нужен только до сохранения после успешного анализа и никогда не
        # попадает в SQLite.
        documents.append({"name": filename, "size": len(raw), "_raw": raw})
        text_parts.append(f"\n\n===== ДОКУМЕНТ: {filename} =====\n{text}")

    if not text_parts:
        raise DocumentAnalysisError("Слишком много текста: не удалось сформировать материал для анализа.")
    return documents, "".join(text_parts)


def document_upload_dir(tender_id: str) -> Path:
    """Папка документов конкретного тендера без небезопасных символов пути."""
    return UPLOAD_ROOT / sha256(tender_id.encode("utf-8")).hexdigest()


def persist_uploads(documents: list[dict], tender_id: str) -> list[dict]:
    """Сохраняет файлы, чтобы ссылки в рекомендациях открывали первоисточник."""
    target = document_upload_dir(tender_id)
    target.mkdir(parents=True, exist_ok=True)
    persisted = []
    for document in documents:
        raw = document.pop("_raw")
        safe_name = secure_filename(document["name"]) or "document"
        stored_name = f"{uuid4().hex}_{safe_name}"
        (target / stored_name).write_bytes(raw)
        persisted.append({**document, "stored_name": stored_name})
    return persisted


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _short(value, limit=180) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    value = value if len(value) <= limit else value[:limit - 1].rstrip(" ,;:") + "…"
    return value[:1].upper() + value[1:] if value else ""


def _sourced_items(items, document_names: list[str], limit: int) -> list[dict]:
    """Нормализует короткие пункты и привязывает их к известному файлу."""
    available = set(document_names)
    result, seen = [], set()
    for item in items or []:
        if isinstance(item, dict):
            text = _short(item.get("text"))
            document = str(item.get("document") or "").strip()
            index = item.get("document_index")
            if isinstance(index, int) and 1 <= index <= len(document_names):
                document = document_names[index - 1]
        else:  # поддержка результатов, созданных до появления ссылок
            text, document = _short(item), ""
        # Совет без проверяемого файла не показываем: иначе ссылка могла бы
        # вести на неверный документ.
        if not text or text in seen or document not in available:
            continue
        seen.add(text)
        result.append({"text": text, "document": document})
    return result[:limit]


def _local_analysis(text: str, include_recommendations: bool, document_names: list[str]) -> dict:
    """Тестирование без API."""
    source = text.lower()
    risks, pitfalls, recommendations = [], [], []
    patterns = [
        (("эквивалент не допускается", "без эквивалента", "оригинального производителя"),
         "Указан конкретный продукт. Проверьте, можно ли предложить аналог."),
        (("единственн", "эксклюзивн", "авторизованн"),
         "Нужны особый статус или авторизация. Это может сузить круг участников."),
        (("аналогичн", "не менее 3", "не менее 5", "опыт исполнения"),
         "Проверьте требования к опыту. Они могут быть слишком узкими."),
        (("обеспечение исполнения", "банковск", "гаранти"),
         "Проверьте сумму гарантии и другие обязательства."),
        (("штраф", "пени", "неустойк"),
         "Есть штрафы. Проверьте их размер и условия."),
        (("срок поставки", "в течение", "календарных дней"),
         "Проверьте, успеет ли команда выполнить работы в срок."),
    ]
    for needles, finding in patterns:
        if any(needle in source for needle in needles):
            risks.append(finding)

    if any(word in source for word in ("товарный знак", "бренд", "модель", "артикул")):
        pitfalls.append("Указаны конкретные названия. Уточните, можно ли предложить аналог.")
    if any(word in source for word in ("по усмотрению заказчика", "вправе отклонить", "без объяснения")):
        pitfalls.append("Заказчик может решать сам. Уточните правила оценки.")
    if not risks:
        risks.append("Явных ограничений не найдено. Всё равно проверьте документы и договор.")
    if not pitfalls:
        pitfalls.append("Проверьте документы, правила оценки и договор до подачи заявки.")

    if include_recommendations:
        source = document_names[0] if document_names else None
        recommendations = [
            {"text": "Покажите, как ваше решение отвечает требованиям заказчика.", "document": source},
            {"text": "Подтвердите сроки, опыт и гарантийные обязательства.", "document": source},
            {"text": "Уточните спорные брендовые и квалификационные требования до подачи.", "document": source},
        ]
    openness = ("Требует дополнительной проверки: локальный режим обнаружил потенциально ограничивающие или финансовые условия."
                if len(risks) > 1 or any("конкретизации" in item for item in pitfalls)
                else "По экспресс‑проверке явных ограничений не найдено; окончательная оценка требует анализа полного комплекта документов.")
    source_index = 1 if document_names else None
    to_records = lambda rows: [{"text": item, "document_index": source_index} for item in _unique(rows)]
    return {
        "risks": _sourced_items(to_records(risks), document_names, 6),
        "pitfalls": _sourced_items(to_records(pitfalls), document_names, 6),
        "recommendations": _sourced_items(recommendations, document_names, 8), "openness": openness,
        "summary": "Экспресс‑анализ выполнен локально; для содержательного ИИ‑анализа добавьте ключ OpenAI.",
        "analyzer": "local",
    }


def _openai_analysis(text: str, include_recommendations: bool, document_names: list[str]) -> dict:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise DocumentAnalysisError("Библиотека OpenAI не установлена. Выполните: pip install -r requirements.txt") from exc
    sources = "\n".join(f"{index}. {name}" for index, name in enumerate(document_names, start=1))
    prompt = f"""Проанализируй документы закупки. Отвечай только валидным JSON без Markdown.
Не делай юридических выводов и не утверждай факт сговора. Отмечай только признаки, основанные на тексте,
и формулируй их как «проверьте» или «может ограничивать конкуренцию».

Нужны поля JSON:
- risks: массив до 6 объектов {{"text": "риск", "document_index": номер файла}};
- pitfalls: массив до 6 объектов {{"text": "подводный камень", "document_index": номер файла}};
- openness: краткая оценка прозрачности закупки и причины;
- summary: одно предложение о главном;
- recommendations: массив до 8 объектов {{"text": "совет", "document_index": номер файла}}.

Пиши простым русским: короткие слова, без канцелярита, юридических терминов и вводных фраз.
Каждый риск, подводный камень и совет — одно простое предложение до 140 символов, с заглавной буквы.
У каждого риска, подводного камня и совета обязательно укажи document_index — номер одного файла-источника из списка ниже.
Не придумывай номер, не указывай страницу и не ссылайся на документ, если в нём нет основания для совета.

СПИСОК ФАЙЛОВ:
{sources}

Особое внимание: точные бренды/модели без эквивалента, закрытые допуски и авторизации, необычно узкий опыт,
дискреционные критерии оценки, короткие сроки, несоразмерные гарантии, штрафы, условия оплаты и поставки.
Рекомендации должны выделять требования, продукты, технологии, компетенции и подтверждения, которые ожидает заказчик.
Если рекомендации не запрошены, верни пустой массив recommendations.
Рекомендации запрошены: {"да" if include_recommendations else "нет"}.

ДОКУМЕНТЫ:{text}"""
    try:
        response = OpenAI().chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Ты внимательный аналитик тендерной документации. Отвечай только JSON."},
                {"role": "user", "content": prompt},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
    except Exception as exc:  # API errors should be shown without breaking the card
        raise DocumentAnalysisError(f"Не удалось получить ИИ-анализ: {exc}") from exc
    result = {
        "risks": _sourced_items(data.get("risks"), document_names, 6),
        "pitfalls": _sourced_items(data.get("pitfalls"), document_names, 6),
        "recommendations": (_sourced_items(data.get("recommendations"), document_names, 8)
                            if include_recommendations else []),
        "openness": _short(data.get("openness") or "Оценка прозрачности не сформирована."),
        "summary": _short(data.get("summary") or "ИИ-анализ документов выполнен."),
        "analyzer": "openai",
    }
    return result


def analyze(text: str, include_recommendations: bool, document_names: list[str]) -> dict:
    return (_openai_analysis(text, include_recommendations, document_names)
            if os.getenv("OPENAI_API_KEY") else _local_analysis(text, include_recommendations, document_names))
