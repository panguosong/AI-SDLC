#!/usr/bin/env bash
# Online installer for AI-SDLC.
# Usage:
#   ./packaging/install_online.sh
#   ./packaging/install_online.sh /path/to/venv
#   ./packaging/install_online.sh --add-to-path
# Env:
#   AI_SDLC_PACKAGE_SPEC=git+https://github.com/panguosong/AI-SDLC.git@v3.2.1   optional published package spec for pip install
#   PYTHON=/path/to/python3.11            optional interpreter override

set -euo pipefail

PACKAGE_SPEC="${AI_SDLC_PACKAGE_SPEC:-git+https://github.com/panguosong/AI-SDLC.git@v3.2.1}"
ADD_TO_PATH=0
POSITIONAL_VENV_TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --add-to-path)
      ADD_TO_PATH=1
      shift
      ;;
    *)
      if [[ -z "${POSITIONAL_VENV_TARGET}" ]]; then
        POSITIONAL_VENV_TARGET="$1"
      fi
      shift
      ;;
  esac
done
VENV_TARGET="${POSITIONAL_VENV_TARGET:-.venv}"

print_status() {
  local status_zh="$1"
  local status_en="$2"
  local command="$3"
  local purpose_zh="$4"
  local purpose_en="$5"
  echo "当前结果 / Result"
  echo "  ${status_zh}"
  echo "  ${status_en}"
  echo ""
  echo "下一步 / Next"
  echo "  ${command}"
  echo "  ${purpose_zh}"
  echo "  ${purpose_en}"
}

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    return 1
  fi
}

append_path_export_if_needed() {
  local bin_dir="$1"
  local shell_name
  local rc_file
  shell_name="${SHELL##*/}"
  case "${shell_name}" in
    zsh) rc_file="${HOME}/.zshrc" ;;
    bash) rc_file="${HOME}/.bashrc" ;;
    *) rc_file="${HOME}/.profile" ;;
  esac
  local export_line="export PATH=\"${bin_dir}:\$PATH\""
  if [[ -f "${rc_file}" ]]; then
    while IFS= read -r line; do
      if [[ "${line}" == *"${bin_dir}"* ]]; then
        return 0
      fi
    done < "${rc_file}"
  fi
  printf '\n# AI-SDLC CLI entrypoint\n%s\n' "${export_line}" >> "${rc_file}"
}

create_user_cli_link() {
  local user_bin="$1"
  local cli_path="$2"
  "${PYTHON_BIN}" - "${user_bin}" "${cli_path}" <<'PY'
from pathlib import Path
import os
import sys

user_bin = Path(os.path.expanduser(sys.argv[1]))
cli_path = Path(sys.argv[2]).resolve()
user_bin.mkdir(parents=True, exist_ok=True)
link = user_bin / "ai-sdlc"
if link.exists() or link.is_symlink():
    link.unlink()
link.symlink_to(cli_path)
PY
}

pick_python() {
  if [[ -n "${PYTHON:-}" ]] && "${PYTHON}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    printf '%s' "${PYTHON}"
    return 0
  fi
  local candidate
  for candidate in python3.11 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 \
      && "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      printf '%s' "${candidate}"
      return 0
    fi
  done
  return 1
}

install_python() {
  local os_name
  os_name="$(uname -s)"
  case "${os_name}" in
    Darwin)
      if ! command -v brew >/dev/null 2>&1; then
        return 1
      fi
      brew install python@3.11
      ;;
    Linux)
      if ! command -v apt-get >/dev/null 2>&1; then
        return 1
      fi
      run_privileged apt-get update
      run_privileged apt-get install -y python3.11 python3.11-venv python3-pip
      ;;
    *)
      return 1
      ;;
  esac
}

set_linux_distro_unknown() {
  LINUX_DISTRO="unknown"
  LINUX_VERSION="unknown"
}

normalize_os_release_value() {
  local value="$1"
  if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value}"
}

is_safe_os_release_value() {
  local value="$1"
  local quoted_value_pattern='^[A-Za-z0-9._:/?&=%#@+~,;() -]+$'
  local unquoted_value_pattern='^[A-Za-z0-9._:/@%+=,~-]+$'
  if [[ "${value:0:1}" == '"' ]]; then
    if [[ "${#value}" -lt 2 || "${value: -1}" != '"' ]]; then
      return 1
    fi
    value="${value:1:${#value}-2}"
    [[ "${value}" =~ ${quoted_value_pattern} ]]
    return
  fi
  if [[ "${value:0:1}" == "'" ]]; then
    if [[ "${#value}" -lt 2 || "${value: -1}" != "'" ]]; then
      return 1
    fi
    value="${value:1:${#value}-2}"
    [[ "${value}" =~ ${quoted_value_pattern} ]]
    return
  fi
  [[ "${value}" =~ ${unquoted_value_pattern} ]]
}

