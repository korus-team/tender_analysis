"""Совместимый импорт сервиса приоритетных компаний."""

from services import priority_companies as _implementation

globals().update({name: getattr(_implementation, name) for name in dir(_implementation)
                  if not name.startswith("__")})
