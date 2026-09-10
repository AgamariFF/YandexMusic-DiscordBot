"""Слой сквозного (DAVE) шифрования для приёма голоса и его диагностика.

С 2 марта 2026 Discord обязателен по DAVE (MLS-based end-to-end encryption)
на всех не-Stage голосовых каналах — отключить его нельзя (уже пробовали,
бот вообще переставал подключаться к голосу, правка откачена). `discord.py`
2.7.1 сам ведёт обмен ключами MLS и поддерживает живую `davey.DaveSession`
(`voice_client._connection.dave_session`), но сам ничего не делает с
входящим звуком — приём голоса целиком на стороне `discord-ext-voice-recv`,
которая про DAVE не знает вообще: она только транспортно расшифровывает RTP
(режимы `xsalsa20_poly1305*`/`aead_xchacha20_poly1305_rtpsize`) и отдаёт
результат дальше как Opus. Если DAVE-сессия активна, то, что приходит после
транспортной расшифровки — это ещё DAVE-зашифрованный Opus, а не сырой,
поэтому `discord.ext.voice_recv` пытается декодировать его как Opus напрямую
и либо получает мусор, либо (что мы и видим в логе) транспортная
расшифровка вовсе падает с `CryptoError`, если согласован не тот режим или
пакет не в том формате, который ожидает `PacketDecryptor` — см. модульный
докстринг `_DaveAwarePacketDecryptor` и `install_dave_decryption` про то,
откуда берётся неопределённость и как её убрать шагом диагностики.

Отправка (для сравнения, см. `discord/voice_client.py::_get_voice_packet`):
    сырой Opus -> dave_session.encrypt_opus(data) -> RTP-заголовок -> транспортное шифрование.
Приём устроен зеркально, и это то, что добавляет данный модуль:
    транспортная расшифровка -> dave_session.decrypt(...) -> сырой Opus.

Точка встраивания
------------------
`discord.ext.voice_recv.reader.AudioReader.__init__` жёстко создаёт
`self.decryptor = PacketDecryptor(voice_client.mode, bytes(voice_client.secret_key))`
и это не параметр и не переопределяемый метод — расширить его, не редактируя
файлы установленного пакета (правка исчезла бы при обновлении зависимостей),
штатно нельзя. Поэтому `install_dave_decryption()` подменяет уже созданный
`voice_client._reader.decryptor` на `_DaveAwarePacketDecryptor` сразу после
`voice_client.listen(...)`: `AudioReader.callback()` каждый раз обращается к
`self.decryptor.decrypt_rtp(packet)` через атрибут заново (не кеширует
ссылку на функцию при старте), так что подмена атрибута уже после `listen()`
подхватывается со следующего же входящего пакета.

Что проверить при обновлении `discord-ext-voice-recv`
-------------------------------------------------------
1. `AudioReader.__init__` всё ещё создаёт `PacketDecryptor` и кладёт его в
   атрибут экземпляра `self.decryptor` (а не, скажем, в приватное имя или
   локальную переменную).
2. `AudioReader.callback()` всё ещё читает `self.decryptor.decrypt_rtp`
   заново при каждом пакете, а не один раз при старте.
3. Сигнатура `PacketDecryptor.__init__(self, mode, secret_key)` и то, что
   она сама кладёт нужную по режиму функцию транспортной расшифровки в
   атрибут экземпляра `self.decrypt_rtp` — на этом основан приём подмены в
   `_DaveAwarePacketDecryptor.__init__` (см. его докстринг).
"""

from __future__ import annotations

import logging
import os

import davey
from discord.ext import voice_recv
from discord.ext.voice_recv import rtp
from discord.ext.voice_recv.reader import PacketDecryptor
from nacl.exceptions import CryptoError

logger = logging.getLogger(__name__)

# Переменная окружения, включающая подробную диагностику приёма голоса
# (см. `_ReceiveDiagnostics`). Выключена по умолчанию, чтобы не засорять лог
# в обычной работе — включается владельцем бота только на время расследования.
_DIAG_ENV_VAR = "VOICE_RECV_DIAG"
_DIAG_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# Сколько первых пакетов логировать целиком и с каким шагом — дальше, чтобы
# диагностика сама не залила лог так же, как сейчас его заливает CryptoError.
_DIAG_BURST_PACKETS = 5
_DIAG_SAMPLE_EVERY = 200

