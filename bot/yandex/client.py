"""Асинхронная обёртка над неофициальным API Яндекс.Музыки (rotor «Моя волна»)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from yandex_music import ClientAsync, StationTracksResult
from yandex_music.exceptions import UnauthorizedError, YandexMusicError

from bot.errors import TrackUnavailableError, WaveUnavailableError, YandexAuthError

logger = logging.getLogger(__name__)

WAVE_STATION_ID = "user:onyourwave"
DEFAULT_WAVE_FROM = "desktop_win-home-playlist_of_the_day-default"


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """Доменное представление трека волны."""

    id: str
    title: str
    artists: str
    duration: float
    raw: Any

    @property
    def display(self) -> str:
        """Строка вида «Артист — Название [3:45]» (без длительности, если она неизвестна)."""
        base = f"{self.artists} — {self.title}"
        if self.duration <= 0:
            return base
        total_seconds = int(self.duration)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{base} [{minutes}:{seconds:02d}]"


@dataclass(frozen=True, slots=True)
class WaveBatch:
    """Пачка треков волны с идентификатором батча для фидбека."""

    batch_id: str | None
    tracks: tuple[TrackInfo, ...]


class YandexMusicClient:
    """Асинхронная обёртка над неофициальным API Яндекс.Музыки (rotor «Моя волна»)."""

    def __init__(self, token: str, *, station: str = WAVE_STATION_ID) -> None:
        """Запоминает токен Яндекса и идентификатор станции волны."""
        self._token = token
        self._station = station
        self._client: ClientAsync | None = None
        self._feedback_warned = False

    @property
    def station(self) -> str:
        """Идентификатор станции rotor, используемой сессией."""
        return self._station

    @property
    def connected(self) -> bool:
        """Признак того, что клиент инициализирован и готов к запросам."""
        return self._client is not None

    async def connect(self) -> None:
        """Создаёт и инициализирует клиент Яндекс.Музыки."""
        logger.info("Подключение к API Яндекс.Музыки")
        client = ClientAsync(self._token)
        try:
            await client.init()
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось авторизоваться в Яндекс.Музыке: %s: %s", type(exc).__name__, exc
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось подключиться к Яндекс.Музыке: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось подключиться к Яндекс.Музыке: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        self._client = client
        logger.info("Подключение к API Яндекс.Музыки установлено")

    def _require_client(self) -> ClientAsync:
        """Возвращает активный клиент либо бросает WaveUnavailableError."""
        if self._client is None:
            raise WaveUnavailableError(
                user_message="«Моя волна» не подключена. Попробуйте позже."
            )
        return self._client

    async def start_wave(
        self, *, from_: str = DEFAULT_WAVE_FROM, batch_id: str | None = None
    ) -> None:
        """Уведомляет API о старте прослушивания станции волны.

        Фидбек о старте станции необязателен: сервер может отклонить его
        (например, ошибкой «condition is not met»), но треки волны при этом
        всё равно получаются и воспроизводятся, поэтому неудача этого
        фидбека не считается фатальной и не прерывает запуск волны.
        Отсутствие подключения к клиенту — самостоятельная ошибка состояния
        и по-прежнему приводит к `WaveUnavailableError`. Протухший токен
        (`UnauthorizedError`) тоже остаётся фатальным: иначе пользователь
        увидел бы «волна недоступна» и не узнал, что токен пора перевыпустить.
        """
        self._require_client()
        logger.info("Старт станции волны %s", self._station)
        try:
            await self._send_feedback("radioStarted", batch_id=batch_id, from_=from_)
        except UnauthorizedError as exc:
            logger.error(
                "Токен Яндекса отклонён при старте станции волны: %s: %s",
                type(exc).__name__,
                exc,
            )
            raise YandexAuthError() from exc
        except Exception as exc:
            self._log_feedback_failure("старт станции волны", exc)

    async def fetch_wave_batch(self, queue: str | int | None = None) -> WaveBatch:
        """Запрашивает очередную пачку треков волны."""
        client = self._require_client()
        try:
            # rotor_station_tracks() библиотеки перезаписывает params вместо
            # обновления: при переданном queue параметр settings2 теряется,
            # хотя официальные клиенты всегда шлют его вместе с queue. Поэтому
            # запрос собирается здесь напрямую, с обоими параметрами сразу.
            url = f"{client.base_url}/rotor/station/{self._station}/tracks"
            params: dict[str, Any] = {"settings2": "True"}
            if queue is not None:
                params["queue"] = queue
            raw_result = await client._request.get(url, params)
            result = StationTracksResult.de_json(raw_result, client)
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось получить пачку треков волны: %s: %s", type(exc).__name__, exc
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось получить пачку треков волны: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось получить пачку треков волны: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc

        if result is None or not result.sequence:
            logger.debug("Получена пустая пачка треков волны")
            return WaveBatch(batch_id=result.batch_id if result is not None else None, tracks=())

        tracks: list[TrackInfo] = []
        for item in result.sequence:
            track = item.track
            if track is None or track.available is False:
                continue
            artists = ", ".join(a.name for a in track.artists) or "Неизвестный исполнитель"
            duration = (track.duration_ms / 1000) if track.duration_ms else 0.0
            tracks.append(
                TrackInfo(
                    id=str(track.id),
                    title=track.title,
                    artists=artists,
                    duration=duration,
                    raw=track,
                )
            )

        logger.info("Получена пачка треков волны: %d шт.", len(tracks))
        return WaveBatch(batch_id=result.batch_id, tracks=tuple(tracks))

    def _log_feedback_failure(self, what: str, exc: Exception) -> None:
        """Логирует неудачу фидбека: первую заметно, последующие — на DEBUG.

        Сервер может отвергать фидбек постоянно (например, ошибкой
        «condition is not met»), а фидбек уходит на каждый трек — warning
        на каждый из них сделал бы лог непригодным для диагностики.
        """
        if self._feedback_warned:
            logger.debug("Не удалось отправить фидбек (%s): %s: %s", what, type(exc).__name__, exc)
            return
        self._feedback_warned = True
        logger.warning(
            "Не удалось отправить фидбек (%s): %s: %s. "
            "Воспроизведению это не мешает; дальнейшие неудачи фидбека — на уровне DEBUG",
            what,
            type(exc).__name__,
            exc,
        )

    async def _send_feedback(
        self,
        type_: str,
        *,
        batch_id: str | None = None,
        track_id: str | None = None,
        total_played_seconds: float | None = None,
        from_: str | None = None,
    ) -> None:
        """Отправляет фидбек rotor напрямую в формате JSON.

        Библиотека `yandex-music` отправляет тело фидбека как
        `application/x-www-form-urlencoded`, а сервер Яндекс.Музыки принимает
        только JSON и на форму отвечает `400 condition is not met` на любой
        тип фидбека. Поэтому запрос собирается здесь вручную, но выполняется
        через приватный `_request` библиотечного клиента — он несёт
        авторизацию и заголовки, менять нужно только тело и его кодирование.
        """
        client = self._require_client()
        url = f"{client.base_url}/rotor/station/{self._station}/feedback"
        params = {"batch-id": batch_id} if batch_id else {}
        payload: dict[str, Any] = {"type": type_, "timestamp": datetime.now().timestamp()}
        if track_id is not None:
            payload["trackId"] = track_id
        if total_played_seconds is not None:
            payload["totalPlayedSeconds"] = total_played_seconds
        if from_ is not None:
            payload["from"] = from_
        logger.debug("Отправка фидбека rotor: type=%s track_id=%s", type_, track_id)
        await client._request.post(url, params=params, json=payload)

    async def notify_track_started(self, track_id: str, batch_id: str | None) -> None:
        """Сообщает API о начале воспроизведения трека."""
        try:
            await self._send_feedback("trackStarted", track_id=track_id, batch_id=batch_id)
        except Exception as exc:
            self._log_feedback_failure(f"старт трека {track_id}", exc)

    async def notify_track_finished(
        self, track_id: str, played_seconds: float, batch_id: str | None
    ) -> None:
        """Сообщает API об окончании воспроизведения трека."""
        try:
            await self._send_feedback(
                "trackFinished",
                track_id=track_id,
                total_played_seconds=played_seconds,
                batch_id=batch_id,
            )
        except Exception as exc:
            self._log_feedback_failure(f"завершение трека {track_id}", exc)

    async def notify_track_skipped(
        self, track_id: str, played_seconds: float, batch_id: str | None
    ) -> None:
        """Сообщает API о пропуске трека."""
        try:
            await self._send_feedback(
                "skip",
                track_id=track_id,
                total_played_seconds=played_seconds,
                batch_id=batch_id,
            )
        except Exception as exc:
            self._log_feedback_failure(f"пропуск трека {track_id}", exc)

    async def resolve_stream_url(self, track: TrackInfo) -> str:
        """Возвращает прямую ссылку на аудиопоток лучшего доступного качества."""
        self._require_client()
        try:
            infos = await track.raw.get_download_info_async()
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось получить ссылку на поток трека %s: %s: %s",
                track.id,
                type(exc).__name__,
                exc,
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось получить ссылку на поток трека %s: %s: %s",
                track.id,
                type(exc).__name__,
                exc,
            )
            raise TrackUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось получить ссылку на поток трека %s: %s: %s",
                track.id,
                type(exc).__name__,
                exc,
            )
            raise TrackUnavailableError() from exc

        candidates = [info for info in infos if not info.preview]
        if not candidates:
            raise TrackUnavailableError()

        mp3_candidates = [info for info in candidates if info.codec == "mp3"]
        pool = mp3_candidates or candidates
        best = max(pool, key=lambda info: info.bitrate_in_kbps or 0)

        logger.debug(
            "Выбран поток трека %s: codec=%s bitrate=%s", track.id, best.codec, best.bitrate_in_kbps
        )

        try:
            return await best.get_direct_link_async()
        except UnauthorizedError as exc:
            # Текст исключения может содержать подписанную ссылку — логируем только тип.
            logger.error(
                "Не удалось получить прямую ссылку на трек %s: %s", track.id, type(exc).__name__
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            # Текст исключения может содержать подписанную ссылку — логируем только тип.
            logger.warning(
                "Не удалось получить прямую ссылку на трек %s: %s", track.id, type(exc).__name__
            )
            raise TrackUnavailableError() from exc
        except OSError as exc:
            # Текст исключения может содержать подписанную ссылку — логируем только тип.
            logger.warning(
                "Не удалось получить прямую ссылку на трек %s: %s", track.id, type(exc).__name__
            )
            raise TrackUnavailableError() from exc

    async def close(self) -> None:
        """Идемпотентно освобождает внутренний клиент."""
        if self._client is None:
            return
        try:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()
        except Exception:
            logger.warning("Ошибка при закрытии клиента Яндекс.Музыки", exc_info=True)
        finally:
            self._client = None
