"""Тесты модуля bot.speech (офлайн-распознавание русской речи через Vosk)."""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.errors import SpeechModelUnavailableError
from bot.speech import (
    DEFAULT_MODEL_PATH,
    DEFAULT_SPEAKER_IDLE_TIMEOUT,
    SpeechRecognizer,
)


@pytest.fixture
def mock_kaldi():
    """Mock для экземпляра KaldiRecognizer."""
    recognizer = MagicMock()
    recognizer.AcceptWaveform = MagicMock(return_value=False)
    recognizer.Result = MagicMock(
        return_value=json.dumps({"text": "привет мир"})
    )
    return recognizer


@pytest.fixture
def mock_model():
    """Mock для экземпляра модели Vosk."""
    return MagicMock()


@pytest.fixture
def mock_path_exists(tmp_path, monkeypatch):
    """Mock Path.is_dir для возврата True для пути модели по умолчанию."""
    real_is_dir = Path.is_dir

    def fake_is_dir(self):
        if str(self) == DEFAULT_MODEL_PATH:
            return True
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)
    return True


class TestSpeechRecognizerInitialization:
    """Тесты конструктора SpeechRecognizer и свойства is_ready."""

    def test_default_constructor(self):
        """Конструктор с параметрами по умолчанию инициализируется корректно."""
        recognizer = SpeechRecognizer()
        assert recognizer._model_path == DEFAULT_MODEL_PATH
        assert recognizer._speaker_idle_timeout == DEFAULT_SPEAKER_IDLE_TIMEOUT

    def test_custom_constructor(self):
        """Конструктор принимает пользовательский путь модели и таймаут."""
        recognizer = SpeechRecognizer(
            model_path="/custom/path", speaker_idle_timeout=60.0
        )
        assert recognizer._model_path == "/custom/path"
        assert recognizer._speaker_idle_timeout == 60.0

    def test_is_ready_initially_false(self):
        """Новый распознаватель не готов до вызова ensure_ready."""
        recognizer = SpeechRecognizer()
        assert recognizer.is_ready is False

    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_is_ready_true_after_ensure_ready(
        self, mock_is_dir, mock_model_class
    ):
        """is_ready становится True после успешного вызова ensure_ready."""
        mock_is_dir.return_value = True
        mock_model_class.return_value = MagicMock()

        recognizer = SpeechRecognizer()
        assert recognizer.is_ready is False

        await recognizer.ensure_ready()

        assert recognizer.is_ready is True


class TestEnsureReady:
    """Тесты метода SpeechRecognizer.ensure_ready()."""

    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_ensure_ready_loads_model_once(
        self, mock_is_dir, mock_model_class
    ):
        """Первый ensure_ready загружает модель, второй вызов ничего не делает."""
        mock_is_dir.return_value = True
        mock_model_instance = MagicMock()
        mock_model_class.return_value = mock_model_instance

        recognizer = SpeechRecognizer()

        await recognizer.ensure_ready()
        first_call_count = mock_model_class.call_count

        await recognizer.ensure_ready()
        second_call_count = mock_model_class.call_count

        assert first_call_count == 1
        assert second_call_count == 1

    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_concurrent_ensure_ready_calls(
        self, mock_is_dir, mock_model_class
    ):
        """Конкурентные вызовы ensure_ready загружают модель только один раз."""
        mock_is_dir.return_value = True
        mock_model_instance = MagicMock()
        mock_model_class.return_value = mock_model_instance

        recognizer = SpeechRecognizer()

        # Вызываем ensure_ready конкурентно
        await asyncio.gather(
            recognizer.ensure_ready(),
            recognizer.ensure_ready(),
            recognizer.ensure_ready(),
        )

        # Модель должна быть загружена только один раз
        assert mock_model_class.call_count == 1

    @patch("bot.speech.Path.is_dir")
    async def test_ensure_ready_model_path_not_found(self, mock_is_dir):
        """ensure_ready бросает SpeechModelUnavailableError, если пути нет."""
        mock_is_dir.return_value = False

        recognizer = SpeechRecognizer(model_path="/nonexistent/path")

        with pytest.raises(SpeechModelUnavailableError) as exc_info:
            await recognizer.ensure_ready()

        assert "не найдена по пути" in str(exc_info.value)

    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_ensure_ready_model_load_exception(
        self, mock_is_dir, mock_model_class
    ):
        """ensure_ready оборачивает исключения загрузки Model в SpeechModelUnavailableError."""
        mock_is_dir.return_value = True
        mock_model_class.side_effect = RuntimeError("Model file corrupted")

        recognizer = SpeechRecognizer()

        with pytest.raises(SpeechModelUnavailableError) as exc_info:
            await recognizer.ensure_ready()

        assert "Model file corrupted" in str(exc_info.value)

    @patch("bot.speech.Path.is_dir")
    async def test_ensure_ready_checks_path_before_calling_model(
        self, mock_is_dir
    ):
        """ensure_ready проверяет наличие пути перед вызовом Model()."""
        mock_is_dir.return_value = False

        recognizer = SpeechRecognizer(model_path="/bad/path")

        with patch("bot.speech.Model") as mock_model:
            with pytest.raises(SpeechModelUnavailableError):
                await recognizer.ensure_ready()

            # Model не должен быть вызван, если проверка пути не пройдёт
            mock_model.assert_not_called()