read_linux_identity() {
  local os_release_path="/etc/os-release"
  local line=""
  local key=""
  local value=""
  local candidate=""
  local raw_arch=""
  local raw_libc=""
  local id_value=""
  local version_value=""
  local id_seen=0
  local version_seen=0

  set_linux_distro_unknown
  LINUX_ARCH="unknown"
  LINUX_LIBC="unknown"
  raw_arch="$(uname -m 2>/dev/null || true)"
  raw_libc="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
  if [[ "${raw_arch}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    LINUX_ARCH="${raw_arch}"
  fi
  case "${raw_libc}" in
    glibc*) LINUX_LIBC="glibc" ;;
    musl*) LINUX_LIBC="musl" ;;
  esac
  if [[ ! -r "${os_release_path}" ]]; then
    return 1
  fi

  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" =~ ^[[:space:]]*$ || "${line}" =~ ^[[:space:]]*\# ]]; then
      continue
    fi
    if [[ "${line}" != *=* ]]; then
      set_linux_distro_unknown
      return 1
    fi
    IFS='=' read -r key value <<< "${line}"
    if [[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      || ! is_safe_os_release_value "${value}"; then
      set_linux_distro_unknown
      return 1
    fi
    case "${key}" in
      ID)
        candidate="$(normalize_os_release_value "${value}")"
        if [[ ! "${candidate}" =~ ^[A-Za-z0-9._-]+$ ]]; then
          set_linux_distro_unknown
          return 1
        fi
        if [[ "${id_seen}" -eq 1 && "${id_value}" != "${candidate}" ]]; then
          set_linux_distro_unknown
          return 1
        fi
        id_seen=1
        id_value="${candidate}"
        ;;
      VERSION_ID)
        candidate="$(normalize_os_release_value "${value}")"
        if [[ ! "${candidate}" =~ ^[A-Za-z0-9._-]+$ ]]; then
          set_linux_distro_unknown
          return 1
        fi
        if [[ "${version_seen}" -eq 1 && "${version_value}" != "${candidate}" ]]; then
          set_linux_distro_unknown
          return 1
        fi
        version_seen=1
        version_value="${candidate}"
        ;;
    esac
  done < "${os_release_path}"

  if [[ "${id_seen}" -ne 1 || "${version_seen}" -ne 1 ]]; then
    set_linux_distro_unknown
    return 1
  fi

  LINUX_DISTRO="${id_value}"
  LINUX_VERSION="${version_value}"
}

is_certified_debian_python_bootstrap() {
  [[ "${LINUX_DISTRO}" == "debian" && "${LINUX_VERSION}" == "12" ]] \
    && [[ "${LINUX_ARCH}" == "x86_64" || "${LINUX_ARCH}" == "amd64" ]] \
    && [[ "${LINUX_LIBC}" == "glibc" ]] \
    && command -v apt-get >/dev/null 2>&1
}

is_amd64_glibc_linux() {
  [[ "${LINUX_ARCH}" == "x86_64" || "${LINUX_ARCH}" == "amd64" ]] \
    && [[ "${LINUX_LIBC}" == "glibc" ]]
}

print_unsupported_linux_host() {
  print_status \
    "当前 Linux 主机不在缺少 Python 的在线自动安装认证范围内：distro=${LINUX_DISTRO} version=${LINUX_VERSION} arch=${LINUX_ARCH} libc=${LINUX_LIBC}。未执行 Python 包或 AI-SDLC 安装。" \
    "Unsupported Linux Python bootstrap host: distro=${LINUX_DISTRO} version=${LINUX_VERSION} arch=${LINUX_ARCH} libc=${LINUX_LIBC}. No Python package or AI-SDLC install was performed." \
    "Use ai-sdlc-offline-3.2.1-linux-amd64.tar.gz from User Guide route 6/12." \
    "缺少 Python 的在线自动安装仅认证 Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc；请使用路线 6/12 的 ai-sdlc-offline-3.2.1-linux-amd64.tar.gz。" \
    "Missing-Python online bootstrap is certified only for Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc; use the exact v3.2.1 Linux offline asset from route 6/12."
}

print_unsupported_linux_arch_or_libc() {
  print_status \
    "当前 Linux 架构或 libc 不受支持：distro=${LINUX_DISTRO} version=${LINUX_VERSION} arch=${LINUX_ARCH} libc=${LINUX_LIBC}。未执行 Python 包或 AI-SDLC 安装。" \
    "Unsupported Linux architecture or libc: distro=${LINUX_DISTRO} version=${LINUX_VERSION} arch=${LINUX_ARCH} libc=${LINUX_LIBC}. No Python package or AI-SDLC install was performed." \
    "Use a host with a compatible v3.2.1 Linux distribution asset." \
    "缺少 Python 的在线自动安装仅认证 Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc；该架构或 libc 没有兼容的 v3.2.1 Linux 发行资产。" \
    "Missing-Python online bootstrap is certified only for Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc; no compatible v3.2.1 Linux distribution asset exists for this architecture or libc."
}

