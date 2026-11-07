# rob_box_station — хост РОББОКСа на Яндекс Станции Лайт

Колонка без Алисы, на которой крутится ядро диалога РОББОКСа (`rob-box-dialog-core`): микрофон → VAD → STT → ход v2 →
TTS → динамик. Границы и план — ADR-0167 в `krikz/rob_box_project` (`docs/adr/0167-dialog-core-repo-and-station-repo.md`).

Состояние на 2026-11-07 (замеры — `docs/inventory_2026-11-07.md`):

| Что | Статус |
|---|---|
| Рут, SSH `-p 2222`, OTA off | сделано, runbook пока в `rob_box_project/docs/runbooks/yandex-station-root.md` |
| Алиса заглушена (`scripts/S99zzyandexoff` в `/etc/init.d/`, маркер `/data/opt/yandex_off`) | проверено reboot'ом 07.11 |
| Питон 3.12 armv7 на колонке (`tools/prune_python.py`) | работает, 29,6 МБ |
| Ядро + колёса armv7 (`tools/bundle_core.py`), консоль (`tools/run_console.py`) | живой ход, пик 49,7 МБ |
| Хост `scripts/station_host.py`: микрофон `plughw:0,1` → VAD → STT MiniMax → ход v2 → TTS MiniMax → динамик | **работает голосом** 07.11: «Игорек, который час?» → ответ, 8,2 с до звука |
| Вейк колонки «Игорёк» (`RBX_WAKE`), громкость Master 230 (`RBX_VOLUME`) | сделано |
| Автозапуск хоста при загрузке | **нет** — запущен руками `setsid nohup`, после reboot не встанет |
| Кнопки, кольцо LED | не начато |
| Настройка Wi-Fi по Bluetooth, страница настроек | не начато (ADR-0167 Ш6) |
| Ядро как пакет из `krikz/rob_box_dialog_core` по тегу | ждёт ADR-0167 Ш1–Ш2; сейчас файлы ядра льются из чекаута `rob_box_project` |

## Установка одной строкой

