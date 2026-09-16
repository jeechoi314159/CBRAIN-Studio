#!/usr/bin/env bash
# Build only. Does not attach to a debugger or flash a device.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TC=/opt/nordic/ncs/toolchains/ccc010f809
export PATH="$TC/bin:$TC/usr/bin:$TC/usr/local/bin:$TC/opt/bin:$TC/opt/nanopb/generator-bin:$TC/opt/zephyr-sdk/gnu/arm-zephyr-eabi/bin:$PATH"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr/gnu
export ZEPHYR_SDK_INSTALL_DIR="$TC/opt/zephyr-sdk"
export ZEPHYR_BASE=/opt/nordic/ncs/v3.4.0/zephyr
export NRFUTIL_HOME="$TC/nrfutil/home"
export CCACHE_DIR="$REPO/build/ccache"
west build -b nrf52dk/nrf52832 --no-sysbuild -d "$REPO/build/headstage_verified" "$REPO/firmware/cb_intan" -- -DCMAKE_GDB=/usr/bin/true -DUSER_CACHE_DIR="$REPO/build/zephyr-cache"
