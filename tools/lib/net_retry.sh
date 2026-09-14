#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 弱网安装辅助库 (weak-network helpers)
#
# 目标：在 GitHub / GitLab / PyPI 访问不稳定（丢包、RST、低速）的环境下，
#       让 openpilot 的 clone、submodule、LFS、uv、apt 各个环节都能：
#         1) 自动重试（指数退避）
#         2) 自动切换到可用的镜像/代理
#         3) 断在半路后重跑能续上，不重复劳动
#
# 用法：被 tools/op.sh 与 tools/setup_dependencies.sh source 使用
#         source "$(dirname "${BASH_SOURCE[0]}")/../lib/net_retry.sh"
#
# 可通过环境变量覆盖（在 git/uv 命令前 export 即可）：
#   OP_NET_MIRROR=auto|off|<前缀>   镜像策略，默认 auto（自动探测）
#   OP_NET_RETRIES=<次数>           默认 8
#   OP_NET_PROBE_TIMEOUT=<秒>       镜像探测超时，默认 8
#   OP_GIT_PROXY=<url>              例如 http://127.0.0.1:7890，设置后走代理
#   OP_PIP_INDEX_URL=auto|off|<url> PyPI 索引策略，默认 auto
#   OP_APT_MIRROR=auto|off|<主机>   apt 源策略，默认 auto（仅非 AGNOS 生效）
# ---------------------------------------------------------------------------

NET_LIB_LOADED=1

OP_NET_RETRIES="${OP_NET_RETRIES:-8}"
OP_NET_MIRROR="${OP_NET_MIRROR:-auto}"
OP_NET_PROBE_TIMEOUT="${OP_NET_PROBE_TIMEOUT:-8}"
OP_PIP_INDEX_URL="${OP_PIP_INDEX_URL:-auto}"
OP_APT_MIRROR="${OP_APT_MIRROR:-auto}"

# 记录本次写入的 git config key，便于精确清理，不误删用户自己的配置
NET_MIRROR_STATE_FILE="${HOME}/.op_net_mirror_keys"

# GitHub / GitLab 反代前缀（直接拼在原 URL 前面，失效不影响直连）
# 说明：第三方反代随时可能下线，脚本会逐个探测，全部不可用时自动回退直连。
NET_GITHUB_MIRRORS=(
  "https://ghfast.top/"
  "https://gh.llkk.cc/"
  "https://gh-proxy.com/"
  "https://ghproxy.net/"
)

NET_GITLAB_MIRRORS=(
  "https://ghfast.top/"
  "https://gh-proxy.com/"
  "https://ghproxy.net/"
)

NET_ACTIVE_MIRROR=""
NET_MIRROR_IDX=0

net_log()  { echo -e "\033[0;36m[net]\033[0m $*"; }
net_ok()   { echo -e "\033[0;32m[net]\033[0m $*"; }
net_warn() { echo -e "\033[0;33m[net]\033[0m $*" >&2; }

# --- 基础工具 ---------------------------------------------------------------

net_has_cmd() { command -v "$1" > /dev/null 2>&1; }

# net_retry <次数> <命令...>   失败后指数退避重试
net_retry() {
  local attempts="${1:-${OP_NET_RETRIES}}"
  shift
  local delay="${OP_NET_SLEEP_INIT:-5}"
  local i rc=0
  for ((i = 1; i <= attempts; i++)); do
    "$@" && return 0
    rc=$?
    if ((i < attempts)); then
      net_warn "第 $i/$attempts 次尝试失败 (exit=$rc)，${delay}s 后重试..."
      sleep "$delay"
      delay=$((delay * 2))
      ((delay > ${OP_NET_SLEEP_MAX:-60})) && delay="${OP_NET_SLEEP_MAX:-60}"
    fi
  done
  net_warn "已连续失败 ${attempts} 次，放弃：$*"
  return "$rc"
}

