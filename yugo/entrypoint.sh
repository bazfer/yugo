#!/bin/sh
set -eu

mode="$(python -c 'from coordinator import resolve_mode; print(resolve_mode())')"
case "$mode" in
  bot)         exec python -u bot.py ;;
  coordinator) exec python -u coordinator_main.py "$@" ;;
esac
