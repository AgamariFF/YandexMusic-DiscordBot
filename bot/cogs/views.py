"""Компоненты интерфейса кога музыки: меню выбора трека из результатов поиска."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from bot.errors import BotError
from bot.yandex import TrackInfo

logger = logging.getLogger(__name__)

#: Discord режет `label`/`description`/`value` опции селекта до 100 символов —
#: превышение лимита не отклоняется мягко, а роняет запрос к API целиком.
_OPTION_TEXT_LIMIT = 100
#: Разрешённый Discord максимум — 25 опций, но меню читается заметно хуже уже
#: при таком количестве; ограничиваем более скромным и удобным числом.
MAX_SEARCH_RESULTS = 10
#: Время жизни меню выбора: по истечении компоненты отключаются сами, чтобы в
#: чате не оставалось «живое» меню, которое уже ничего не сделает.
SELECT_TIMEOUT_SECONDS = 60.0


def _truncate(text: str, limit: int = _OPTION_TEXT_LIMIT) -> str:
    """Обрезает текст до лимита Discord, добавляя многоточие при обрезке."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class TrackSelect(discord.ui.Select["TrackSearchView"]):
    """Выпадающий список найденных треков."""

    def __init__(self, tracks: tuple[TrackInfo, ...]) -> None:
        """Строит опции селекта из результатов поиска, обрезая текст под лимиты Discord."""
        options = [
            discord.SelectOption(
                label=_truncate(f"{track.artists} — {track.title}"),
                description=_truncate(_option_description(track)),
                value=str(index),
            )
            for index, track in enumerate(tracks)
        ]
        super().__init__(placeholder="Выберите трек…", options=options, min_values=1, max_values=1)
        self._tracks = tracks

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обрабатывает выбор трека: запускает волну от него и обновляет сообщение."""
        view = self.view
        if view is None:
            return
        index = int(self.values[0])
        track = self._tracks[index]
        await view.handle_selection(interaction, track)


def _option_description(track: TrackInfo) -> str:
    """Строит вторичную строку опции: длительность и альбом (если он есть в raw)."""
    minutes, seconds = divmod(int(max(0.0, track.duration)), 60)
    duration = f"{minutes}:{seconds:02d}"
    album_title = getattr(track.raw, "albums", None)
    album_name = album_title[0].title if album_title else None
    if album_name:
        return f"{duration} • {album_name}"
    return duration


class TrackSearchView(discord.ui.View):
    """Меню выбора трека из результатов поиска, доступное только автору команды."""

    def __init__(
        self,
        *,
        tracks: tuple[TrackInfo, ...],
        author_id: int,
        on_select: Callable[[discord.Interaction, TrackInfo], Awaitable[None]],
    ) -> None:
        """Запоминает автора команды (для проверки прав) и колбэк выбора трека."""
        super().__init__(timeout=SELECT_TIMEOUT_SECONDS)
        self._author_id = author_id
        self._on_select = on_select
        self._message: discord.Message | discord.InteractionMessage | None = None
        # discord.py запускает каждый callback компонента отдельной задачей, поэтому
        # два быстрых выбора подряд могут обработаться параллельно ещё до того, как
        # первый успеет отключить компоненты, — этот флаг закрывает гонку.
        self._handled = False
        self.add_item(TrackSelect(tracks))

    def attach_message(self, message: discord.Message | discord.InteractionMessage) -> None:
        """Сохраняет ссылку на отправленное сообщение — она нужна для правки после таймаута."""
        self._message = message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Разрешает нажатия только автору команды — иначе чужой мог бы переключить волну."""
        if interaction.user.id != self._author_id:
            await interaction.response.send_message(
                "Это меню не для вас — вызовите поиск сами.", ephemeral=True
            )
            return False
        return True

    async def handle_selection(self, interaction: discord.Interaction, track: TrackInfo) -> None:
        """Отключает меню и делегирует запуск волны колбэку, показывая пользователю итог."""
        if self._handled:
            # Уже обрабатывается или обработан другим (более ранним) выбором —
            # тихо выходим, не запуская волну повторно и не трогая сообщение.
            return
        self._handled = True
        self._disable_all_items()
        try:
            await interaction.response.edit_message(
                content=f"Выбрано: {track.display}",
                # track.display собран из названия и исполнителя, пришедших от
                # Яндекса, — полагаться, что там не окажется текста вида упоминания
                # роли, не стоит; тот же канал, что и у остального ответа бота.
                allowed_mentions=discord.AllowedMentions.none(),
                view=self,
            )
            await self._on_select(interaction, track)
        except BotError as exc:
            logger.warning("Ошибка запуска волны после выбора трека: %s", exc)
            await self._send_followup_error(interaction, exc.user_message)
        except Exception:
            logger.exception("Необработанная ошибка запуска волны после выбора трека")
            await self._send_followup_error(
                interaction, "Внутренняя ошибка, подробности в логах."
            )
        finally:
            self.stop()

    @staticmethod
    async def _send_followup_error(interaction: discord.Interaction, text: str) -> None:
        """Отправляет текст ошибки через followup — ответ на взаимодействие уже отправлен."""
        try:
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException:
            logger.warning("Не удалось отправить сообщение об ошибке после выбора трека")

    async def on_timeout(self) -> None:
        """Отключает компоненты меню по истечении таймаута, если сообщение ещё доступно."""
        self._disable_all_items()
        if self._message is None:
            return
        try:
            await self._message.edit(view=self)
        except discord.HTTPException:
            logger.warning("Не удалось отключить меню выбора трека после таймаута")

    def _disable_all_items(self) -> None:
        """Отключает все компоненты меню (используется и при выборе, и по таймауту)."""
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                item.disabled = True
