"""Компоненты интерфейса кога музыки: меню выбора трека из результатов поиска."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from bot.cogs.interactions import notify_window_expired, safe_defer
from bot.errors import BotError
from bot.yandex import TrackInfo

logger = logging.getLogger(__name__)

#: Discord помещает в один ряд не больше пяти компонентов — ограничиваем
#: результаты поиска этим числом, чтобы все кнопки выбора трека были видны
#: сразу, без второго ряда и прокрутки. Яндекс и так возвращает результаты
#: по релевантности, так что достаточно первых пяти — это и есть «самые
#: подходящие».
MAX_SEARCH_RESULTS = 5
#: Время жизни меню выбора: по истечении компоненты отключаются сами, чтобы в
#: чате не оставалось «живое» меню, которое уже ничего не сделает.
SELECT_TIMEOUT_SECONDS = 60.0
#: Лимит длины одной строки нумерованного списка треков в embed'е. Исполнитель
#: и название приходят от Яндекса без гарантий по длине — аномально длинная
#: строка и портит список, и рискует упереться в лимиты embed'а Discord.
_LIST_LINE_LIMIT = 100


def _truncate(text: str, limit: int = _LIST_LINE_LIMIT) -> str:
    """Обрезает текст до лимита, добавляя многоточие при обрезке."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def numbered_tracks(tracks: tuple[TrackInfo, ...]) -> list[tuple[int, TrackInfo]]:
    """Нумерует треки с единицы — единственный источник правды для нумерации.

    Им пользуется и `TrackSearchView` при сборке кнопок, и `build_search_embed`
    при сборке списка треков в сообщении. Раз счётчик один на двоих, номер на
    кнопке и номер в списке разъехаться не могут в принципе.
    """
    return list(enumerate(tracks, start=1))


def build_search_embed(
    query: str, tracks: tuple[TrackInfo, ...], color: discord.Color
) -> discord.Embed:
    """Собирает embed со списком найденных треков для сообщения с меню выбора.

    Раньше названия треков были видны только внутри выпадающего списка;
    кнопки же подписаны только номерами (см. `TrackButton`), поэтому сам
    список — единственное место, где пользователь видит, что нашлось и что
    именно выбирает. Нумерация берётся из `numbered_tracks`, той же функции,
    которой пользуется `TrackSearchView` для кнопок.

    Цвет принимается параметром, а не читается константой модуля: `EMBED_COLOR`
    определён в `bot.cogs.music`, и импорт оттуда сюда закольцевал бы модули
    (music.py уже импортирует из views.py).
    """
    lines = [
        f"**{number}.** {_truncate(track.display)}" for number, track in numbered_tracks(tracks)
    ]
    return discord.Embed(
        title=f"Найдено несколько треков по запросу «{_truncate(query)}»",
        description="\n".join(lines),
        color=color,
    )


class TrackButton(discord.ui.Button["TrackSearchView"]):
    """Кнопка выбора одного трека из результатов поиска."""

    def __init__(self, number: int, track: TrackInfo) -> None:
        """Подписывает кнопку номером трека, а не его названием.

        Подпись кнопки Discord ограничивает 80 символами, а до пяти кнопок
        делят ширину одного ряда — «Артист — Название» на такой кнопке
        превратится в нечитаемый огрызок. Сам трек пользователь читает в
        сообщении со списком (`build_search_embed`), кнопка нужна только
        для выбора.

        Ряд вычисляется из номера, а не захардкожен: при нынешнем лимите
        `MAX_SEARCH_RESULTS = 5` все кнопки и так попадают в ряд 0, но если
        лимит когда-нибудь поднимут, шестая и последующие кнопки просто уйдут
        на следующий ряд (Discord допускает до пяти рядов по пять кнопок),
        а не уронят конструктор `TrackSearchView` ошибкой переполнения ряда.
        """
        row = (number - 1) // 5
        super().__init__(label=str(number), style=discord.ButtonStyle.secondary, row=row)
        self._track = track

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие: запускает волну от выбранного трека и обновляет сообщение."""
        view = self.view
        if view is None:
            return
        await view.handle_selection(interaction, self._track)


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
        for number, track in numbered_tracks(tracks):
            self.add_item(TrackButton(number, track))

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
        """Отключает меню, удаляет его сообщение и делегирует запуск волны колбэку.

        Раньше сообщение меню правилось на "Выбрано: ..." и оставалось
        висеть в канале — а к этому моменту сообщение-плеер уже могло
        обновиться колбэком `_announce` и оказаться СТАРШЕ меню, то есть не
        последним сообщением канала. Владелец прямо просил, чтобы после
        выбора в чате оставался только плеер, поэтому вместо правки текста
        меню теперь: пустой `defer()` (взаимодействию нужен хоть какой-то
        ответ) и удаление самого сообщения меню — см. `_delete_message`.

        Порядок операций принципиален. `safe_defer` вызывается самым первым,
        ещё до проверки `self._handled` — раньше `self._handled = True` и
        `_disable_all_items()` выставлялись ДО `defer()`, и при истёкшем окне
        ответа меню портилось необратимо (кнопки disabled, `self.stop()` уже
        вызван), а пользователь при этом получал «повторите» — повторить
        было уже физически нечем. Теперь при истёкшем окне состояние
        `self._handled` не трогается вовсе и меню остаётся рабочим.

        Проверка `self._handled` и присвоение ей `True` идут сразу вслед за
        успешным `defer()` без единого `await` между ними (следом сразу
        `_disable_all_items()`) — так закрыта гонка двух кликов,
        обработавшихся параллельно (см. комментарий в `__init__` про
        `self._handled`): переключение на другую задачу event loop возможно
        только на `await`, а между проверкой и присвоением его нет.

        `notify_window_expired` вызывается только если это не гонка с уже
        обработанным (другим) кликом — иначе пользователь получил бы
        «повторите» на клик, который на самом деле уже отработал через
        параллельный, и в чате осталось бы сразу два таких предупреждения.
        """
        if not await safe_defer(interaction):
            if not self._handled:
                await notify_window_expired(
                    interaction, action="выбор трека из результатов поиска"
                )
            return
        if self._handled:
            # Уже обработан другим (более ранним) кликом, пока этот ждал свой
            # defer(), — волну повторно не запускаем, меню не трогаем.
            return
        self._handled = True
        self._disable_all_items()
        try:
            await self._delete_message(interaction)
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
    async def _delete_message(interaction: discord.Interaction) -> None:
        """Удаляет сообщение меню после ответа на взаимодействие.

        Удаление может не получиться: `discord.NotFound` — сообщение уже
        удалили (например, кто-то вручную), `discord.Forbidden` — у бота нет
        права удалять сообщения в этом канале. Ни то, ни другое не должно
        останавливать запуск волны — трек уже выбран и его нужно доиграть,
        даже если список результатов так и останется висеть в чате.
        """
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.Forbidden):
            logger.warning("Не удалось удалить сообщение меню выбора трека")

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
        """Отключает все кнопки меню (используется и при выборе, и по таймауту)."""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
