@echo off
rem Файл сохранён в кодировке CP866 - иначе cmd.exe ломается на русских буквах.
setlocal
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ОШИБКА] Не найдено виртуальное окружение: %PYTHON%
    echo Создайте его и установите зависимости - раздел 3 README:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    goto :fail
)

if not exist ".env" (
    echo [ОШИБКА] Нет файла .env с настройками.
    echo Скопируйте .env.example в .env и заполните DISCORD_TOKEN, GUILD_ID и YANDEX_MUSIC_TOKEN:
    echo     copy .env.example .env
    goto :fail
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [ВНИМАНИЕ] ffmpeg не найден в PATH. Если он установлен в другой каталог,
    echo            укажите путь к нему в переменной FFMPEG_PATH в .env.
    echo.
)

echo Запуск бота. Для остановки - Ctrl+C.
echo.
"%PYTHON%" -m bot
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo Бот завершился с кодом %CODE%. Подробности - выше и в logs\bot.log.
    goto :fail
)

echo.
echo Бот остановлен.
endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1
