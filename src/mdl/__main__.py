"""允许 `python -m mdl` 直接跑 CLI。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
