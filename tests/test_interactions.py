"""Tests for bot.cogs.interactions: safe_defer, notify_window_expired, respond."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs.interactions import (
    is_expired_interaction,
    notify_window_expired,
    respond,
    safe_defer,
)

# ============================================================================
# Helpers to create discord.NotFound with specific error codes
# ============================================================================


def create_discord_not_found(code: int) -> discord.NotFound:
    """Create discord.NotFound with specific error code.

    The code is passed as part of the message dict to HTTPException.__init__,
    which extracts it as exc.code.
    """
    response_mock = MagicMock()
    response_mock.status = 404
    message_dict = {"code": code, "message": "Not found"}
    return discord.NotFound(response_mock, message_dict)


def create_discord_forbidden() -> discord.Forbidden:
    """Create discord.Forbidden (403 error)."""
    response_mock = MagicMock()
    response_mock.status = 403
    message_dict = {"code": 50013, "message": "Missing permissions"}
    return discord.Forbidden(response_mock, message_dict)


def create_mock_interaction():
    """Create a basic mock interaction."""
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.edit_original_response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.channel = AsyncMock()
    interaction.channel.send = AsyncMock()
    interaction.command = MagicMock()
    interaction.command.qualified_name = "test_command"
    return interaction


# ============================================================================
# Tests for is_expired_interaction()
# ============================================================================


class TestIsExpiredInteraction:
    """Tests for is_expired_interaction() function."""

    def test_returns_true_for_not_found_with_code_10062(self):
        """is_expired_interaction returns True for NotFound with code 10062."""
        exc = create_discord_not_found(10062)
        assert is_expired_interaction(exc) is True

    def test_returns_false_for_not_found_with_different_code(self):
        """is_expired_interaction returns False for NotFound with other codes."""
        # 10008 = Unknown Message
        exc = create_discord_not_found(10008)
        assert is_expired_interaction(exc) is False

    def test_returns_false_for_forbidden(self):
        """is_expired_interaction returns False for Forbidden (403)."""
        exc = create_discord_forbidden()
        assert is_expired_interaction(exc) is False

    def test_returns_false_for_generic_exception(self):
        """is_expired_interaction returns False for non-HTTPException."""
        exc = ValueError("Something went wrong")
        assert is_expired_interaction(exc) is False

    def test_returns_false_for_runtime_error(self):
        """is_expired_interaction returns False for RuntimeError."""
        exc = RuntimeError("Unexpected error")
        assert is_expired_interaction(exc) is False


# ============================================================================
# Tests for safe_defer()
# ============================================================================


class TestSafeDeferSuccessPath:
    """Tests for safe_defer() success path."""

    @pytest.mark.asyncio
    async def test_defers_and_returns_true_on_success(self):
        """safe_defer calls defer() and returns True on success."""
        interaction = create_mock_interaction()
        interaction.response.defer = AsyncMock()

        result = await safe_defer(interaction)

        assert result is True
        interaction.response.defer.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_passes_ephemeral_true_when_set(self):
        """safe_defer passes ephemeral=True to defer() when requested."""
        interaction = create_mock_interaction()
        interaction.response.defer = AsyncMock()

        await safe_defer(interaction, ephemeral=True)

        interaction.response.defer.assert_called_once_with(ephemeral=True)

    @pytest.mark.asyncio
    async def test_passes_thinking_true_when_set(self):
        """safe_defer passes thinking=True to defer() when requested."""
        interaction = create_mock_interaction()
        interaction.response.defer = AsyncMock()

        await safe_defer(interaction, thinking=True)

        interaction.response.defer.assert_called_once_with(thinking=True)

    @pytest.mark.asyncio
    async def test_passes_both_ephemeral_and_thinking_when_set(self):
        """safe_defer passes both ephemeral and thinking when both are True."""
        interaction = create_mock_interaction()
        interaction.response.defer = AsyncMock()

        await safe_defer(interaction, ephemeral=True, thinking=True)

        interaction.response.defer.assert_called_once_with(ephemeral=True, thinking=True)

    @pytest.mark.asyncio
    async def test_passes_no_kwargs_when_both_false(self):
        """safe_defer calls defer() without kwargs when both flags are False."""
        interaction = create_mock_interaction()
        interaction.response.defer = AsyncMock()

        await safe_defer(interaction, ephemeral=False, thinking=False)

        interaction.response.defer.assert_called_once_with()


class TestSafeDeferExpiredInteraction:
    """Tests for safe_defer() when interaction window is expired."""

    @pytest.mark.asyncio
    async def test_returns_false_when_defer_raises_expired_not_found(self):
        """safe_defer returns False when defer() raises NotFound with code 10062."""
        interaction = create_mock_interaction()
        exc = create_discord_not_found(10062)
        interaction.response.defer = AsyncMock(side_effect=exc)

        result = await safe_defer(interaction)

        assert result is False

    @pytest.mark.asyncio
    async def test_does_not_raise_when_defer_raises_expired_not_found(self):
        """safe_defer does not raise when defer() raises expired NotFound."""
        interaction = create_mock_interaction()
        exc = create_discord_not_found(10062)
        interaction.response.defer = AsyncMock(side_effect=exc)

        # Should not raise
        await safe_defer(interaction)

    @pytest.mark.asyncio
    async def test_logs_warning_when_defer_raises_expired_not_found(self, caplog):
        """safe_defer logs WARNING when defer() raises expired NotFound."""
        interaction = create_mock_interaction()
        exc = create_discord_not_found(10062)
        interaction.response.defer = AsyncMock(side_effect=exc)

        await safe_defer(interaction)

        assert any("истекло" in record.message.lower() for record in caplog.records)


class TestSafeDeferOtherHttpException:
    """Tests for safe_defer() when defer() raises other HTTPException."""

    @pytest.mark.asyncio
    async def test_raises_forbidden_when_defer_raises_forbidden(self):
        """safe_defer raises Forbidden when defer() raises Forbidden."""
        interaction = create_mock_interaction()
        exc = create_discord_forbidden()
        interaction.response.defer = AsyncMock(side_effect=exc)

        with pytest.raises(discord.Forbidden):
            await safe_defer(interaction)

    @pytest.mark.asyncio
    async def test_raises_not_found_with_different_code(self):
        """safe_defer raises NotFound when code is not 10062."""
        interaction = create_mock_interaction()
        exc = create_discord_not_found(10008)  # Unknown Message
        interaction.response.defer = AsyncMock(side_effect=exc)

        with pytest.raises(discord.NotFound):
            await safe_defer(interaction)


# ============================================================================
# Tests for notify_window_expired()
# ============================================================================


class TestNotifyWindowExpiredHappyPath:
    """Tests for notify_window_expired() happy path."""

    @pytest.mark.asyncio
    async def test_sends_message_when_channel_available(self):
        """notify_window_expired sends message to channel.send()."""
        interaction = create_mock_interaction()
        interaction.channel = AsyncMock()
        interaction.channel.send = AsyncMock()

        await notify_window_expired(interaction, action="/join")

        interaction.channel.send.assert_called_once()
        call_args = interaction.channel.send.call_args[0][0]
        assert "/join" in call_args

    @pytest.mark.asyncio
    async def test_message_contains_action(self):
        """notify_window_expired includes action text in message."""
        interaction = create_mock_interaction()
        interaction.channel = AsyncMock()
        interaction.channel.send = AsyncMock()

        await notify_window_expired(interaction, action="/skip")

        call_args = interaction.channel.send.call_args[0][0]
        assert "/skip" in call_args


class TestNotifyWindowExpiredNoChannel:
    """Tests for notify_window_expired() when channel is None."""

    @pytest.mark.asyncio
    async def test_does_nothing_when_channel_is_none(self):
        """notify_window_expired does nothing when interaction.channel is None."""
        interaction = create_mock_interaction()
        interaction.channel = None

        # Should not raise
        await notify_window_expired(interaction, action="/join")

    @pytest.mark.asyncio
    async def test_does_not_raise_when_channel_is_none(self):
        """notify_window_expired does not raise when channel is None."""
        interaction = create_mock_interaction()
        interaction.channel = None

        # Verify no exception is raised
        try:
            await notify_window_expired(interaction, action="/join")
        except Exception as e:
            pytest.fail(f"Should not raise exception, got {e}")


class TestNotifyWindowExpiredNoSendMethod:
    """Tests for notify_window_expired() when channel has no send method."""

    @pytest.mark.asyncio
    async def test_does_nothing_when_channel_has_no_send(self):
        """notify_window_expired does nothing when channel lacks send attribute."""
        interaction = create_mock_interaction()
        interaction.channel = MagicMock()
        # Explicitly delete the send attribute to simulate its absence
        del interaction.channel.send

        # Should not raise
        await notify_window_expired(interaction, action="/join")

    @pytest.mark.asyncio
    async def test_does_not_raise_when_channel_has_no_send(self):
        """notify_window_expired does not raise when channel has no send method."""
        interaction = create_mock_interaction()
        interaction.channel = MagicMock(spec=[])  # spec=[] means no attributes

        try:
            await notify_window_expired(interaction, action="/join")
        except Exception as e:
            pytest.fail(f"Should not raise exception, got {e}")


class TestNotifyWindowExpiredSendFails:
    """Tests for notify_window_expired() when channel.send raises HTTPException."""

    @pytest.mark.asyncio
    async def test_does_not_raise_when_send_raises_forbidden(self):
        """notify_window_expired does not raise when channel.send raises Forbidden."""
        interaction = create_mock_interaction()
        interaction.channel = AsyncMock()
        interaction.channel.send = AsyncMock(side_effect=create_discord_forbidden())

        # Should not raise
        await notify_window_expired(interaction, action="/join")

    @pytest.mark.asyncio
    async def test_does_not_raise_when_send_raises_not_found(self):
        """notify_window_expired does not raise when channel.send raises NotFound."""
        interaction = create_mock_interaction()
        interaction.channel = AsyncMock()
        exc = create_discord_not_found(10008)
        interaction.channel.send = AsyncMock(side_effect=exc)

        # Should not raise
        await notify_window_expired(interaction, action="/join")



# ============================================================================
# Tests for respond()
# ============================================================================


class TestRespondResponseNotDone:
    """Tests for respond() when response is not yet done."""

    @pytest.mark.asyncio
    async def test_sends_message_when_response_not_done(self):
        """respond calls response.send_message when response.is_done() is False."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.send_message = AsyncMock()

        await respond(interaction, "An error occurred")

        call_args = interaction.response.send_message.call_args
        assert call_args[0][0] == "An error occurred"
        assert call_args[1]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_passes_ephemeral_flag_when_response_not_done(self):
        """respond passes ephemeral flag to send_message."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.send_message = AsyncMock()

        await respond(interaction, "An error occurred", ephemeral=False)

        call_args = interaction.response.send_message.call_args
        assert call_args[0][0] == "An error occurred"
        assert call_args[1]["ephemeral"] is False


class TestRespondResponseDoneNoPreferFollowup:
    """Tests for respond() when response is done and prefer_followup is False."""

    @pytest.mark.asyncio
    async def test_edits_original_response_when_response_done(self):
        """respond calls edit_original_response when response.is_done() is True."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock()

        await respond(interaction, "An error occurred")

        interaction.edit_original_response.assert_called_once_with(content="An error occurred")

    @pytest.mark.asyncio
    async def test_edit_without_clear_view_by_default(self):
        """respond edits without embed/view when clear_view is False."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock()

        await respond(interaction, "An error occurred", clear_view=False)

        interaction.edit_original_response.assert_called_once_with(content="An error occurred")

    @pytest.mark.asyncio
    async def test_edit_with_clear_view_when_requested(self):
        """respond edits with embed=None, view=None when clear_view=True."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock()

        await respond(interaction, "An error occurred", clear_view=True)

        interaction.edit_original_response.assert_called_once_with(
            content="An error occurred", embed=None, view=None
        )