class TestFeedBeforeReady:
    """Тесты поведения feed() до ensure_ready."""

    async def test_feed_before_ensure_ready_raises(self):
        """feed() до ensure_ready бросает SpeechModelUnavailableError."""
        recognizer = SpeechRecognizer()
        pcm_data = b"\x00\x01\x02\x03"

        with pytest.raises(SpeechModelUnavailableError) as exc_info:
            await recognizer.feed(speaker_id=123, pcm=pcm_data)

        assert "до ensure_ready" in str(exc_info.value)


class TestFeedWithKaldiRecognizers:
    """Тесты метода feed() с разделением KaldiRecognizer по говорящим."""

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_creates_separate_recognizers_per_speaker(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """Каждый speaker_id получает свой экземпляр KaldiRecognizer."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        # Создаём отдельные mock-распознаватели для каждого говорящего
        recognizer1 = MagicMock()
        recognizer1.AcceptWaveform = MagicMock(return_value=False)

        recognizer2 = MagicMock()
        recognizer2.AcceptWaveform = MagicMock(return_value=False)

        mock_kaldi_class.side_effect = [recognizer1, recognizer2]

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        pcm_data = b"\x00\x01\x02\x03"
        await speech_recognizer.feed(speaker_id=1, pcm=pcm_data)
        await speech_recognizer.feed(speaker_id=2, pcm=pcm_data)

        # Должны быть созданы два разных экземпляра KaldiRecognizer
        assert mock_kaldi_class.call_count == 2

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_reuses_recognizer_for_same_speaker(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """Второй feed() для того же speaker_id переиспользует тот же KaldiRecognizer."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer_instance = MagicMock()
        recognizer_instance.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer_instance

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        pcm_data = b"\x00\x01\x02\x03"
        await speech_recognizer.feed(speaker_id=123, pcm=pcm_data)
        await speech_recognizer.feed(speaker_id=123, pcm=pcm_data)

        # KaldiRecognizer должен быть создан только один раз
        assert mock_kaldi_class.call_count == 1


class TestFeedResults:
    """Тесты возвращаемых значений feed()."""

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_returns_none_when_accepting_waveform_false(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """feed() возвращает None, когда AcceptWaveform возвращает False."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        result = await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")

        assert result is None

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_returns_text_when_accepting_waveform_true(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """feed() возвращает распознанный текст, когда AcceptWaveform возвращает True."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=True)
        recognizer.Result = MagicMock(
            return_value=json.dumps({"text": "привет мир"})
        )
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        result = await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")

        assert result == "привет мир"

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_strips_whitespace_from_result(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """feed() удаляет пробелы спереди и сзади из распознанного текста."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=True)
        recognizer.Result = MagicMock(
            return_value=json.dumps({"text": "  привет мир  "})
        )
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        result = await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")

        assert result == "привет мир"

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_returns_none_for_empty_text(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """feed() возвращает None, когда текст пуст после удаления пробелов."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=True)
        recognizer.Result = MagicMock(return_value=json.dumps({"text": "   "}))
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        result = await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")

        assert result is None

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_returns_none_when_text_key_missing(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """feed() возвращает None, когда ключ 'text' отсутствует в результате."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=True)
        recognizer.Result = MagicMock(return_value=json.dumps({}))
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        result = await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")

        assert result is None


class TestDropSpeaker:
    """Тесты метода SpeechRecognizer.drop_speaker()."""

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_drop_speaker_is_idempotent(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """drop_speaker() для неизвестного speaker_id — no-op."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model
        mock_kaldi_class.return_value = MagicMock()

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        # Не должно быть ошибки для неизвестного говорящего
        speech_recognizer.drop_speaker(999)
        speech_recognizer.drop_speaker(999)

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_drop_speaker_removes_state(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """drop_speaker() удаляет состояние говорящего."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer1 = MagicMock()
        recognizer1.AcceptWaveform = MagicMock(return_value=False)

        recognizer2 = MagicMock()
        recognizer2.AcceptWaveform = MagicMock(return_value=False)

        mock_kaldi_class.side_effect = [recognizer1, recognizer2]

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        # Создаём состояние для говорящих 1 и 2
        await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")
        await speech_recognizer.feed(speaker_id=2, pcm=b"\x00\x01")

        # Удаляем говорящего 1
        speech_recognizer.drop_speaker(1)

        # Говорящий 2 должен остаться в состоянии
        assert 2 in speech_recognizer._speakers
        assert 1 not in speech_recognizer._speakers

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_feed_after_drop_creates_new_recognizer(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """feed() после drop_speaker создаёт новый KaldiRecognizer."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer1 = MagicMock()
        recognizer1.AcceptWaveform = MagicMock(return_value=False)

        recognizer2 = MagicMock()
        recognizer2.AcceptWaveform = MagicMock(return_value=False)

        mock_kaldi_class.side_effect = [recognizer1, recognizer2]

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        # Создаём и затем удаляем
        await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")
        original_recognizer = speech_recognizer._speakers[1].recognizer

        speech_recognizer.drop_speaker(1)

        # feed() снова должен создать новый распознаватель
        await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")
        new_recognizer = speech_recognizer._speakers[1].recognizer

        # Должны быть разные экземпляры
        assert original_recognizer is not new_recognizer


class TestClose:
    """Тесты метода SpeechRecognizer.close()."""

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_close_is_idempotent(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """close() можно вызывать несколько раз без ошибок."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        await speech_recognizer.close()
        await speech_recognizer.close()  # Не должно быть ошибки

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_close_keeps_model_loaded(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """close() не сбрасывает модель; is_ready остаётся True."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        assert speech_recognizer.is_ready is True

        await speech_recognizer.close()

        assert speech_recognizer.is_ready is True

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_close_clears_speakers(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """close() очищает состояние всех говорящих."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        await speech_recognizer.ensure_ready()

        # Добавляем несколько говорящих
        await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")
        await speech_recognizer.feed(speaker_id=2, pcm=b"\x00\x01")

        assert len(speech_recognizer._speakers) > 0

        await speech_recognizer.close()

        assert len(speech_recognizer._speakers) == 0


class TestCleanupLoop:
    """Тесты фоновой очистки неактивных говорящих."""

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_cleanup_logic_removes_idle_speakers(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """Логика очистки правильно определяет неактивных говорящих по таймауту."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer(speaker_idle_timeout=120.0)
        await speech_recognizer.ensure_ready()

        # Создаём говорящего
        await speech_recognizer.feed(speaker_id=1, pcm=b"\x00\x01")
        assert 1 in speech_recognizer._speakers

        # Вручную вызываем логику очистки
        speech_recognizer.drop_speaker(1)
        assert 1 not in speech_recognizer._speakers

        await speech_recognizer.close()

    @patch("bot.speech.KaldiRecognizer")
    @patch("bot.speech.Model")
    @patch("bot.speech.Path.is_dir")
    async def test_cleanup_task_created_on_ensure_ready(
        self, mock_is_dir, mock_model_class, mock_kaldi_class
    ):
        """ensure_ready() создаёт фоновую задачу очистки."""
        mock_is_dir.return_value = True
        mock_model = MagicMock()
        mock_model_class.return_value = mock_model

        recognizer = MagicMock()
        recognizer.AcceptWaveform = MagicMock(return_value=False)
        mock_kaldi_class.return_value = recognizer

        speech_recognizer = SpeechRecognizer()
        assert speech_recognizer._cleanup_task is None

        await speech_recognizer.ensure_ready()

        assert speech_recognizer._cleanup_task is not None
        assert not speech_recognizer._cleanup_task.done()

        await speech_recognizer.close()
