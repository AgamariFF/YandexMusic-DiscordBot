"""CLI для перехвата трафика официального приложения Яндекс.Музыки через CDP.

Официальный десктопный клиент — это Electron-обёртка над Chromium (в каталоге
установки есть `resources/app.asar` и ресурсы Chromium), поэтому весь его
сетевой обмен виден через Chrome DevTools Protocol, если запустить его с
флагом `--remote-debugging-port=9222` — без прокси и без установки корневого
сертификата. Скрипт подключается к уже запущенному приложению и записывает
его запросы к music.yandex, чтобы понять, как оригинальный клиент общается
с сервером при пропуске трека в «Моей волне» (в первую очередь интересны
идентификаторы сессии/волны, которых может не быть в нашей реализации).

Скрипт только наблюдает: ничего не отправляет от имени пользователя и не
изменяет перехваченный трафик.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import sys
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_TARGETS_URL = f"http://{CDP_HOST}:{CDP_PORT}/json"
CDP_CALL_TIMEOUT = 10
# Пауза между повторными опросами /json в поисках целей, появившихся после старта
# (например, новый worker, созданный приложением уже во время наблюдения).
CDP_POLL_INTERVAL = 3

LOG_FILE = Path("logs/yandex_cdp.jsonl")
FILTER_HOST_SUBSTRING = "music.yandex"
REQUEST_BODY_LIMIT = 4000
RESPONSE_BODY_LIMIT = 20000
CONSOLE_BODY_LIMIT = 200
HIGHLIGHT_MARKERS = ("rotor", "feedback", "session", "wave", "queue")
REDACTED = "<вырезано>"

# Отправка команды CDP и получение ответа на неё по тому же id.
CdpCall = Callable[[str, "dict[str, Any] | None"], Awaitable["dict[str, Any]"]]

# Заголовки, чьё имя стоит скрывать целиком — включает Authorization/Cookie/Set-Cookie.
_SECRET_HEADER_RE = re.compile(r"token|auth|secret|cookie", re.IGNORECASE)
# Ключи JSON-тел, чьи значения стоит скрывать (access_token покрывается подстрокой "token").
_SECRET_KEY_RE = re.compile(r"token|access_token|password|secret", re.IGNORECASE)


def _truncate(text: str, limit: int) -> str:
    """Обрезает текст до limit символов с пометкой.

    Ответы вида station/tracks на волне могут содержать тысячи треков — без обрезки
    один такой обмен раздул бы лог до десятков мегабайт, при этом сам факт обмена
    и его начало важнее полного содержимого.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<обрезано, всего {len(text)} символов>"


def _decode_json_maybe(text: str) -> tuple[bool, Any]:
    """Пытается разобрать текст как JSON, не бросая исключение при неудаче."""
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        return False, None


def _format_body(raw: str | None, content_type: str, limit: int, *, allow_form: bool) -> Any:
    """Готовит тело запроса/ответа к записи: JSON -> объект, форма -> словарь, иначе -> строка.

    Обрезка выполняется до попытки разбора: тело, обрезанное на 4000/20000 символах,
    всё равно не будет валидным JSON целиком, поэтому нет смысла держать в памяти
    и разбирать огромный документ ради результата, который потом всё равно обрежется.
    """
    if raw is None:
        return None
    if raw == "":
        return ""
    if len(raw) > limit:
        return _truncate(raw, limit)

    ok, parsed = _decode_json_maybe(raw)
    if ok:
        return parsed

    if allow_form and "x-www-form-urlencoded" in content_type.lower():
        try:
            return dict(parse_qsl(raw, keep_blank_values=True))
        except ValueError:
            pass

    return raw


def _scrub_secret_keys(value: Any) -> Any:
    """Рекурсивно скрывает значения ключей-секретов во вложенном JSON-теле."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _SECRET_KEY_RE.search(key) else _scrub_secret_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_secret_keys(item) for item in value]
    return value


def _scrub_secret_string(value: Any, secret: str) -> Any:
    """Рекурсивно заменяет во вложенной структуре все вхождения известного токена."""
    if isinstance(value, str):
        return value.replace(secret, REDACTED)
    if isinstance(value, dict):
        return {key: _scrub_secret_string(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_secret_string(item, secret) for item in value]
    return value


def _redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Заменяет значения заголовков-секретов (авторизация, куки, токены) перед записью."""
    return {
        name: REDACTED if _SECRET_HEADER_RE.search(name) else value
        for name, value in headers.items()
    }


def _content_type_from_headers(headers: dict[str, Any]) -> str:
    """Ищет заголовок Content-Type без учёта регистра его имени."""
    for name, value in headers.items():
        if name.lower() == "content-type":
            return str(value)
    return ""


