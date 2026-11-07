#!/usr/bin/env bash
# Полный цикл на песочнице (GPU): окружение → озвучка (только новое) → обучение → out/.
# Переучить на другие слова: поправь config.yaml и запусти снова. Лог — out/train.log.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
  python3 -m venv --without-pip venv
  W=/tmp/pip-24.3.1-py3-none-any.whl
  curl -sSL -o "$W" "https://files.pythonhosted.org/packages/py3/p/pip/pip-24.3.1-py3-none-any.whl"
  venv/bin/python "$W/pip" install -q "$W" && rm -f "$W"
  venv/bin/python -m pip install -q torch torchaudio --index-url https://download.pytorch.org/whl/cu121
  venv/bin/python -m pip install -q omegaconf pyyaml soundfile numpy onnx
fi
mkdir -p out data/noise data/real
venv/bin/python synth.py 2>&1 | tee out/synth.log
venv/bin/python train.py 2>&1 | tee out/train.log