# Раз в сколько суммарных сбоев печатать сводку статистики — независимо от
# флага подробной диагностики, чтобы массовые сбои расшифровки были видны в
# логе всегда, а не только при включённом VOICE_RECV_DIAG.
_STATS_LOG_EVERY_FAILURES = 100


def diagnostics_enabled() -> bool:
    """Проверяет переменную окружения `VOICE_RECV_DIAG`, включающую подробный лог приёма.

    Читается заново при каждом вызове, без кеширования: к моменту, когда
    вызывающий код (см. `install_dave_decryption`) это проверяет, `.env` уже
    загружен `bot.config.load_config` при старте бота, а на лету значение не
    меняется — кеш тут не даёт выигрыша, а только усложняет код.
    """
    return os.environ.get(_DIAG_ENV_VAR, "").strip().lower() in _DIAG_TRUE_VALUES


class _ReceiveDiagnostics:
    """Ограниченный по объёму подробный лог приёма голоса — включается флагом окружения.

    Печатает первые `_DIAG_BURST_PACKETS` пакетов целиком, затем один из
    каждых `_DIAG_SAMPLE_EVERY` — что именно нужно для диагностики см.
    докстринг задачи: режим шифрования и длину секретного ключа, состояние
    DAVE-сессии, длины пакета/заголовка и признак RTP-расширений, и сам факт
    прошла ли транспортная расшифровка. Двоичные данные целиком никогда не
    логируются — только длины.
    """

    def __init__(self, enabled: bool) -> None:
        """Запоминает, включена ли диагностика, и готовит счётчик пакетов."""
        self._enabled = enabled
        self._packet_index = 0
        self._log_this_packet = False

    def begin_packet(self) -> None:
        """Решает, войдёт ли текущий пакет в лог-выборку. Вызывать один раз в начале обработки."""
        if not self._enabled:
            return
        self._packet_index += 1
        self._log_this_packet = (
            self._packet_index <= _DIAG_BURST_PACKETS
            or self._packet_index % _DIAG_SAMPLE_EVERY == 0
        )

    def log_transport(
        self, voice_client: voice_recv.VoiceRecvClient, packet: rtp.RTPPacket, *, ok: bool
    ) -> None:
        """Логирует режим шифрования, состояние DAVE-сессии и итог транспортной расшифровки."""
        if not self._log_this_packet:
            return
        connection = voice_client._connection
        dave_session = connection.dave_session
        logger.info(
            "voice-diag #%d transport: mode=%s secret_key_len=%d dave_session=%s "
            "can_encrypt=%s protocol_version=%s ready=%s packet_len=%d header_len=%d "
            "extended=%s result=%s",
            self._packet_index,
            voice_client.mode,
            len(bytes(voice_client.secret_key)),
            dave_session is not None,
            connection.can_encrypt,
            connection.dave_protocol_version,
            dave_session.ready if dave_session is not None else None,
            len(packet.header) + len(packet.data),
            len(packet.header),
            packet.extended,
            "ok" if ok else "CryptoError",
        )

    def log_dave(self, packet: rtp.RTPPacket, *, user_id: int | None, outcome: str) -> None:
        """Логирует исход DAVE-этапа расшифровки для уже прошедшего транспорт пакета."""
        if not self._log_this_packet:
            return
        logger.info(
            "voice-diag #%d dave: ssrc=%d user_id=%s outcome=%s",
            self._packet_index,
            packet.ssrc,
            user_id,
            outcome,
        )


