#!/usr/bin/env bash
# ============================================================================
# chestnut (ASM2464 / VID 3801) eGPU USB 节点权限守护
#
# 冷启动时 devtmpfs 以 0600 root:root 创建 /dev/bus/usb/<bus>/<dev>，
# udev 要等 builtin usb_id 把设备描述符读完，才按
# /usr/lib/udev/rules.d/50-udev-default.rules:72 设成 0664 root:root。
# 当 eGPU 底座和车机同时上电时，ASM2464 刚上电、描述符响应慢，
# 这个窗口能拖到几十秒；而 modeld 以 comma 身份在开机早期就 libusb_open
# -> EACCES -> load_big() 只试一次即放弃 -> 静默回退小模型。
# comma 在 root 组，所以只要节点到 0664 就能打开。
# 本脚本抢在 udev 前面把节点权限放开。
#
# 幂等：可重复执行；同一个 (bus,devnum) 只处理一次。
# 停止：pkill -f "egpu_usb_per[m]"
# ============================================================================
LOG=/data/egpu_usb_perm.log
VENDOR=3801

# comma.service 是 User=comma，/dev/bus/usb/*/* 属 root，
# comma 直接 chmod 会 EPERM。comma 有 NOPASSWD sudo，这里自提权到 root。
if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then
    exec sudo -n "$0" "$@"
  fi
  echo "$(date -u +%FT%TZ 2>/dev/null) [warn] uid=$(id -u) 非 root 且 sudo 不可用 -> chmod 会失败" >> "$LOG"
fi

# ---- 单实例保护 ----
# launch_env.sh 不只在开机时被 source（updated.py 会反复 bash -c source 它），
# 不加锁会累积几百个进程，每个都在 0.5s 轮询 sysfs。
PIDFILE=/tmp/egpu_usb_perm.pid
if [ -f "$PIDFILE" ]; then
  holder=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$PIDFILE" 2>/dev/null || true
trap 'rm -f "$PIDFILE"' EXIT INT TERM

echo "$(date -u +%FT%TZ 2>/dev/null) [start] pid=$$ uid=$(id -u) uptime=$(cut -d' ' -f1 /proc/uptime)s" >> "$LOG"

seen=" "
while :; do
  for d in /sys/bus/usb/devices/*/; do
    [ "$(cat "${d}idVendor" 2>/dev/null)" = "$VENDOR" ] || continue
    b=$(cat "${d}busnum" 2>/dev/null)
    n=$(cat "${d}devnum" 2>/dev/null)
    [ -n "$b" ] && [ -n "$n" ] || continue
    key="$b-$n"
    case "$seen" in *" $key "*) continue ;; esac
    node=$(printf '/dev/bus/usb/%03d/%03d' "$b" "$n")
    [ -e "$node" ] || continue
    before=$(stat -c '%a %U:%G' "$node" 2>/dev/null)
    if chmod 666 "$node" 2>/dev/null; then
      seen="$seen$key "
      echo "$(date -u +%FT%TZ 2>/dev/null) chmod 666 $node  (was: $before)  uptime=$(cut -d' ' -f1 /proc/uptime)s" >> "$LOG"
    fi
  done
  sleep 0.5
done