class TestRespondEditFallbackToFollowup:
    """Tests for respond() fallback to followup when edit fails."""

    @pytest.mark.asyncio
    async def test_fallback_to_followup_when_edit_raises_exception(self):
        """respond calls followup.send when edit_original_response raises HTTPException."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock(side_effect=create_discord_forbidden())
        interaction.followup.send = AsyncMock()

        await respond(interaction, "An error occurred")

        interaction.followup.send.assert_called_once_with("An error occurred", ephemeral=True)

    @pytest.mark.asyncio
    async def test_followup_uses_provided_ephemeral_flag(self):
        """respond passes ephemeral flag to followup.send on fallback."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock(side_effect=create_discord_forbidden())
        interaction.followup.send = AsyncMock()

        await respond(interaction, "An error occurred", ephemeral=False)

        interaction.followup.send.assert_called_once_with("An error occurred", ephemeral=False)


class TestRespondPreferFollowup:
    """Tests for respond() with prefer_followup=True."""

    @pytest.mark.asyncio
    async def test_skips_edit_when_prefer_followup_true(self):
        """respond skips edit_original_response when prefer_followup=True."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock()
        interaction.followup.send = AsyncMock()

        await respond(interaction, "An error occurred", prefer_followup=True)

        interaction.edit_original_response.assert_not_called()
        call_args = interaction.followup.send.call_args
        assert call_args[0][0] == "An error occurred"
        assert call_args[1]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_uses_send_message_when_prefer_followup_true_and_not_done(self):
        """respond uses send_message when prefer_followup=True but response not done."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.send_message = AsyncMock()

        await respond(interaction, "An error occurred", prefer_followup=True)

        call_args = interaction.response.send_message.call_args
        assert call_args[0][0] == "An error occurred"
        assert call_args[1]["ephemeral"] is True


