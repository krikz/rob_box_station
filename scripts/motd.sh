# Приветствие при входе по SSH на колонку (как MOTD РОББОКСа) + быстрые команды. Подключается из
# /etc/profile.d/rbx.sh; сам файл живёт в /data/opt/rbx и приходит с обновлением колонки (scripts/ → /data/opt/rbx).
# busybox ash: без bash-измов.
[ -n "$PS1" ] || return 0 2>/dev/null

RBX=/data/opt/rbx
_c() { printf '\033[%sm' "$1"; }
_ver=$(cat $RBX/VERSION 2>/dev/null || echo "без версии")
_ip=$(wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^ip_address=//p')
_ssid=$(wpa_cli -i wlan0 status 2>/dev/null | sed -n 's/^ssid=//p')
_up=$(awk '{d=int($1/86400); h=int($1%86400/3600); m=int($1%3600/60); printf "%dд %02d:%02d", d, h, m}' /proc/uptime)
_mem=$(free -m | awk '/^Mem:/ {printf "%d / %d МБ (%d%%)", $3, $2, $3*100/$2}')
_data=$(df -m /data | awk 'NR==2 {printf "%d МБ свободно из %d", $4, $2}')
_t=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
_temp=$([ -n "$_t" ] && awk -v t="$_t" 'BEGIN {printf "%.1f°C", t/1000}' || echo "—")
if pidof python3.12 >/dev/null; then _host="$(_c 32)✓ слушает$(_c 0)"; else _host="$(_c 31)✗ не запущен$(_c 0)"; fi
[ -f /data/opt/yandex_off ] && _alice="заглушена" || _alice="$(_c 33)работает$(_c 0)"
[ -f $RBX/kws/kws_model.bin ] && _kws="своя модель вейка (тень)" || _kws="нет"
_wake=$(grep -m1 '"RBX_WAKE"' $RBX/settings.json 2>/dev/null | sed 's/.*: *"\(.*\)".*/\1/' | cut -d, -f1-4)

_c 36
cat <<'ART'
    ╔═══════════════════════════════════════════════════════╗
    ║                                                       ║
    ║   ██╗ ██████╗  ██████╗ ██████╗ ███████╗██╗  ██╗       ║
    ║   ██║██╔════╝ ██╔═══██╗██╔══██╗██╔════╝██║ ██╔╝       ║
    ║   ██║██║  ███╗██║   ██║██████╔╝█████╗  █████╔╝        ║
    ║   ██║██║   ██║██║   ██║██╔══██╗██╔══╝  ██╔═██╗        ║
    ║   ██║╚██████╔╝╚██████╔╝██║  ██║███████╗██║  ██╗       ║
    ║   ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝       ║
    ║                                                       ║
    ╚═══════════════════════════════════════════════════════╝
ART
_c 0
printf '              🔊  Колонка «Игорёк» · семья РОББОКС  🤖\n\n'
printf '═══════════════════════════════════════════════════════════════\n'
printf 'КОЛОНКА\n'
printf '  Версия:           %s\n' "$_ver"
printf '  Хост:             %b\n' "$_host"
printf '  Вейк:             %s\n' "${_wake:-игорёк (по умолчанию)}"
printf '  Распознавание:    %s\n' "$_kws"
printf '  Алиса:            %b\n' "$_alice"
printf '  Работает:         %s\n' "$_up"
printf 'ЖЕЛЕЗО\n'
printf '  Устройство:       Яндекс Станция Лайт (armv7, 4 ядра)\n'
printf '  Память:           %s\n' "$_mem"
printf '  /data:            %s\n' "$_data"
printf '  Температура:      %s\n' "$_temp"
printf 'СЕТЬ\n'
printf '  Wi-Fi:            %s\n' "${_ssid:-нет}"
printf '  IP:               %s\n' "${_ip:-нет}"
printf '  Настройки:        http://%s:8080/\n' "${_ip:-<ip>}"
printf '═══════════════════════════════════════════════════════════════\n'
printf 'Быстрые команды:\n'
printf '  rbxlog        — последние события хоста (ходы, кнопки, ошибки)\n'
printf '  rbxtail       — лог хоста вживую\n'
printf '  rbxrestart    — перезапустить хост\n'
printf '  rbxstop       — остановить хост (до перезагрузки)\n'
printf '  rbxver        — версия и проверка обновления\n\n'

alias rbxlog='grep -E "🎤|ход [0-9]|🔘|🎵|📶|⚙|⬆|Traceback|Error" /tmp/station.log | tail -25'
alias rbxtail='tail -f /tmp/station.log'
alias rbxrestart='/etc/init.d/S99zzzrbxhost restart'
alias rbxstop='/etc/init.d/S99zzzrbxhost stop'
alias rbxver='cd $RBX && /data/opt/python/bin/python3.12 -X utf8 -c "import sys; sys.path[:0]=[\"site\",\".\"]; import station_update as u, pathlib; up=u.Updater(pathlib.Path(\".\"), lambda: \"\", print); print(up.current()); print(up.latest()[1])"; cd - >/dev/null'
unset _ver _ip _ssid _up _mem _data _t _temp _host _alice _kws _wake
