#!/usr/bin/env bash
set -euo pipefail
tmux -f /dev/null has-session -t workbench 2>/dev/null || \
  tmux -f /dev/null new-session -d -s workbench -c /home/app/workspace
exec tmux -f /dev/null attach-session -t workbench
