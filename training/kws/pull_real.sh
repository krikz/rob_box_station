#!/usr/bin/env bash
# С ПК (Git Bash): забрать живые примеры, накопленные колонкой, на песочницу и очистить колонку.
#   pos (первая секунда обращения с вейком) → data/real/igorek, neg (фраза без вейка) → data/real/_unknown_
#   miss (вероятный промах облака: попытка позвать до удачной, пустой STT при «Игорьке» своей модели) →
#   data/miss/ — в обучение НЕ идут, сначала разметить руками (index.jsonl: текст STT, ответ модели, провайдер)
# Переменные: STATION (root@10.1.1.13, порт 2222), SANDBOX (ros2@10.1.1.250), KWS (~/kws на песочнице).
set -euo pipefail
STATION="${STATION:-root@10.1.1.13}"
SANDBOX="${SANDBOX:-ros2@10.1.1.250}"
KWS="${KWS:-kws}"
SSH_ST=(ssh -o BatchMode=yes -p 2222 "$STATION")
SSH_SB=(ssh -o BatchMode=yes "$SANDBOX")
n=$("${SSH_ST[@]}" 'ls /data/opt/rbx/kws_real/pos /data/opt/rbx/kws_real/neg /data/opt/rbx/kws_real/miss 2>/dev/null | grep -c wav || true')
echo "на колонке примеров: $n"
[ "$n" -gt 0 ] || exit 0
"${SSH_ST[@]}" 'cd /data/opt/rbx/kws_real && tar cf - $(ls -d pos neg miss index.jsonl 2>/dev/null)' | "${SSH_SB[@]}" "set -e; t=\$(mktemp -d); tar xf - -C \$t;
  mkdir -p $KWS/data/real/igorek $KWS/data/real/_unknown_ $KWS/data/miss;
  [ -d \$t/pos ] && mv \$t/pos/*.wav $KWS/data/real/igorek/ 2>/dev/null || true;
  [ -d \$t/neg ] && mv \$t/neg/*.wav $KWS/data/real/_unknown_/ 2>/dev/null || true;
  [ -d \$t/miss ] && mv \$t/miss/*.wav $KWS/data/miss/ 2>/dev/null || true;
  [ -f \$t/index.jsonl ] && cat \$t/index.jsonl >> $KWS/data/index.jsonl || true;
  rm -rf \$t; echo \"на песочнице: вейк \$(ls $KWS/data/real/igorek | wc -l), чужие \$(ls $KWS/data/real/_unknown_ | wc -l), промахи \$(ls $KWS/data/miss | wc -l)\""
"${SSH_ST[@]}" 'rm -f /data/opt/rbx/kws_real/pos/*.wav /data/opt/rbx/kws_real/neg/*.wav /data/opt/rbx/kws_real/miss/*.wav /data/opt/rbx/kws_real/index.jsonl'
