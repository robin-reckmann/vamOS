#!/bin/bash
# Internal panda (STM32) reset, ported from openpilot's reset_internal_panda.
# On mici the panda is on SPI (no USB enumeration), so this is one-shot:
# pulse reset and mark ready. Aliveness needs a real SPI client (boardd).
source /usr/comma/gpio_base.sh

STM_RST_N=124
STM_BOOT0=134

case "$1" in
  start)
    echo "Resetting panda..."
    gpio $STM_RST_N 1
    gpio $STM_BOOT0 0
    sleep 0.01
    gpio $STM_RST_N 0

    touch /run/panda.ready
    echo " Panda reset done"
    ;;
  *)
    echo "Specify start as first argument!"
    exit 1
    ;;
esac
