"""Компоненты сообщения-статуса чат-рулетки: постоянный ряд кнопок «Следующий»/«Стоп».

Устроено по образцу `bot.cogs.player_view` (сообщение-плеер «Моей волны»):
не разовое меню с таймаутом, а постоянный ряд кнопок при живой рулетке.
Отдельный модуль, а не часть `bot.cogs.roulette`, по той же причине, что и
разделение `music.py`/`player_view.py` — команды и компоненты интерфейса
меняются по разным поводам.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord

from bot.cogs.interactions import respond
from bot.errors import BotError
from bot.roulette import GuildRoulette, RouletteStatus

logger = logging.getLogger(__name__)


class RouletteController(Protocol):
    """Минимальный интерфейс кога, нужный кнопкам статуса рулетки.

    Protocol по тем же причинам, что и `PlayerController` в `player_view.py`:
    `roulette.py` (ког) и так уже импортирует `RouletteView` отсюда, обратный
    импорт закольцевал бы модули, а тестам достаточно любого объекта с этими
    методами.
    """

    async def handle_next(self, interaction: discord.Interaction) -> None: ...

    async def handle_stop(self, interaction: discord.Interaction) -> None: ...


class RouletteView(discord.ui.View):
    """Постоянный ряд кнопок сообщения-статуса чат-рулетки: «Следующий» и «Стоп».

    `timeout=None` и пересборка на каждое обновление сообщения — тот же
    приём, что и у `PlayerView` (см. её докстринг про непостоянство вью между
    перезапусками процесса бота): вид кнопок читается из `roulette` один раз
    при создании конкретного экземпляра, ровно перед тем как он прикрепляется
    к отредактированному или новому сообщению.
    """

    def __init__(self, *, roulette: GuildRoulette, controller: RouletteController) -> None:
        """Строит ряд кнопок под текущее состояние рулетки."""
        super().__init__(timeout=None)
        self._roulette = roulette
        self._controller = controller
        stopped = roulette.status is RouletteStatus.STOPPED
        self.add_item(NextButton(disabled=stopped))
        self.add_item(StopButton(disabled=stopped))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Разрешает нажатия только участникам того же голосового канала, что и бот.

        См. докстринг `PlayerView.interaction_check` — то же обоснование:
        управлять чужим разговором не должен кто попало из текстового чата
        (см. заметку "Бот на общем сервере" в памяти проекта). Если бот
        никуда не подключён (рулетка полностью остановлена), ограничивать
        нажатия по каналу нечем — в этом состоянии обе кнопки disabled и
        Discord их нажатие не пришлёт вовсе.
        """
        channel = self._roulette.channel
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
                "Управлять чат-рулеткой могут только участники голосового канала бота.",
                ephemeral=True,
            )
            return False
        return True

    async def handle_next(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки перехода к следующему собеседнику."""
        await self._run(interaction, self._controller.handle_next)

    async def handle_stop(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки остановки чат-рулетки."""
        await self._run(interaction, self._controller.handle_stop)

    async def _run(
        self,
        interaction: discord.Interaction,
        action: Callable[[discord.Interaction], Awaitable[None]],
    ) -> None:
        """Общая обработка нажатий: доменные ошибки — пользователю, остальное — в лог и в чат.

        См. `PlayerView._run` — та же схема: ошибки компонентов не доходят
        до `cog_app_command_error` кога (он ловит только ошибки slash-команд),
        и та же причина `prefer_followup=True` — `response` к этому моменту
        мог быть израсходован на `edit_message` с публичным сообщением-статусом.
        """
        try:
            await action(interaction)
        except BotError as exc:
            logger.warning("Ошибка кнопки чат-рулетки: %s", exc)
            await respond(interaction, exc.user_message, prefer_followup=True)
        except Exception:
            logger.exception("Необработанная ошибка кнопки чат-рулетки")
            await respond(
                interaction, "Внутренняя ошибка, подробности в логах.", prefer_followup=True
            )


class NextButton(discord.ui.Button["RouletteView"]):
    """Кнопка перехода к следующему собеседнику."""

    def __init__(self, *, disabled: bool) -> None:
        """Создаёт кнопку перехода к следующему собеседнику."""
        super().__init__(
            label="Следующий",
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Делегирует обработку нажатия владеющему `RouletteView`."""
        view = self.view
        if view is None:
            return
        await view.handle_next(interaction)


class StopButton(discord.ui.Button["RouletteView"]):
    """Кнопка остановки чат-рулетки и отключения от канала — опасное действие, стиль danger."""

    def __init__(self, *, disabled: bool) -> None:
        """Создаёт кнопку остановки."""
        super().__init__(
            label="Стоп",
            emoji="🔌",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Делегирует обработку нажатия владеющему `RouletteView`."""
        view = self.view
        if view is None:
            return
        await view.handle_stop(interaction)
