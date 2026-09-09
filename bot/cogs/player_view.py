"""Компоненты сообщения-плеера: постоянный ряд кнопок и модальное окно поиска.

Отделено от `views.py` (там живёт меню выбора трека из результатов поиска),
потому что плеер — самостоятельный набор компонентов другого назначения:
не разовое меню с таймаутом, а постоянный ряд кнопок при живой волне.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord

from bot.errors import BotError
from bot.player import GuildPlayer, PlayerState

logger = logging.getLogger(__name__)


class PlayerController(Protocol):
    """Минимальный интерфейс кога, нужный компонентам плеера для выполнения действий.

    Protocol, а не прямой импорт `MusicCog`: `music.py` и так уже импортирует
    `PlayerView` отсюда, обратный импорт закольцевал бы модули (тот же приём,
    что и в `build_search_embed` про `EMBED_COLOR` из `views.py`). Заодно это
    делает модуль проверяемым без реального кога — тестам достаточно любого
    объекта с такими методами.
    """

    async def handle_pause_toggle(self, interaction: discord.Interaction) -> None: ...

    async def handle_skip(self, interaction: discord.Interaction) -> None: ...

    async def handle_disconnect(self, interaction: discord.Interaction) -> None: ...

    async def handle_search_query(self, interaction: discord.Interaction, query: str) -> None: ...


async def _respond_component_error(interaction: discord.Interaction, text: str) -> None:
    """Отправляет текст ошибки компонента с учётом того, был ли уже отправлен ответ."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        logger.warning("Не удалось отправить сообщение об ошибке компонента плеера")


