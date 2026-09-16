#!/usr/bin/env bash
# Build only; publishing/flashing is a separate verified step.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TC=/opt/nordic/ncs/toolchains/ccc010f809
export PATH="$TC/bin:$TC/usr/bin:$TC/usr/local/bin:$TC/opt/bin:$TC/opt/nanopb/generator-bin:$TC/opt/zephyr-sdk/gnu/arm-zephyr-eabi/bin:$PATH"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr/gnu
export ZEPHYR_SDK_INSTALL_DIR="$TC/opt/zephyr-sdk"
export ZEPHYR_BASE=/opt/nordic/ncs/v3.4.0/zephyr
export NRFUTIL_HOME="$TC/nrfutil/home"
west build -b nrf52840dongle/nrf52840 --no-sysbuild -d "$REPO/build/bridge_discovery" "$REPO/firmware/cb_bridge" -- -DCMAKE_GDB=/usr/bin/true -DCONFIG_CB_BRIDGE_TARGET_ID=0
