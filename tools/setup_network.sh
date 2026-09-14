#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 独立入口：网络不好时，先跑一次这个脚本，再跑 tools/op.sh setup
#
#   tools/setup_network.sh                # 自动探测：直连不通就换镜像，配置弱网参数
#   tools/setup_network.sh status         # 查看当前生效的网络配置
#   tools/setup_network.sh off            # 恢复直连，清掉脚本写入的所有配置
#   tools/setup_network.sh <镜像前缀>      # 手动指定，如 https://ghfast.top/
#   tools/setup_network.sh --proxy http://127.0.0.1:7890
#
# 常用环境变量：
#   OP_NET_RETRIES=15 tools/setup_network.sh        # 加大重试次数
#   OP_GIT_PROXY=http://127.0.0.1:7890 ...          # 走本地代理
# ---------------------------------------------------------------------------

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)"
# shellcheck source=lib/net_retry.sh
source "$DIR/lib/net_retry.sh"

ACTION="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --proxy)
      export OP_GIT_PROXY="$2"
      shift 2
      ;;
    -h | --help)
      awk 'NR==1 {next} /^#/ {print; next} {exit}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      ACTION="$1"
      shift
      ;;
  esac
done

case "$ACTION" in
  status)
    net_mirror_status
    ;;
  off)
    net_mirror_clear
    net_git_config_weak_network
    net_ok "已恢复直连（镜像重写已清除）"
    ;;
  auto)
    net_init
    net_mirror_status
    ;;
  *)
    OP_NET_MIRROR="$ACTION" net_init
    ;;
esac
