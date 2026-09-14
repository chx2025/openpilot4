#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"
ROOT="$(git -C "$DIR" rev-parse --show-toplevel)"

# 弱网安装辅助库（缺失时自动降级为原有行为）
if [[ -f "$DIR/lib/net_retry.sh" ]]; then
  # shellcheck source=lib/net_retry.sh
  source "$DIR/lib/net_retry.sh"
fi

function retry() {
  local attempts=$1
  shift
  if [[ "${NET_LIB_LOADED:-0}" == "1" ]]; then
    net_retry "$attempts" "$@"
    return $?
  fi
  for i in $(seq 1 "$attempts"); do
    if "$@"; then
      return 0
    fi
    if [ "$i" -lt "$attempts" ]; then
      echo "  Attempt $i/$attempts failed, retrying in 5s..."
      sleep 5
    fi
  done
  return 1
}

function install_linux_deps() {
  SUDO=""

  if [[ ! $(id -u) -eq 0 ]]; then
    if [[ -z $(which sudo) ]]; then
      echo "Please install sudo or run as root"
      exit 1
    fi
    SUDO="sudo"
  fi

  local missing_linux_deps=0
  for cmd in gcc g++ make curl curl-config git; do
    if ! command -v "$cmd" > /dev/null 2>&1; then
      missing_linux_deps=1
      break
    fi
  done

  # ------------------------------------------------
  # dependencies should never be added to this list.
  # these are only for inflating bare docker images
  # to their desktop equivalents.
  # ------------------------------------------------
  if [[ "$missing_linux_deps" -eq 0 ]]; then
    # the native package managers are slow, so skip if we can
    echo "[ ] system packages already installed t=$SECONDS"
  elif command -v apt-get > /dev/null 2>&1; then
    # 官方源慢/不通时自动换国内镜像（AGNOS 上不动系统源）
    net_apt_mirror_setup 2> /dev/null || true
    # shellcheck disable=SC2046
    retry 3 $SUDO apt-get update $(net_apt_args)
    # shellcheck disable=SC2046
    retry 3 $SUDO apt-get install -y --no-install-recommends $(net_apt_args) ca-certificates build-essential curl libcurl4-openssl-dev locales git xclip wl-clipboard
  elif command -v dnf > /dev/null 2>&1; then
    $SUDO dnf install -y ca-certificates gcc gcc-c++ make curl libcurl-devel glibc-langpack-en git
  elif command -v yum > /dev/null 2>&1; then
    $SUDO yum install -y ca-certificates gcc gcc-c++ make curl libcurl-devel glibc-langpack-en git
  elif command -v pacman > /dev/null 2>&1; then
    $SUDO pacman -Syu --noconfirm --needed base-devel ca-certificates curl git
  elif command -v zypper > /dev/null 2>&1; then
    $SUDO zypper --non-interactive refresh
    $SUDO zypper --non-interactive install ca-certificates gcc gcc-c++ make curl libcurl-devel glibc-locale git
  elif command -v apk > /dev/null 2>&1; then
    $SUDO apk add --no-cache ca-certificates build-base curl curl-dev musl-locales git
  elif command -v xbps-install > /dev/null 2>&1; then
    $SUDO xbps-install -Syu base-devel ca-certificates curl git libcurl-devel glibc-locales
  else
    echo "Unsupported Linux distribution. Supported package managers: apt-get, dnf, yum, pacman, zypper, apk, xbps-install."
    exit 1
  fi

  if [[ -d "/etc/udev/rules.d/" ]]; then
    $SUDO tee /etc/udev/rules.d/11-openpilot.rules > /dev/null <<-EOF
	# Panda Jungle devices
	SUBSYSTEM=="usb", ATTRS{idVendor}=="3801", ATTRS{idProduct}=="ddcf", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="3801", ATTRS{idProduct}=="ddef", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="bbaa", ATTRS{idProduct}=="ddcf", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="bbaa", ATTRS{idProduct}=="ddef", MODE="0666"

	# Panda devices
	SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df11", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="3801", ATTRS{idProduct}=="ddcc", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="3801", ATTRS{idProduct}=="ddee", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="bbaa", ATTRS{idProduct}=="ddcc", MODE="0666"
	SUBSYSTEM=="usb", ATTRS{idVendor}=="bbaa", ATTRS{idProduct}=="ddee", MODE="0666"

	# comma devices over ADB
	SUBSYSTEM=="usb", ATTR{idVendor}=="04d8", ATTR{idProduct}=="1234", ENV{adb_user}="yes"
	EOF

    # delete the old ones
    $SUDO rm -f /etc/udev/rules.d/11-panda.rules /etc/udev/rules.d/12-panda_jungle.rules /etc/udev/rules.d/50-comma-adb.rules

    $SUDO udevadm control --reload-rules && $SUDO udevadm trigger || true
  fi
}

