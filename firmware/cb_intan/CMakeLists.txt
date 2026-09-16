cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cb_intan)

target_sources(app PRIVATE src/main.c src/cb_rhd.c src/cb_dsp.c)
