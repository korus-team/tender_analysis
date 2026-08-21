from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositiveExample:
    direction: str
    title: str


POSITIVE_EXAMPLES: tuple[PositiveExample, ...] = (
    PositiveExample(
        "bi_analytics",
        "Запрос на изучение информации (ПКО/RFI) имеющихся на рынке готовых "
        "BI-систем/систем для анализа и визуализации данных.",
    ),
    PositiveExample(
        "data_warehouses",
        "Поставка ПО и работы по пуско-наладке для миграции DWH на MPP платформу",
    ),
    PositiveExample(
        "big_data_platforms",
        "Развитие системы инфраструктуры хранения и автоматизации на кластере "
        "Hadoop Big Data",
    ),
    PositiveExample(
        "master_data",
        "Внедрение единой системы управления нормативно-справочной информацией, "
        "нормализация и интеграция данных",
    ),
    PositiveExample(
        "data_quality",
        "Анализ рынка на выбор платформы Data Quality и команды внедрения",
    ),
    PositiveExample(
        "databases",
        "Поставка лицензий коммерческой версии СУБД на базе PostgreSQL и "
        "технической поддержки",
    ),
    PositiveExample(
        "ai_ml",
        "Создание и внедрение информационной системы «Технологии машинного обучения»",
    ),
    PositiveExample(
        "process_automation",
        "Программное обеспечение для роботизации бизнес-процессов и услуги по "
        "его внедрению",
    ),
    PositiveExample(
        "information_systems",
        "Выбор ИТ-решения для автоматизированного управления процессом обработки "
        "заявок и мониторинга залогов",
    ),
    PositiveExample(
        "data_integration",
        "Проектирование, разработка и внедрение витрины данных для таможенного "
        "мониторинга",
    ),
)
