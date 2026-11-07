# Осмотр Яндекс Станции Лайт 1 — 2026-11-07 (#4246, ADR-0160 §8 К0)

Снято `ssh -p 2222 root@10.1.1.13 'sh -s' < scripts/station/inventory.sh` (только чтение), 2026-11-07T10:09:56Z.
Сырой вывод — в конце файла. Ниже — выжимка и граница выноса.

## Что есть на колонке (измерено)

| Что | Значение | Откуда |
|---|---|---|
| Ядро / userland | Linux 4.9.68 aarch64, Buildroot 2020.02.1, userland armhf (`/lib/ld-linux-armhf.so.3`, glibc 2.30) | раздел «железо», «что можно запустить» |
| Процессор | 4 ядра ARMv8 (A113, `Yandex Station Micro v1.2`) | `/proc/cpuinfo`, device-tree |
| ОЗУ | 244 МБ всего; доступно 135 МБ с Алисой, **176 МБ без неё** | `/proc/meminfo`, `free` после `S99zzyandexoff` |
| Флеш | `/` ubifs **ro**, свободно 12 МБ; `/data` (= `/bkupgrade/data`) rw, **свободно 76 МБ**; swap нет | `df` |
| Языки и пакеты | Python нет, opkg нет, curl/openssl/nc нет; есть busybox 1.31.1, `wget`, `telnet` | «что можно запустить», ручная проверка |
| Звук | одна карта `AML-AUGESOUND`: `hw:0,0` динамик (TDM, ntp8910a) + петля, `hw:0,1` PDM 8 каналов | `/proc/asound/pcm` |
| ALSA | `default` = `soft_volume`→`dmix` (динамик) и `multi_capture` (4 микрофона: каналы 0,2,4,6 + петля 2 канала) | `/etc/asound.conf` |
| Утилиты звука | `arecord`, `aplay`, `amixer` есть | ручная проверка |
| Стек Яндекса | `quasar_launcher2` + `maind --telemetry/--fluent-bitd/--platformd/--mediad` | `ps` |
| LED / кнопки | кольцо `micro:{red,green,blue}:ring{1,2,3}`, `event0`, `event1` | `/sys/class/leds`, `/dev/input` |

Проверка звука без Алисы (`killall quasar_launcher2 maind`, 2026-11-07 10:10 UTC):

- `arecord -D multi_capture -f S16_LE -c 6 -t raw` пишет; ALSA отдаёт **48 кГц** вместо 16 (нужен `plug:` или ресемпл на хосте).
  Каналы 0–3 (микрофоны): RMS ≈ −23 dBFS, пик 32765 (похоже на щелчок старта; речь не проверялась); каналы 4–5 (петля) — 0,
  пока ничего не играет.
- `aplay -D default -f S16_LE -r 48000 -c 2` тон 440 Гц 1 с — `aplay_exit=0`. **Слышен ли — не проверено ушами.**
- watchdog (`watchdog -t 1 -T 5`) без Алисы колонку не перезагружает: `up 29 min` после kill.

Своего эхоподавления и луча (`vqe_0` из исследования) без стека Яндекса нет: это делал `maind`. Остаются сырые 4 микрофона
и петля динамика — эхоподавление делать самим по петле.

## Память ядра диалога (измерено на ПК, не на колонке)

`import scripts.dialog.dry_stand` (настоящий `TurnEngine` + мост команд + тулы памяти) в Python 3.11 x86-64 на Windows:
частная память 9,3 → **63,6 МБ**, 1353 модуля (`openai`, `httpx`, `pydantic`, `yaml`, `sqlite3`), 4,1 с.
На armhf 32 бит ожидаемо меньше, но это **догадка**, не замер. Время импорта на A53 из NAND — не мерено.

## Граница выноса — варианты