function install_uv() {
  # 依次尝试：官方脚本 → GitHub 反代 → PyPI（可走国内镜像）
  local installers=(
    "https://astral.sh/uv/install.sh"
    "https://ghfast.top/https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh"
    "https://gh.llkk.cc/https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh"
  )
  local url cand
  for url in "${installers[@]}"; do
    echo "installing uv from $url ..."
    if retry 3 sh -c "curl --retry 5 --retry-delay 5 --retry-all-errors -LsSf '$url' | UV_GITHUB_TOKEN='${GITHUB_TOKEN:-}' sh"; then
      for cand in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        [[ -x "$cand/uv" ]] && PATH="$cand:$PATH" && export PATH
      done
      command -v uv > /dev/null 2>&1 && return 0
    fi
  done

  echo "installing uv via pip ..."
  retry 3 python3 -m pip install --user uv && return 0
  retry 3 pip3 install --user uv && return 0
  return 1
}

function install_python_deps() {
  # Increase the pip timeout to handle TimeoutError
  export PIP_DEFAULT_TIMEOUT=200
  export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-180}"
  export UV_CONCURRENT_DOWNLOADS="${UV_CONCURRENT_DOWNLOADS:-4}"
  export UV_FETCH_RETRIES="${UV_FETCH_RETRIES:-5}"

  cd "$ROOT"

  # PyPI 直连慢时自动切国内源（只换下载地址，不影响 uv.lock 的 hash 校验）
  if [[ "${NET_LIB_LOADED:-0}" == "1" ]]; then
    net_pip_index_setup
  fi

  if ! command -v "uv" > /dev/null 2>&1; then
    if ! install_uv; then
      echo "uv 安装失败，可手动安装后重试：https://docs.astral.sh/uv/getting-started/installation/"
      return 1
    fi
  fi

  echo "updating uv..."
  # ok to fail, can also fail due to installing with brew
  timeout 90 uv self update || true

  echo "installing python packages..."
  retry 3 uv sync --frozen --all-extras
  source .venv/bin/activate
}

# --- Main ---

if [[ "${NET_LIB_LOADED:-0}" == "1" ]]; then
  # 单独运行本脚本时也要把弱网参数配上（幂等，重复执行无副作用）
  net_git_config_weak_network
  net_lfs_config
fi

if [[ "$OSTYPE" == "linux-gnu"* ]]; then
  install_linux_deps
  echo "[ ] installed system dependencies t=$SECONDS"
elif [[ "$OSTYPE" == "darwin"* ]]; then
  if [[ $SHELL == "/bin/zsh" ]]; then
    RC_FILE="$HOME/.zshrc"
  elif [[ $SHELL == "/bin/bash" ]]; then
    RC_FILE="$HOME/.bash_profile"
  fi
fi

if [ -f "$ROOT/pyproject.toml" ]; then
  install_python_deps
  echo "[ ] installed python dependencies t=$SECONDS"
fi

if [[ "$OSTYPE" == "darwin"* ]] && [[ -n "${RC_FILE:-}" ]]; then
  echo
  echo "----   OPENPILOT SETUP DONE   ----"
  echo "Open a new shell or configure your active shell env by running:"
  echo "source $RC_FILE"
fi