class TestRespondNeverRaises:
    """Tests for respond() never raising exceptions."""

    @pytest.mark.asyncio
    async def test_does_not_raise_when_send_message_fails(self):
        """respond does not raise when response.send_message fails."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.send_message = AsyncMock(side_effect=create_discord_forbidden())

        # Should not raise
        await respond(interaction, "An error occurred")

    @pytest.mark.asyncio
    async def test_does_not_raise_when_followup_send_fails_after_edit_fails(self):
        """respond does not raise when both edit and followup.send fail."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.edit_original_response = AsyncMock(side_effect=create_discord_forbidden())
        interaction.followup.send = AsyncMock(side_effect=create_discord_forbidden())

        # Should not raise
        await respond(interaction, "An error occurred")

    @pytest.mark.asyncio
    async def test_does_not_raise_when_followup_send_fails_with_prefer_followup(self):
        """respond does not raise when followup.send fails with prefer_followup=True."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        interaction.followup.send = AsyncMock(side_effect=create_discord_forbidden())

        # Should not raise
        await respond(interaction, "An error occurred", prefer_followup=True)

    @pytest.mark.asyncio
    async def test_does_not_raise_when_expired_interaction_on_edit(self):
        """respond does not raise when edit_original_response gets expired interaction."""
        interaction = create_mock_interaction()
        interaction.response.is_done = MagicMock(return_value=True)
        exc = create_discord_not_found(10062)
        interaction.edit_original_response = AsyncMock(side_effect=exc)
        interaction.followup.send = AsyncMock(side_effect=exc)

        # Should not raise
        await respond(interaction, "An error occurred")