def _console_body_snippet(body: Any) -> str:
    """Готовит короткий фрагмент тела запроса для однострочного вывода в консоль."""
    if body in (None, ""):
        return ""
    try:
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(body)
    if len(text) <= CONSOLE_BODY_LIMIT:
        return text
    return f"{text[:CONSOLE_BODY_LIMIT]}…"


def _emit(record: dict[str, Any], log_path: Path) -> None:
    """Дописывает обмен в JSONL-файл и печатает компактную строку в консоль."""
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    path = record["path"]
    is_interesting = any(marker in path.lower() for marker in HIGHLIGHT_MARKERS)
    prefix = ">>> " if is_interesting else ""
    status = record["status"] if record["status"] is not None else "?"
    snippet = _console_body_snippet(record["request_body"])
    print(f"{prefix}{record['method']} {path} -> {status}  {snippet}")


def _load_yandex_token() -> str | None:
    """Пытается получить текущий токен из конфига проекта для дополнительной вычистки.

    Любая проблема при чтении конфига (нет .env, невалидные переменные) не должна
    мешать работе скрипта наблюдения — в этом случае просто отключаем эту доп. защиту
    и полагаемся только на вычистку заголовков и ключей JSON.
    """
    try:
        from bot.config import load_config

        return load_config().yandex_token or None
    except Exception:
        return None


async def _finalize_exchange(
    entry: dict[str, Any],
    request_id: str,
    call: CdpCall,
    log_path: Path,
    token: str | None,
) -> None:
    """Собирает итоговую запись обмена, вычищает секреты и записывает её."""
    parsed_url = urlsplit(entry["url"])
    host = parsed_url.netloc
    if FILTER_HOST_SUBSTRING not in host.lower():
        return  # чужой хост — не то, ради чего запущен скрипт, полностью игнорируем

    response_body: Any = None
    if entry.get("status") is not None:
        try:
            result = await call("Network.getResponseBody", {"requestId": request_id})
        except Exception:
            # Тело могло уже быть выгружено из буфера CDP или запрос — не HTTP-обмен
            # (например, EventSource) — это не повод останавливать наблюдение.
            result = {}
        if result and result.get("base64Encoded"):
            response_body = "<бинарные данные>"
        elif result:
            response_body = _format_body(
                result.get("body", ""), "", RESPONSE_BODY_LIMIT, allow_form=False
            )

    content_type = _content_type_from_headers(entry["headers"])
    request_body = _format_body(
        entry.get("post_data"), content_type, REQUEST_BODY_LIMIT, allow_form=True
    )
    if isinstance(request_body, dict | list):
        request_body = _scrub_secret_keys(request_body)
    if isinstance(response_body, dict | list):
        response_body = _scrub_secret_keys(response_body)

    record: dict[str, Any] = {
        "timestamp": entry["timestamp"],
        "method": entry["http_method"],
        "host": host,
        "path": parsed_url.path,
        "query": dict(parse_qsl(parsed_url.query, keep_blank_values=True)),
        "request_headers": _redact_headers(entry["headers"]),
        "request_body": request_body,
        "status": entry.get("status"),
        "response_body": response_body,
    }
    if token:
        record = _scrub_secret_string(record, token)

    _emit(record, log_path)


async def _handle_event(
    event: dict[str, Any],
    requests: dict[str, dict[str, Any]],
    call: CdpCall,
    log_path: Path,
    token: str | None,
) -> None:
    """Обновляет накопленное состояние обмена по одному событию домена Network."""
    method = event.get("method")
    params = event.get("params") or {}
    request_id = params.get("requestId")

    if method == "Network.requestWillBeSent" and request_id:
        request = params.get("request") or {}
        requests[request_id] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "http_method": request.get("method", ""),
            "url": request.get("url", ""),
            "headers": request.get("headers") or {},
            "post_data": request.get("postData"),
            "status": None,
        }
        return

    if method == "Network.responseReceived" and request_id in requests:
        response = params.get("response") or {}
        requests[request_id]["status"] = response.get("status")
        return

    if method in ("Network.loadingFinished", "Network.loadingFailed") and request_id:
        entry = requests.pop(request_id, None)
        if entry is not None:
            await _finalize_exchange(entry, request_id, call, log_path, token)


