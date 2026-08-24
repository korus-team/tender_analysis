from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositiveExample:
    direction: str
    task: str
    title: str


POSITIVE_EXAMPLES: tuple[PositiveExample, ...] = (
    PositiveExample(
        "bi_analytics",
        "selection",
        "Запрос на изучение информации (ПКО/RFI) имеющихся на рынке готовых "
        "BI-систем/систем для анализа и визуализации данных.",
    ),
    PositiveExample(
        "bi_analytics",
        "development",
        "Оказание услуг по разработке дашбордов на базе платформы Visiology",
    ),
    PositiveExample(
        "bi_analytics",
        "licensing_support",
        "Закупка лицензии и технической поддержки Fine BI",
    ),
    PositiveExample(
        "bi_analytics",
        "consulting",
        "Консультационные услуги по развитию системы корпоративной отчётности "
        "и операционной модели финансовой функции",
    ),
    PositiveExample(
        "bi_analytics",
        "training",
        "Обучение персонала работе с платформой FineBI",
    ),
    PositiveExample(
        "data_warehouses",
        "migration_implementation",
        "Поставка ПО и работы по пуско-наладке для миграции DWH на MPP платформу",
    ),
    PositiveExample(
        "data_warehouses",
        "support",
        "Техническая поддержка информационной системы «Корпоративное хранилище "
        "данных»",
    ),
    PositiveExample(
        "data_warehouses",
        "development",
        "Модификация Siebel CRM и DWH в составе информационно-аналитической "
        "системы",
    ),
    PositiveExample(
        "big_data_platforms",
        "development",
        "Развитие системы инфраструктуры хранения и автоматизации на кластере "
        "Hadoop Big Data",
    ),
    PositiveExample(
        "big_data_platforms",
        "support",
        "Безвендорная техническая поддержка ClickHouse в подсистеме обработки "
        "и хранения данных платформы ЕХД",
    ),
    PositiveExample(
        "big_data_platforms",
        "selection_implementation",
        "Выбор партнёра на поставку и внедрение программного обеспечения для "
        "замены существующей инфраструктуры Big Data",
    ),
    PositiveExample(
        "master_data",
        "implementation",
        "Внедрение единой системы управления нормативно-справочной информацией, "
        "нормализация и интеграция данных",
    ),
    PositiveExample(
        "master_data",
        "selection",
        "Анализ рынка российского ПО для замены системы SAP MDM",
    ),
    PositiveExample(
        "master_data",
        "licensing",
        "Предоставление права использования системы управления мастер-данными "
        "Universe MDM",
    ),
    PositiveExample(
        "master_data",
        "development_implementation",
        "Модификация и внедрение системы управления мастер-данными Universe MDM",
    ),
    PositiveExample(
        "data_quality",
        "selection_implementation",
        "Анализ рынка на выбор платформы Data Quality и команды внедрения",
    ),
    PositiveExample(
        "databases",
        "licensing_support",
        "Поставка лицензий коммерческой версии СУБД на базе PostgreSQL и "
        "технической поддержки",
    ),
    PositiveExample(
        "databases",
        "support",
        "Поддержка СУБД",
    ),
    PositiveExample(
        "ai_ml",
        "development_implementation",
        "Создание и внедрение информационной системы «Технологии машинного обучения»",
    ),
    PositiveExample(
        "ai_ml",
        "development",
        "Развитие информационной системы прогнозирования товарного спроса",
    ),
    PositiveExample(
        "ai_ml",
        "development",
        "Разработка ИИ-ассистента для нормативно-справочной информации",
    ),
    PositiveExample(
        "ai_ml",
        "implementation",
        "Сервис интеллектуальной видеоаналитики для производства готовой еды",
    ),
    PositiveExample(
        "process_automation",
        "implementation",
        "Программное обеспечение для роботизации бизнес-процессов и услуги по "
        "его внедрению",
    ),
    PositiveExample(
        "process_automation",
        "licensing",
        "Закупка лицензий Sherpa RPA (Robot + Orchestrator)",
    ),
    PositiveExample(
        "process_automation",
        "implementation",
        "Внедрение RPA и IDP под ключ",
    ),
    PositiveExample(
        "process_automation",
        "support",
        "Настройка и техническая поддержка RPA",
    ),
    PositiveExample(
        "information_systems",
        "selection_implementation",
        "Выбор ИТ-решения для автоматизированного управления процессом обработки "
        "заявок и мониторинга залогов",
    ),
    PositiveExample(
        "information_systems",
        "implementation",
        "Автоматизация жизненного цикла работника и создание единой HR-экосистемы "
        "с интеграцией в 1С:ЗУП",
    ),
    PositiveExample(
        "information_systems",
        "implementation",
        "Внедрение автоматизированной системы управления нормативно-технической "
        "документацией",
    ),
    PositiveExample(
        "data_integration",
        "development_implementation",
        "Проектирование, разработка и внедрение витрины данных для таможенного "
        "мониторинга",
    ),
    PositiveExample(
        "data_integration",
        "implementation",
        "Внедрение корпоративного хранилища и шины данных",
    ),
    PositiveExample(
        "data_integration",
        "migration",
        "Миграция данных из систем-источников в DWH для построения отчётности",
    ),
)
