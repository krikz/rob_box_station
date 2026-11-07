# rob_box_station — хост РОББОКСа на Яндекс Станции Лайт

Колонка без Алисы, на которой крутится ядро диалога РОББОКСа (`rob-box-dialog-core`): микрофон → VAD → STT → ход v2 →
TTS → динамик. Границы и план — ADR-0167 в `krikz/rob_box_project` (`docs/adr/0167-dialog-core-repo-and-station-repo.md`).

Состояние на 2026-11-07 (замеры — `docs/inventory_2026-11-07.md`):

| Что | Статус |
|---|---|
| Рут, SSH `-p 2222`, OTA off | сделано, runbook пока в `rob_box_project/docs/runbooks/yandex-station-root.md` |
| Алиса заглушена (`scripts/S99zzyandexoff`, маркер `/data/opt/yandex_off`) | проверено reboot'ом |
| Питон 3.12 armv7 на колонке (`tools/prune_python.py`) | работает, 29,6 МБ |
| Ядро + колёса armv7 (`tools/bundle_core.py`), консоль (`tools/run_console.py`) | живой ход «который час», пик 49,7 МБ |
| Голос (`scripts/station_host.py --selftest`) | синтез → динамик → микрофон → STT — та же фраза |
| Постоянный хост с вейком (`station_host.py` без ключей) | **не запускался** |
| Настройка Wi-Fi по Bluetooth, страница настроек | не начато (ADR-0167 Ш6) |
| Ядро как пакет из `krikz/rob_box_dialog_core` по тегу | ждёт ADR-0167 Ш1–Ш2; сейчас файлы ядра льются из чекаута `rob_box_project` |

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
