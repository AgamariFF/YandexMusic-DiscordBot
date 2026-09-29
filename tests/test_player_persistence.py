"""Тесты персистентности кнопок плеера и наблюдаемости устаревших нажатий.

Покрывают конкретный сбой: нажатие на сообщение-плеер, оставшееся от
прошлого запуска бота, не находило обработчика, `ViewStore.dispatch_view`
молча выходил, бот не подтверждал interaction — Discord показывал человеку
«Приложение не ответило вовремя», а в логе не оставалось ни строчки.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs.music import MusicCog
from bot.cogs.player_view import (
    DISCONNECT_CUSTOM_ID,
    PAUSE_CUSTOM_ID,
    PLAYER_CUSTOM_ID_PREFIX,
    SEARCH_CUSTOM_ID,
    SKIP_CUSTOM_ID,
    PlayerController,
    PlayerView,
)
from bot.config import Config
from bot.player import GuildPlayer, PlayerState

#: Код типа компонента «кнопка» в протоколе Discord — этим значением
#: `ViewStore` индексирует обработчики вместе с `custom_id`.
BUTTON_COMPONENT_TYPE = discord.ComponentType.button.value


@pytest.fixture
def mock_player():
    """Мок плеера: остановленное состояние, вне голосового канала."""
    player = MagicMock(spec=GuildPlayer)
    player.current = None
    player.state = PlayerState.IDLE
    player.channel = None
    return player


@pytest.fixture
def mock_controller():
    """Мок контроллера (кога), которому вью делегирует действия."""
    return MagicMock(spec=PlayerController)


@pytest.fixture
def mock_config():
    """Минимальная конфигурация, достаточная для создания MusicCog."""
    config = MagicMock(spec=Config)
    config.ffmpeg_path = "ffmpeg"
    config.default_volume = 0.5
    config.idle_timeout = 300
    config.guild_id = 12345
    config.tts_enabled = False
    config.tts_model_name = "test-model"
    config.tts_speaker_id = 0
    return config


@pytest.fixture
def cog(mock_config):
    """MusicCog с моками бота и клиента Яндекс.Музыки."""
    bot = MagicMock(spec=discord.ext.commands.Bot)
    return MusicCog(bot, mock_config, AsyncMock())


def _message(message_id: int):
    """Мок сообщения с заданным id — только он и важен для проверок."""
    message = MagicMock(spec=discord.Message)
    message.id = message_id
    return message


def _component_interaction(*, custom_id: str, message_id: int | None):
    """Мок нажатия на компонент с заданным custom_id и сообщением."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.type = discord.InteractionType.component
    interaction.data = {"custom_id": custom_id, "component_type": 2}
    interaction.message = None if message_id is None else _message(message_id)
    return interaction


class TestPlayerViewPersistence:
    """Контракт: вью пригоден для `Client.add_view` и его кнопки узнаваемы."""

    def test_view_is_persistent(self, mock_player, mock_controller):
        """`is_persistent()` — единственное условие, которое проверяет add_view."""
        view = PlayerView(player=mock_player, controller=mock_controller)

        assert view.is_persistent() is True

    def test_add_view_accepts_the_player_view(self, mock_player, mock_controller):
        """Реальный `Client.add_view` принимает вью — на неперсистентном он бросил бы ValueError."""
        client = discord.Client(intents=discord.Intents.none())
        view = PlayerView(player=mock_player, controller=mock_controller)

        client.add_view(view)

        assert view in client.persistent_views

    def test_custom_ids_are_the_declared_constants(self, mock_player, mock_controller):
        """Порядок и значения `custom_id` кнопок зафиксированы константами модуля."""
        view = PlayerView(player=mock_player, controller=mock_controller)

        assert [item.custom_id for item in view.children] == [
            PAUSE_CUSTOM_ID,
            SKIP_CUSTOM_ID,
            SEARCH_CUSTOM_ID,
            DISCONNECT_CUSTOM_ID,
        ]

    def test_custom_ids_share_the_player_prefix(self, mock_player, mock_controller):
        """По префиксу нажатие узнаётся как плеерное там, где вью уже нет под рукой."""
        view = PlayerView(player=mock_player, controller=mock_controller)

        assert all(item.custom_id.startswith(PLAYER_CUSTOM_ID_PREFIX) for item in view.children)

    def test_custom_ids_stable_across_instances_with_different_state(
        self, mock_player, mock_controller
    ):
        """Идентификаторы не зависят ни от экземпляра, ни от состояния плеера.

        Это и есть суть починки: раньше discord.py генерировал их случайно
        (`os.urandom(16).hex()`), поэтому кнопки знал только тот процесс,
        который создал конкретный экземпляр.
        """
        stopped = PlayerView(player=mock_player, controller=mock_controller)

        playing = MagicMock(spec=GuildPlayer)
        playing.current = MagicMock()
        playing.state = PlayerState.PAUSED
        playing.channel = None
        paused = PlayerView(player=playing, controller=mock_controller)

        assert [item.custom_id for item in stopped.children] == [
            item.custom_id for item in paused.children
        ]


