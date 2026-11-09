#!/bin/sh
# Установка колонки «Игорёк» на Яндекс Станцию Лайт одной строкой (с компьютера, где есть SSH к колонке после рута —
# docs/runbooks/yandex-station-root.md в krikz/rob_box_project):
#
#   curl -sL https://raw.githubusercontent.com/krikz/rob_box_station/main/install.sh | sh -s -- 10.1.1.13
#
# Аргументы: IP колонки [порт SSH, 2222]. DRY_RUN=1 — только показать шаги.
# Ставит: Питон + библиотеки (station-runtime-armv7.tar.gz), ядро диалога (station-core.tar.gz), свою модель вейка
# (station-kws-armv7.tar.gz, если есть), код колонки последнего тега, автозапуск, глушение Алисы (вернуть:
# rm /data/opt/yandex_off && reboot), приветствие SSH. Ключи — потом на странице настроек. Ассеты собирает CI
# (.github/workflows/assets.yml) из исходников; то, что уже стоит, не трогается.
# Переменные: TAG=vX.Y.Z — конкретный тег; FORCE_RUNTIME=1 / FORCE_CORE=1 / FORCE_KWS=1 — переставить часть;
# RUNTIME_URL / CORE_URL / KWS_URL=https://… — свой адрес ассета; NEED_MB=NN — порог свободного места.
set -eu
HOST="${1:?укажи IP колонки: ... | sh -s -- 10.1.1.13}"
PORT="${2:-2222}"
REPO="krikz/rob_box_station"
RBX=/data/opt/rbx
say() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
run() { if [ "${DRY_RUN:-0}" = 1 ]; then printf '  [dry] %s\n' "$*"; else sh -c "$*"; fi; }
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p $PORT root@$HOST"
# Скрипт запускают как `curl … | sh`, то есть sh читает САМ СЕБЯ из stdin.
# Поэтому любой ssh без -n съест остаток скрипта — для одиночных вызовов только SSHN.
SSHN="ssh -n -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p $PORT root@$HOST"

RUNTIME_ASSET=station-runtime-armv7.tar.gz   # Питон 3.12 + библиотеки (tools/build_runtime.py)
CORE_ASSET=station-core.tar.gz               # ядро диалога из коммита rob_box_project (tools/build_core.py)
KWS_ASSET=station-kws-armv7.tar.gz           # своя модель вейка (tools/build_kws.py), необязательна

# Адрес ассета: свой URL (переменная) → релиз нашего тега → любой релиз репозитория с таким ассетом.
asset_url() {  # $1 — имя ассета, $2 — свой адрес (может быть пустым)
  if [ -n "${2:-}" ]; then printf '%s' "$2"; return 0; fi
  u="https://github.com/$REPO/releases/download/$TAG/$1"
  if [ "$(curl -sIL -o /dev/null -w '%{http_code}' "$u")" = 200 ]; then printf '%s' "$u"; return 0; fi
  curl -sL "https://api.github.com/repos/$REPO/releases?per_page=100" |
    sed -n 's/.*"browser_download_url": *"\([^"]*\)".*//p' | grep -F "/$1" | head -1
}
no_asset() {
  echo "не нашёл $1: ни в релизе $TAG, ни в других релизах $REPO"
  echo "  собери ассеты: Actions → assets → Run workflow (тег $TAG), или локально tools/build_*.py"
  echo "  либо укажи свой адрес:  $2=https://… sh -s -- $HOST"
  exit 1
}

say "колонка $HOST:$PORT — проверяю вход"
$SSHN 'uname -m; df -m /data | tail -1' || { echo "нет SSH к колонке — сначала рут (runbook)"; exit 1; }

# Тег — источник истины: по нему берётся код, по нему ищутся ассеты.
if [ -z "${TAG:-}" ]; then
  TAG=$(curl -sL "https://api.github.com/repos/$REPO/tags?per_page=1" |
        sed -n 's/.*"name": *"\([^"]*\)".*/\1/p' | head -1)
fi
[ -n "${TAG:-}" ] || { echo "не нашёл тег в $REPO"; exit 1; }
say "тег $TAG"

FREE=$($SSHN "df -m /data | awk 'NR==2 {print \$4}'")
HAVE=$($SSHN "[ -x /data/opt/python/bin/python3.12 ] && [ -d $RBX/site/openai ] && echo yes || echo no")
HAVE_CORE=$($SSHN "[ -f $RBX/core/scripts/dialogue/chat.py ] && echo yes || echo no")
HAVE_KWS=$($SSHN "[ -x $RBX/kws/kws_run ] && [ -f $RBX/kws/kws_model.bin ] && echo yes || echo no")
# место — по тому, чего НЕТ: рунтайм 48,4 МБ распакованным + __pycache__, ядро 7,2 МБ, модель 0,6 МБ (сборки 09.11)
# + запас 5 МБ; переустановка поверх места почти не берёт, кроме ядра (старое и новое рядом на время подмены)
need=5
[ "$HAVE" = yes ] || need=$((need + 52))
[ "$HAVE_CORE" = yes ] && [ "${FORCE_CORE:-0}" != 1 ] || need=$((need + 9))
[ "$HAVE_KWS" = yes ] || need=$((need + 1))
[ "${FORCE_RUNTIME:-0}" = 1 ] && HAVE=no
[ "${FORCE_CORE:-0}" = 1 ] && HAVE_CORE=no
[ "${FORCE_KWS:-0}" = 1 ] && HAVE_KWS=no
NEED_MB="${NEED_MB:-$need}"
say "на /data свободно $FREE МБ, нужно $NEED_MB (Питон: $HAVE, ядро: $HAVE_CORE, вейк: $HAVE_KWS)"
[ "$FREE" -ge "$NEED_MB" ] || { echo "мало места на /data (NEED_MB=NN — переопределить)"; exit 1; }

