#!/usr/bin/env bash
# ============================================================================
# eGPU(chestnut / ASM2464) USB 链路时序记录
#
# 只在 /data/egpu_trace.enable 存在时由 launch_env.sh 拉起，默认记录 180 秒。
# 目的：把「继电器动作 / 底座上电 / USB 枚举 / 掉线」压到同一条时间轴上。
# 只在状态变化时写快照（外加 10s 心跳），日志很小。
#
# 启动：touch /data/egpu_trace.enable && 重启（或手动 setsid 跑一次）
# 停止：rm /data/egpu_trace.enable; pkill -f "egpu_usb_trac[e]"
# 查看：cat /data/egpu_trace.log
# ============================================================================
LOG=/data/egpu_trace.log
DUR=${EGPU_TRACE_SEC:-180}
PIDFILE=/tmp/egpu_usb_trace.pid

if [ -f "$PIDFILE" ]; then
  holder=$(cat "$PIDFILE" 2>/dev/null)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$PIDFILE" 2>/dev/null || true
trap 'rm -f "$PIDFILE"' EXIT INT TERM

snapshot() {
  local up=$1
  echo "=== t=${up}s  $(date -u +%FT%TZ 2>/dev/null)"
  echo -n "  devices: "
  local n=0
  for d in /sys/bus/usb/devices/*/; do
    [ -f "${d}idVendor" ] || continue
    v=$(cat "${d}idVendor" 2>/dev/null)
    [ "$v" = "1d6b" ] && continue
    echo -n "[$(basename "$d") ${v}:$(cat "${d}idProduct" 2>/dev/null) '$(cat "${d}product" 2>/dev/null)' speed=$(cat "${d}speed" 2>/dev/null) ] "
    n=$((n+1))
  done
  [ "$n" -eq 0 ] && echo -n "(none)"
  echo
  echo -n "  extcon: "
  cat /sys/class/extcon/*/state 2>/dev/null | tr '\n' ' '
  echo
  echo "  port1.connect_type=$(cat /sys/bus/usb/devices/usb4/4-0:1.0/usb4-port1/connect_type 2>/dev/null)"
  echo "  vbus.enable=$(sudo -n cat /sys/kernel/debug/regulator/smb2-vbus/enable 2>/dev/null) use_count=$(sudo -n cat /sys/kernel/debug/regulator/smb2-vbus/use_count 2>/dev/null)"
}

sig() {
  lsusb 2>/dev/null | grep -v "root hub"
  cat /sys/class/extcon/*/state 2>/dev/null
}

t0=$(cut -d. -f1 /proc/uptime)
: > "$LOG"
echo "[start] pid=$$ uptime=${t0}s dur=${DUR}s" >> "$LOG"

prev=""
last_hb=0
while :; do
  up=$(cut -d. -f1 /proc/uptime)
  cur=$(sig)
  el=$((up - t0))
  if [ "$cur" != "$prev" ] || [ $((up - last_hb)) -ge 10 ]; then
    snapshot "$up" >> "$LOG" 2>&1
    prev="$cur"
    last_hb=$up
  fi
  [ "$el" -ge "$DUR" ] && break
  sleep 1
done
echo "[done] uptime=$(cut -d. -f1 /proc/uptime)s" >> "$LOG"