# 探测 URL 是否可达（返回 http 状态码，超时返回 000）
net_http_code() {
  local url="$1"
  curl -s -o /dev/null -m "${OP_NET_PROBE_TIMEOUT}" -L \
    -w '%{http_code}' "$url" 2> /dev/null || echo "000"
}

net_url_reachable() {
  local code
  code="$(net_http_code "$1")"
  case "$code" in
    200 | 204 | 301 | 302) return 0 ;;
    *) return 1 ;;
  esac
}

# 针对 git 的 info/refs 探测：光看状态码不够（反代常返回 200 的假页面），
# 必须确认返回体真的是 git-upload-pack 的应答
net_git_refs_ok() {
  local body
  # git 应答里含 NUL 字节，先去掉再比对，否则 bash 会刷警告
  body="$(curl -s -m "${OP_NET_PROBE_TIMEOUT}" -L "$1" 2> /dev/null | head -c 128 | tr -d '\000')"
  [[ "$body" == *"git-upload-pack"* || "$body" == *"service=git"* ]]
}

# 返回 URL 响应耗时（秒），不可达返回 99
net_url_time() {
  local t
  t="$(curl -s -o /dev/null -m "${OP_NET_PROBE_TIMEOUT}" -L \
    -w '%{time_total}' "$1" 2> /dev/null)"
  [[ -z "$t" ]] && t="99"
  echo "$t"
}

# 比较两个耗时字符串，返回较快的一方
net_faster() {
  local a="$1" b="$2"
  awk -v a="$a" -v b="$b" 'BEGIN { if (a + 0 <= b + 0) print a; else print b }'
}

# --- git 弱网参数 -----------------------------------------------------------

# 让 git 在"低速/卡死"时尽快放弃，把机会交给外层重试，而不是无限挂起
net_git_config_weak_network() {
  net_has_cmd git || return 0

  git config --global http.lowSpeedLimit 1000        # 低于 1KB/s
  git config --global http.lowSpeedTime 60           # 持续 60s 判定为卡死并断开
  git config --global http.postBuffer 524288000
  git config --global http.maxRequestBuffer 104857600
  git config --global http.version HTTP/1.1          # 部分反代对 HTTP/2 支持差
  git config --global core.compression 0             # 弱网下省 CPU，换取吞吐
  git config --global core.preloadIndex true
  git config --global pack.threads 1                 # 内存小的设备不打满
  git config --global fetch.writeCommitGraph false
  git config --global gc.auto 0                      # 安装过程中不要触发 gc
  git config --global advice.detachedHead false

  if [[ -n "${OP_GIT_PROXY:-}" ]]; then
    net_log "使用 git 代理：${OP_GIT_PROXY}"
    git config --global http.proxy "${OP_GIT_PROXY}"
    git config --global https.proxy "${OP_GIT_PROXY}"
  fi

  # 注意：调用方多在 `set -e` 下运行，这里必须显式返回 0，
  # 否则上一条 [[ ]] 为假时会直接中断整个安装流程
  return 0
}

# --- 镜像：探测 / 应用 / 轮换 / 清理 ----------------------------------------

_net_mirror_test_url() {
  case "$1" in
    github.com) echo "https://github.com/commaai/rednose/info/refs?service=git-upload-pack" ;;
    gitlab.com) echo "https://gitlab.com/sunnypilot/public/sunnypilot-new-lfs.git/info/refs?service=git-upload-pack" ;;
    *) echo "https://$1/info/refs?service=git-upload-pack" ;;
  esac
}

_net_mirror_list_for() {
  case "$1" in
    gitlab.com) printf '%s\n' "${NET_GITLAB_MIRRORS[@]}" ;;
    *) printf '%s\n' "${NET_GITHUB_MIRRORS[@]}" ;;
  esac
}

net_mirror_clear() {
  net_has_cmd git || return 0
  if [[ -f "${NET_MIRROR_STATE_FILE}" ]]; then
    local key
    while IFS= read -r key; do
      [[ -n "$key" ]] && git config --global --unset-all "$key" 2> /dev/null
    done < "${NET_MIRROR_STATE_FILE}"
    rm -f "${NET_MIRROR_STATE_FILE}"
  fi
  NET_ACTIVE_MIRROR=""
}