class TestReplacePlayerView:
    """Контракт `_replace_player_view`: когда прежний вью можно останавливать.

    С фиксированными `custom_id` ключи старого и нового экземпляров в
    `ViewStore` совпадают, поэтому остановка старого вью на ТОМ ЖЕ
    сообщении сняла бы записи нового — кнопки снова стали бы мёртвыми.
    """

    def test_same_message_keeps_new_handlers_registered(self, cog, mock_player, mock_controller):
        """Главная проверка: после правки на месте кнопки сообщения остаются живыми.

        Проверяется не факт вызова `stop()`, а наблюдаемое следствие —
        содержимое `ViewStore`, по которому discord.py ищет обработчик
        нажатия. Именно его пустота и давала «Приложение не ответило
        вовремя» без единой строчки в логе.
        """
        client = discord.Client(intents=discord.Intents.none())
        old_view = PlayerView(player=mock_player, controller=mock_controller)
        new_view = PlayerView(player=mock_player, controller=mock_controller)
        client.add_view(old_view, message_id=777)
        client.add_view(new_view, message_id=777)
        cog._player_message = _message(777)
        cog._player_view = old_view

        cog._replace_player_view(new_view, message=_message(777))

        registered = client._connection._view_store._views.get(777, {})
        assert (BUTTON_COMPONENT_TYPE, SKIP_CUSTOM_ID) in registered
        assert registered[(BUTTON_COMPONENT_TYPE, SKIP_CUSTOM_ID)].view is new_view
        assert cog._player_view is new_view

    def test_new_message_releases_old_registration(self, cog, mock_player, mock_controller):
        """Пересозданное сообщение — записи старого вычищаются, нового остаются."""
        client = discord.Client(intents=discord.Intents.none())
        old_view = PlayerView(player=mock_player, controller=mock_controller)
        new_view = PlayerView(player=mock_player, controller=mock_controller)
        client.add_view(old_view, message_id=777)
        client.add_view(new_view, message_id=888)
        cog._player_message = _message(777)
        cog._player_view = old_view

        cog._replace_player_view(new_view, message=_message(888))

        store = client._connection._view_store
        assert 777 not in store._views
        assert (BUTTON_COMPONENT_TYPE, SKIP_CUSTOM_ID) in store._views.get(888, {})
        assert cog._player_view is new_view

    def test_first_assignment_without_previous_message(self, cog, mock_player, mock_controller):
        """Первое сообщение-плеер: останавливать нечего, состояние просто запоминается."""
        view = PlayerView(player=mock_player, controller=mock_controller)
        message = _message(555)

        cog._replace_player_view(view, message=message)

        assert cog._player_view is view
        assert cog._player_message is message


class TestOrphanInteractionLogging:
    """Контракт слушателя `on_interaction`: устаревшее нажатие обязано попасть в лог."""

    @pytest.mark.asyncio
    async def test_warns_on_outdated_player_message(self, cog, caplog):
        """Нажатие кнопки плеера на не-текущем сообщении даёт WARNING с custom_id."""
        cog._player_message = _message(100)
        interaction = _component_interaction(custom_id=SKIP_CUSTOM_ID, message_id=200)

        with caplog.at_level(logging.WARNING, logger="bot.cogs.music"):
            await cog.on_interaction(interaction)

        assert len(caplog.records) == 1
        assert SKIP_CUSTOM_ID in caplog.records[0].getMessage()
        assert "200" in caplog.records[0].getMessage()

    @pytest.mark.asyncio
    async def test_warns_when_cog_has_no_player_message(self, cog, caplog):
        """Плеера у кога нет вовсе — нажатие всё равно пришло по старому сообщению."""
        cog._player_message = None
        interaction = _component_interaction(custom_id=PAUSE_CUSTOM_ID, message_id=200)

        with caplog.at_level(logging.WARNING, logger="bot.cogs.music"):
            await cog.on_interaction(interaction)

        assert len(caplog.records) == 1

    @pytest.mark.asyncio
    async def test_silent_on_current_player_message(self, cog, caplog):
        """Штатное нажатие на актуальный плеер лог не засоряет."""
        cog._player_message = _message(100)
        interaction = _component_interaction(custom_id=SKIP_CUSTOM_ID, message_id=100)

        with caplog.at_level(logging.WARNING, logger="bot.cogs.music"):
            await cog.on_interaction(interaction)

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_silent_on_foreign_component(self, cog, caplog):
        """Компоненты чужих вью (меню выбора трека) не дают ложных предупреждений."""
        cog._player_message = _message(100)
        interaction = _component_interaction(custom_id="search:select", message_id=200)

        with caplog.at_level(logging.WARNING, logger="bot.cogs.music"):
            await cog.on_interaction(interaction)

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_silent_on_non_component_interaction(self, cog, caplog):
        """Slash-команды проходят через то же событие и слушателя не касаются."""
        cog._player_message = _message(100)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.type = discord.InteractionType.application_command
        interaction.data = {"name": "wave"}
        interaction.message = None

        with caplog.at_level(logging.WARNING, logger="bot.cogs.music"):
            await cog.on_interaction(interaction)

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_silent_when_interaction_has_no_message(self, cog, caplog):
        """Без сообщения сравнивать не с чем — молчим, а не гадаем."""
        cog._player_message = _message(100)
        interaction = _component_interaction(custom_id=SKIP_CUSTOM_ID, message_id=None)

        with caplog.at_level(logging.WARNING, logger="bot.cogs.music"):
            await cog.on_interaction(interaction)

        assert caplog.records == []
