#!/usr/bin/env bash
# Сборка kws_run в Docker на песочнице: out/kws_run.armv7 (статически, NEON — для колонки) и out/kws_run.x86 (сверка
# с PyTorch на песочнице). Компилятор ставится внутри контейнера, хост не трогается.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p out
docker run --rm -v "$PWD":/w -w /w debian:bookworm bash -c '
  set -e
  apt-get update -qq >/dev/null && apt-get install -y -qq gcc gcc-arm-linux-gnueabihf libc6-dev-armhf-cross >/dev/null
  arm-linux-gnueabihf-gcc -O3 -march=armv7-a -mfpu=neon-vfpv4 -mfloat-abi=hard -ffast-math -static \
      -o out/kws_run.armv7 kws_run.c -lm
  gcc -O3 -ffast-math -o out/kws_run.x86 kws_run.c -lm
  chown '"$(id -u):$(id -g)"' out/kws_run.armv7 out/kws_run.x86
'
ls -la out/kws_run.armv7 out/kws_run.x86