async def _watch_target(
    session: aiohttp.ClientSession,
    target: dict[str, Any],
    log_path: Path,
    token: str | None,
) -> None:
    """Слушает Network-события одной CDP-цели, пока её не отменят или она не оборвётся.

    Заранее не известно, какая из открытых страниц приложения делает запросы к API,
    поэтому наблюдение ведётся ко всем сразу, а ошибка одной цели (например, окно
    закрыли) не должна прерывать наблюдение за остальными — все исключения, кроме
    отмены задачи, гасятся на этом уровне.
    """
    ws_url = target.get("webSocketDebuggerUrl", "")
    title = target.get("title") or target.get("id") or ws_url
    target_type = target.get("type", "?")
    try:
        async with session.ws_connect(ws_url, max_msg_size=0) as ws:
            requests: dict[str, dict[str, Any]] = {}
            pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
            command_id = itertools.count(1)
            event_tasks: set[asyncio.Task[None]] = set()

            async def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                """Отправляет команду CDP и дожидается ответа с тем же id.

                Ответ читает отдельная задача-читатель (см. _read_loop ниже) — call() лишь
                кладёт future в pending и ждёт его. Поэтому вызов из обработчика события
                (через _finalize_exchange) не блокирует чтение сокета и не приводит
                к взаимоблокировке, из-за которой раньше не приходил ответ ни на один запрос.
                """
                cid = next(command_id)
                future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
                pending[cid] = future
                await ws.send_json({"id": cid, "method": method, "params": params or {}})
                try:
                    return await asyncio.wait_for(future, timeout=CDP_CALL_TIMEOUT)
                finally:
                    pending.pop(cid, None)

            def _on_event_task_done(task: asyncio.Task[None]) -> None:
                """Убирает завершённую задачу-обработчик события и логирует её падение.

                Ошибка обработки одного события (например, сбой при чтении тела ответа)
                не должна останавливать наблюдение за остальным трафиком этой цели.
                """
                event_tasks.discard(task)
                if task.cancelled():
                    return
                exc = task.exception()
                if exc is not None:
                    print(
                        f"Обработка события CDP цели {title} упала: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )

            async def _read_loop() -> None:
                """Непрерывно читает сокет: ответы на команды резолвит сама, события — задачами.

                Событие нельзя обрабатывать прямо в этом цикле через await: _handle_event
                сам может вызвать call() и ждать ответа на новую команду, а этот ответ придёт
                через тот же сокет. Если бы читатель был занят ожиданием обработчика, он не
                смог бы прочитать этот ответ, и call() завис бы до CDP_CALL_TIMEOUT — именно
                так раньше терялся и ответ на Network.enable, и тела ответов запросов.
                """
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue

                    if "id" in data:
                        future = pending.get(data["id"])
                        if future and not future.done():
                            future.set_result(data.get("result") or {})
                        continue

                    if "method" in data:
                        task = asyncio.create_task(
                            _handle_event(data, requests, call, log_path, token)
                        )
                        event_tasks.add(task)
                        task.add_done_callback(_on_event_task_done)

            reader = asyncio.create_task(_read_loop())
            try:
                try:
                    await call("Network.enable")
                except TimeoutError:
                    # Обычные (dedicated) worker в этой сборке Chromium не отвечают на
                    # Network.enable — домен Network им не поддерживается. Это ожидаемое
                    # ограничение, а не сбой, поэтому без трейсбека и без остановки других целей.
                    print(
                        f"Цель {title} (тип: {target_type}) не поддерживает домен Network, "
                        "пропускаем."
                    )
                    return
                print(f"Подключено к цели CDP: {title} (тип: {target_type})")
                await reader
            finally:
                # Читатель мог остановиться сам (обрыв соединения) или быть отменённым
                # (Ctrl+C) — в обоих случаях висящие задачи обработки событий больше не
                # получат ответ на свои команды, поэтому их нужно аккуратно отменить,
                # а не бросить недожатыми.
                reader.cancel()
                for task in list(event_tasks):
                    task.cancel()
                await asyncio.gather(reader, *event_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        traceback.print_exc()
        print(
            f"Наблюдение за целью {title} остановлено из-за ошибки: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


async def _list_targets(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """Запрашивает список целей отладки CDP и отбирает все, к которым можно подключиться.

    Раньше отбирался только type == "page", но на живом приложении Яндекс.Музыки
    запросы к API вполне могут уходить из воркера (у запущенного клиента среди семи
    целей — одна page, одна iframe и пять worker), а не только со страницы. Опытным
    путём заранее не проверить, откуда придёт трафик, поэтому слушаем все цели с
    непустым webSocketDebuggerUrl независимо от их типа.
    """
    async with session.get(CDP_TARGETS_URL, timeout=aiohttp.ClientTimeout(total=5)) as response:
        response.raise_for_status()
        targets = await response.json(content_type=None)
    return [t for t in targets if t.get("webSocketDebuggerUrl")]


def _format_type_counts(targets: list[dict[str, Any]]) -> str:
    """Формирует сводку по типам целей вида 'page: 1, iframe: 1, worker: 5' для вывода."""
    counts = Counter(target.get("type", "?") for target in targets)
    return ", ".join(f"{target_type}: {count}" for target_type, count in sorted(counts.items()))


async def _poll_new_targets(
    session: aiohttp.ClientSession,
    known_ids: set[str],
    watch_tasks: set[asyncio.Task[None]],
    log_path: Path,
    token: str | None,
) -> None:
    """Раз в CDP_POLL_INTERVAL секунд ищет цели, появившиеся после старта, и подключается к ним.

    Приложение может создать новый worker уже во время наблюдения (например, в ответ
    на действие пользователя) — без повторного опроса такая цель осталась бы
    незамеченной до перезапуска скрипта. Уже наблюдаемые цели различаем по id и
    повторно не подключаем. Работает, пока задачу не отменят (Ctrl+C).
    """
    while True:
        await asyncio.sleep(CDP_POLL_INTERVAL)
        try:
            targets = await _list_targets(session)
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Приложение могло временно не ответить на /json — это не повод прекращать
            # наблюдение за уже подключёнными целями, пробуем снова на следующем цикле.
            print(
                f"Повторный опрос целей CDP не удался: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

        for target in targets:
            target_id = target.get("id")
            if target_id is None or target_id in known_ids:
                continue
            known_ids.add(target_id)
            title = target.get("title") or target_id
            print(f"Подхвачена новая цель CDP: {title} (тип: {target.get('type', '?')})")
            task = asyncio.create_task(_watch_target(session, target, log_path, token))
            watch_tasks.add(task)
            task.add_done_callback(watch_tasks.discard)


def _ensure_log_dir(log_path: Path) -> None:
    """Создаёт директорию для лог-файла, если её ещё нет."""
    log_path.parent.mkdir(parents=True, exist_ok=True)


def _print_intro(log_path: Path) -> None:
    """Печатает памятку при старте: куда пишется лог и как остановить наблюдение."""
    print("Перехват трафика Яндекс.Музыки через Chrome DevTools Protocol.")
    print(f"  Запросы к music.yandex будут записаны в: {log_path}")
    print("  Приложение должно быть запущено с флагом --remote-debugging-port=9222.")
    print("  Остановка наблюдения — Ctrl+C.")
    print()


def _print_no_app_hint() -> None:
    """Печатает подсказку, если не удалось подключиться к отладочному порту CDP."""
    print(
        "Не удалось подключиться к 127.0.0.1:9222 — похоже, Яндекс.Музыка не запущена "
        "с отладочным портом.\n"
        "Закройте приложение и запустите его заново с флагом отладки, например:\n"
        '  "%LOCALAPPDATA%\\Programs\\YandexMusic\\Яндекс Музыка.exe" '
        "--remote-debugging-port=9222",
        file=sys.stderr,
    )


async def _run(log_path: Path) -> int:
    """Подключается ко всем целям приложения (включая воркеры) и наблюдает за трафиком до отмены."""
    token = _load_yandex_token()
    _ensure_log_dir(log_path)
    _print_intro(log_path)

    async with aiohttp.ClientSession() as session:
        try:
            targets = await _list_targets(session)
        except (aiohttp.ClientError, TimeoutError):
            _print_no_app_hint()
            return 1

        if not targets:
            print(
                "Не найдено ни одной цели с доступным WebSocket-адресом отладки — похоже, "
                "окно приложения ещё не открыто.",
                file=sys.stderr,
            )
            return 1

        print(f"Найдено целей для наблюдения: {len(targets)} ({_format_type_counts(targets)})\n")

        known_ids: set[str] = {t["id"] for t in targets if t.get("id") is not None}
        watch_tasks: set[asyncio.Task[None]] = set()
        for target in targets:
            task = asyncio.create_task(_watch_target(session, target, log_path, token))
            watch_tasks.add(task)
            task.add_done_callback(watch_tasks.discard)

        # Повторный опрос /json — отдельная задача, живущая параллельно наблюдению за
        # уже найденными целями, пока пользователь не остановит скрипт по Ctrl+C.
        poll_task = asyncio.create_task(
            _poll_new_targets(session, known_ids, watch_tasks, log_path, token)
        )
        await asyncio.gather(poll_task, *watch_tasks)
    return 0


def main() -> int:
    """Точка входа CLI: запускает наблюдение до Ctrl+C."""
    return asyncio.run(_run(LOG_FILE))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
