# -*- coding: utf-8 -*-
"""
Рабочее место тендерного отдела: доска статусов и управление тендерами (ТЗ 3.8).

Запуск в PyCharm: правой кнопкой -> Run 'manage'. Аргументы не нужны — всё через
меню в консоли. Данные берутся из tenders.db, которая наполняется при импорте тендеров.

Что умеет:
  - показать доску (сколько тендеров в каждом статусе);
  - показать список тендеров выбранного статуса;
  - сменить статус тендера (новый -> на рассмотрении -> в работе -> выиграли/...).

Статус живёт отдельно от импорта: повторная загрузка тендеров не сбросит его.
"""

from __future__ import annotations

from storage import (
    connect, pipeline_counts, list_by_status, set_status, get_tender,
    STATUS_LABELS, STATUS_ORDER, DEFAULT_STATUS,
)


def show_board(conn) -> None:
    counts = pipeline_counts(conn)
    print("\n" + "=" * 44)
    print("ДОСКА ТЕНДЕРОВ")
    print("=" * 44)
    for s in STATUS_ORDER:
        print(f"  {STATUS_LABELS[s]:<18} {counts[s]:>4}")
    print("-" * 44)
    print(f"  {'ИТОГО':<18} {sum(counts.values()):>4}")


def print_tender_line(i: int, t: dict) -> None:
    price = f"{t['price_rub']:,} ₽" if t.get("price_rub") else "— ₽"
    print(f"  {i:>2}. [{str(t.get('score', '?')):>3}] {(t.get('title') or '')[:62]}")
    print(f"       {price} | {t.get('region') or '—'} | дедлайн: {t.get('deadline') or '—'}")


def _pick_status(prompt: str = "Номер статуса (пусто — отмена): ") -> str | None:
    for i, s in enumerate(STATUS_ORDER, 1):
        print(f"  {i}. {STATUS_LABELS[s]}")
    raw = input(prompt).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(STATUS_ORDER):
        return STATUS_ORDER[int(raw) - 1]
    return None


def action_list(conn) -> None:
    print("\nКакой статус показать?")
    status = _pick_status("Номер: ")
    if not status:
        print("Отмена.")
        return
    items = list_by_status(conn, status, limit=30)
    if not items:
        print(f"В статусе «{STATUS_LABELS[status]}» пусто.")
        return
    print(f"\n— {STATUS_LABELS[status]} ({len(items)}) —")
    for i, t in enumerate(items, 1):
        print_tender_line(i, t)


def action_change(conn) -> None:
    # Чаще всего разбираем «Новые» — показываем их и даём выбрать по номеру.
    items = list_by_status(conn, DEFAULT_STATUS, limit=30)
    if items:
        print(f"\nНовые тендеры (топ по баллу):")
        for i, t in enumerate(items, 1):
            print_tender_line(i, t)
        raw = input("\nНомер из списка (или впиши tender_id): ").strip()
        tid = items[int(raw) - 1]["tender_id"] if (raw.isdigit() and 1 <= int(raw) <= len(items)) else raw
    else:
        tid = input("Новых нет. Впиши номер тендера (tender_id): ").strip()

    t = get_tender(conn, tid)
    if not t:
        print(f"Тендер {tid} не найден.")
        return

    print(f"\nВыбран: [{t.get('score')}] {t.get('title')}")
    print(f"Текущий статус: {STATUS_LABELS.get(t.get('status'), t.get('status'))}")
    print("\nНовый статус:")
    status = _pick_status()
    if not status:
        print("Отмена.")
        return
    note = input("Комментарий (необязательно, Enter — пропустить): ").strip() or None

    res = set_status(conn, tid, status, note)
    if res["ok"]:
        old = STATUS_LABELS.get(res["old"], res["old"])
        print(f"Готово: {old} -> {STATUS_LABELS[res['new']]}")
    else:
        print("Ошибка:", res["error"])


def main() -> None:
    conn = connect()
    print("Рабочее место тендеров. Данные из tenders.db.")
    while True:
        show_board(conn)
        print("\nМеню:")
        print("  1. Показать тендеры по статусу")
        print("  2. Изменить статус тендера")
        print("  0. Выход")
        choice = input("Выбор: ").strip()
        if choice == "1":
            action_list(conn)
        elif choice == "2":
            action_change(conn)
        elif choice == "0":
            break
        else:
            print("Не понял, попробуй ещё раз.")
    conn.close()
    print("Готово, до встречи!")


if __name__ == "__main__":
    main()