require_git_for_package_source() {
  if [[ "${PACKAGE_SPEC}" != git+* ]]; then
    return 0
  fi
  if command -v git >/dev/null 2>&1; then
    git --version
    return 0
  fi
  print_status \
    "当前安装源使用 git+ 地址，但主机未检测到 Git。" \
    "Git is required for the configured git+ package source, but it was not detected." \
    "Install Git, then rerun this installer." \
    "macOS 可运行 xcode-select --install；Debian/Ubuntu 可运行 sudo apt-get install -y git；Fedora/RHEL 可运行 sudo dnf install -y git。" \
    "On macOS run xcode-select --install; on Debian/Ubuntu run sudo apt-get install -y git; on Fedora/RHEL run sudo dnf install -y git."
  exit 1
}

require_git_for_package_source

if ! PYTHON_BIN="$(pick_python)"; then
  OS_NAME="$(uname -s)"
  if [[ "${OS_NAME}" == "Linux" ]]; then
    read_linux_identity || true
    if ! is_certified_debian_python_bootstrap; then
      if is_amd64_glibc_linux; then
        print_unsupported_linux_host
      else
        print_unsupported_linux_arch_or_libc
      fi
      exit 1
    fi
  fi
  echo "No Python 3.11+ detected. Attempting online installation…"
  if ! install_python; then
    print_status \
      "当前主机未检测到 Python 3.11+，且无法自动完成在线安装。" \
      "Python 3.11+ was not detected, and online auto-install could not be completed on this host." \
      "./packaging/install_online.sh" \
      "在具备 Homebrew 或 apt 权限的环境中重新执行此脚本。" \
      "Rerun this script on a host with Homebrew or apt privileges available."
    exit 1
  fi
  if ! PYTHON_BIN="$(pick_python)"; then
    print_status \
      "当前主机未检测到 Python 3.11+，且无法自动完成在线安装。" \
      "Python 3.11+ was not detected, and online auto-install could not be completed on this host." \
      "./packaging/install_online.sh" \
      "自动安装已执行，但当前 shell 还未发现可用的 Python 3.11+；请刷新环境后重试此脚本。" \
      "Automatic installation ran, but the current shell still cannot discover Python 3.11+; refresh the environment and rerun this script."
    exit 1
  fi
fi

echo "Using Python runtime: ${PYTHON_BIN}"
echo "Creating venv: ${VENV_TARGET}"
"${PYTHON_BIN}" -m venv "${VENV_TARGET}"
VENV_PYTHON="${VENV_TARGET}/bin/python"
"${VENV_PYTHON}" -m pip install -U pip >/dev/null
"${VENV_PYTHON}" -m pip install "${PACKAGE_SPEC}"

CLI_PATH="${VENV_TARGET}/bin/ai-sdlc"
VENV_PYTHON_DIR="${VENV_PYTHON%/*}"
VENV_PYTHON_BASE="${VENV_PYTHON##*/}"
RESOLVED_VENV_PYTHON="$(cd "${VENV_PYTHON_DIR}" && pwd)/${VENV_PYTHON_BASE}"
NEXT_COMMAND="cd <your-project> && \"${RESOLVED_VENV_PYTHON}\" -m ai_sdlc init ."
if [[ "${ADD_TO_PATH}" == "1" ]]; then
  USER_BIN="${HOME}/.local/bin"
  create_user_cli_link "${USER_BIN}" "${CLI_PATH}"
  append_path_export_if_needed "${USER_BIN}"
  export PATH="${USER_BIN}:${PATH}"
  NEXT_COMMAND="cd <your-project> && ai-sdlc init ."
fi

echo ""
print_status \
  "在线安装完成。安装脚本已创建运行环境并安装 AI-SDLC。" \
  "Online installation completed. The installer created the runtime and installed AI-SDLC." \
  "${NEXT_COMMAND}" \
  "进入你的项目后执行初始化；init 会自动完成必要检查和安全预演。" \
  "Enter your project and initialize it; init will automatically run the required checks and safe rehearsal."

if [[ "${ADD_TO_PATH}" == "1" ]]; then
  echo ""
  echo "New terminals can run ai-sdlc directly."
else
  echo ""
  echo "Use the full command above, or rerun with --add-to-path for new terminals."
fi
