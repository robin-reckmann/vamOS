#!/bin/sh
set -e

find_adsp_remoteproc() {
  for d in /sys/class/remoteproc/remoteproc*; do
    [ -d "$d" ] || continue
    if [ "$(cat "$d/name" 2>/dev/null)" = "adsp" ]; then
      echo "$d"
      return 0
    fi
  done
  return 1
}

start_adsp_remoteproc() {
  ADSP_RP=""
  ADSP_STATE=""
  TRY_N=0
  ADSP_N=0
  while :; do
    ADSP_RP="$(find_adsp_remoteproc || true)"
    [ -n "$ADSP_RP" ] && break
    ADSP_N=$((ADSP_N + 1))
    [ "$ADSP_N" -ge 200 ] && return 1
    sleep 0.1
  done

  ADSP_STATE="$(cat "$ADSP_RP/state" 2>/dev/null || true)"
  if [ "$ADSP_STATE" = "running" ]; then
    return 0
  fi

  TRY_N=0
  while :; do
    echo "start" > "$ADSP_RP/state" 2>/dev/null || true
    ADSP_STATE="$(cat "$ADSP_RP/state" 2>/dev/null || true)"
    [ "$ADSP_STATE" = "running" ] && return 0
    TRY_N=$((TRY_N + 1))
    [ "$TRY_N" -ge 120 ] && return 1
    sleep 0.1
  done
}

echo "[INFO] Starting ADSP via remoteproc"
start_adsp_remoteproc
