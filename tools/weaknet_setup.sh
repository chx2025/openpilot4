#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 弱网一键安装 / 续装
#
# 特点：
#   * 每一步都是幂等的，中断后重跑同一条命令即可继续，不会从头再来
#   * 子模块逐个拉取，已完成的自动跳过，失败自动换线路重试
#   * LFS 分轮补齐，单个大文件失败不会让整批前功尽弃
#   * 默认浅克隆（--depth 1），下载量最小；网络好时可 --unshallow 补历史
#
# 用法（车机 C3/C3XL 示例）：
#   tools/weaknet_setup.sh --dir /data/openpilot --branch dev-sp-egpu
#   tools/weaknet_setup.sh --dir /data/openpilot --branch dev-sp-egpu --build
#   OP_NET_RETRIES=15 tools/weaknet_setup.sh            # 网络极差时加大重试
#   tools/weaknet_setup.sh --proxy http://192.168.1.10:7890   # 走局域网代理
#   tools/weaknet_setup.sh --only lfs                   # 只补 LFS 模型文件
#
# 常用参数：
#   --dir DIR        安装目录（默认：AGNOS 为 /data/openpilot，PC 为 ~/openpilot）
#   --repo URL       仓库地址（默认 https://github.com/chx2025/openpilot）
#   --branch NAME    分支（默认 dev-sp-egpu）
#   --depth N        克隆深度，0 表示完整历史（默认 1）
#   --unshallow      装完后补全 git 历史（可选）
#   --mirror auto|off|<前缀>   镜像策略（默认 auto）
#   --proxy URL      http(s) 代理，如 http://127.0.0.1:7890
#   --retries N      每步重试次数（默认 8）
#   --only STEP      只跑某一步：net|clone|submodules|lfs|deps|build
#   --clean          更新前清空工作区（会删除未跟踪文件，谨慎）
#   --no-deps        跳过 Python / 系统依赖安装
#   --build          装完后编译
# ---------------------------------------------------------------------------

set -uo pipefail

DIR=""
REPO="https://github.com/chx2025/openpilot"
BRANCH="dev-sp-egpu"
DEPTH="1"
UNSHALLOW=0
CLEAN=0
NO_DEPS=0
BUILD=0
ONLY=""
RETRIES="${OP_NET_RETRIES:-8}"

if [[ -f /AGNOS ]]; then
  DEFAULT_DIR="/data/openpilot"
else
  DEFAULT_DIR="${HOME}/openpilot"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --depth) DEPTH="$2"; shift 2 ;;
    --retries) RETRIES="$2"; export OP_NET_RETRIES="$2"; shift 2 ;;
    --mirror) export OP_NET_MIRROR="$2"; shift 2 ;;
    --proxy) export OP_GIT_PROXY="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    --unshallow) UNSHALLOW=1; shift ;;
    --clean) CLEAN=1; shift ;;
    --no-deps) NO_DEPS=1; shift ;;
    --build) BUILD=1; shift ;;
    -h | --help)
      awk 'NR==1 {next} /^#/ {print; next} {exit}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
done

DIR="${DIR:-$DEFAULT_DIR}"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" > /dev/null && pwd)"
NET_LIB="$SELF_DIR/lib/net_retry.sh"

# 支持单文件自举：只拿到本脚本也能跑（例如车机上 curl 下来直接执行）
if [[ ! -f "$NET_LIB" ]]; then
  echo "未找到 $NET_LIB，尝试从仓库获取..."
  raw_base="${REPO%.git}"
  if [[ "$raw_base" == *github.com* ]]; then
    raw_base="${raw_base/github.com/raw.githubusercontent.com}"
    mkdir -p "$SELF_DIR/lib"
    curl --retry 5 --retry-delay 3 --retry-all-errors -fsSL "${raw_base}/${BRANCH}/tools/lib/net_retry.sh" -o "$NET_LIB" || true
  fi
fi

if [[ -f "$NET_LIB" ]]; then
  # shellcheck source=lib/net_retry.sh
  source "$NET_LIB"
else
  echo "缺少 $NET_LIB，请把它和本脚本一起放到 tools/ 下"
  exit 1
fi

STEP=0
TOTAL=5
step() {
  STEP=$((STEP + 1))
  echo
  echo "======================================================================"
  echo "[$STEP/$TOTAL] $*  t=${SECONDS}s"
  echo "======================================================================"
}

should_run() { [[ -z "$ONLY" || "$ONLY" == "$1" ]]; }

# ---------------------------------------------------------------- 1. 网络
if should_run net; then
  step "网络自适应：探测镜像 / 配置 git 与 LFS 超时"
  net_init
fi

