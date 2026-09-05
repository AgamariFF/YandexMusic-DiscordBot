"""Асинхронная обёртка над неофициальным API Яндекс.Музыки (rotor «Моя волна»)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from yandex_music import ClientAsync
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
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            raise WaveUnavailableError() from exc
        except OSError as exc:
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
        """Сообщает API о старте прослушивания станции волны."""
        client = self._require_client()
        logger.info("Старт станции волны %s", self._station)
        try:
            await client.rotor_station_feedback_radio_started(
                self._station, from_, batch_id=batch_id
            )
        except UnauthorizedError as exc:
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            raise WaveUnavailableError() from exc
        except OSError as exc:
            raise WaveUnavailableError() from exc

    async def fetch_wave_batch(self, queue: str | int | None = None) -> WaveBatch:
        """Запрашивает очередную пачку треков волны."""
        client = self._require_client()
        try:
            result = await client.rotor_station_tracks(self._station, queue=queue)
        except UnauthorizedError as exc:
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            raise WaveUnavailableError() from exc
        except OSError as exc:
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

    async def notify_track_started(self, track_id: str, batch_id: str | None) -> None:
        """Сообщает API о начале воспроизведения трека."""
        client = self._require_client()
        try:
            await client.rotor_station_feedback_track_started(
                self._station, track_id, batch_id=batch_id
            )
        except Exception:
            logger.warning("Не удалось отправить фидбек о старте трека %s", track_id)

    async def notify_track_finished(
        self, track_id: str, played_seconds: float, batch_id: str | None
    ) -> None:
        """Сообщает API об окончании воспроизведения трека."""
        client = self._require_client()
        try:
            await client.rotor_station_feedback_track_finished(
                self._station, track_id, played_seconds, batch_id=batch_id
            )
        except Exception:
            logger.warning("Не удалось отправить фидбек о завершении трека %s", track_id)

    async def notify_track_skipped(
        self, track_id: str, played_seconds: float, batch_id: str | None
    ) -> None:
        """Сообщает API о пропуске трека."""
        client = self._require_client()
        try:
            await client.rotor_station_feedback_skip(
                self._station, track_id, played_seconds, batch_id=batch_id
            )
        except Exception:
            logger.warning("Не удалось отправить фидбек о пропуске трека %s", track_id)

    async def resolve_stream_url(self, track: TrackInfo) -> str:
        """Возвращает прямую ссылку на аудиопоток лучшего доступного качества."""
        self._require_client()
        try:
            infos = await track.raw.get_download_info_async()
        except UnauthorizedError as exc:
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            raise TrackUnavailableError() from exc
        except OSError as exc:
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
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            raise TrackUnavailableError() from exc
        except OSError as exc:
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
