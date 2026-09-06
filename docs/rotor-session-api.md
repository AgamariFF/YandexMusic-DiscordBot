# Сессионный rotor-API Яндекс.Музыки

Протокол, которым пользуется официальное десктопное приложение
(`YandexMusic/5.114.1`, Electron 38). Восстановлен 6 сентября 2026 из бандла
приложения (`resources/app.asar`) и **проверен вживую** на боевом API.

Бот сейчас работает по «классическому» ротору
(`/rotor/station/<станция>/tracks` + `/feedback`). Классический ротор не
исчез и продолжает отвечать `200`, но приложение перешло на сессионный —
именно в нём у прослушивания есть идентификатор, к которому привязываются
все оценки.

## Зачем это нужно

У классического ротора нет идентификатора сеанса: максимум, что связывает
фидбек с выдачей, — `batch-id` очередной пачки. В сессионном API появляется
`radioSessionId`, и сервер понимает, какую именно волну правит очередной
скип или дизлайк.

## Идентификатор сессии

`radioSessionId` — строка вида `52Aj-bdctPL0vlCssal-5itl`. Живёт с момента
создания сессии до её конца, `batchId` внутри неё меняется на каждой пачке.

## Эндпоинты

Базовый адрес — `https://api.music.yandex.net`.

| Метод и путь | Назначение |
| --- | --- |
| `POST /rotor/session/new` | создать сессию |
| `POST /rotor/session/<sid>/tracks` | следующая пачка треков |
| `POST /rotor/session/<sid>/clone` | клонировать сессию |
| `POST /rotor/session/<sid>/feedback/` | один фидбек |
| `POST /rotor/session/<sid>/feedbacks/` | пачка фидбеков |
| `POST /rotor/sessions/feedbacks/` | фидбеки сразу по нескольким сессиям |
| `GET /rotor/wave/last` | последняя волна пользователя |
| `GET /rotor/wave/settings?seeds=…` | настройки волны для набора seed'ов |
| `POST /rotor/wave/last/reset` | сбросить последнюю волну |

**Завершающий слэш у путей фидбека обязателен.** Без него сервер не отвечает
`404`, а обрывает соединение (`ConnectionError`/`RemoteDisconnected`) — на
это легко потратить много времени, приняв обрыв за сетевую проблему.
`GET` на эти пути даёт `405`, то есть маршрут существует и ждёт `POST`.

## Создание сессии

```jsonc
POST /rotor/session/new
{
  "seeds": ["track:20599729"],      // "user:onyourwave" — обычная «Моя волна»
  "includeTracksInResponse": true,
  "trackToStartFrom": "20599729"    // необязателен, см. ниже
}
```

Ответ: `radioSessionId`, `batchId`, `acceptedSeeds`, `descriptionSeed`,
`pumpkin` и `sequence` — список элементов с полями `track`, `liked`,
`trackParameters` (`bpm`, `energy`, `hue`, `mixFade`).

Приложение умеет передавать также `queue`, `clientRemoteType`, `incognito`,
`child`, `allowExplicit`, `aliceExperiments`, `djData`, `useIchwill`,
`includeWaveModel`, `interactive` и `sessions` (список
`{sessionId, seeds, feedbacks}` — так переносятся оценки из прошлых сессий).

### Волна от конкретного трека

Ровно это делает приложение по кнопке «Моя волна» на треке:
`seeds: ["track:<id>"]` плюс `trackToStartFrom: "<id>"`. Проверено: с
`trackToStartFrom` запрошенный трек становится **первым** в `sequence`, без
него волна начинается с похожего, но другого трека.

Тот же сценарий доступен и на классическом роторе — станция `track:<id>`
отвечает и на `/info`, и на `/tracks?settings2=True`, — но там нельзя
попросить начать именно с этого трека.

## Фидбек

```jsonc
POST /rotor/session/<sid>/feedbacks/
{
  "feedbacks": [
    {
      "event": { "type": "trackStarted", "timestamp": 1788695215.0, "trackId": "20599729" },
      "batchId": "1788695214629126-16941439846510084672.Q5eb",
      "from": "desktop_win-radio-track-default"
    }
  ]
}
```

Обратите внимание: полезная нагрузка лежит во вложенном объекте **`event`**,
а `batchId` и `from` — соседи `event`, а не его поля. В классическом роторе
`type`/`timestamp`/`trackId` лежали на верхнем уровне, а `batch-id` был
query-параметром. Одиночный `POST …/feedback/` принимает такой же объект без
обёртки `feedbacks`.

Состав `event` по типам (как его собирает приложение):

- `radioStarted` — `type`, `timestamp`;
- `trackStarted`, `like`, `unlike`, `undislike` — плюс `trackId`;
- `skip`, `dislike` — плюс `trackId`, `totalPlayedSeconds`;
- `trackFinished` — плюс `trackId`, `totalPlayedSeconds`, `trackLengthSeconds`.

Полный список типов из бандла: `radioStarted`, `trackStarted`,
`trackFinished`, `skip`, `skipFailed`, `like`, `dislike`, `ad`, `jingle`,
`unlike`, `undislike`, `combinedQueueStarted` и семейство
`playableItem*` (для не-музыкальных сущностей).

**`like` и `dislike` здесь — типы фидбека ротора.** То есть волне можно
сказать «такое мне не нравится» сигналом заметно более сильным, чем скип, не
трогая коллекцию пользователя.

## Фидбеки вместе с запросом треков

Главная особенность, которой нет в классическом роторе:

```jsonc
POST /rotor/session/<sid>/tracks
{ "queue": ["20599729", "19801149"], "feedbacks": [ { "event": {…}, "batchId": "…", "from": "…" } ] }
```

Оценки уходят **тем же запросом**, которым запрашивается следующая пачка, —
сервер учитывает их при подборе и сразу возвращает новый `batchId`. При
раздельной отправке гонка неизбежна: пачка может быть подобрана раньше, чем
доедет скип.

## Проверено вживую

6 сентября 2026, с реальным токеном: `session/new` (в том числе с
`trackToStartFrom`), `feedbacks/` пачкой, `feedback/` с `skip` и с
`dislike`, `tracks` с фидбеками в теле — все ответили `200`.
