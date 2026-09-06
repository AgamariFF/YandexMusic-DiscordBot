"""Аддон mitmproxy для перехвата трафика официального приложения Яндекс.Музыки.

Разовый диагностический инструмент: нужен, чтобы понять, как десктопное
приложение Яндекс.Музыки (Windows, Electron) общается с сервером при пропуске
трека в «Моей волне» — есть ли там идентификатор сессии волны или иной
механизм подстройки, которого нет в клиенте бота. Перехватывается собственный
трафик пользователя на его же машине и с его же аккаунтом — аддон только
наблюдает и пишет лог, ничего не отправляет и не подменяет в трафике.

Установка и запуск (mitmproxy не входит в requirements проекта — ставится
отдельно только для этого разового разбора):
    pip install mitmproxy
    mitmdump -q -s scripts/capture_yandex_traffic.py

Дальше нужно направить трафик приложения Яндекс.Музыки через прокси mitmproxy
(обычно 127.0.0.1:8080) и установить сертификат mitmproxy как доверенный —
это делается штатными средствами mitmproxy (см. http://mitm.it после запуска
mitmdump), сам аддон эту настройку не выполняет.

Каждый перехваченный обмен запрос/ответ дописывается отдельной строкой в
JSONL-файл logs/yandex_traffic.jsonl. Токен доступа и прочие секреты
вычищаются перед печатью в консоль и перед записью в файл — см. функции
`_redact_headers`, `_redact_json` и `_scrub_token_occurrences` ниже.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

# Импортируем типы mitmproxy только для проверки типов: сам файл должен
# оставаться читаемым и компилируемым линтером/py_compile даже там, где
# mitmproxy не установлен (он нужен только тому интерпретатору, которым
# реально запускают mitmdump).
if TYPE_CHECKING:
    from mitmproxy import http

LOG_PATH = Path("logs/yandex_traffic.jsonl")

MAX_REQUEST_BODY_CHARS = 4000
MAX_RESPONSE_BODY_CHARS = 20000
CONSOLE_PREVIEW_CHARS = 200

REDACTED = "<вырезано>"

# Любой хост, содержащий эту подстроку, считаем относящимся к Яндекс.Музыке
# (покрывает и api.music.yandex.net, и music.yandex.ru, и прочие поддомены).
_TARGET_HOST_MARKER = "music.yandex"

# Подстроки в имени заголовка (без учёта регистра), при которых значение
# вырезаем целиком. Покрывает в том числе явно упомянутые Authorization,
# Cookie, Set-Cookie и X-Yandex-Music-Access-Token — все они содержат одну
# из этих подстрок.
_SENSITIVE_HEADER_MARKERS = ("token", "auth", "secret", "cookie")

# Подстроки в имени ключа JSON (без учёта регистра), при которых значение
# вырезаем рекурсивно внутри тел запроса/ответа.
_SENSITIVE_JSON_KEY_MARKERS = ("token", "password", "secret")

# Пути с этими подстроками — то, ради чего затевался перехват (ротор волны,
# фидбек по треку, сессии) — в консоли их отмечаем отдельно.
_INTERESTING_PATH_MARKERS = ("rotor", "feedback", "session", "wave")


def _is_target_host(host: str) -> bool:
    """Проверяет, относится ли хост к Яндекс.Музыке."""
    return _TARGET_HOST_MARKER in host.lower()


def _is_interesting_path(path: str) -> bool:
    """Проверяет, стоит ли пометить обмен как «интересный» в консоли."""
    lowered = path.lower()
    return any(marker in lowered for marker in _INTERESTING_PATH_MARKERS)


def _is_sensitive_header(name: str) -> bool:
    """Проверяет, нужно ли вырезать значение заголовка по имени."""
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE_HEADER_MARKERS)


def _is_sensitive_json_key(key: str) -> bool:
    """Проверяет, нужно ли вырезать значение ключа JSON по имени."""
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_JSON_KEY_MARKERS)


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Возвращает копию заголовков с вырезанными секретными значениями (правило 1)."""
    return {
        name: (REDACTED if _is_sensitive_header(name) else value) for name, value in headers.items()
    }


