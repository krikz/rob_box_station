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

`RBX_DEBUG=1` — уровни VAD раз в 3 с. Ключи — `/data/opt/rbx/core/scripts/dialogue/.env` (0600), их в git нет.

**Следующие шаги (совет — по порядку):**

1. Автозапуск хоста: init-скрипт `S99zzrbxhost` рядом с `S99zzyandexoff` (rootfs `remount,rw`), рестарт при падении.
2. Потоковый синтез MiniMax — первый звук ~1 с вместо 2,7 (догадка).
3. Контекст хода колонки без фона мастерской; затем Yandex STT (поток, нет 429).
4. Кнопки (`/dev/input/event0`, `event1`) → громкость, пауза, «стоп»; кольцо LED `micro:{red,green,blue}:ring{1,2,3}` →
   слушаю / думаю / говорю.
5. ADR-0167 Ш6: Wi-Fi по Bluetooth (BlueZ + D-Bus, Improv Wi-Fi) и страница настроек (ключи, промпт, провайдеры).
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
# ядро: tools/bundle_core.py (колёса + PyYAML + список файлов ядра) → /data/opt/rbx
ssh -p 2222 root@10.1.1.13 'cd /data/opt/rbx; TZ=CET-1 /data/opt/python/bin/python3.12 -X utf8 station_host.py --selftest'
```

На колонке нет `scp`, `nc`, `curl`, `tar -z`: файлы — через `ssh … 'cat > файл'` или `gunzip -c | tar xf -`.
Часовой пояс колонки — UTC, ходу ставить `TZ`.

Правила работы — как в `rob_box_project/AGENTS.md`: «Честный FAIL лучше красивого PASS», raw-вывод в каждом утверждении.
