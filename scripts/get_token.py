"""CLI для получения OAuth-токена Яндекс.Музыки через device flow.

Позволяет авторизовать бота на аккаунте Яндекс.Музыки без передачи ему пароля:
код подтверждения вводится на любом другом устройстве (телефон, ноутбук),
поэтому скрипт можно запускать и на headless-сервере по SSH.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from yandex_music import ClientAsync
from yandex_music.device_auth.device_code import DeviceCode
from yandex_music.exceptions import DeviceAuthError, NetworkError, TimedOutError, YandexMusicError

DEFAULT_TIMEOUT = 300
DEFAULT_ENV_FILE = ".env"
ENV_KEY = "YANDEX_MUSIC_TOKEN"


def _on_code(code: DeviceCode) -> None:
    """Печатает пользователю инструкцию по подтверждению кода устройства."""
    print("Для авторизации в Яндекс.Музыке выполните следующие шаги:")
    print(f"  1. Откройте на любом устройстве: {code.verification_url}")
    print(f"  2. Введите код: {code.user_code}")
    print(f"  3. Код действителен {code.expires_in} секунд.")
    print("Ожидание подтверждения...")


def _write_env(env_file: Path, token: str) -> None:
    """Дописывает или обновляет строку YANDEX_MUSIC_TOKEN в указанном .env-файле."""
    lines: list[str] = []
    if env_file.is_file():
        lines = env_file.read_text(encoding="utf-8").splitlines()

    new_line = f"{ENV_KEY}={token}"
    for index, line in enumerate(lines):
        if line.startswith(f"{ENV_KEY}="):
            lines[index] = new_line
            break
    else:
        lines.append(new_line)

    if env_file.parent != Path():
        env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _request_token(wait_seconds: int) -> str:
    """Выполняет device flow авторизации и возвращает access_token."""
    client = ClientAsync()
    token = await client.device_auth(
        on_code=_on_code, timeout=wait_seconds, device_name="Discord Wave Bot"
    )
    return token.access_token


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Получение OAuth-токена Яндекс.Музыки через device flow."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Таймаут ожидания подтверждения в секундах (по умолчанию {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="Записать полученный токен в .env-файл вместо вывода значения в консоль.",
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"Путь к .env-файлу для --write-env (по умолчанию {DEFAULT_ENV_FILE}).",
    )
    return parser.parse_args(argv)


def main() -> int:
    """Точка входа CLI: запускает device flow и выводит либо сохраняет результат."""
    args = _parse_args()

    try:
        token = asyncio.run(_request_token(args.timeout))
    except DeviceAuthError:
        print(
            "Не удалось получить токен: код подтверждения истёк, отклонён "
            "или вышло время ожидания.",
            file=sys.stderr,
        )
        return 1
    except (NetworkError, TimedOutError):
        print(
            "Сетевая ошибка при обращении к Яндекс.Музыке. Проверьте соединение и повторите.",
            file=sys.stderr,
        )
        return 1
    except YandexMusicError as exc:
        print(f"Ошибка Яндекс.Музыки при авторизации: {exc}", file=sys.stderr)
        return 1

    if args.write_env:
        env_file = Path(args.env_file)
        _write_env(env_file, token)
        print(f"Токен получен и сохранён в файл: {env_file}")
        print("Не публикуйте и не пересылайте этот файл — он содержит секретный токен.")
    else:
        print(f"Токен получен. Добавьте в .env строку: {ENV_KEY}={token}")
        print(
            "ВНИМАНИЕ: никому не передавайте и не публикуйте это значение — "
            "оно даёт полный доступ к вашему аккаунту Яндекс.Музыки."
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
