"""Совместимый импорт интеграции с Excel-выгрузками Контур.Закупок."""

from integrations import kontur_excel as _implementation

globals().update({name: getattr(_implementation, name) for name in dir(_implementation)
                  if not name.startswith("__")})