# net_mirror_apply <host> <prefix>  : 对某个 host 启用前缀反代
net_mirror_apply() {
  local host="$1" prefix="$2" key
  [[ -z "$prefix" ]] && return 0
  key="url.${prefix}https://${host}/.insteadOf"
  git config --global "$key" "https://${host}/"
  echo "$key" >> "${NET_MIRROR_STATE_FILE}"
  net_ok "已启用 ${host} 反代：${prefix}https://${host}/"
}

# 探测 host 直连是否可用
net_host_direct_ok() {
  net_git_refs_ok "$(_net_mirror_test_url "$1")"
}

# 为 host 选择镜像：直连可用就直连，否则逐个探测镜像
# 输出：写到 stdout 的是选中的前缀（空串表示直连）
net_pick_mirror_for() {
  local host="$1" prefix
  if [[ "${OP_NET_MIRROR}" == "off" ]]; then
    echo ""
    return 0
  fi
  if [[ "${OP_NET_MIRROR}" != "auto" ]]; then
    if net_git_refs_ok "${OP_NET_MIRROR}$(_net_mirror_test_url "$host")"; then
      echo "${OP_NET_MIRROR}"
    else
      echo ""
    fi
    return 0
  fi

  # 直连优先，避免被不可靠的第三方镜像拖慢
  if net_host_direct_ok "$host"; then
    echo ""
    return 0
  fi

  while IFS= read -r prefix; do
    [[ -z "$prefix" ]] && continue
    if net_git_refs_ok "${prefix}$(_net_mirror_test_url "$host")"; then
      net_log "直连 ${host} 不通，改用镜像 ${prefix}"
      echo "$prefix"
      return 0
    fi
  done < <(_net_mirror_list_for "$host")

  echo ""
}

net_mirror_setup() {
  local host prefix
  net_mirror_clear
  for host in github.com gitlab.com; do
    prefix="$(net_pick_mirror_for "$host")"
    [[ -n "$prefix" ]] && net_mirror_apply "$host" "$prefix"
  done
  return 0
}