class PlayerView(discord.ui.View):
    """Постоянный ряд кнопок управления сообщением-плеером.

    `timeout=None`: волна живёт неопределённо долго, и по времени кнопкам
    отключаться незачем — они и так пересобираются заново при каждом
    обновлении сообщения (см. `MusicCog._update_player_message`). Вид кнопок
    (подпись паузы/продолжения, disabled в остановленном состоянии) читается
    из `player` один раз при создании конкретного экземпляра — ровно перед
    тем, как он прикрепляется к отредактированному или новому сообщению.

    Кнопка поиска — исключение: она не отключается в остановленном
    состоянии, потому что именно ею запускается новая волна (см. `SearchButton`).
    """

    def __init__(self, *, player: GuildPlayer, controller: PlayerController) -> None:
        """Строит ряд кнопок под текущее состояние плеера."""
        super().__init__(timeout=None)
        self._player = player
        self._controller = controller
        stopped = player.current is None
        paused = player.state is PlayerState.PAUSED
        self.add_item(PauseResumeButton(paused=paused, disabled=stopped))
        self.add_item(SkipButton(disabled=stopped))
        self.add_item(SearchButton())
        self.add_item(DisconnectButton(disabled=stopped))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Разрешает нажатия только участникам того же голосового канала, что и бот.

        Плеер общий для всех, кто слушает волну на сервере, но управлять им
        должен именно слушающий, а не любой человек из текстового чата —
        иначе кто угодно мог бы поставить чужую музыку на паузу или отключить
        бота у слушателей (см. заметку "Бот на общем сервере" в памяти проекта).

        Если бот нигде не подключён (`player.channel is None` — полная
        остановка после отключения), ограничивать нажатия по каналу нечем:
        в этом состоянии всё равно активна только кнопка поиска (остальные
        disabled, и Discord их нажатие никогда не пришлёт), а ей должен
        суметь воспользоваться любой, чтобы перезапустить волну.
        """
        channel = self._player.channel
        if channel is None:
            return True

        member = interaction.user
        listening = (
            isinstance(member, discord.Member)
            and member.voice is not None
            and member.voice.channel is not None
            and member.voice.channel.id == channel.id
        )
        if not listening:
            await interaction.response.send_message(
                "Управлять плеером могут только участники голосового канала бота.",
                ephemeral=True,
            )
            return False
        return True

    async def handle_pause_toggle(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки паузы/продолжения."""
        await self._run(interaction, self._controller.handle_pause_toggle)

    async def handle_skip(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки пропуска трека."""
        await self._run(interaction, self._controller.handle_skip)

    async def handle_disconnect(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки отключения от голосового канала."""
        await self._run(interaction, self._controller.handle_disconnect)

    async def handle_search(self, interaction: discord.Interaction) -> None:
        """Открывает модальное окно поиска трека."""
        await self._run(interaction, self._open_search_modal)

    async def _open_search_modal(self, interaction: discord.Interaction) -> None:
        """Показывает модальное окно поиска — сам поиск запускается после его отправки."""
        await interaction.response.send_modal(SearchModal(controller=self._controller))

    async def _run(
        self,
        interaction: discord.Interaction,
        action: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        """Общая обработка нажатий: доменные ошибки — пользователю, остальное — в лог и в чат.

        Ошибки компонентов не доходят до `MusicCog.cog_app_command_error`
        (тот ловит только ошибки slash-команд), поэтому каждая кнопка сама
        решает, что показать пользователю, — как и `TrackSearchView.handle_selection`.
        """
        try:
            await action(interaction)
        except BotError as exc:
            logger.warning("Ошибка кнопки плеера: %s", exc)
            await _respond_component_error(interaction, exc.user_message)
        except Exception:
            logger.exception("Необработанная ошибка кнопки плеера")
            await _respond_component_error(
                interaction, "Внутренняя ошибка, подробности в логах."
            )


class PauseResumeButton(discord.ui.Button["PlayerView"]):
    """Кнопка паузы/продолжения — подпись и стиль зависят от текущего состояния плеера."""

    def __init__(self, *, paused: bool, disabled: bool) -> None:
        """Показывает «Продолжить» на паузе и «Пауза» во время игры — одна кнопка на оба случая."""
        label = "Продолжить" if paused else "Пауза"
        emoji = "▶️" if paused else "⏸️"
        style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        super().__init__(label=label, emoji=emoji, style=style, disabled=disabled, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        """Делегирует обработку нажатия владеющему `PlayerView`."""
        view = self.view
        if view is None:
            return
        await view.handle_pause_toggle(interaction)


class SkipButton(discord.ui.Button["PlayerView"]):
    """Кнопка перехода к следующему треку волны."""

    def __init__(self, *, disabled: bool) -> None:
        """Создаёт кнопку пропуска трека."""
        super().__init__(
            label="Следующий",
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Делегирует обработку нажатия владеющему `PlayerView`."""
        view = self.view
        if view is None:
            return
        await view.handle_skip(interaction)


class SearchButton(discord.ui.Button["PlayerView"]):
    """Кнопка поиска трека — открывает модальное окно ввода запроса.

    В отличие от остальных кнопок ряда, никогда не отключается: когда волна
    остановлена, поиск — единственное осмысленное действие, которым её можно
    перезапустить, не набирая команду вручную.
    """

    def __init__(self) -> None:
        """Создаёт кнопку поиска — всегда активна."""
        super().__init__(
            label="Поиск",
            emoji="🔍",
            style=discord.ButtonStyle.secondary,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Делегирует обработку нажатия владеющему `PlayerView`."""
        view = self.view
        if view is None:
            return
        await view.handle_search(interaction)


class DisconnectButton(discord.ui.Button["PlayerView"]):
    """Кнопка отключения бота от голосового канала — опасное действие, стиль danger."""

    def __init__(self, *, disabled: bool) -> None:
        """Создаёт кнопку отключения."""
        super().__init__(
            label="Отключить",
            emoji="🔌",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Делегирует обработку нажатия владеющему `PlayerView`."""
        view = self.view
        if view is None:
            return
        await view.handle_disconnect(interaction)


class SearchModal(discord.ui.Modal, title="Поиск трека"):
    """Модальное окно поиска, открываемое кнопкой «Поиск» на сообщении-плеере."""

    query_label = discord.ui.Label(
        text="Что ищем?",
        component=discord.ui.TextInput(
            placeholder="Исполнитель и/или название трека", max_length=100
        ),
    )

    def __init__(self, *, controller: PlayerController) -> None:
        """Запоминает контроллер (ког), который выполнит поиск после отправки формы."""
        super().__init__()
        self._controller = controller

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Делегирует контроллеру поиск и запуск волны — та же ветка, что и у команды /search.

        Запрос — пользовательский текст, поэтому дальнейшая обработка (как и
        у /search) обязана слать его только с `allowed_mentions=none()`; эту
        защиту обеспечивает сам контроллер, здесь текст никуда не подставляется.
        """
        query = str(self.query_label.component.value)
        try:
            await self._controller.handle_search_query(interaction, query)
        except BotError as exc:
            logger.warning("Ошибка поиска через модальное окно плеера: %s", exc)
            await _respond_component_error(interaction, exc.user_message)
        except Exception:
            logger.exception("Необработанная ошибка поиска через модальное окно плеера")
            await _respond_component_error(
                interaction, "Внутренняя ошибка, подробности в логах."
            )
