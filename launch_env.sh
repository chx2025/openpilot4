#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="19.9"
fi

export STAGING_ROOT="/data/safe_staging"

# chestnut eGPU(ASM24) 功率上限：默认走显卡 SMU PPT 上限(182W)，
# 压到 100W 降低峰值电流，缓解高负载下 USB/PCIe 链路抖动。
# 有 AM_POWER_LIMIT 时 amdev 走 set_power_limit()+set_clocks(None)，由 SMU 自己调频；
# 不要设 AM_PERF_LEVEL=high，那会钉最高频硬撞功率墙。
export AM_POWER_LIMIT=100

# ==== [port-egpu-usb] 移植自 spfix1016 0010-egpu-c3xl-ife ====

# eGPU(chestnut) 插着时 SConscript 会尝试编译 big_driving_supercombo.onnx，
# 而仓库未提供该 onnx；下载的模型已自带编译好的 pkl，无需本地重编。
export SKIP_TINYGRAD_COMPILE=1

# 让 tinygrad 的下载/编译缓存落在持久分区，避免每次开机重建
# （/home 是 overlay 临时层，重启即清空）
export TINYGRAD_CACHE_DIR=/data/tg_cache/tinygrad

# ---- chestnut(eGPU) USB 节点权限守护（冷启动 EACCES 竞态）----
# devtmpfs 以 0600 root:root 建 /dev/bus/usb/<bus>/<dev>，udev 要读完描述符才放宽成
# 0664；第三方底座(ASM2464)描述符响应慢，这个窗口能拖到几十秒，而 modeld 早早就
# libusb_open -> EACCES -> load_big() 只试一次即放弃 -> 静默回退小模型。
# 这里开机即后台轮询、抢在 udev 前把节点 chmod 666；脚本自带自提权。
if [ -x "${DIR:-/data/openpilot}/system/egpu_usb_perm.sh" ]; then
  setsid "${DIR:-/data/openpilot}/system/egpu_usb_perm.sh" >/dev/null 2>&1 &
fi

# ---- [port-egpu-trace] eGPU USB 链路时序记录 ----
# 只在 /data/egpu_trace.enable 存在时拉起，默认记 180s 到 /data/egpu_trace.log，
# 用于把「底座上电 / 继电器动作 / USB 枚举 / 掉线」对到同一条时间轴。
# 排查完 rm /data/egpu_trace.enable 即可，不加钩子也不会再跑。
if [ -f /data/egpu_trace.enable ] && [ -x "${DIR:-/data/openpilot}/system/egpu_usb_trace.sh" ]; then
  setsid "${DIR:-/data/openpilot}/system/egpu_usb_trace.sh" >/dev/null 2>&1 &
fi
