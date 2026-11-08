#!/bin/sh
# Установка колонки «Игорёк» на Яндекс Станцию Лайт одной строкой (с компьютера, где есть SSH к колонке после рута —
# docs/runbooks/yandex-station-root.md в krikz/rob_box_project):
#
#   curl -sL https://raw.githubusercontent.com/krikz/rob_box_station/main/install.sh | sh -s -- 10.1.1.13
#
# Аргументы: IP колонки [порт SSH, 2222]. DRY_RUN=1 — только показать шаги. FORCE_RUNTIME=1 — переставить Питон и ядро.
# Ставит: Питон + библиотеки + ядро (ассет station-runtime-armv7.tar.gz), код колонки последнего тега, автозапуск,
# глушение Алисы (вернуть: rm /data/opt/yandex_off && reboot), приветствие SSH. Ключи — потом на странице настроек.
# Переменные: TAG=vX.Y.Z — конкретный тег; RUNTIME_URL=https://… — свой адрес рунтайма; NEED_MB=NN — порог свободного места.
set -eu
HOST="${1:?укажи IP колонки: ... | sh -s -- 10.1.1.13}"
PORT="${2:-2222}"
REPO="krikz/rob_box_station"
RBX=/data/opt/rbx
say() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
run() { if [ "${DRY_RUN:-0}" = 1 ]; then printf '  [dry] %s\n' "$*"; else sh -c "$*"; fi; }
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p $PORT root@$HOST"

RUNTIME_ASSET=station-runtime-armv7.tar.gz

# Ассет рунтайма: RUNTIME_URL → наш тег → любой релиз репозитория с таким ассетом.
runtime_url() {
  if [ -n "${RUNTIME_URL:-}" ]; then printf '%s' "$RUNTIME_URL"; return 0; fi
  u="https://github.com/$REPO/releases/download/$TAG/$RUNTIME_ASSET"
  if [ "$(curl -sIL -o /dev/null -w '%{http_code}' "$u")" = 200 ]; then printf '%s' "$u"; return 0; fi
  curl -sL "https://api.github.com/repos/$REPO/releases?per_page=100" |
    sed -n 's/.*"browser_download_url": *"\([^"]*\)".*/\1/p' | grep -F "$RUNTIME_ASSET" | head -1
}

say "колонка $HOST:$PORT — проверяю вход"
$SSH 'uname -m; df -m /data | tail -1' || { echo "нет SSH к колонке — сначала рут (runbook)"; exit 1; }

# Тег — источник истины: по нему берётся код, по нему ищется ассет рунтайма.
if [ -z "${TAG:-}" ]; then
  TAG=$(curl -sL "https://api.github.com/repos/$REPO/tags?per_page=1" |
        sed -n 's/.*"name": *"\([^"]*\)".*/\1/p' | head -1)
fi
[ -n "${TAG:-}" ] || { echo "не нашёл тег в $REPO"; exit 1; }
say "тег $TAG"

FREE=$($SSH "df -m /data | awk 'NR==2 {print \$4}'")
HAVE=$($SSH "[ -x /data/opt/python/bin/python3.12 ] && [ -d $RBX/core ] && echo yes || echo no")
if [ "$HAVE" = no ] || [ "${FORCE_RUNTIME:-0}" = 1 ]; then
  # 55 МБ: питон 29,6 + библиотеки 6,1 + ядро 8,7 ≈ 44 МБ (docs/inventory_2026-11-07.md) + запас
  NEED_MB="${NEED_MB:-55}"
  [ "$HAVE" = yes ] || [ "$FREE" -ge "$NEED_MB" ] || {
    echo "на /data свободно $FREE МБ, нужно $NEED_MB (NEED_MB=NN — переопределить)"; exit 1; }
  RURL=$(runtime_url || true)
  [ -n "$RURL" ] || {
    echo "не нашёл $RUNTIME_ASSET: ни в релизе $TAG, ни в других релизах $REPO"
    echo "  либо приложи ассет к тегу:  gh release create $TAG $RUNTIME_ASSET --title $TAG"
    echo "  либо укажи свой адрес:      RUNTIME_URL=https://… sh -s -- $HOST"
    exit 1
  }
  say "ставлю Питон, библиотеки и ядро ($RURL)"
  run "curl -fsSL '$RURL' | $SSH 'mkdir -p /data/opt && cd /data/opt && gunzip -c | tar xf -'"
else
  say "Питон и ядро уже стоят — пропускаю (FORCE_RUNTIME=1 — переставить)"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
say "код колонки $TAG"
curl -sL "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" | (cd "$TMP" && tar xzf - --strip-components=1)
run "cd '$TMP/scripts' && tar cf - *.py motd.sh | $SSH 'mkdir -p $RBX && cd $RBX && tar xf - && echo $TAG > VERSION'"
run "cd '$TMP' && tar cf - assets prompts | $SSH 'cd /tmp && rm -rf rbxnew && mkdir rbxnew && cd rbxnew && tar xf - && cp -a assets $RBX/ && mkdir -p $RBX/prompts && for f in prompts/*; do [ -f $RBX/\$f ] || cp \$f $RBX/\$f; done; cd /tmp && rm -rf rbxnew'"
run "$SSH 'E=$RBX/core/scripts/dialogue/.env; [ -f \$E ] || { printf \"DEEPSEEK_API_KEY=\\nMINIMAX_API_KEY=\\nMINIMAX_GROUP_ID=\\n\" > \$E; chmod 600 \$E; }'"

say "автозапуск, глушение Алисы, приветствие SSH (системная часть: remount rw → ro)"
run "cd '$TMP/scripts' && tar cf - S99zzyandexoff S99zzzrbxhost | $SSH 'mount -o remount,rw / && cd /etc/init.d && tar xf - && chmod +x S99zzyandexoff S99zzzrbxhost && printf \"[ -f $RBX/motd.sh ] && . $RBX/motd.sh\\n\" > /etc/profile.d/rbx.sh && sync; mount -o remount,ro /'"
run "$SSH 'touch /data/opt/yandex_off /data/opt/rbx_host && /etc/init.d/S99zzyandexoff start && DELAY=0 /etc/init.d/S99zzzrbxhost restart'"

say "готово. Открой http://$HOST:8080 — впиши ключи (DeepSeek, MiniMax), нажми любую кнопку колонки, «Сохранить»."
say "Wi-Fi потом можно сменить там же или с телефона по Bluetooth (Chrome → improv-wifi.com). Обновления: «Игорёк, обновись»."