def _redact_json(value: Any) -> Any:
    """Рекурсивно вырезает значения секретных ключей в JSON-теле (правило 3).

    Нечувствителен к типу: словари и списки обходятся рекурсивно, всё
    остальное (строки, числа, None) возвращается как есть.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _is_sensitive_json_key(key)
                else _redact_json(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _scrub_token_occurrences(value: Any, token: str) -> Any:
    """Рекурсивно заменяет буквальные вхождения секретного токена (правило 2).

    Применяется поверх уже собранной записи целиком (заголовки, query,
    тела) — на случай, если токен утёк в поле, не покрытое вырезанием по
    имени (например, в подписанную ссылку внутри тела ответа).
    """
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, REDACTED) if token in value else value
    if isinstance(value, dict):
        return {key: _scrub_token_occurrences(val, token) for key, val in value.items()}
    if isinstance(value, list):
        return [_scrub_token_occurrences(item, token) for item in value]
    return value


def _load_token_to_scrub() -> str:
    """Пытается получить токен Яндекс.Музыки из .env проекта для доп. вычистки.

    Нужен только для правила 2 — вырезания буквальных вхождений токена вне
    заголовков. Заголовки в любом случае вычищаются по имени (правило 1)
    независимо от результата этой функции. Любая ошибка чтения конфига
    (нет .env, не заданы остальные обязательные переменные и т.п.) означает,
    что мы просто продолжаем без этого дополнительного шага.
    """
    try:
        from bot.config import load_config

        return load_config().yandex_token
    except Exception:
        return ""


def _parse_body(raw: bytes, content_type: str, limit: int) -> Any:
    """Готовит тело запроса/ответа к записи: JSON — объектом, форма — словарём.

    Если раскодированный текст длиннее `limit` символов, парсинг не делаем
    (обрубок JSON всё равно был бы невалиден) — вместо этого обрезаем сырой
    текст и явно помечаем, что запись усечена.
    """
    if not raw:
        return ""

    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        return f"{text[:limit]}…[обрезано, исходно {len(text)} симв.]"

    content_type = content_type.lower()
    if "application/x-www-form-urlencoded" in content_type:
        return dict(parse_qsl(text, keep_blank_values=True))

    stripped = text.strip()
    if "json" in content_type or stripped.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return text


def _short_preview(body: Any, limit: int = CONSOLE_PREVIEW_CHARS) -> str:
    """Готовит короткое превью тела запроса для однострочного вывода в консоль."""
    if not body:
        return ""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


class YandexTrafficCapture:
    """Аддон mitmproxy: пишет обмены с Яндекс.Музыкой в JSONL и в консоль."""

    def __init__(self) -> None:
        """Готовит файл лога, определяет секрет для доп. вычистки, печатает памятку."""
        self._log_path = LOG_PATH
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._token = _load_token_to_scrub()
        self._print_banner()

    def _print_banner(self) -> None:
        """Печатает при старте, куда пишется лог и что нужно для перехвата."""
        print("Перехват трафика Яндекс.Музыки запущен.")
        print(f"Записи дописываются в файл: {self._log_path.resolve()}")
        print(
            "Чтобы трафик приложения попадал сюда: направьте его через прокси "
            "mitmproxy (обычно 127.0.0.1:8080) и установите сертификат "
            "mitmproxy как доверенный (см. http://mitm.it после запуска "
            "mitmdump). Саму настройку прокси/сертификата этот скрипт не делает."
        )

    def response(self, flow: http.HTTPFlow) -> None:
        """Хук mitmproxy: вызывается после получения полного ответа на запрос."""
        if not _is_target_host(flow.request.pretty_host):
            return
        try:
            record = self._build_record(flow)
            self._write_record(record)
            self._print_line(record)
        except Exception as exc:
            # Один странный обмен не должен обрывать перехват всей сессии.
            print(f"[capture_yandex_traffic] Ошибка обработки обмена: {exc}")

    def _build_record(self, flow: http.HTTPFlow) -> dict[str, Any]:
        """Собирает одну (уже вычищенную) запись JSONL по обмену запрос/ответ."""
        request = flow.request
        response = flow.response

        request_body = _redact_json(
            _parse_body(
                request.content or b"",
                request.headers.get("content-type", ""),
                MAX_REQUEST_BODY_CHARS,
            )
        )

        status_code: int | None = None
        response_body: Any = ""
        if response is not None:
            status_code = response.status_code
            response_body = _redact_json(
                _parse_body(
                    response.content or b"",
                    response.headers.get("content-type", ""),
                    MAX_RESPONSE_BODY_CHARS,
                )
            )

        record: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "method": request.method,
            "host": request.pretty_host,
            "path": urlsplit(request.path).path,
            "query": dict(request.query),
            "request_headers": _redact_headers(dict(request.headers)),
            "request_body": request_body,
            "status_code": status_code,
            "response_body": response_body,
        }

        if self._token:
            record = _scrub_token_occurrences(record, self._token)
        return record

    def _write_record(self, record: dict[str, Any]) -> None:
        """Дописывает запись отдельной строкой в JSONL-файл лога."""
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _print_line(self, record: dict[str, Any]) -> None:
        """Печатает компактную строку по обмену, выделяя интересные пути."""
        marker = "★ " if _is_interesting_path(record["path"]) else "  "
        line = f"{marker}{record['method']} {record['path']} -> {record['status_code']}"
        preview = _short_preview(record.get("request_body"))
        if preview:
            line += f"  {preview}"
        print(line)


addons = [YandexTrafficCapture()]