class _DecryptStats:
    """Всегда включённые счётчики пропущенных/неудачных пакетов DAVE-расшифровки.

    В отличие от `_ReceiveDiagnostics`, не зависит от флага окружения: это
    компактный предохранитель, чтобы массовые сбои расшифровки было видно в
    логе (пусть и редкой сводкой) даже без включённой подробной диагностики.
    """

    def __init__(self) -> None:
        """Обнуляет все счётчики."""
        self.transport_failures = 0
        self.dave_unknown_ssrc = 0
        self.dave_failures = 0
        self.dave_passthrough_fallbacks = 0

    def note_transport_failure(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Учитывает сбой транспортной расшифровки (CryptoError)."""
        self.transport_failures += 1
        self._maybe_warn(voice_client)

    def note_unknown_ssrc(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Учитывает пакет, для чьего ssrc ещё не пришло сопоставление с user_id."""
        self.dave_unknown_ssrc += 1
        self._maybe_warn(voice_client)

    def note_dave_failure(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Учитывает пакет, DAVE-расшифровка которого не удалась и не является passthrough."""
        self.dave_failures += 1
        self._maybe_warn(voice_client)

    def note_passthrough_fallback(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Учитывает пакет, принятый как открытый Opus по запасному пути passthrough.

        Не считается сбоем (это штатный, предусмотренный протоколом DAVE
        режим на время перехода — см. `set_passthrough_mode` в
        `discord/voice_state.py`), поэтому не входит в порог `_maybe_warn`.
        """
        self.dave_passthrough_fallbacks += 1

    def _maybe_warn(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Раз в `_STATS_LOG_EVERY_FAILURES` суммарных сбоев печатает сводку уровня WARNING."""
        total = self.transport_failures + self.dave_unknown_ssrc + self.dave_failures
        if total and total % _STATS_LOG_EVERY_FAILURES == 0:
            logger.warning(
                "Приём голоса гильдии %s: сбоев расшифровки — транспорт=%d, "
                "неизвестный ssrc=%d, DAVE=%d, passthrough-фолбэк=%d",
                voice_client.guild.id,
                self.transport_failures,
                self.dave_unknown_ssrc,
                self.dave_failures,
                self.dave_passthrough_fallbacks,
            )


class _DaveAwarePacketDecryptor(PacketDecryptor):
    """`PacketDecryptor`, дополненный DAVE-расшифровкой поверх транспортной.

    `PacketDecryptor.__init__` сам выбирает нужную по режиму функцию
    транспортной расшифровки и кладёт её в атрибут экземпляра
    `self.decrypt_rtp` (`self.decrypt_rtp = getattr(self, '_decrypt_rtp_' + mode)`).
    Из-за этого просто переопределить `decrypt_rtp` обычным методом подкласса
    нельзя — `__init__` (унаследованный, мы его не трогаем) всё равно
    перезапишет атрибут экземпляра поверх метода подкласса. Поэтому здесь
    после `super().__init__()` исходная транспортная функция сохраняется в
    `self._decrypt_transport`, а `self.decrypt_rtp` подменяется на обёртку
    `_decrypt_rtp_with_dave`, которая сперва зовёт сохранённую транспортную
    функцию, а затем прогоняет результат через DAVE.
    """

    def __init__(
        self, voice_client: voice_recv.VoiceRecvClient, diagnostics: _ReceiveDiagnostics
    ) -> None:
        """Строит обычный транспортный `PacketDecryptor` и оборачивает его DAVE-слоем."""
        super().__init__(voice_client.mode, bytes(voice_client.secret_key))
        self._voice_client = voice_client
        self._diagnostics = diagnostics
        self._stats = _DecryptStats()
        self._decrypt_transport = self.decrypt_rtp
        self.decrypt_rtp = self._decrypt_rtp_with_dave

    def _decrypt_rtp_with_dave(self, packet: rtp.RTPPacket) -> bytes:
        """Транспортная расшифровка (как в `PacketDecryptor`), затем DAVE поверх неё."""
        self._diagnostics.begin_packet()
        try:
            transport_data = self._decrypt_transport(packet)
        except CryptoError:
            self._diagnostics.log_transport(self._voice_client, packet, ok=False)
            self._stats.note_transport_failure(self._voice_client)
            raise
        self._diagnostics.log_transport(self._voice_client, packet, ok=True)
        return self._apply_dave(packet, transport_data)

    def _apply_dave(self, packet: rtp.RTPPacket, transport_data: bytes) -> bytes:
        """Прогоняет уже транспортно расшифрованные данные через DAVE, если сессия готова.

        Возвращает сырой Opus. Любая ветка отказа (неизвестный ssrc, сбой
        расшифровки без права на passthrough) отдаёт `rtp.OPUS_SILENCE` —
        тот же трёхбайтовый маркер тишины, что штатно распознаёт
        `RTPPacket.is_silence()`, — а не бросает исключение: обёртка
        `AudioReader.callback()` пакета не ждёт, что `decrypt_rtp` может
        сигналить "пропустить" иначе как через успешный (пусть и тихий)
        результат, а бросать исключение здесь означало бы либо потерю
        `packet.decrypted_data` (падение позже при декодировании), либо
        ложный `CryptoError` в логе на месте, где транспорт на самом деле
        отработал верно — задача требует ронять не приём целиком, а только
        содержимое одного пакета.
        """
        connection = self._voice_client._connection
        dave_session = connection.dave_session
        if dave_session is None or not connection.can_encrypt:
            # DAVE-сессия ещё не готова либо недоступна — по тому же
            # условию `can_encrypt`, что и при отправке (см.
            # discord/voice_client.py::_get_voice_packet): раз собеседники
            # сейчас не шифруют исходящий Opus, расшифровывать входящий
            # не нужно, он уже сырой.
            self._diagnostics.log_dave(packet, user_id=None, outcome="no_dave")
            return transport_data

        user_id = self._voice_client._get_id_from_ssrc(packet.ssrc)
        if user_id is None:
            # DAVE привязывает расшифровку к user_id, а RTP-пакет несёт
            # только ssrc; сопоставление ssrc -> user_id приходит отдельным
            # событием голосового шлюза (см. `VoiceRecvClient._add_ssrc`,
            # используется в т.ч. для событий речи) и на первых пакетах
            # только что заговорившего может ещё не успеть прийти.
            # Расшифровать нечем — пропускаем содержимое, а не падаем.
            self._diagnostics.log_dave(packet, user_id=None, outcome="unknown_ssrc")
            self._stats.note_unknown_ssrc(self._voice_client)
            return rtp.OPUS_SILENCE

        try:
            plain_opus = dave_session.decrypt(user_id, davey.MediaType.audio, transport_data)
        except Exception:
            # У davey нет публичного признака "именно этот пакет не был
            # E2EE-зашифрован" — есть только `can_passthrough(user_id)`,
            # сообщающий текущий режим декриптора пользователя в целом
            # (включается на время перехода DAVE-сессии, см.
            # `set_passthrough_mode(True, 10)` в discord/voice_state.py).
            # Поэтому действуем так, как и предполагалось при отсутствии
            # надёжного признака: сначала пробуем расшифровать, и только
            # при неудаче, если декриптору пользователя сейчас разрешён
            # passthrough, считаем пакет открытым (нешифрованным) Opus и
            # отдаём его как есть; иначе это настоящий сбой расшифровки —
            # пропускаем содержимое.
            if dave_session.can_passthrough(user_id):
                self._diagnostics.log_dave(
                    packet, user_id=user_id, outcome="passthrough_fallback"
                )
                self._stats.note_passthrough_fallback(self._voice_client)
                return transport_data
            self._diagnostics.log_dave(packet, user_id=user_id, outcome="failed")
            self._stats.note_dave_failure(self._voice_client)
            return rtp.OPUS_SILENCE
        else:
            self._diagnostics.log_dave(packet, user_id=user_id, outcome="decrypted")
            return plain_opus


def install_dave_decryption(voice_client: voice_recv.VoiceRecvClient) -> None:
    """Подменяет расшифровщик пакетов чтеца приёма голоса на DAVE-совместимый.

    Вызывать сразу после `voice_client.listen(...)` — см. докстринг модуля
    про то, почему подмена атрибута `voice_client._reader.decryptor` уже
    после `listen()`, а не патч самого пакета, является здесь единственной
    рабочей точкой встраивания.
    """
    if not voice_client.is_listening():
        raise RuntimeError(
            "install_dave_decryption() нужно вызывать после voice_client.listen(...): "
            "чтецу приёма голоса ещё некому подменять расшифровщик."
        )
    reader = voice_client._reader
    diagnostics = _ReceiveDiagnostics(diagnostics_enabled())
    reader.decryptor = _DaveAwarePacketDecryptor(voice_client, diagnostics)