С компьютера (Linux, Mac, Git Bash), где есть SSH к колонке после рута
([runbook](https://github.com/krikz/rob_box_project/blob/develop/docs/runbooks/yandex-station-root.md)):

```bash
curl -sL https://raw.githubusercontent.com/krikz/rob_box_station/main/install.sh | sh -s -- 10.1.1.13
```

Ставит Питон, библиотеки и ядро (ассет релиза `station-runtime-armv7.tar.gz`), код колонки последнего релиза,
автозапуск, глушение Алисы, приветствие SSH. Потом — ключи на странице `http://<колонка>:8080`. Обновления —
«Игорёк, проверь обновление» / «Игорёк, обновись». `DRY_RUN=1` — показать шаги без изменений.
Нужен релиз с ассетом (`gh release create vX.Y.Z station-runtime-armv7.tar.gz`) — первый ещё не выпущен.

## Хендофф 07.11.2026 (что знать следующей сессии)

**Замер задержки** (лог `/tmp/station.log`, 12:45 CET): STT 1,73 с + ход 3,73 с + синтез 2,74 с = 8,20 с от конца фразы
до звука. Было 31 с: ресемпл 48→16 кГц на Питоне под GIL растягивал ход 3,6 → 22 с; лечится `plughw:0,1`.

**Грабли, пойманные руками:**

- Уровни (PDM Gain 0 из 48, как оставила Алиса): шум 13–45 RMS, голос с метра ~200, свой динамик в своём микрофоне
  200–350; щелчок старта `arecord` ~15000 (он, а не речь, дал «RMS 1074» в первом selftest). Порог VAD — 150.
- MiniMax STT отвечает 429 даже на первый запрос после паузы — хост ждёт 20 с. Кандидат — Yandex STT, как у робота.
- Каждый звук громче порога уходит в облачный STT, даже без вейка (фон мастерской ~7 фраз/мин) — нужен вейк до облака
  (родные модели Алисы `/system/vendor/quasar/spotter_models` работали внутри `maind`; отдельно не пробовали).
- Ход замедляется до 13–18 с: в запрос LLM идут прошлые ходы и `background_since_reply` (фон). Для колонки фон резать.
- Робот рядом тоже отвечает на «Робот, …» (его часы со сдвигом +2 ч — догадка по логу) — поэтому вейк «Игорёк».
- Консоль печатает время хода `0.0 с` — счётчик не меряет, не опираться.
- Часовой пояс колонки UTC — хост запускать с `TZ=CET-1`.

**Запуск / остановка хоста на колонке:**

```bash
ssh -p 2222 root@10.1.1.13 'cd /data/opt/rbx; TZ=CET-1 setsid nohup /data/opt/python/bin/python3.12 -X utf8 -u station_host.py --db /data/opt/rbx/station_memory.db > /tmp/station.log 2>&1 < /dev/null &'
ssh -p 2222 root@10.1.1.13 'kill $(pidof python3.12); killall arecord'
ssh -p 2222 root@10.1.1.13 'grep -E "🎤|ход [0-9]|🔊|429|Error" /tmp/station.log | tail'
```

**Автозапуск** (`scripts/S99zzzrbxhost`): после загрузки ждёт 60 с, пока `S99zzyandexoff` заглушит Алису, и держит
хост в цикле — при падении рестарт с паузой 10 с × номер попытки (потолок 5 мин). Прошлый лог — `/tmp/station.log.1`.
Работает, пока есть маркер `/data/opt/rbx_host`. Установка:

```bash
ssh -p 2222 root@10.1.1.13 'mount -o remount,rw / && cat > /etc/init.d/S99zzzrbxhost && chmod +x /etc/init.d/S99zzzrbxhost && sync && mount -o remount,ro / && touch /data/opt/rbx_host' < scripts/S99zzzrbxhost
ssh -p 2222 root@10.1.1.13 '/etc/init.d/S99zzzrbxhost restart'   # без перезагрузки; stop — погасить до следующей
```

`RBX_DEBUG=1` — уровни VAD раз в 3 с. Ключи — `/data/opt/rbx/core/scripts/dialogue/.env` (0600), их в git нет.

**Следующие шаги (совет — по порядку):**

1. Автозапуск хоста: init-скрипт `S99zzzrbxhost` (выше) — стоит на колонке, проверен перезагрузкой 07.11 (хост поднялся через ~60 с).
2. ~~Потоковый синтез~~ сделан 07.11: куски SSE MiniMax сразу в `aplay`. Замер `--say` на колонке, фраза ~9,5 с звука,
   по 3 прогона: до первого звука целым куском 4,69–5,10 с, потоком 1,02–1,24 с. Паузы внутри фразы по записи своим
   микрофоном почти те же (поток — на одну паузу 0,4 с больше; недобор куска или естественная — на слух не проверено).
   `RBX_TTS_STREAM=0` — старый путь; если поток упал до первого куска — повтор целым синтезом с записью в лог.
   **Живые выходы хода** (07.11, `LiveOut`): стенд консоли только пишет `stage` и реплики, хост их перехватывает —
   earcon «услышал» (`assets/heard.raw` = `button_click.mp3` робота) на `addressed`, реплики в динамик по ходу.
   Замер `--turn 'Игорёк, как дела?'`: earcon сразу, «Дай подумать.» на 7,86 с, ответ на 13,08 с (ход 12,61 с).
3. Медленный ход: в замере выше фона в снимке нет, а ход 12,6 с — почти всё ожидание DeepSeek (по логу
   `LLM REQUEST START`→`RESPONSE`). Гипотеза «виноват фон» не подтвердилась; мерить сам запрос (длина промпта, кэш,
   другая модель). Затем Yandex STT (поток, нет 429).
   Замер `tools/probe_turn.py` (07.11): ядро на колонке 0,05–0,15 с; DeepSeek на тот же промпт (7380 ток., кэш 97 %)
   то 1,9 с, то 12 с — ~4 из 15 запросов 6–12 с; на следующих 12 ходах провалов не было. Хедж в хосте
   (`RBX_HEDGE_S`, 4 с): не ответил — второй такой же запрос, берётся первый ответ. Механика проверена при 0,5 с;
   выигрыш второго запроса на живом провале ещё не пойман. Место хеджу — ядро (поможет и роботу).
   Голос сессии (`set_voice`) хост отдаёт MiniMax; после рестарта хоста — снова голос по умолчанию.
4. Кнопки и кольцо — сделаны 07.11 (`Keys`, `Buttons`, `Ring` в хосте), коды по нажатиям Шифу: event0 113 MUTE,
   event1 114/115 громкость −/+, 582 кнопка Алисы. Громкость ±25 (60–255) + щелчок + белые огоньки по уровню; MUTE —
   микрофон выкл (красное кольцо); кнопка Алисы — во время речи стоп, иначе 8 с слушать без вейка (синее). Кольцо:
   фиолетовое — думаю, зелёное — говорю. Сенсорные кнопки срабатывают парой — второе нажатие в 150 мс отброшено.
   Кольцо — родные анимации Алисы (`/system/vendor/quasar/ledpatterns/*.led`: `loop`/`gammacorrection` + кадры
   «3 цвета hex + мс»): слушаю/думаю/говорю/сеть/ошибка; файлов нет — эффекты кода. Яркость `RBX_LED` из 255 (255 —
   как у Алисы). Прежние эффекты кода (07.11): синее дыхание, бегущий фиолетовый огонёк, зелёная пульсация; ~12 кадров/с, в покое
   5/с; хост в покое ~7 % одного ядра (замер `/proc/<pid>/stat`, 71 тик за 10 с, вместе с микрофоном и VAD).
   **Промпт колонки** — `prompts/master_prompt_compact.txt` (ставится в `/data/opt/rbx/prompts/`): колонка «Игорёк», не
   ровер; что умеет и чего нет. Инструменты-заглушки стенда (музыка, лицо, звуки) — честный отказ `unavailable`; поиск в интернете —
   настоящий тул робота (`web_search.py` в ядре колонки; DuckDuckGo нет — отвечает поиск DeepSeek, CA — `certifi`);
   громкость голосом — настоящая (`amixer`), статус/батарея/камера — настоящие колонки.
   Живая проверка кнопок — после заливки, по логу `🔘`.
5. ADR-0167 Ш6 — сделано 07.11 (`scripts/station_setup.py`, тесты `tests/test_station_setup.py`), живьём с телефона
   не проверено:
   * **Wi-Fi по Bluetooth — Improv Wi-Fi.** Android, Chrome → improv-wifi.com → Connect → устройство «Igorek» →
     нажать любую кнопку колонки (окно 60 с) → имя сети и пароль. Новая сеть добавляется к сохранённым с приоритетом
     выше; не подключилась за 30 с — удаляется, колонка возвращается в прежнюю. Нужен `dbus-next` в `site/` (разрешение
     Шифу 07.11; колесо 0.2.3 с PyPI, чистый Питон).
   * **Страница настроек** `http://<колонка>:8080/`: вейк, голос, громкость, яркость, ключи (видны хвосты). Сохранить —
     только после нажатия любой кнопки колонки (без него 403). Настройки — `/data/opt/rbx/settings.json`, ключи — в `.env`; новые ключи
     перезапускают хост (`execv`).
   * **Промпт на странице**: «дополнение для колонки» (в конец системного промпта) и «основа» (свой текст вместо
     `master_prompt_compact.txt` ядра, «вернуть основу ядра»). Свои копии — `/data/opt/rbx/prompts/`, ядро не правится;
     хост собирает промпт в `<workdir>/prompt` и отдаёт стенду как `PROMPT_DIR`. Сохранение перезапускает хост.
   * **Wi-Fi на странице**: текущая сеть, список вокруг (`wpa_cli scan_results`), подключение тем же безопасным путём,
     что Improv; итог — кольцо мигает зелёным/красным и строка «последняя попытка» на странице.
6. ADR-0167 Ш1 в `rob_box_project` (промпт, `wake_words.yaml`, `migrations/*.sql` — в пакет ядра), затем Ш2 — залить
   `krikz/rob_box_dialog_core` и ставить ядро отсюда по тегу.

**Хвост в `rob_box_project`:** ветка `feat/station-yandex-off` (worktree `D:/PROJECTS/rob_box_wt_station`, 4 коммита,
не запушена) — её файлы уже здесь; шаг 8 runbook рута (установка `S99zzyandexoff`) — команда ниже. Ветку можно удалить.

```bash
ssh -p 2222 root@10.1.1.13 'mount -o remount,rw / && cat > /etc/init.d/S99zzyandexoff && chmod +x /etc/init.d/S99zzyandexoff && sync && mount -o remount,ro / && touch /data/opt/yandex_off' < scripts/S99zzyandexoff
```

## Раскладка на колонке

```
/data/opt/python/            Питон 3.12 (python-build-standalone armv7, обрезан)
/data/opt/rbx/site/          колёса armv7: openai, httpx, pydantic, … + PyYAML (чистый Питон)
/data/opt/rbx/core/          файлы ядра из rob_box_project (src/<пакет>/<пакет>, scripts/dialog*, prompts, migrations)
/data/opt/rbx/core/scripts/dialogue/.env   ключи (0600, root)
/data/opt/rbx/station_host.py
```

## Как залить (с ПК, Git Bash)

```bash
# Питон: релиз astral-sh/python-build-standalone, cpython-3.12.*-armv7-unknown-linux-gnueabihf-install_only_stripped.tar.gz
python -I tools/prune_python.py py.tar.gz py_pruned.tar.gz
ssh -p 2222 root@10.1.1.13 'cd /data/opt && rm -rf python && gunzip -c | tar xf -' < py_pruned.tar.gz
# колёса: pip download --only-binary=:all: --python-version 3.12 --platform manylinux2014_armv7l … -d dl_whl
# + чистый Питон: pip download dbus-next==0.2.3 --no-deps -d dl_whl  (Improv по Bluetooth)
# ядро: tools/bundle_core.py (колёса + PyYAML + список файлов ядра) → /data/opt/rbx
ssh -p 2222 root@10.1.1.13 'cd /data/opt/rbx; TZ=CET-1 /data/opt/python/bin/python3.12 -X utf8 station_host.py --selftest'
```

На колонке нет `scp`, `nc`, `curl`, `tar -z`: файлы — через `ssh … 'cat > файл'` или `gunzip -c | tar xf -`.
Часовой пояс колонки — UTC, ходу ставить `TZ`.

Правила работы — как в `rob_box_project/AGENTS.md`: «Честный FAIL лучше красивого PASS», raw-вывод в каждом утверждении.