Тракт: микрофон → эхо/луч → VAD → вейк → STT (облако) → `TurnEngine` (LLM облако) → владельцы (память, музыка) →
адаптер провайдера → TTS (облако) → динамик. На колонке обязаны жить микрофон, динамик, LED и кнопки; облако остаётся
облаком. Двигается только середина.

| Вариант | На колонке | На хосте (Sound Pi) | Что нужно | Риск |
|---|---|---|---|---|
| **А. Тонкая колонка** | `arecord`/`aplay`, LED — штатное, ничего не ставим | эхо, VAD, вейк, STT, ядро, TTS — нынешний голосовой стек, колонка = ещё один микрофон и динамик | мост звука по сети (SSH-поток или TCP), хост-приёмник колонки | без Sound Pi колонка немая; Wi-Fi-задержка |
| Б. Колонка слушает сама | + VAD и вейк (статический C-бинарь armhf) | STT, ядро, TTS | кросс-сборка, своё эхоподавление по петле | сборка под armhf, CPU A53 |
| В. Чат в колонке | + Python (Entware armv7) и пакет ядра (#4245) | ничего, кроме облаков | `/opt` из `/data`, ≤76 МБ флеша, ≤176 МБ ОЗУ | флеш и ОЗУ впритык; пакет ядра ещё не выделен (#4245) |

Вариант В — цель Шифу («чат запихнуть в колонку»), но упирается в #4245 (ядро пакетом без ROS) и в место. Вариант А не
требует ничего на колонке и даёт живую проверку #4247 («который час» голосом в колонку) на нынешнем стеке. Какой брать —
решает Шифу.

## План В: ядро на колонке — первый замер (2026-11-07, на колонке)

Шифу выбрал вариант В («да давай на питоне в начале пробуем», Telegram 07.11).

- Питон: `cpython-3.12.16+20261026-armv7-unknown-linux-gnueabihf-install_only_stripped.tar.gz`
  (astral-sh/python-build-standalone, sha256 `8ebcbfd8…896cd` совпал с релизом). Выкинуты libpython `.so`, tcl/tk,
  `site-packages`, `ensurepip`, `idlelib`, `test`, `__pycache__`: 67,2 → 28,4 МБ, на колонке `du` 29,6 МБ в `/data/opt/python`.
  Запуск `python3.12 -c …` — 0,86 с, `3.12.16`, `('glibc', '2.30')`.
- Библиотеки (wheel `manylinux*_armv7l` с PyPI): openai 3.46.0, httpx 0.28.1, pydantic 2.14.3 / pydantic_core 2.50.4, jiter,
  anyio и зависимости — 6,1 МБ. PyYAML 6.0.3 — чистый Питон из sdist (`lib/yaml`). Под armv7 нет numpy, sqlite-vec, PIL;
  в ядре они нужны только лицам (`face_roster`, `face_collage`) и векторной памяти (`voice_memory`, необязательный импорт).
- Ядро: 111 файлов репо, которые грузит `scripts.dialog.dry_stand` на ПК (2,2 МБ), в `/data/opt/rbx`.

| Замер `import scripts.dialog.dry_stand` на колонке | VmRSS | RssAnon | Время | Модулей |
|---|---|---|---|---|
| пустой Питон | 8,8 МБ | 2,4 МБ | — | — |
| первый прогон (без байткода) | 45,1 МБ | 32,3 МБ | 21,9 с | 994 |
| второй прогон (с `__pycache__`) | 41,0 МБ | 27,9 МБ | 8,3 с | 994 |

После: `/data` свободно 33,5 МБ; `free` — available 173 МБ.

### Живой ход консоли на колонке (2026-11-07 11:03–11:06 UTC)

Консоль `scripts/dialogue/chat.py --once …` (стенд v2: настоящие адресация, Tier-1, цепочка LLM `deepseek` с мостом
команд, исполнитель; владельцы — фейковые, речь — текстом). Файлы: пакеты `rob_box_core`, `rob_box_dialog`, `rob_box_harness`,
`rob_box_llm`, `rob_box_mcp_tools` целиком без тестов + 133 файла, которые консоль открывает на ПК (трасса `sys.addaudithook`):
`/data/opt/rbx/core` 8,7 МБ. Ключи — `scripts/dialogue/.env` консоли, на колонке `-rw-------` root (разрешение Шифу в чате 07.11).
Часовой пояс колонки — UTC, для хода `TZ=CET-1`.

```text
› Робот, который час?
🤖 Сейчас двенадцать ноль пять, суббота, седьмое ноября.
🔧 тулы: get_current_time  →  исходы: get_current_time: done
[ход 1 · вариант A · 0.0 с · вызовов LLM 2 · токены 14950 · касался: clock,speaker]
› Робот, запомни, что колонка стоит на кухне
🤖 Не могу запомнить: я ещё не знаю, как тебя зовут. Скажи имя, и я сохраню это.
[ход 1 · вариант A · 0.0 с · вызовов LLM 1 · токены 7965 · касался: speaker]
ПАМЯТЬ ['VmHWM:   49720 kB', 'VmRSS:   49720 kB'] процесс 14.1 с      # запуск + один ход «который час»
ПАМЯТЬ ['VmHWM:   49396 kB', 'VmRSS:   49396 kB'] процесс 13.1 с
```

- Пик памяти хода — 49,7 МБ. Запуск + ход — 13–14 с; ход без загрузки ≈ 5 с — вычитание (13,1 − 8,3), не прямой замер.
- Консоль печатает время хода `0.0 с` и на ПК — счётчик консоли не меряет ход, на него не опираться.
- `/data` после заливки: свободно 33,6 МБ.
- Не сделано: ядро постоянным процессом, голос (микрофон → STT, TTS → динамик).

## Колонка без Алисы

`scripts/station/S99zzyandexoff` в `/etc/init.d/` (ставится как `S99zdropbear`, через `remount,rw`): если есть маркер
`/data/opt/yandex_off`, через 20 с после загрузки глушит `quasar_launcher2` и `maind`. Вернуть Алису:
`rm /data/opt/yandex_off && reboot`; фабричный сброс возвращает всё. Проверено 2026-11-07 10:13 UTC: после `reboot`
`ps` — «яндекса нет», лог `/data/opt/yandex_off.log`: `Sat Nov  7 10:13:02 UTC 2026 yandex off`.

## Сырой вывод `inventory.sh`

```text

===== железо и ОС =====
Linux yandex-station-lite 4.9.68 #1 SMP PREEMPT Tue Oct 13 15:25:17 MSK 2026 aarch64 GNU/Linux
--- /proc/device-tree/model
Yandex Station Micro v1.2--- /etc/os-release
NAME=Buildroot
VERSION=2020.02.1-ge4999fe
ID=buildroot
VERSION_ID=2020.02.1
PRETTY_NAME="Buildroot 2020.02.1"
BUILD_ID=4778262442
BUILD_TYPE=user
--- /etc/issue
Welcome to Buildroot
processor	: 0
Features	: fp asimd evtstrm aes pmull sha1 sha2 crc32
CPU architecture: 8
CPU revision	: 4
processor	: 1
Features	: fp asimd evtstrm aes pmull sha1 sha2 crc32
CPU architecture: 8
CPU revision	: 4
ядер: 4

===== память, диск, нагрузка =====
--- /proc/meminfo
MemTotal:         243956 kB
MemFree:           77148 kB
MemAvailable:     135388 kB
Buffers:               0 kB
Cached:            56368 kB
SwapCached:            0 kB
Active:            68980 kB
Inactive:          26820 kB
Active(anon):      44036 kB
Inactive(anon):      264 kB
Active(file):      24944 kB
Inactive(file):    26556 kB
Unevictable:           0 kB
Mlocked:               0 kB
SwapTotal:             0 kB
SwapFree:              0 kB
Dirty:                24 kB
Writeback:             0 kB
AnonPages:         39468 kB
Mapped:            43904 kB
Shmem:              4868 kB
Slab:              29876 kB
SReclaimable:      14188 kB
SUnreclaim:        15688 kB
KernelStack:        4416 kB
PageTables:         1336 kB
NFS_Unstable:          0 kB
Bounce:                0 kB
WritebackTmp:          0 kB
CommitLimit:      121976 kB
Committed_AS:     336096 kB
VmallocTotal:   263061440 kB
VmallocUsed:           0 kB
VmallocChunk:          0 kB
CmaTotal:          24576 kB
CmaFree:               0 kB
Filesystem                Size      Used Available Use% Mounted on
ubi0:rootfs              64.8M     52.6M     12.3M  81% /
devtmpfs                106.2M         0    106.2M   0% /dev
/dev/ubi1_0              87.5M      6.5M     76.4M   8% /bkupgrade
tmpfs                   119.1M         0    119.1M   0% /dev/shm
tmpfs                   119.1M      4.6M    114.6M   4% /tmp
tmpfs                   119.1M     40.0K    119.1M   0% /run
/dev/mtdblock4            1.0M    396.0K    628.0K  39% /tee
tmpfs                   119.1M      4.6M    114.6M   4% /etc/unbound
tmpfs                   119.1M      4.6M    114.6M   4% /etc/wifi/w1
--- /proc/loadavg
1.29 1.16 1.01 4/266 3279
/sys/class/thermal/thermal_zone0/temp: 45000

===== что можно запустить =====
нет python3
нет python
нет python3.12
нет python3.11
нет pip3
есть busybox: /bin/busybox
нет toybox
нет opkg
нет apt
нет apk
нет curl
есть wget: /usr/bin/wget
нет openssl
нет git
нет gcc
нет команды ldd
/lib/ld-2.30.so
/lib/ld-linux-armhf.so.3
/etc/ssl/certs

===== звук =====
--- /proc/asound/cards
 0 [AMLAUGESOUND   ]: AML-AUGESOUND - AML-AUGESOUND
                      AML-AUGESOUND
--- /proc/asound/pcm
00-00: TDM-B-ntp8910a-amplifier ntp8910a-amplifier-0 :  : playback 1 : capture 1
00-01: PDM-dummy dummy-1 :  : capture 1
--- /etc/asound.conf
pcm.!default {
  type asym
  playback.pcm "soft_volume"
  capture.pcm "multi_capture"
}

pcm.soft_volume {
  type softvol
  slave.pcm "dmix_spk_plug"

  control {
    name "Master Playback Volume"
  }
}

pcm.multi_capture {
  type multi
  slaves {
    mics     { pcm "hw:0,1"; channels 8 }
    loopback { pcm "hw:0,0"; channels 2 }
  }
  bindings {
    0 { slave mics;     channel 0 }
    1 { slave mics;     channel 2 }
    2 { slave mics;     channel 4 }
    3 { slave mics;     channel 6 }
    4 { slave loopback; channel 0 }
    5 { slave loopback; channel 1 }
  }
}

pcm.dmix_spk_plug {
  type plug
  slave.pcm "dmix_spk"
}

pcm.dmix_spk {
  type dmix
  ipc_key 1024
  ipc_key_add_uid false
  ipc_perm 0666
  slave {
    pcm "hw:0,0"
    channels 2
    buffer_size 7680
    period_size 960
  }
}
total 0
crw-rw----    1 root     audio     116,   0 Jan  1  1970 controlC0
crw-rw----    1 root     audio     116,  24 Jan  1  1970 pcmC0D0c
crw-rw----    1 root     audio     116,  16 Jan  1  1970 pcmC0D0p
crw-rw----    1 root     audio     116,  25 Jan  1  1970 pcmC0D1c
crw-rw----    1 root     audio     116,  33 Jan  1  1970 timer
**** List of CAPTURE Hardware Devices ****
card 0: AMLAUGESOUND [AML-AUGESOUND], device 0: TDM-B-ntp8910a-amplifier ntp8910a-amplifier-0 []
  Subdevices: 0/1
  Subdevice #0: subdevice #0
card 0: AMLAUGESOUND [AML-AUGESOUND], device 1: PDM-dummy dummy-1 []
  Subdevices: 0/1
  Subdevice #0: subdevice #0
**** List of PLAYBACK Hardware Devices ****
card 0: AMLAUGESOUND [AML-AUGESOUND], device 0: TDM-B-ntp8910a-amplifier ntp8910a-amplifier-0 []
  Subdevices: 1/1
  Subdevice #0: subdevice #0

===== процессы (что уже крутится на колонке) =====
PID   USER     COMMAND
    1 root     init
    2 root     [kthreadd]
    3 root     [ksoftirqd/0]
    5 root     [kworker/0:0H]
    7 root     [rcu_preempt]
    8 root     [rcu_sched]
    9 root     [rcu_bh]
   10 root     [migration/0]
   11 root     [lru-add-drain]
   12 root     [cpuhp/0]
   13 root     [cpuhp/1]
   14 root     [migration/1]
   15 root     [ksoftirqd/1]
   17 root     [kworker/1:0H]
   18 root     [cpuhp/2]
   19 root     [migration/2]
   20 root     [ksoftirqd/2]
   22 root     [kworker/2:0H]
   23 root     [cpuhp/3]
   24 root     [migration/3]
   25 root     [ksoftirqd/3]
   26 root     [kworker/3:0]
   27 root     [kworker/3:0H]
   28 root     [kdevtmpfs]
  455 root     [oom_reaper]
  456 root     [writeback]
  458 root     [kcompactd0]
  459 root     [crypto]
  460 root     [bioset]
  462 root     [kblockd]
  493 root     [devfreq_wq]
  505 root     [kworker/3:1]
  506 root     [cfg80211]
  510 root     [watchdogd]
  595 root     [cfinteractive]
  598 root     [rpciod]
  599 root     [xprtiod]
  620 root     [kswapd0]
  621 root     [vmstat]
  685 root     [nfsiod]
  804 root     [bioset]
  807 root     [bioset]
  810 root     [bioset]
  813 root     [bioset]
  816 root     [bioset]
  819 root     [bioset]
  822 root     [bioset]
  825 root     [bioset]
  830 root     [bioset]
  868 root     [dm_bufio_cache]
  948 root     [null]
  949 root     [cpu_hogplug_thr]
  950 root     [irq/31-meson-am]
  955 root     [bioset]
  960 root     [bioset]
  965 root     [bioset]
  970 root     [bioset]
  975 root     [bioset]
  980 root     [bioset]
  985 root     [bioset]
 1049 root     [ipv6_addrconf]
 1057 root     [krfcommd]
 1066 root     [kworker/1:2]
 1082 root     [ubi_bgt0d]
 1085 root     [ubi_bgt1d]
 1086 root     [irq/33-aw9610x_]
 1090 root     [dwc_otg]
 1106 root     [ubifs_bgt1_0]
 1123 root     /sbin/syslogd -n
 1126 root     watchdog -t 1 -T 5 /dev/watchdog
 1130 root     /sbin/klogd -n
 1142 root     /sbin/mdev -df
 1163 dbus     dbus-daemon --system
 1217 root     [work_thread aml]
 1218 root     [rx_proc amlwifi]
 1219 root     [txok amlwifi]
 1220 root     [hi_irq amlwifi]
 1221 root     [aml_pwrsave_wq]
 1236 root     /usr/sbin/wpa_supplicant -sd -B -Dnl80211 -iwlan0 -c/bkupgrade/data/wpa_supplicant.conf -P /var/run/wpa_supplicant.pid
 1240 dhcpcd   dhcpcd: [manager] [ip4] [ip6]
 1241 root     dhcpcd: [privileged proxy]
 1242 dhcpcd   dhcpcd: [network proxy]
 1243 dhcpcd   dhcpcd: [control proxy]
 1279 root     [kworker/3:1H]
 1288 dhcpcd   dhcpcd: [BPF BOOTP] p2p0
 1289 root     [kworker/0:1H]
 1293 root     chronyd
 1308 root     /bin/tee-supplicant
 1311 root     /usr/bin/keymaster_svc
 1325 root     [kworker/u9:0]
 1326 root     [hci0]
 1327 root     [hci0]
 1330 root     hciattach -s 115200 /dev/ttyS1 aml
 1331 root     [kworker/u9:1]
 1343 root     /usr/bin/bluealsa -p a2dp-sink -p a2dp-source -p hfp-ag --sbc-quality=high
 1347 root     /usr/bin/bluealsa-aplay -M disable -S
 1351 root     /usr/bin/just-works-bluez-agent
 1360 root     /usr/libexec/bluetooth/bluetoothd -d
 1600 root     adbd
 1641 root     /system/vendor/quasar/quasar_launcher2 /system/vendor/quasar/launch.json
 1643 root     /system/vendor/quasar/maind --telemetry
 1644 root     /system/vendor/quasar/maind --fluent-bitd
 1660 dhcpcd   dhcpcd: [BPF ARP] p2p0 169.254.219.143
 1725 root     /data/opt/bin/dropbear -r /data/opt/etc/dropbear/hk_ed25519 -p 2222 -B
 1726 root     sleep infinity
 1728 root     /system/vendor/quasar/maind --platformd
 1904 root     /system/vendor/quasar/maind --mediad
 1994 root     [kworker/1:3]
 2259 root     /data/opt/bin/dropbear -r /data/opt/etc/dropbear/hk_ed25519 -p 2222 -B -2 8
 2260 root     -sh
 2264 root     [kworker/0:4]
 2357 root     [kworker/u8:0]
 2636 root     [kworker/2:1]
 2637 root     [kworker/2:3]
 2675 root     [kworker/1:0]
 2764 root     [kworker/1:1]
 2765 root     [kworker/1:4]
 2766 root     [kworker/1:5]
 2904 root     [kworker/u8:1]
 2906 root     [kworker/0:2]
 2992 root     [kworker/1:6]
 3099 root     [kworker/0:0]
 3138 root     [kworker/1:7]
 3226 dhcpcd   dhcpcd: [BPF ARP] wlan0 10.1.1.13
 3257 nobody   /usr/sbin/unbound
 3267 root     /data/opt/bin/dropbear -r /data/opt/etc/dropbear/hk_ed25519 -p 2222 -B -2 8
 3268 root     sh -s
 3294 root     ps -ef
 3295 root     head -n 150

===== сеть =====
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue qlen 1
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
    inet 127.0.0.1/8 scope host lo
       valid_lft forever preferred_lft forever
    inet6 ::1/128 scope host 
       valid_lft forever preferred_lft forever
2: tunl0@NONE: <NOARP> mtu 1480 qdisc noop qlen 1
    link/ipip 0.0.0.0 brd 0.0.0.0
3: sit0@NONE: <NOARP> mtu 1480 qdisc noop qlen 1
    link/sit 0.0.0.0 brd 0.0.0.0
4: ip6tnl0@NONE: <NOARP> mtu 1452 qdisc noop qlen 1
    link/tunnel6 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00 brd 00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00
5: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast qlen 1000
    link/ether 3c:0b:4f:b7:54:6e brd ff:ff:ff:ff:ff:ff
    inet 10.1.1.13/24 brd 10.1.1.255 scope global dynamic wlan0
       valid_lft 7186sec preferred_lft 6286sec
    inet6 fe80::3e0b:4fff:feb7:546e/64 scope link 
       valid_lft forever preferred_lft forever
6: p2p0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast qlen 1000
    link/ether 3c:0b:5f:b7:54:6e brd ff:ff:ff:ff:ff:ff
    inet 169.254.219.143/16 brd 169.254.255.255 scope global p2p0
       valid_lft forever preferred_lft forever
    inet6 fe80::6424:7de7:a5e1:f666/64 scope link 
       valid_lft forever preferred_lft forever
netstat: invalid option -- 'p'
BusyBox v1.31.1 (2026-10-13 15:20:55 MSK) multi-call binary.

Usage: netstat [-ral] [-tuwx] [-en]

Display networking information

	-r	Routing table
	-a	All sockets
	-l	Listening sockets
		Else: connected sockets
	-t	TCP sockets
	-u	UDP sockets
	-w	Raw sockets
	-x	Unix sockets
		Else: all socket types
	-e	Other/more information
	-n	Don't resolve names

===== свои пакеты колонки (имена, без содержимого) =====
/etc/init.d/S99quasar
/sys/kernel/wakeup_reasons
/sys/power/wake_unlock
/sys/power/wake_lock
/sys/power/wakeup_count
/usr/bin/yandex-btfwversion
/usr/bin/yandex-chrony
/usr/bin/yandex-get-localization
/usr/bin/yandex-wifireset
/usr/bin/yandex-waitfile
/usr/bin/yandex-badblocks
/usr/bin/yandex-wifichipmodel
/usr/bin/yandex-apply-dtb-overlay
/usr/sbin/ether-wake
/bkupgrade/data/quasar
/system/vendor/quasar
/system/vendor/quasar
/system/vendor/quasar/quasar.cfg
/system/vendor/quasar/quasar_launcher2
/system/vendor/quasar/quasar_client
/system/vendor/quasar/spotter_models
/usr/bin/yandex-btfwversion
/usr/bin/yandex-chrony
/usr/bin/yandex-get-localization
/usr/bin/yandex-wifireset
/usr/bin/yandex-waitfile
/usr/bin/yandex-badblocks
/usr/bin/yandex-wifichipmodel
/usr/bin/yandex-apply-dtb-overlay

===== куда можно писать =====
ubi0:rootfs on / type ubifs (ro,relatime)
devtmpfs on /dev type devtmpfs (rw,relatime,size=108728k,nr_inodes=27182,mode=755)
proc on /proc type proc (rw,relatime)
/dev/ubi1_0 on /bkupgrade type ubifs (rw,relatime)
devpts on /dev/pts type devpts (rw,relatime,gid=5,mode=620,ptmxmode=666)
tmpfs on /dev/shm type tmpfs (rw,relatime,mode=777)
tmpfs on /tmp type tmpfs (rw,relatime)
tmpfs on /run type tmpfs (rw,nosuid,nodev,relatime,mode=755)
sysfs on /sys type sysfs (rw,relatime)
none on /sys/kernel/debug type debugfs (rw,relatime)
pstore on /sys/fs/pstore type pstore (rw,relatime)
/dev/mtdblock4 on /tee type jffs2 (ro,relatime)
tmpfs on /etc/unbound type tmpfs (rw,relatime)
tmpfs on /etc/wifi/w1 type tmpfs (rw,relatime)
lrwxrwxrwx    1 root     root            15 Oct 13 12:18 /data -> /bkupgrade/data
drwxr-xr-x    2 root     root           160 Oct 13 12:18 /opt
drwxrwxrwt   10 root     root           540 Nov  7 10:03 /tmp

===== светодиоды и кнопки =====
micro:blue:ring1
micro:blue:ring2
micro:blue:ring3
micro:green:ring1
micro:green:ring2
micro:green:ring3
micro:red:ring1
micro:red:ring2
micro:red:ring3
event0
event1

осмотр закончен: 2026-11-07T10:09:56Z
```