# ---------------------------------------------------------------- 2. 主仓库
do_clone() {
  local depth_args=()
  [[ "$DEPTH" != "0" ]] && depth_args=(--depth "$DEPTH")

  if [[ -d "$DIR/.git" ]]; then
    echo "目录已存在，增量更新：$DIR"
    git -C "$DIR" remote set-url origin "$REPO" 2> /dev/null || git -C "$DIR" remote add origin "$REPO" 2> /dev/null
    if [[ "$CLEAN" == "1" ]]; then
      echo "清理未跟踪文件（--clean）..."
      git -C "$DIR" clean -dfx
    fi
    if [[ "${#depth_args[@]}" -gt 0 ]]; then
      net_retry "$RETRIES" git -C "$DIR" fetch "${depth_args[@]}" origin "$BRANCH" || \
        net_retry "$RETRIES" git -C "$DIR" fetch origin "$BRANCH"
    else
      net_retry "$RETRIES" git -C "$DIR" fetch origin "$BRANCH"
    fi
    git -C "$DIR" checkout -f FETCH_HEAD
    git -C "$DIR" checkout -B "$BRANCH" FETCH_HEAD 2> /dev/null || true
  else
    echo "初始化仓库：$DIR"
    mkdir -p "$DIR"
    git -C "$DIR" init -q
    git -C "$DIR" remote add origin "$REPO" 2> /dev/null || \
      git -C "$DIR" remote set-url origin "$REPO"
    if [[ "${#depth_args[@]}" -gt 0 ]]; then
      net_retry "$RETRIES" git -C "$DIR" fetch "${depth_args[@]}" origin "$BRANCH" || \
        net_retry "$RETRIES" git -C "$DIR" fetch origin "$BRANCH"
    else
      net_retry "$RETRIES" git -C "$DIR" fetch origin "$BRANCH"
    fi
    git -C "$DIR" checkout -f -B "$BRANCH" FETCH_HEAD
  fi

  if ! git -C "$DIR" rev-parse --verify HEAD > /dev/null 2>&1; then
    net_err_exit "仓库检出失败：$DIR"
  fi
}

net_err_exit() {
  echo -e "\033[0;31m[✗]\033[0m $1" >&2
  exit 1
}

if should_run clone; then
  step "拉取主仓库 $BRANCH"
  do_clone
fi

# ---------------------------------------------------------------- 3. 子模块
if should_run submodules; then
  step "拉取子模块（逐个重试，已完成的自动跳过）"
  net_submodules_update "$DIR" "$RETRIES" "$DEPTH" || net_err_exit "子模块拉取失败，重跑本脚本即可继续"
fi

# ---------------------------------------------------------------- 4. LFS
if should_run lfs; then
  step "拉取 LFS 模型文件（分轮补齐）"
  if ! net_lfs_pull "$DIR" "$RETRIES"; then
    echo -e "\033[0;33m[!]\033[0m LFS 未完全下载，稍后重跑："
    echo "    $0 --dir $DIR --branch $BRANCH --only lfs"
  fi
fi

# ---------------------------------------------------------------- 5. 依赖 / 编译
if [[ "$NO_DEPS" == "0" ]] && should_run deps; then
  step "安装系统 / Python 依赖"
  if [[ -f "$DIR/tools/setup_dependencies.sh" ]]; then
    # shellcheck source=/dev/null
    "$DIR/tools/setup_dependencies.sh"
  else
    echo "未找到 tools/setup_dependencies.sh，跳过"
  fi
fi

if [[ "$UNSHALLOW" == "1" ]]; then
  step "补全 git 历史（--unshallow）"
  net_retry "$RETRIES" git -C "$DIR" fetch --unshallow origin "$BRANCH" || \
    echo "补全历史失败，不影响使用（浅克隆也能正常编译运行）"
fi

if [[ "$BUILD" == "1" ]] && should_run build; then
  step "编译"
  cd "$DIR"
  if [[ -f /AGNOS ]]; then
    openpilot/system/manager/build.py
  else
    scons -u -j"$(nproc 2> /dev/null || echo 4)"
  fi
fi

echo
echo "======================================================================"
echo "完成，用时 ${SECONDS}s"
echo "  目录：$DIR"
echo "  分支：$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2> /dev/null)"
echo "  LFS 未下载对象：$(net_lfs_missing_count "$DIR")"
echo "======================================================================"
echo "后续："
echo "  cd $DIR && tools/op.sh setup      # 装 op 命令 + 依赖（可反复重跑）"
echo "  cd $DIR && tools/op.sh lfs        # 只补 LFS 文件"
echo "  cd $DIR && tools/op.sh net status # 查看当前走的是直连还是镜像"
