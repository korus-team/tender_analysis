"""Совместимый запуск консольного рабочего места."""

from tools import manage as _implementation

globals().update({name: getattr(_implementation, name) for name in dir(_implementation)
                  if not name.startswith("__")})


if __name__ == "__main__":
    main()