# 失败后调用：强制换到下一个镜像（不再探测直连），返回 0 表示已切换
net_mirror_next() {
  local host="github.com" i=0 prefix
  local -a all
  mapfile -t all < <(_net_mirror_list_for "$host") 2> /dev/null || all=("${NET_GITHUB_MIRRORS[@]}")

  ((NET_MIRROR_IDX += 1))
  if ((NET_MIRROR_IDX > ${#all[@]})); then
    net_mirror_clear
    net_warn "所有镜像都试过了，回退直连"
    return 1
  fi

  prefix="${all[$((NET_MIRROR_IDX - 1))]}"
  net_mirror_clear
  net_mirror_apply "$host" "$prefix"
  net_mirror_apply "gitlab.com" "$prefix"
  return 0
}

net_mirror_status() {
  echo "OP_NET_MIRROR   = ${OP_NET_MIRROR}"
  echo "OP_NET_RETRIES  = ${OP_NET_RETRIES}"
  echo "OP_GIT_PROXY    = ${OP_GIT_PROXY:-<未设置>}"
  echo "OP_PIP_INDEX_URL= ${OP_PIP_INDEX_URL}"
  net_has_cmd git || return 0
  echo "--- 当前 url.* 重写 ---"
  git config --global --get-regexp '^url\.' 2> /dev/null || echo "  (无)"
  echo "--- LFS 配置 ---"
  git config --global --get-regexp '^lfs\.' 2> /dev/null || echo "  (无)"
}

# --- git-lfs 弱网参数 -------------------------------------------------------

net_lfs_config() {
  net_has_cmd git || return 0
  git config --global lfs.activitytimeout 180        # 默认 30s 太短，弱网必超时
  git config --global lfs.keepalive 30
  git config --global lfs.dialtimeout 30
  git config --global lfs.tlstimeout 60
  git config --global lfs.concurrenttransfers 3      # 默认 8，弱网并发高反易失败
  git config --global lfs.transfer.maxretries 10
  git config --global lfs.transfer.maxretrydelay 60
  git config --global lfs.largefilewarning false
  # 关键：单个大文件失败不中断整批，之后可反复 `op lfs` 补齐
  git config --global lfs.skipdownloaderrors true
}

# 统计尚未下载的 LFS 对象数量（0 表示齐全）
net_lfs_missing_count() {
  local dir="${1:-.}"
  net_has_cmd git-lfs || { echo 0; return 0; }
  git -C "$dir" lfs ls-files 2> /dev/null | grep -c '^-' || true
}

# 反复拉取 LFS 直到补齐（单点失败不会前功尽弃）
net_lfs_pull() {
  local dir="$1" tries="${2:-${OP_NET_RETRIES}}" i missing
  net_lfs_config
  for ((i = 1; i <= tries; i++)); do
    # 失败不当场退出：单点失败靠下一轮补齐（调用方常在 set -e 下）
    git -C "$dir" lfs pull || true
    missing="$(net_lfs_missing_count "$dir")"
    missing="${missing//[^0-9]/}"
    [[ -z "$missing" ]] && missing=0
    if [[ "$missing" == "0" ]]; then
      net_ok "LFS 文件齐全"
      return 0
    fi
    net_warn "还有 ${missing} 个 LFS 对象没拉下来（第 $i/${tries} 轮）"
    net_mirror_next || true
    sleep 5
  done
  net_warn "LFS 未完全下载，网络恢复后重跑本脚本或 'op lfs' 即可继续"
  return 1
}

# 某个子模块是否已就绪（目录非空且已 checkout 到记录中的 commit）
net_submodule_ready() {
  local dir="$1" p="$2" st
  [[ -n "$(ls -A "$dir/$p" 2> /dev/null)" ]] || return 1
  st="$(git -C "$dir" submodule status -- "$p" 2> /dev/null | head -1)"
  [[ -n "$st" && "${st:0:1}" != "-" ]]
}

# 更新子模块：先整体并行来一次；失败后逐个模块重试 + 自动换镜像，
# 已完成的模块自动跳过，重跑不会重复下载
net_submodules_update() {
  local dir="$1" tries="${2:-${OP_NET_RETRIES}}" depth="${3:-0}"
  local paths p i ok

  mapfile -t paths < <(git -C "$dir" config --file .gitmodules --get-regexp path 2> /dev/null | awk '{print $2}')
  if ((${#paths[@]} == 0)); then
    net_log "没有子模块，跳过"
    return 0
  fi

  net_log "更新子模块（共 ${#paths[@]} 个）..."
  if [[ "$depth" != "0" ]]; then
    if git -C "$dir" submodule update --jobs 4 --init --recursive --depth "$depth"; then
      net_ok "子模块就绪"
      return 0
    fi
  else
    if git -C "$dir" submodule update --jobs 4 --init --recursive; then
      net_ok "子模块就绪"
      return 0
    fi
  fi

  for p in "${paths[@]}"; do
    if net_submodule_ready "$dir" "$p"; then
      net_log "  $p 已就绪，跳过"
      continue
    fi
    ok=0
    for ((i = 1; i <= tries; i++)); do
      # 用 if 包住，避免 set -e 下失败直接把整个安装打断
      if [[ "$depth" != "0" ]]; then
        if git -C "$dir" submodule update --init --recursive --depth "$depth" -- "$p"; then
          ok=1
          break
        fi
      else
        if git -C "$dir" submodule update --init --recursive -- "$p"; then
          ok=1
          break
        fi
      fi
      net_warn "  $p 第 $i/${tries} 次失败，换线路重试"
      net_mirror_next || true
      sleep 5
    done
    if [[ "$ok" != "1" ]]; then
      net_warn "  $p 改用完整拉取（浅克隆可能拿不到指定 commit）"
      net_retry "$tries" git -C "$dir" submodule update --init --recursive -- "$p" || return 1
    fi
    net_ok "  $p 完成"
  done
  return 0
}

# --- PyPI / uv --------------------------------------------------------------

net_pip_index_setup() {
  [[ "${OP_PIP_INDEX_URL}" == "off" ]] && return 0

  local idx=""
  if [[ "${OP_PIP_INDEX_URL}" != "auto" ]]; then
    idx="${OP_PIP_INDEX_URL}"
  else
    local t_direct t_mirror
    t_direct="$(net_url_time "https://pypi.org/simple/uv/")"
    t_mirror="$(net_url_time "https://pypi.tuna.tsinghua.edu.cn/simple/uv/")"
    if [[ "${t_direct}" == "99" && "${t_mirror}" != "99" ]]; then
      idx="https://pypi.tuna.tsinghua.edu.cn/simple"
    elif [[ "${t_mirror}" != "99" ]]; then
      local best
      best="$(net_faster "$t_direct" "$t_mirror")"
      if [[ "$best" == "$t_mirror" ]]; then
        idx="https://pypi.tuna.tsinghua.edu.cn/simple"
      fi
    fi
  fi

  if [[ -n "$idx" ]]; then
    export UV_INDEX_URL="$idx"
    export PIP_INDEX_URL="$idx"
    net_ok "PyPI 使用索引：${idx}"
  fi
  return 0
}

# --- apt（仅 PC / 非 AGNOS 使用）-------------------------------------------

net_apt_args() {
  echo "-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=5 -o Acquire::http::Pipeline-Depth=0"
}

# 系统源慢的时候自动换国内镜像；只处理 Ubuntu 官方源，已改过的不动
net_apt_mirror_setup() {
  [[ "${OP_APT_MIRROR}" == "off" ]] && return 0
  net_has_cmd apt-get || return 0
  [[ -f /AGNOS ]] && return 0
  [[ -r /etc/os-release ]] || return 0

  local codename
  codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")"
  [[ -z "$codename" ]] && return 0

  local list="/etc/apt/sources.list"
  local target=""
  if [[ "${OP_APT_MIRROR}" != "auto" ]]; then
    target="${OP_APT_MIRROR}"
  else
    local t_direct t_mirror
    t_direct="$(net_url_time "http://archive.ubuntu.com/ubuntu/dists/${codename}/Release")"
    t_mirror="$(net_url_time "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/dists/${codename}/Release")"
    if [[ "${t_direct}" == "99" || "$(net_faster "$t_direct" "$t_mirror")" == "$t_mirror" ]]; then
      if [[ "${t_mirror}" != "99" ]]; then
        target="https://mirrors.tuna.tsinghua.edu.cn/ubuntu"
      fi
    fi
  fi
  [[ -z "$target" ]] && return 0

  if [[ -f "$list" ]] && grep -qE "^(deb|deb-src).*(archive|security)\.ubuntu\.com" "$list"; then
    cp "$list" "${list}.opbak" 2> /dev/null
    sed -i.bak -E "s#https?://(archive|security)\.ubuntu\.com/ubuntu#${target}#g" "$list" 2> /dev/null
    net_ok "apt 源已切换到 ${target}（原文件备份为 ${list}.opbak）"
  fi

  # deb822 格式（Ubuntu 24.04+）
  local deb822="/etc/apt/sources.list.d/ubuntu.sources"
  if [[ -f "$deb822" ]] && grep -qE "URIs:.*(archive|security)\.ubuntu\.com" "$deb822"; then
    cp "$deb822" "${deb822}.opbak" 2> /dev/null
    sed -i.bak -E "s#(URIs: *)https?://(archive|security)\.ubuntu\.com/ubuntu#\1${target}#g" "$deb822" 2> /dev/null
    net_ok "apt 源(deb822) 已切换到 ${target}"
  fi
  return 0
}

# --- 统一初始化 -------------------------------------------------------------

net_init() {
  net_git_config_weak_network
  net_mirror_setup
  net_lfs_config
  return 0
}
