#!/bin/bash
set -euo pipefail
set -a
. $HOME/dsh-suite/data/qq-bridge/reverse.env
set +a
exec $HOME/dsh-suite/.venv/bin/python $HOME/dsh-suite/qq-bridge/onebot-reverse.py
