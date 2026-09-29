"""Общие helpers устойчивого ответа на `discord.Interaction`.

Discord даёт боту всего три секунды на первый ответ (`defer()` или
`send_message()`) на взаимодействие. Если бот не уложился — под нагрузкой,
на холодном старте (см. `bot.cogs.music.MusicCog._warm_up_tts`) или просто
из-за сетевой задержки, — окно закрывается, и Discord начинает отвечать на
любую попытку ответить `discord.NotFound` (404, error code 10062, «Unknown
interaction»). Раньше это валило команду необработанным трейсбеком, а
обработчик ошибок (`cog_app_command_error`) падал следом второй такой же
ошибкой, пытаясь сообщить об исходном сбое тому же мёртвому взаимодействию.

Модуль не зависит от когов (только `discord` и stdlib), чтобы его можно было
без циклических импортов использовать и в самих когах (`bot.cogs.music`,
`bot.cogs.roulette`), и в их компонентах интерфейса (`bot.cogs.player_view`,
`bot.cogs.roulette_view`, `bot.cogs.views`).
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

#: Код ошибки Discord «Unknown interaction» — именно он приходит, когда
#: трёхсекундное окно первого ответа уже закрылось. Другие `discord.NotFound`
#: (например, удалённое вручную сообщение) используют другие коды и к этой
#: проблеме отношения не имеют.
_UNKNOWN_INTERACTION_CODE = 10062


def is_expired_interaction(exc: discord.HTTPException) -> bool:
    """Проверяет, что исключение — это именно истёкшее окно ответа на interaction.

    Отличает «окно закрылось» от любых других HTTP-ошибок Discord (нет прав,
    сообщение удалено, временная недоступность API и т. п.) — их
    `safe_defer` обязан пробросить дальше, а не проглотить молча.
    """
    return isinstance(exc, discord.NotFound) and exc.code == _UNKNOWN_INTERACTION_CODE


async def safe_defer(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False,
    thinking: bool = False,
) -> bool:
    """Откладывает ответ на interaction, не роняя команду, если окно уже закрылось.

    Kwargs `ephemeral`/`thinking` передаются в `interaction.response.defer()`
    только когда запрошены явно (`True`) — так вызов `safe_defer(interaction)`
    без аргументов делает ровно `defer()`, а не `defer(ephemeral=False,
    thinking=False)`, и поведение мест вызова (было где-то `defer()`, где-то
    `defer(thinking=True)`, где-то `defer(ephemeral=True)`) не меняется.

    Возвращает `True` при успехе. Если `defer()` упал именно из-за истёкшего
    окна (см. `is_expired_interaction`) — логирует одну строку WARNING без
    трейсбека (он здесь бесполезен: причина всегда одна и та же, не
    программная ошибка, а гонка со временем) и возвращает `False`, оставляя
    вызывающему коду решить, что делать дальше (см. `notify_window_expired`).
    Любая другая `discord.HTTPException` пробрасывается дальше — её должен
    увидеть `cog_app_command_error` кога.
    """
    kwargs: dict[str, bool] = {}
    if ephemeral:
        kwargs["ephemeral"] = True
    if thinking:
        kwargs["thinking"] = True
    try:
        await interaction.response.defer(**kwargs)
    except discord.HTTPException as exc:
        if not is_expired_interaction(exc):
            raise
        command_name = interaction.command.qualified_name if interaction.command else "?"
        logger.warning(
            "Окно ответа на /%s истекло до defer() — Discord уже считает interaction неизвестным",
            command_name,
        )
        return False
    return True


async def notify_window_expired(interaction: discord.Interaction, *, action: str) -> None:
    """Best-effort уведомляет канал, что окно ответа на `action` истекло.

    Вызывается после `safe_defer(...) is False`: само взаимодействие к этому
    моменту уже мертво (никакой `response`/`followup` на него больше не
    ответит), поэтому единственный способ хоть что-то сказать — обычное
    сообщение в тот же канал, а не ephemeral-ответ. `action` — человекочитаемое
    название команды/кнопки в винительном падеже (например `"/join"` или
    `"нажатие кнопки «Следующий»"`), его подставляет вызывающий код.

    Никогда не бросает исключений: это чисто информационная надстройка поверх
    и без того сбойного пути, ронять из-за неё дальнейшую обработку нельзя.
    `interaction.channel` может быть `None` (например, у interaction без
    привязанного канала) или объектом без `send` — тогда просто молча выходим.
    Отсутствие прав на отправку (`discord.Forbidden`) и прочие
    `discord.HTTPException` — не более чем неудача этой лучшей попытки,
    поэтому уходят в DEBUG, а не в WARNING/ERROR.
    """
    channel = interaction.channel
    send = getattr(channel, "send", None)
    if channel is None or send is None:
        return
    text = f"Discord не успел обработать {action} (истекло окно в 3 секунды), повторите."
    try:
        await send(text)
    except discord.HTTPException as exc:
        logger.debug("Не удалось сообщить об истёкшем окне ответа (%s): %s", action, exc)


async def respond(
    interaction: discord.Interaction,
    text: str,
    *,
    ephemeral: bool = True,
    clear_view: bool = False,
    prefer_followup: bool = False,
) -> None:
    """Отправляет текст взаимодействию, никогда не бросая `discord.HTTPException`.

    Единая замена продублированных по когам приватных методов ответа —
    подтверждения запуска волны/рулетки и текста ошибки: если ответ уже
    начат (в т. ч. отложен `defer()`) — правит его `edit_original_response`,
    а если это не получилось (в частности, взаимодействие протухло между
    `defer()` и этим вызовом) — уходит в `followup.send`. Если ответ ещё не
    начат — отправляет обычный `response.send_message`. Раньше именно
    последняя ветка ничем не была защищена и роняла `cog_app_command_error`
    второй раз подряд на живом логе из задачи — теперь любая
    `discord.HTTPException` здесь лишь логируется WARNING и функция тихо
    завершается.

    `clear_view=True` дополнительно сбрасывает `embed=None, view=None` при
    правке — так по-разному ведут себя подтверждение запуска (нужно стереть,
    например, меню выбора трека, которое могло остаться в первом ответе) и
    обычный текст ошибки (ничего, кроме текста, не трогает). Различие
    осознанно сохранено параметром, а не унифицировано — оно меняет то, что
    видит пользователь.

    `prefer_followup=True` пропускает попытку `edit_original_response` даже
    если ответ уже начат, и сразу уходит в `followup.send`. Нужен кнопкам
    `bot.cogs.player_view.PlayerView`/`bot.cogs.roulette_view.RouletteView`
    (их обработка нажатий `_run`): к моменту ошибки `response` нередко уже
    израсходован на `edit_message` с самим сообщением-плеером/статусом
    (публичным, на весь канал), и `edit_original_response` правил бы именно
    это публичное сообщение — ошибка конкретного нажатия стала бы видна всем
    слушателям вместо того, кто нажал. `followup.send` с `ephemeral=True` —
    то же самое приватное поведение, что было в прежних одноимённых
    `_respond_component_error` этих модулей до объединения в общий `respond`.
    """
    if interaction.response.is_done():
        if prefer_followup:
            try:
                await interaction.followup.send(text, ephemeral=ephemeral)
            except discord.HTTPException:
                logger.warning("Не удалось отправить followup взаимодействию: %s", text)
            return
        try:
            if clear_view:
                await interaction.edit_original_response(content=text, embed=None, view=None)
            else:
                await interaction.edit_original_response(content=text)
        except discord.HTTPException:
            try:
                await interaction.followup.send(text, ephemeral=ephemeral)
            except discord.HTTPException:
                logger.warning("Не удалось отправить followup взаимодействию: %s", text)
        return

    try:
        await interaction.response.send_message(text, ephemeral=ephemeral)
    except discord.HTTPException:
        logger.warning("Не удалось отправить ответ на взаимодействие: %s", text)
