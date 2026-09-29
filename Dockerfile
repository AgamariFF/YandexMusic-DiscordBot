# Сборка в два этапа: зависимости ставятся в отдельный слой, а в итоговый
# образ переезжает только готовое виртуальное окружение. Так в рантайме не
# остаётся ни компилятора, ни заголовков — образ меньше, поверхность уже.
#
# Базовый образ пришпилен к конкретному релизу Debian (bookworm), а не взят
# как «python:3.12-slim»: имена системных пакетов между релизами меняются
# (libvpx7 -> libvpx9 и подобное), и плавающий тег однажды молча сломал бы
# сборку в момент, когда Docker Hub переедет на следующий Debian.

FROM python:3.12-slim-bookworm AS builder

# Почти всё дерево зависимостей — либо чистый Python (aiortc, vosk-tts,
# discord-ext-voice-recv, python-socketio), либо готовые колёса manylinux
# (PyNaCl, cryptography, av, vosk). Компилятор здесь нужен как страховка
# для пакетов, которые опубликованы только исходниками (сейчас это
# yandex-music) и для случая, когда под новую версию колеса ещё нет.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        python3-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Сначала только список зависимостей, отдельным слоем: правка кода бота не
# должна обесценивать кэш и тянуть переустановку всего дерева пакетов.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt


FROM python:3.12-slim-bookworm AS runtime

# ffmpeg — не опциональная зависимость, а основной декодер музыки: бот
# запускает его отдельным процессом (см. FFMPEG_PATH в .env.example, по
# умолчанию просто «ffmpeg» из PATH).
# libopus0 нужен discord.py, чтобы кодировать исходящий звук; libgomp1 —
# нативной части vosk (OpenMP).
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ffmpeg \
        libopus0 \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Бот не слушает входящие соединения и не нуждается в правах root.
RUN useradd --create-home --uid 1000 appuser

# PYTHONUNBUFFERED — чтобы логи шли в docker logs сразу, а не копились в
# буфере: без него вывод контейнера отстаёт и отладка идёт вслепую.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser bot/ ./bot/
COPY --chown=appuser:appuser pyproject.toml ./

# Каталоги под монтирование. Модели специально НЕ копируются в образ:
# vosk-model-ru-0.42 весит около двух гигабайт, и зашивать её в слои значит
# раздувать образ и пересобирать его ради обновления модели. Каталог кэша
# нужен vosk-tts: модель синтеза он скачивает сам при первой фразе, и без
# тома качал бы её заново после каждого пересоздания контейнера.
RUN mkdir -p /app/models /app/logs /home/appuser/.cache/vosk \
    && chown -R appuser:appuser /app/models /app/logs /home/appuser/.cache

USER appuser

CMD ["python", "-m", "bot"]