if [ "$HAVE" = no ]; then
  RURL=$(asset_url "$RUNTIME_ASSET" "${RUNTIME_URL:-}" || true)
  [ -n "$RURL" ] || no_asset "$RUNTIME_ASSET" RUNTIME_URL
  say "ставлю Питон и библиотеки ($RURL)"
  run "curl -fsSL '$RURL' | $SSH 'mkdir -p /data/opt && cd /data/opt && gunzip -c | tar xf -'"
else
  say "Питон и библиотеки уже стоят — пропускаю (FORCE_RUNTIME=1 — переставить)"
fi

if [ "$HAVE_CORE" = no ]; then
  CURL=$(asset_url "$CORE_ASSET" "${CORE_URL:-}" || true)
  [ -n "$CURL" ] || no_asset "$CORE_ASSET" CORE_URL
  say "ставлю ядро диалога ($CURL)"
  # рядом, потом подмена: оборванная закачка не ломает старое ядро; ключи (.env) переезжают в новое
  run "curl -fsSL '$CURL' | $SSH 'set -e; cd /data/opt; rm -rf rbx_core_new; mkdir rbx_core_new; cd rbx_core_new; gunzip -c | tar xf -; cd /data/opt; mkdir -p $RBX; [ -d $RBX/core ] && mv $RBX/core $RBX/core.old; mv rbx_core_new/rbx/core $RBX/core; E=scripts/dialogue/.env; [ -f $RBX/core.old/\$E ] && cp -p $RBX/core.old/\$E $RBX/core/\$E; rm -rf rbx_core_new $RBX/core.old; head -3 $RBX/core/SOURCE'"
else
  say "ядро уже стоит — пропускаю (FORCE_CORE=1 — переставить по тегу)"
fi

if [ "$HAVE_KWS" = no ]; then
  KURL=$(asset_url "$KWS_ASSET" "${KWS_URL:-}" || true)
  if [ -n "$KURL" ]; then
    say "ставлю свою модель вейка ($KURL)"
    run "curl -fsSL '$KURL' | $SSH 'set -e; mkdir -p $RBX/kws; [ -f $RBX/kws/kws_model.bin ] && cp $RBX/kws/kws_model.bin $RBX/kws/kws_model.prev.bin; cd /data/opt && gunzip -c | tar xf -; cat $RBX/kws/KWS'"
  else
    say "нет $KWS_ASSET — колонка будет без своей модели вейка (вейк только по тексту STT)"
  fi
else
  say "своя модель вейка уже стоит — пропускаю (FORCE_KWS=1 — переставить)"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
say "код колонки $TAG"
curl -sL "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" | (cd "$TMP" && tar xzf - --strip-components=1)
run "cd '$TMP/scripts' && tar cf - *.py motd.sh | $SSH 'mkdir -p $RBX && cd $RBX && tar xf - && echo $TAG > VERSION'"
run "cd '$TMP' && tar cf - assets prompts | $SSH 'cd /tmp && rm -rf rbxnew && mkdir rbxnew && cd rbxnew && tar xf - && cp -a assets $RBX/ && mkdir -p $RBX/prompts && for f in prompts/*; do [ -f $RBX/\$f ] || cp \$f $RBX/\$f; done; cd /tmp && rm -rf rbxnew'"
run "$SSHN 'E=$RBX/core/scripts/dialogue/.env; [ -f \$E ] || { printf \"DEEPSEEK_API_KEY=\\nMINIMAX_API_KEY=\\nMINIMAX_GROUP_ID=\\n\" > \$E; chmod 600 \$E; }'"

say "автозапуск, глушение Алисы, приветствие SSH (системная часть: remount rw → ro)"
run "cd '$TMP/scripts' && tar cf - S99zzyandexoff S99zzzrbxhost | $SSH 'mount -o remount,rw / && cd /etc/init.d && tar xf - && chmod +x S99zzyandexoff S99zzzrbxhost && printf \"[ -f $RBX/motd.sh ] && . $RBX/motd.sh\\n\" > /etc/profile.d/rbx.sh && sync; mount -o remount,ro /'"
# глушение — в переднем плане (~30 с) и с проверкой: фон из ssh-сессии умирал вместе с ней, Алиса оставалась жива,
# микрофон был занят (#4551 в rob_box_project); не заглушилась — второй заход
run "$SSHN 'touch /data/opt/yandex_off /data/opt/rbx_host && (/etc/init.d/S99zzyandexoff now || /etc/init.d/S99zzyandexoff now) && DELAY=0 /etc/init.d/S99zzzrbxhost restart'"

say "готово. Открой http://$HOST:8080 — впиши ключи (DeepSeek, MiniMax), нажми любую кнопку колонки, «Сохранить»."
say "Wi-Fi потом можно сменить там же или с телефона по Bluetooth (Chrome → improv-wifi.com). Обновления: «Игорёк, обновись»."
