# AI-SDLC 3.2.0 中文用户指南

本指南面向第一次接触 AI-SDLC 的普通用户，对应 `v3.2.0` 发布版本。所有安装器、离线包、校验文件和安装后版本必须保持一致；仅在对应正式 Release 可用后使用这些下载链接，不用开发分支替代未就绪的制品。

项目地址：<https://github.com/panguosong/AI-SDLC>

外部 stable shim 与 `python -m ai_sdlc` 是 Windows 支持即时更新和原命令重放的入口。Windows 运行时目录内的 direct `Scripts\ai-sdlc.exe` 活动时不能安全替换：它只给出迁移提示、零安装并让当前业务命令继续一次；显式 direct self-update 不修改安装且返回非零。`-AddToPath` 或 `--add-to-path` 成功后，新终端中的裸 `ai-sdlc` 是日常入口；当前安装窗口使用路线内给出的 module 命令。

初始化会让你选择实际用于聊天开发的 AI 代理入口和 Shell。可选代理包括 Claude Code、Codex、Cursor、VS Code、其他-通用；Shell 按当前系统选择 PowerShell、Bash、Zsh 或 Cmd。

`v3.2.0` 正式 Release 应提供以下离线资产：

- <https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/ai-sdlc-offline-3.2.0-windows-amd64.zip>
- <https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/ai-sdlc-offline-3.2.0-windows-amd64.zip.sha256>
- <https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/ai-sdlc-offline-3.2.0-macos-arm64.tar.gz>
- <https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/ai-sdlc-offline-3.2.0-macos-arm64.tar.gz.sha256>
- <https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/ai-sdlc-offline-3.2.0-linux-amd64.tar.gz>
- <https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/ai-sdlc-offline-3.2.0-linux-amd64.tar.gz.sha256>

每个归档旁都必须同时下载完全同名并追加 `.sha256` 的 sidecar。

<!-- AI-SDLC-USER-GUIDE-MATRIX: 2x2x3=12 -->

## 路线选择器

先判断项目目录是空的还是已有业务文件，再选择渠道和操作系统。每条路线都包含准备、获取、校验、安装、初始化或接入、成功证据和就地恢复。

| 项目状态 | 渠道 | Windows AMD64 | macOS Apple Silicon | Linux AMD64 |
| --- | --- | --- | --- | --- |
| 全新用户 + 全新空项目 | 在线 | [路线 1](#route-new-online-windows-amd64) | [路线 2](#route-new-online-macos-arm64) | [路线 3](#route-new-online-linux-amd64) |
| 全新用户 + 全新空项目 | 离线 | [路线 4](#route-new-offline-windows-amd64) | [路线 5](#route-new-offline-macos-arm64) | [路线 6](#route-new-offline-linux-amd64) |
| 全新用户 + 已有项目 | 在线 | [路线 7](#route-existing-online-windows-amd64) | [路线 8](#route-existing-online-macos-arm64) | [路线 9](#route-existing-online-linux-amd64) |
| 全新用户 + 已有项目 | 离线 | [路线 10](#route-existing-offline-windows-amd64) | [路线 11](#route-existing-offline-macos-arm64) | [路线 12](#route-existing-offline-linux-amd64) |

### 1.1 Windows

全新空项目选择路线 1 或 4；已有项目选择路线 7 或 10。

### 1.2 macOS（Apple Silicon）

全新空项目选择路线 2 或 5；已有项目选择路线 8 或 11。

### 1.3 Linux（amd64）

全新空项目选择路线 3 或 6；已有项目选择路线 9 或 12。

### 1.4 选择 AI 适配器和 Shell

空项目在各自路线的初始化步骤选择实际使用的 AI 代理入口与 Shell。

### 2.4 选择 AI 适配器和 Shell

已有项目同样先完成初始化选择，再执行 `adopt`；不得跳过初始化直接扫描项目。

## 第一章：全新用户 + 全新空项目

<a id="route-new-online-windows-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|online|windows-amd64 -->
## 路线 1：全新空项目 · 在线安装 · Windows AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 64 位 Windows（`windows-amd64`）和 PowerShell。需要联网访问 GitHub，并允许在当前用户目录创建项目与运行环境；安装器负责 Python、venv 和依赖，但在线 Git 安装源要求主机已有 Git。

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = Join-Path $HOME "projects\my-new-project"
$InstallRoot = Join-Path $HOME "AI-SDLC\online-v3.2.0"
$VenvRoot = Join-Path $InstallRoot ".venv"
$DownloadRoot = Join-Path $env:TEMP "ai-sdlc-v3.2.0-online"
New-Item -ItemType Directory -Force -Path $ProjectRoot, $InstallRoot, $DownloadRoot | Out-Null
if ((Get-ChildItem -LiteralPath $ProjectRoot -Force).Count -ne 0) { throw "Project directory must be empty" }
$GitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $GitCommand) { throw "Git is required. Run: winget install --id Git.Git -e, then reopen PowerShell." }
git --version
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

```powershell
$InstallerName = "install_online.ps1"
$InstallerUrl = "https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.0/packaging/install_online.ps1"
$InstallerPath = Join-Path $DownloadRoot $InstallerName
Invoke-WebRequest -Uri $InstallerUrl -OutFile $InstallerPath
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```powershell
$PinnedTag = "v3.2.0"
if (-not (Select-String -LiteralPath $InstallerPath -SimpleMatch $PinnedTag -Quiet)) { throw "Installer is not pinned to v3.2.0" }
Write-Host "After install verify with: python -m ai_sdlc --version"
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```powershell
# 固定标签安装器：install_online.ps1 -AddToPath
powershell -NoProfile -ExecutionPolicy Bypass -File $InstallerPath -VenvPath $VenvRoot -AddToPath
$ModulePython = Join-Path $VenvRoot "Scripts\python.exe"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化

```powershell
Set-Location $ProjectRoot
# 新终端等价入口：ai-sdlc init .
& $ModulePython -m ai_sdlc init .
```

选择实际使用的 Claude Code、Codex、Cursor、VS Code 或其他-通用，再选择 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```powershell
& $ModulePython -m ai_sdlc --version
& $ModulePython -m ai_sdlc status
```

必须看到 `3.2.0`、`Initialized AI-SDLC project`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点；空项目不会出现示例业务代码。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

下载失败时停止并重试固定标签 URL，不要改用未发布分支。PowerShell 阻止脚本时继续使用上面的单次 Bypass 命令。Windows 运行时目录内的 direct `Scripts\ai-sdlc.exe` 活动时不能安全原地替换；显式 direct self-update 零安装并返回非零，请改用 `python -m ai_sdlc`（本路线即 `& $ModulePython -m ai_sdlc self-update install --version 3.2.0`）或新终端中的 stable `ai-sdlc`。裸命令不可用时运行 `& $ModulePython -m ai_sdlc status`；若出现 `No module named ai_sdlc`，重跑 `install_online.ps1 -AddToPath`。若显示 `open gates`，按 CLI 提示查看详情；代理或 Shell 选错时运行 `ai-sdlc adapter select`、`ai-sdlc adapter shell-select`。

<a id="route-new-online-macos-arm64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|online|macos-arm64 -->
## 路线 2：全新空项目 · 在线安装 · macOS Apple Silicon

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 Apple Silicon macOS（`macos-arm64`）和 Terminal 中的 zsh/bash。需要联网访问 GitHub；安装器负责 Python 运行环境，但在线 Git 安装源要求主机已有 Git。

```bash
set -e
PROJECT_ROOT="$HOME/projects/my-new-project"
INSTALL_ROOT="$HOME/Applications/AI-SDLC/online-v3.2.0"
VENV_ROOT="$INSTALL_ROOT/.venv"
DOWNLOAD_ROOT="$(mktemp -d)"
mkdir -p "$PROJECT_ROOT" "$INSTALL_ROOT"
test -z "$(ls -A "$PROJECT_ROOT")" || { echo "Project directory must be empty"; exit 1; }
command -v git >/dev/null 2>&1 || { echo "Git is required. Run: xcode-select --install, then reopen Terminal." >&2; exit 1; }
git --version
if ! command -v brew >/dev/null 2>&1; then
  if test ! -x /opt/homebrew/bin/brew; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
brew --version
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

```bash
INSTALLER_NAME="install_online.sh"
INSTALLER_URL="https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.0/packaging/install_online.sh"
INSTALLER_PATH="$DOWNLOAD_ROOT/$INSTALLER_NAME"
curl --fail --location --retry 3 --output "$INSTALLER_PATH" "$INSTALLER_URL"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
PINNED_TAG="v3.2.0"
grep -F "$PINNED_TAG" "$INSTALLER_PATH" >/dev/null || { echo "Installer is not pinned to v3.2.0"; exit 1; }
echo 'After install verify with: python -m ai_sdlc --version'
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
# 固定标签安装器：install_online.sh --add-to-path
bash "$INSTALLER_PATH" "$VENV_ROOT" --add-to-path
MODULE_PYTHON="$VENV_ROOT/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化

```bash
cd "$PROJECT_ROOT"
# 新终端等价入口：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
```

必须看到 `3.2.0`、`Initialized AI-SDLC project`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点；空目录不会出现示例业务文件。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

网络错误时停止并重试固定 URL。若缺少 Homebrew，运行 `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`，再运行 `eval "$(/opt/homebrew/bin/brew shellenv)"` 后重试安装。裸命令不可用时运行 `"$MODULE_PYTHON" -m ai_sdlc status`；`No module named ai_sdlc` 时重跑 `install_online.sh --add-to-path`。出现 `open gates` 时按 CLI 指示查看详情；入口选择错误使用 `ai-sdlc adapter select`、`ai-sdlc adapter shell-select`。

<a id="route-new-online-linux-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|online|linux-amd64 -->
## 路线 3：全新空项目 · 在线安装 · Linux AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 64 位 Linux（`linux-amd64`）和 bash。已存在 Python 3.11+ 时，保持发行版无关的在线兼容路径；缺少 Python 3.11+ 时，自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) 的 amd64/x86_64 + glibc 主机。其他无 Python 的 amd64/x86_64 + glibc 主机应使用路线 6/12 的 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。非 AMD64 或非 glibc 的 Linux 主机，v3.2.0 没有兼容的 Linux 发行资产；不得使用路线 6/12 的 AMD64 离线包。需要联网访问 GitHub，当前用户应能写入 `$HOME/.local/share`；下载与在线 Git 安装源要求主机具备 CA 证书、curl 和 Git。

```bash
set -e
PROJECT_ROOT="$HOME/projects/my-new-project"
INSTALL_ROOT="$HOME/.local/share/AI-SDLC/online-v3.2.0"
VENV_ROOT="$INSTALL_ROOT/.venv"
DOWNLOAD_ROOT="$(mktemp -d)"
mkdir -p "$PROJECT_ROOT" "$INSTALL_ROOT"
test -z "$(ls -A "$PROJECT_ROOT")" || { echo "Project directory must be empty"; exit 1; }
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else echo "Root or sudo is required to install online prerequisites." >&2; return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl git
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl git
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl git
  else echo "No supported prerequisite package manager (apt-get/dnf/yum) was found." >&2; exit 1; fi
fi
command -v git >/dev/null 2>&1 || { echo "Git installation did not produce an executable git command." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "Prerequisite installation did not produce an executable curl command." >&2; exit 1; }
ca_bundle_available || { echo "Prerequisite installation did not produce a readable CA certificate bundle." >&2; exit 1; }
git --version
curl --version
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

```bash
INSTALLER_NAME="install_online.sh"
INSTALLER_URL="https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.0/packaging/install_online.sh"
INSTALLER_PATH="$DOWNLOAD_ROOT/$INSTALLER_NAME"
curl --fail --location --retry 3 --output "$INSTALLER_PATH" "$INSTALLER_URL"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
PINNED_TAG="v3.2.0"
grep -F "$PINNED_TAG" "$INSTALLER_PATH" >/dev/null || { echo "Installer is not pinned to v3.2.0"; exit 1; }
echo 'After install verify with: python -m ai_sdlc --version'
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
# 固定标签安装器：install_online.sh --add-to-path
bash "$INSTALLER_PATH" "$VENV_ROOT" --add-to-path
MODULE_PYTHON="$VENV_ROOT/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化

```bash
cd "$PROJECT_ROOT"
# 新终端等价入口：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
```

必须看到 `3.2.0`、`Initialized AI-SDLC project`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点，且空目录没有示例业务代码。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

已存在 Python 3.11+ 时，保持发行版无关的在线兼容路径；缺少 Python 3.11+ 时，自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) 的 amd64/x86_64 + glibc 主机。其他无 Python 的 amd64/x86_64 + glibc 主机应使用路线 6/12 的 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。非 AMD64 或非 glibc 的 Linux 主机，v3.2.0 没有兼容的 Linux 发行资产；不得使用路线 6/12 的 AMD64 离线包。

Git、curl 或 CA 证书不可用时执行：

```bash
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl git
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl git
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl git
  else echo "No supported prerequisite package manager (apt-get/dnf/yum)" >&2; exit 1; fi
fi
command -v git >/dev/null 2>&1 && command -v curl >/dev/null 2>&1 && ca_bundle_available
git --version && curl --version
```

下载或权限错误时停止，确认安装目录可写后重跑固定标签的 `install_online.sh --add-to-path`。裸命令不可用时使用 module 路径；`No module named ai_sdlc` 时重跑安装器。`open gates`、代理和 Shell 问题分别按 CLI 提示、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 恢复。

<a id="route-new-offline-windows-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|offline|windows-amd64 -->
## 路线 4：全新空项目 · 离线安装 · Windows AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `windows-amd64`。联网机器下载包和同名 `.sha256`，目标机器可以完全离线。准备空项目目录和可写安装目录。

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = Join-Path $HOME "projects\my-new-project"
$InstallRoot = Join-Path $HOME "AI-SDLC"
$DownloadRoot = Join-Path $HOME "Downloads\ai-sdlc-v3.2.0"
New-Item -ItemType Directory -Force -Path $ProjectRoot, $InstallRoot, $DownloadRoot | Out-Null
if ((Get-ChildItem -LiteralPath $ProjectRoot -Force).Count -ne 0) { throw "Project directory must be empty" }
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

离线包内包含 `install_offline.ps1`。联网机器下载两项后原样复制到目标机器。

```powershell
$ErrorActionPreference = "Stop"
$DownloadRoot = Join-Path $HOME "Downloads\ai-sdlc-v3.2.0"
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
$PackageName = "ai-sdlc-offline-3.2.0-windows-amd64.zip"
$PackageUrl = "https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/$PackageName"
Invoke-WebRequest -Uri $PackageUrl -OutFile (Join-Path $DownloadRoot $PackageName)
Invoke-WebRequest -Uri "$PackageUrl.sha256" -OutFile (Join-Path $DownloadRoot "$PackageName.sha256")
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = Join-Path $HOME "projects\my-new-project"
$InstallRoot = Join-Path $HOME "AI-SDLC"
$DownloadRoot = Join-Path $HOME "Downloads\ai-sdlc-v3.2.0"
$PackageName = "ai-sdlc-offline-3.2.0-windows-amd64.zip"
New-Item -ItemType Directory -Force -Path $ProjectRoot, $InstallRoot, $DownloadRoot | Out-Null
if ((Get-ChildItem -LiteralPath $ProjectRoot -Force).Count -ne 0) { throw "Project directory must be empty" }
$PackagePath = Join-Path $DownloadRoot $PackageName
$Parts = (Get-Content -LiteralPath "$PackagePath.sha256" -Raw).Trim() -split '\s+', 2
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $PackagePath).Hash.ToLowerInvariant()
if ($Parts.Count -ne 2 -or $Parts[1] -ne $PackageName -or $Parts[0].ToLowerInvariant() -ne $Actual) { throw "SHA256 verification failed for $PackageName" }
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```powershell
Expand-Archive -LiteralPath $PackagePath -DestinationPath $InstallRoot -Force
$BundleRoot = Join-Path $InstallRoot "ai-sdlc-offline-3.2.0-windows-amd64"
Push-Location $BundleRoot
try { powershell -NoProfile -ExecutionPolicy Bypass -File ".\install_offline.ps1" -AddToPath } finally { Pop-Location }
$ModulePython = Join-Path $BundleRoot ".venv\Scripts\python.exe"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化

```powershell
Set-Location $ProjectRoot
# 新终端等价入口：ai-sdlc init .
& $ModulePython -m ai_sdlc init .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```powershell
& $ModulePython -m ai_sdlc --version
& $ModulePython -m ai_sdlc status
```

应看到 `Offline installation completed`、`3.2.0`、`Initialized AI-SDLC project`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点；空项目没有示例业务代码。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

出现 `SHA256 verification failed` 时停止，删除包和 sidecar 后重新获取。权限错误使用单次 Bypass。Windows 运行时目录内的 direct `Scripts\ai-sdlc.exe` 活动时不能安全原地替换；显式 direct self-update 零安装并返回非零，请改用 `python -m ai_sdlc`（本路线即 `& $ModulePython -m ai_sdlc self-update install --version 3.2.0`）或新终端中的 stable `ai-sdlc`。裸命令不可用时运行 `& $ModulePython -m ai_sdlc status`；`No module named ai_sdlc` 时重跑 `install_offline.ps1 -AddToPath`。`open gates`、代理和 Shell 问题分别按 CLI 指示、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

<a id="route-new-offline-macos-arm64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|offline|macos-arm64 -->
## 路线 5：全新空项目 · 离线安装 · macOS Apple Silicon

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `macos-arm64`。联网机器下载归档和 sidecar，目标 Mac 可离线安装。不要移动安装完成后的运行目录。

```bash
set -e
PROJECT_ROOT="$HOME/projects/my-new-project"
INSTALL_ROOT="$HOME/Applications/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$PROJECT_ROOT" "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
test -z "$(ls -A "$PROJECT_ROOT")" || { echo "Project directory must be empty"; exit 1; }
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

归档内包含 `install_offline.sh`。联网机器下载两项后原样复制到目标 Mac。

```bash
set -e
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$DOWNLOAD_ROOT"
PACKAGE_NAME="ai-sdlc-offline-3.2.0-macos-arm64.tar.gz"
PACKAGE_URL="https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/$PACKAGE_NAME"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME" "$PACKAGE_URL"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME.sha256" "$PACKAGE_URL.sha256"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
set -e
PROJECT_ROOT="$HOME/projects/my-new-project"
INSTALL_ROOT="$HOME/Applications/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
PACKAGE_NAME="ai-sdlc-offline-3.2.0-macos-arm64.tar.gz"
mkdir -p "$PROJECT_ROOT" "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
test -z "$(ls -A "$PROJECT_ROOT")" || { echo "Project directory must be empty"; exit 1; }
(cd "$DOWNLOAD_ROOT" && shasum -a 256 -c "$PACKAGE_NAME.sha256")
```

只有 `$PACKAGE_NAME: OK` 才能继续。

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
tar xzf "$DOWNLOAD_ROOT/$PACKAGE_NAME" -C "$INSTALL_ROOT"
BUNDLE_ROOT="$INSTALL_ROOT/ai-sdlc-offline-3.2.0-macos-arm64"
(cd "$BUNDLE_ROOT" && ./install_offline.sh --add-to-path)
MODULE_PYTHON="$BUNDLE_ROOT/.venv/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化

```bash
cd "$PROJECT_ROOT"
# 新终端等价入口：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
```

应看到 `Offline installation completed`、`3.2.0`、`Initialized AI-SDLC project`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点，项目目录仍只包含初始化工件。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

`shasum -a 256` 不一致时停止，视同 `SHA256 verification failed`，重新复制归档和 sidecar。权限错误时确认安装目录属于当前用户，再重跑 `install_offline.sh --add-to-path`。命令不可用或 `No module named ai_sdlc` 时使用 module 路径或重装。`open gates`、代理和 Shell 问题分别按 CLI 指引、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

<a id="route-new-offline-linux-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|offline|linux-amd64 -->
## 路线 6：全新空项目 · 离线安装 · Linux AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `linux-amd64`。联网机器下载归档与 sidecar，目标机器可离线；安装目录必须由当前用户写入。

该离线包只兼容 amd64/x86_64 + glibc。先在目标机执行以下检查；若架构或 libc 不兼容，必须停止，不得下载、解压或安装该包。

```bash
set -e
detect_linux_libc() {
  local value="" first_line="" loader="" glibc_seen=0 musl_seen=0
  local getconf_glibc_re='^glibc [0-9]+\.[0-9]+$'
  local getconf_musl_re='^musl [0-9]+\.[0-9]+$'
  local loader_glibc_re='^ld\.so \((GNU libc|[^)]+ GLIBC [0-9][^)]*)\) stable release version [0-9]+\.[0-9]+\.?$'
  local ldd_musl_re='^musl libc \((x86_64|amd64)\)$'
  if command -v getconf >/dev/null 2>&1; then
    value="$(LC_ALL=C getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $getconf_glibc_re ]]; then glibc_seen=1
    elif [[ "$first_line" =~ $getconf_musl_re ]]; then musl_seen=1; fi
  fi
  for loader in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    [ -x "$loader" ] || continue
    value="$(LC_ALL=C "$loader" --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $loader_glibc_re ]]; then glibc_seen=1; fi
  done
  if command -v ldd >/dev/null 2>&1; then
    value="$(LC_ALL=C ldd --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $ldd_musl_re ]]; then musl_seen=1; fi
  fi
  if [ "$musl_seen" -eq 1 ]; then printf 'musl\n'; return 0; fi
  if [ "$glibc_seen" -eq 1 ]; then printf 'glibc\n'; return 0; fi
  printf 'unknown\n'
}
ARCH="$(uname -m)"
LIBC="$(detect_linux_libc)"
if { [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; } || [ "$LIBC" = "musl" ]; then
  echo "停止：v3.2.0 没有与此主机兼容的 Linux 发行资产；不得使用 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
if [ "$LIBC" != "glibc" ]; then
  echo "停止：无法确定此主机使用的 libc；为避免误装，未下载、解压或安装 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
PROJECT_ROOT="$HOME/projects/my-new-project"
INSTALL_ROOT="$HOME/.local/share/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$PROJECT_ROOT" "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
test -z "$(ls -A "$PROJECT_ROOT")" || { echo "Project directory must be empty"; exit 1; }
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

归档内包含 `install_offline.sh`。联网机器下载两项后原样复制到目标机。

```bash
set -e
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$DOWNLOAD_ROOT"
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else echo "Root or sudo is required to install download prerequisites." >&2; return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl
  else echo "No supported prerequisite package manager (apt-get/dnf/yum) was found." >&2; exit 1; fi
fi
command -v curl >/dev/null 2>&1 || { echo "Prerequisite installation did not produce curl." >&2; exit 1; }
ca_bundle_available || { echo "Prerequisite installation did not produce a readable CA bundle." >&2; exit 1; }
PACKAGE_NAME="ai-sdlc-offline-3.2.0-linux-amd64.tar.gz"
PACKAGE_URL="https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/$PACKAGE_NAME"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME" "$PACKAGE_URL"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME.sha256" "$PACKAGE_URL.sha256"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
set -e
PROJECT_ROOT="$HOME/projects/my-new-project"
INSTALL_ROOT="$HOME/.local/share/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
PACKAGE_NAME="ai-sdlc-offline-3.2.0-linux-amd64.tar.gz"
mkdir -p "$PROJECT_ROOT" "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
test -z "$(ls -A "$PROJECT_ROOT")" || { echo "Project directory must be empty"; exit 1; }
(cd "$DOWNLOAD_ROOT" && sha256sum -c "$PACKAGE_NAME.sha256")
```

只有 `$PACKAGE_NAME: OK` 才能继续。

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
tar xzf "$DOWNLOAD_ROOT/$PACKAGE_NAME" -C "$INSTALL_ROOT"
BUNDLE_ROOT="$INSTALL_ROOT/ai-sdlc-offline-3.2.0-linux-amd64"
(cd "$BUNDLE_ROOT" && ./install_offline.sh --add-to-path)
MODULE_PYTHON="$BUNDLE_ROOT/.venv/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化

```bash
cd "$PROJECT_ROOT"
# 新终端等价入口：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
```

应看到 `Offline installation completed`、`3.2.0`、`Initialized AI-SDLC project`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点；空目录未写入示例业务文件。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

在目标机重试前，先重新执行兼容性检查；若出现停止消息，不得继续下载、解压或安装：

```bash
detect_linux_libc() {
  local value="" first_line="" loader="" glibc_seen=0 musl_seen=0
  local getconf_glibc_re='^glibc [0-9]+\.[0-9]+$'
  local getconf_musl_re='^musl [0-9]+\.[0-9]+$'
  local loader_glibc_re='^ld\.so \((GNU libc|[^)]+ GLIBC [0-9][^)]*)\) stable release version [0-9]+\.[0-9]+\.?$'
  local ldd_musl_re='^musl libc \((x86_64|amd64)\)$'
  if command -v getconf >/dev/null 2>&1; then
    value="$(LC_ALL=C getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $getconf_glibc_re ]]; then glibc_seen=1
    elif [[ "$first_line" =~ $getconf_musl_re ]]; then musl_seen=1; fi
  fi
  for loader in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    [ -x "$loader" ] || continue
    value="$(LC_ALL=C "$loader" --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $loader_glibc_re ]]; then glibc_seen=1; fi
  done
  if command -v ldd >/dev/null 2>&1; then
    value="$(LC_ALL=C ldd --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $ldd_musl_re ]]; then musl_seen=1; fi
  fi
  if [ "$musl_seen" -eq 1 ]; then printf 'musl\n'; return 0; fi
  if [ "$glibc_seen" -eq 1 ]; then printf 'glibc\n'; return 0; fi
  printf 'unknown\n'
}
ARCH="$(uname -m)"
LIBC="$(detect_linux_libc)"
if { [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; } || [ "$LIBC" = "musl" ]; then
  echo "停止：v3.2.0 没有与此主机兼容的 Linux 发行资产；不得使用 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
if [ "$LIBC" != "glibc" ]; then
  echo "停止：无法确定此主机使用的 libc；为避免误装，未下载、解压或安装 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
```

联网获取机缺少 curl 或 CA 证书时执行：

```bash
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl
  else echo "No supported prerequisite package manager (apt-get/dnf/yum)" >&2; exit 1; fi
fi
command -v curl >/dev/null 2>&1 && ca_bundle_available
```

`sha256sum` 失败时立即停止，重新复制归档与 `.sha256`。权限错误先修正 `$INSTALL_ROOT` 的当前用户写权限，再重跑 `install_offline.sh --add-to-path`。命令不可用时使用 module 路径；`No module named ai_sdlc` 时重装。`open gates`、代理和 Shell 问题分别按 CLI 指引、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

## 第二章：全新用户 + 已有项目

<a id="route-existing-online-windows-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: existing|online|windows-amd64 -->
## 路线 7：已有项目 · 在线安装 · Windows AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `windows-amd64`。在已有项目根目录打开 PowerShell，先提交或备份当前工作；AI-SDLC 接入不得静默修改业务文件。

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = (Get-Location).Path
$InstallRoot = Join-Path $HOME "AI-SDLC\online-v3.2.0"
$VenvRoot = Join-Path $InstallRoot ".venv"
$DownloadRoot = Join-Path $env:TEMP "ai-sdlc-v3.2.0-online"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DownloadRoot | Out-Null
$GitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $GitCommand) { throw "Git is required. Run: winget install --id Git.Git -e, then reopen PowerShell." }
git --version
git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -eq 0) { git status --short --untracked-files=all }
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

```powershell
$InstallerName = "install_online.ps1"
$InstallerUrl = "https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.0/packaging/install_online.ps1"
$InstallerPath = Join-Path $DownloadRoot $InstallerName
Invoke-WebRequest -Uri $InstallerUrl -OutFile $InstallerPath
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```powershell
$PinnedTag = "v3.2.0"
if (-not (Select-String -LiteralPath $InstallerPath -SimpleMatch $PinnedTag -Quiet)) { throw "Installer is not pinned to v3.2.0" }
Write-Host "After install verify with: python -m ai_sdlc --version"
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```powershell
# 固定标签安装器：install_online.ps1 -AddToPath
powershell -NoProfile -ExecutionPolicy Bypass -File $InstallerPath -VenvPath $VenvRoot -AddToPath
$ModulePython = Join-Path $VenvRoot "Scripts\python.exe"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化并接入

```powershell
Set-Location $ProjectRoot
# 新终端等价第一步：ai-sdlc init .
& $ModulePython -m ai_sdlc init .
& $ModulePython -m ai_sdlc adopt .
```

在 `init` 中选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd；随后 `adopt` 只扫描并生成桥接结果。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```powershell
& $ModulePython -m ai_sdlc --version
& $ModulePython -m ai_sdlc status
git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -eq 0) { git status --short --untracked-files=all }
```

应看到 `3.2.0`、`Initialized AI-SDLC project`、`接入已有项目：已生成桥接结果`、`原任务文件不会被修改`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点；Git 差异只应包含用户确认的 AI-SDLC 工件。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

下载或安装错误时停止，不改用开发分支。PowerShell 受限时使用单次 Bypass。Windows 运行时目录内的 direct `Scripts\ai-sdlc.exe` 活动时不能安全原地替换；显式 direct self-update 零安装并返回非零，请改用 `python -m ai_sdlc`（本路线即 `& $ModulePython -m ai_sdlc self-update install --version 3.2.0`）或新终端中的 stable `ai-sdlc`。裸命令不可用时运行 `& $ModulePython -m ai_sdlc status`；`No module named ai_sdlc` 时重跑 `install_online.ps1 -AddToPath`。若 `git status --short --untracked-files=all` 出现未预期业务文件，停止并人工检查。`open gates`、代理和 Shell 问题分别按 CLI 指示、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

<a id="route-existing-online-macos-arm64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: existing|online|macos-arm64 -->
## 路线 8：已有项目 · 在线安装 · macOS Apple Silicon

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `macos-arm64`。在已有项目根目录打开 Terminal并保存当前工作。

```bash
set -e
PROJECT_ROOT="$PWD"
INSTALL_ROOT="$HOME/Applications/AI-SDLC/online-v3.2.0"
VENV_ROOT="$INSTALL_ROOT/.venv"
DOWNLOAD_ROOT="$(mktemp -d)"
mkdir -p "$INSTALL_ROOT"
command -v git >/dev/null 2>&1 || { echo "Git is required. Run: xcode-select --install, then reopen Terminal." >&2; exit 1; }
git --version
if ! command -v brew >/dev/null 2>&1; then
  if test ! -x /opt/homebrew/bin/brew; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  fi
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
brew --version
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

```bash
INSTALLER_NAME="install_online.sh"
INSTALLER_URL="https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.0/packaging/install_online.sh"
INSTALLER_PATH="$DOWNLOAD_ROOT/$INSTALLER_NAME"
curl --fail --location --retry 3 --output "$INSTALLER_PATH" "$INSTALLER_URL"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
PINNED_TAG="v3.2.0"
grep -F "$PINNED_TAG" "$INSTALLER_PATH" >/dev/null || { echo "Installer is not pinned to v3.2.0"; exit 1; }
echo 'After install verify with: python -m ai_sdlc --version'
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
# 固定标签安装器：install_online.sh --add-to-path
bash "$INSTALLER_PATH" "$VENV_ROOT" --add-to-path
MODULE_PYTHON="$VENV_ROOT/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化并接入

```bash
cd "$PROJECT_ROOT"
# 新终端等价第一步：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
"$MODULE_PYTHON" -m ai_sdlc adopt .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

输出应包含 `3.2.0`、`Initialized AI-SDLC project`、`接入已有项目：已生成桥接结果`、`原任务文件不会被修改`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

网络错误时停止并重试固定标签 URL。若缺少 Homebrew，运行 `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`，再运行 `eval "$(/opt/homebrew/bin/brew shellenv)"` 后重试安装。裸命令不可用时运行 `"$MODULE_PYTHON" -m ai_sdlc status`；`No module named ai_sdlc` 时重跑 `install_online.sh --add-to-path`。若 Git 显示未预期业务差异，停止并检查。`open gates`、代理和 Shell 问题分别按 CLI 指示、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 恢复。

<a id="route-existing-online-linux-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: existing|online|linux-amd64 -->
## 路线 9：已有项目 · 在线安装 · Linux AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `linux-amd64`。已存在 Python 3.11+ 时，保持发行版无关的在线兼容路径；缺少 Python 3.11+ 时，自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) 的 amd64/x86_64 + glibc 主机。其他无 Python 的 amd64/x86_64 + glibc 主机应使用路线 6/12 的 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。非 AMD64 或非 glibc 的 Linux 主机，v3.2.0 没有兼容的 Linux 发行资产；不得使用路线 6/12 的 AMD64 离线包。在已有项目根目录使用 bash 并保存当前工作，确认当前用户可写安装目录。

```bash
set -e
PROJECT_ROOT="$PWD"
INSTALL_ROOT="$HOME/.local/share/AI-SDLC/online-v3.2.0"
VENV_ROOT="$INSTALL_ROOT/.venv"
DOWNLOAD_ROOT="$(mktemp -d)"
mkdir -p "$INSTALL_ROOT"
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else echo "Root or sudo is required to install online prerequisites." >&2; return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl git
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl git
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl git
  else echo "No supported prerequisite package manager (apt-get/dnf/yum) was found." >&2; exit 1; fi
fi
command -v git >/dev/null 2>&1 || { echo "Git installation did not produce an executable git command." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "Prerequisite installation did not produce an executable curl command." >&2; exit 1; }
ca_bundle_available || { echo "Prerequisite installation did not produce a readable CA certificate bundle." >&2; exit 1; }
git --version
curl --version
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

```bash
INSTALLER_NAME="install_online.sh"
INSTALLER_URL="https://raw.githubusercontent.com/panguosong/AI-SDLC/v3.2.0/packaging/install_online.sh"
INSTALLER_PATH="$DOWNLOAD_ROOT/$INSTALLER_NAME"
curl --fail --location --retry 3 --output "$INSTALLER_PATH" "$INSTALLER_URL"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
PINNED_TAG="v3.2.0"
grep -F "$PINNED_TAG" "$INSTALLER_PATH" >/dev/null || { echo "Installer is not pinned to v3.2.0"; exit 1; }
echo 'After install verify with: python -m ai_sdlc --version'
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
# 固定标签安装器：install_online.sh --add-to-path
bash "$INSTALLER_PATH" "$VENV_ROOT" --add-to-path
MODULE_PYTHON="$VENV_ROOT/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化并接入

```bash
cd "$PROJECT_ROOT"
# 新终端等价第一步：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
"$MODULE_PYTHON" -m ai_sdlc adopt .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

应看到 `3.2.0`、`Initialized AI-SDLC project`、`接入已有项目：已生成桥接结果`、`原任务文件不会被修改`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

已存在 Python 3.11+ 时，保持发行版无关的在线兼容路径；缺少 Python 3.11+ 时，自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) 的 amd64/x86_64 + glibc 主机。其他无 Python 的 amd64/x86_64 + glibc 主机应使用路线 6/12 的 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。非 AMD64 或非 glibc 的 Linux 主机，v3.2.0 没有兼容的 Linux 发行资产；不得使用路线 6/12 的 AMD64 离线包。

Git、curl 或 CA 证书不可用时执行：

```bash
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl git
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl git
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl git
  else echo "No supported prerequisite package manager (apt-get/dnf/yum)" >&2; exit 1; fi
fi
command -v git >/dev/null 2>&1 && command -v curl >/dev/null 2>&1 && ca_bundle_available
git --version && curl --version
```

网络或权限错误时停止，修正权限后重跑固定标签的 `install_online.sh --add-to-path`。裸命令不可用时使用 module 路径；`No module named ai_sdlc` 时重装。若业务文件出现非预期差异，停止并检查。`open gates`、代理和 Shell 问题分别按 CLI 指示、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

<a id="route-existing-offline-windows-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: existing|offline|windows-amd64 -->
## 路线 10：已有项目 · 离线安装 · Windows AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `windows-amd64`。在已有项目根目录打开 PowerShell并保存当前工作；联网机器下载，目标机器可离线。

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = (Get-Location).Path
$InstallRoot = Join-Path $HOME "AI-SDLC"
$DownloadRoot = Join-Path $HOME "Downloads\ai-sdlc-v3.2.0"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DownloadRoot | Out-Null
$GitCommand = Get-Command git -ErrorAction SilentlyContinue
if ($GitCommand) {
    git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -eq 0) { git status --short --untracked-files=all }
}
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

包内包含 `install_offline.ps1`。联网机器下载两项后原样复制到目标机。

```powershell
$ErrorActionPreference = "Stop"
$DownloadRoot = Join-Path $HOME "Downloads\ai-sdlc-v3.2.0"
New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
$PackageName = "ai-sdlc-offline-3.2.0-windows-amd64.zip"
$PackageUrl = "https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/$PackageName"
Invoke-WebRequest -Uri $PackageUrl -OutFile (Join-Path $DownloadRoot $PackageName)
Invoke-WebRequest -Uri "$PackageUrl.sha256" -OutFile (Join-Path $DownloadRoot "$PackageName.sha256")
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```powershell
$ErrorActionPreference = "Stop"
$ProjectRoot = (Get-Location).Path
$InstallRoot = Join-Path $HOME "AI-SDLC"
$DownloadRoot = Join-Path $HOME "Downloads\ai-sdlc-v3.2.0"
$PackageName = "ai-sdlc-offline-3.2.0-windows-amd64.zip"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DownloadRoot | Out-Null
$GitCommand = Get-Command git -ErrorAction SilentlyContinue
$PackagePath = Join-Path $DownloadRoot $PackageName
$Parts = (Get-Content -LiteralPath "$PackagePath.sha256" -Raw).Trim() -split '\s+', 2
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $PackagePath).Hash.ToLowerInvariant()
if ($Parts.Count -ne 2 -or $Parts[1] -ne $PackageName -or $Parts[0].ToLowerInvariant() -ne $Actual) { throw "SHA256 verification failed for $PackageName" }
```

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```powershell
Expand-Archive -LiteralPath $PackagePath -DestinationPath $InstallRoot -Force
$BundleRoot = Join-Path $InstallRoot "ai-sdlc-offline-3.2.0-windows-amd64"
Push-Location $BundleRoot
try { powershell -NoProfile -ExecutionPolicy Bypass -File ".\install_offline.ps1" -AddToPath } finally { Pop-Location }
$ModulePython = Join-Path $BundleRoot ".venv\Scripts\python.exe"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化并接入

```powershell
Set-Location $ProjectRoot
# 新终端等价第一步：ai-sdlc init .
& $ModulePython -m ai_sdlc init .
& $ModulePython -m ai_sdlc adopt .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```powershell
& $ModulePython -m ai_sdlc --version
& $ModulePython -m ai_sdlc status
if ($GitCommand) {
    git rev-parse --is-inside-work-tree *> $null
    if ($LASTEXITCODE -eq 0) { git status --short --untracked-files=all }
}
```

必须看到 `Offline installation completed`、`3.2.0`、`Initialized AI-SDLC project`、`接入已有项目：已生成桥接结果`、`原任务文件不会被修改`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

`SHA256 verification failed` 时停止并重新获取包与 sidecar。权限错误使用单次 Bypass；Windows 运行时目录内的 direct `Scripts\ai-sdlc.exe` 活动时不能安全原地替换；显式 direct self-update 零安装并返回非零，请改用 `python -m ai_sdlc`（本路线即 `& $ModulePython -m ai_sdlc self-update install --version 3.2.0`）或新终端中的 stable `ai-sdlc`。命令不可用时使用 `& $ModulePython -m ai_sdlc status`，`No module named ai_sdlc` 时重跑 `install_offline.ps1 -AddToPath`。若 Git 显示非预期业务变化，停止检查。`open gates`、代理和 Shell 问题分别按 CLI 指引、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

<a id="route-existing-offline-macos-arm64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: existing|offline|macos-arm64 -->
## 路线 11：已有项目 · 离线安装 · macOS Apple Silicon

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `macos-arm64`。在已有项目根目录打开 Terminal并保存当前工作；联网机器下载，目标 Mac 离线安装。

```bash
set -e
PROJECT_ROOT="$PWD"
INSTALL_ROOT="$HOME/Applications/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

归档内包含 `install_offline.sh`。联网机器下载两项后原样复制。

```bash
set -e
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$DOWNLOAD_ROOT"
PACKAGE_NAME="ai-sdlc-offline-3.2.0-macos-arm64.tar.gz"
PACKAGE_URL="https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/$PACKAGE_NAME"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME" "$PACKAGE_URL"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME.sha256" "$PACKAGE_URL.sha256"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
set -e
PROJECT_ROOT="$PWD"
INSTALL_ROOT="$HOME/Applications/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
PACKAGE_NAME="ai-sdlc-offline-3.2.0-macos-arm64.tar.gz"
mkdir -p "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
(cd "$DOWNLOAD_ROOT" && shasum -a 256 -c "$PACKAGE_NAME.sha256")
```

只有 `$PACKAGE_NAME: OK` 才能继续。

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
tar xzf "$DOWNLOAD_ROOT/$PACKAGE_NAME" -C "$INSTALL_ROOT"
BUNDLE_ROOT="$INSTALL_ROOT/ai-sdlc-offline-3.2.0-macos-arm64"
(cd "$BUNDLE_ROOT" && ./install_offline.sh --add-to-path)
MODULE_PYTHON="$BUNDLE_ROOT/.venv/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化并接入

```bash
cd "$PROJECT_ROOT"
# 新终端等价第一步：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
"$MODULE_PYTHON" -m ai_sdlc adopt .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

应看到 `Offline installation completed`、`3.2.0`、`Initialized AI-SDLC project`、`接入已有项目：已生成桥接结果`、`原任务文件不会被修改`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

`shasum -a 256` 不通过时停止并重新复制归档和 `.sha256`。权限错误时修复当前用户目录权限，再重跑 `install_offline.sh --add-to-path`。裸命令不可用或 `No module named ai_sdlc` 时使用 module 路径或重装。若业务文件异常变化，停止接入。`open gates`、代理和 Shell 问题分别按 CLI 指引、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 恢复。

<a id="route-existing-offline-linux-amd64"></a>
<!-- AI-SDLC-USER-GUIDE-ROUTE: existing|offline|linux-amd64 -->
## 路线 12：已有项目 · 离线安装 · Linux AMD64

<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
### 1. 准备

适用于 `linux-amd64`。在已有项目根目录使用 bash并保存当前工作；联网机器下载，目标机离线安装。

该离线包只兼容 amd64/x86_64 + glibc。先在目标机执行以下检查；若架构或 libc 不兼容，必须停止，不得下载、解压或安装该包。

```bash
set -e
detect_linux_libc() {
  local value="" first_line="" loader="" glibc_seen=0 musl_seen=0
  local getconf_glibc_re='^glibc [0-9]+\.[0-9]+$'
  local getconf_musl_re='^musl [0-9]+\.[0-9]+$'
  local loader_glibc_re='^ld\.so \((GNU libc|[^)]+ GLIBC [0-9][^)]*)\) stable release version [0-9]+\.[0-9]+\.?$'
  local ldd_musl_re='^musl libc \((x86_64|amd64)\)$'
  if command -v getconf >/dev/null 2>&1; then
    value="$(LC_ALL=C getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $getconf_glibc_re ]]; then glibc_seen=1
    elif [[ "$first_line" =~ $getconf_musl_re ]]; then musl_seen=1; fi
  fi
  for loader in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    [ -x "$loader" ] || continue
    value="$(LC_ALL=C "$loader" --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $loader_glibc_re ]]; then glibc_seen=1; fi
  done
  if command -v ldd >/dev/null 2>&1; then
    value="$(LC_ALL=C ldd --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $ldd_musl_re ]]; then musl_seen=1; fi
  fi
  if [ "$musl_seen" -eq 1 ]; then printf 'musl\n'; return 0; fi
  if [ "$glibc_seen" -eq 1 ]; then printf 'glibc\n'; return 0; fi
  printf 'unknown\n'
}
ARCH="$(uname -m)"
LIBC="$(detect_linux_libc)"
if { [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; } || [ "$LIBC" = "musl" ]; then
  echo "停止：v3.2.0 没有与此主机兼容的 Linux 发行资产；不得使用 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
if [ "$LIBC" != "glibc" ]; then
  echo "停止：无法确定此主机使用的 libc；为避免误装，未下载、解压或安装 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
PROJECT_ROOT="$PWD"
INSTALL_ROOT="$HOME/.local/share/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
### 2. 获取

归档内包含 `install_offline.sh`。联网机器下载两项后原样复制。

```bash
set -e
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
mkdir -p "$DOWNLOAD_ROOT"
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else echo "Root or sudo is required to install download prerequisites." >&2; return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl
  else echo "No supported prerequisite package manager (apt-get/dnf/yum) was found." >&2; exit 1; fi
fi
command -v curl >/dev/null 2>&1 || { echo "Prerequisite installation did not produce curl." >&2; exit 1; }
ca_bundle_available || { echo "Prerequisite installation did not produce a readable CA bundle." >&2; exit 1; }
PACKAGE_NAME="ai-sdlc-offline-3.2.0-linux-amd64.tar.gz"
PACKAGE_URL="https://github.com/panguosong/AI-SDLC/releases/download/v3.2.0/$PACKAGE_NAME"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME" "$PACKAGE_URL"
curl --fail --location --retry 3 --output "$DOWNLOAD_ROOT/$PACKAGE_NAME.sha256" "$PACKAGE_URL.sha256"
```

<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
### 3. 校验

```bash
set -e
PROJECT_ROOT="$PWD"
INSTALL_ROOT="$HOME/.local/share/AI-SDLC/offline-v3.2.0"
DOWNLOAD_ROOT="$HOME/Downloads/ai-sdlc-v3.2.0"
PACKAGE_NAME="ai-sdlc-offline-3.2.0-linux-amd64.tar.gz"
mkdir -p "$INSTALL_ROOT" "$DOWNLOAD_ROOT"
(cd "$DOWNLOAD_ROOT" && sha256sum -c "$PACKAGE_NAME.sha256")
```

只有 `$PACKAGE_NAME: OK` 才能继续。

<!-- AI-SDLC-USER-GUIDE-STEP: install -->
### 4. 安装

```bash
tar xzf "$DOWNLOAD_ROOT/$PACKAGE_NAME" -C "$INSTALL_ROOT"
BUNDLE_ROOT="$INSTALL_ROOT/ai-sdlc-offline-3.2.0-linux-amd64"
(cd "$BUNDLE_ROOT" && ./install_offline.sh --add-to-path)
MODULE_PYTHON="$BUNDLE_ROOT/.venv/bin/python"
```

<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
### 5. 初始化并接入

```bash
cd "$PROJECT_ROOT"
# 新终端等价第一步：ai-sdlc init .
"$MODULE_PYTHON" -m ai_sdlc init .
"$MODULE_PYTHON" -m ai_sdlc adopt .
```

选择 Claude Code、Codex、Cursor、VS Code 或其他-通用，以及 PowerShell、Bash、Zsh 或 Cmd。

<!-- AI-SDLC-USER-GUIDE-STEP: success -->
### 6. 成功证据

```bash
"$MODULE_PYTHON" -m ai_sdlc --version
"$MODULE_PYTHON" -m ai_sdlc status
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git status --short --untracked-files=all; fi
```

应看到 `Offline installation completed`、`3.2.0`、`Initialized AI-SDLC project`、`接入已有项目：已生成桥接结果`、`原任务文件不会被修改`、`当前结果 / Result`、`下一步 / Next` 和推荐继续点。

<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
### 7. 就地恢复

在目标机重试前，先重新执行兼容性检查；若出现停止消息，不得继续下载、解压或安装：

```bash
detect_linux_libc() {
  local value="" first_line="" loader="" glibc_seen=0 musl_seen=0
  local getconf_glibc_re='^glibc [0-9]+\.[0-9]+$'
  local getconf_musl_re='^musl [0-9]+\.[0-9]+$'
  local loader_glibc_re='^ld\.so \((GNU libc|[^)]+ GLIBC [0-9][^)]*)\) stable release version [0-9]+\.[0-9]+\.?$'
  local ldd_musl_re='^musl libc \((x86_64|amd64)\)$'
  if command -v getconf >/dev/null 2>&1; then
    value="$(LC_ALL=C getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $getconf_glibc_re ]]; then glibc_seen=1
    elif [[ "$first_line" =~ $getconf_musl_re ]]; then musl_seen=1; fi
  fi
  for loader in /lib64/ld-linux-x86-64.so.2 /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2; do
    [ -x "$loader" ] || continue
    value="$(LC_ALL=C "$loader" --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $loader_glibc_re ]]; then glibc_seen=1; fi
  done
  if command -v ldd >/dev/null 2>&1; then
    value="$(LC_ALL=C ldd --version 2>&1 || true)"
    first_line="${value%%$'\n'*}"
    if [[ "$first_line" =~ $ldd_musl_re ]]; then musl_seen=1; fi
  fi
  if [ "$musl_seen" -eq 1 ]; then printf 'musl\n'; return 0; fi
  if [ "$glibc_seen" -eq 1 ]; then printf 'glibc\n'; return 0; fi
  printf 'unknown\n'
}
ARCH="$(uname -m)"
LIBC="$(detect_linux_libc)"
if { [ "$ARCH" != "x86_64" ] && [ "$ARCH" != "amd64" ]; } || [ "$LIBC" = "musl" ]; then
  echo "停止：v3.2.0 没有与此主机兼容的 Linux 发行资产；不得使用 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
if [ "$LIBC" != "glibc" ]; then
  echo "停止：无法确定此主机使用的 libc；为避免误装，未下载、解压或安装 ai-sdlc-offline-3.2.0-linux-amd64.tar.gz。" >&2
  exit 1
fi
```

联网获取机缺少 curl 或 CA 证书时执行：

```bash
run_as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; elif command -v sudo >/dev/null 2>&1; then sudo "$@"; else return 1; fi; }
ca_bundle_available() { test -s "${CURL_CA_BUNDLE:-}" || test -s "${SSL_CERT_FILE:-}" || test -s /etc/ssl/certs/ca-certificates.crt || test -s /etc/pki/tls/certs/ca-bundle.crt || test -s /etc/ssl/ca-bundle.pem; }
if ! command -v curl >/dev/null 2>&1 || ! ca_bundle_available; then
  if command -v apt-get >/dev/null 2>&1; then run_as_root apt-get update && run_as_root apt-get install -y ca-certificates curl
  elif command -v dnf >/dev/null 2>&1; then run_as_root dnf install -y ca-certificates curl
  elif command -v yum >/dev/null 2>&1; then run_as_root yum install -y ca-certificates curl
  else echo "No supported prerequisite package manager (apt-get/dnf/yum)" >&2; exit 1; fi
fi
command -v curl >/dev/null 2>&1 && ca_bundle_available
```

`sha256sum` 失败时停止并重新复制归档与 sidecar。目录权限错误时修复当前用户写权限，再重跑 `install_offline.sh --add-to-path`。裸命令不可用时使用 `"$MODULE_PYTHON" -m ai_sdlc status`；`No module named ai_sdlc` 时重装。若业务文件出现非预期变化，停止检查。`open gates`、代理和 Shell 问题分别按 CLI 指引、`ai-sdlc adapter select`、`ai-sdlc adapter shell-select` 处理。

## 异常情况速查

- 校验失败：立即停止，重新获取归档与同名 `.sha256`，不要跳过摘要校验。
- 裸命令不可用：在当前窗口使用对应路线保存的 module Python；重开终端后再检查 PATH。
- `No module named ai_sdlc`：保持原安装目录不动，重跑同一路线的正式安装器。
- `open gates`：按 CLI 的 `当前结果 / Result` 与 `下一步 / Next` 处理，不手工改状态文件。
- 已有项目出现非预期业务差异：停止并检查 Git，不继续接入或执行。

## 安装后的统一入口

每条路线完成后，重开终端并进入项目目录，优先执行：

```text
ai-sdlc --version
ai-sdlc status
ai-sdlc run
```

已有项目若需要在新终端重新执行接入，必须先完成 `ai-sdlc init .`，再运行 `ai-sdlc adopt .`。

只有在 PATH 尚未刷新或 CLI 明确要求排障时，才使用路线保存的 module Python。不要移动或删除安装目录，也不要用开发分支、源码 worktree 或手工依赖安装替代正式路线。

## 用 v3.2.0 推进量化 Loop

安装并完成 `init`（已有项目还需完成 `adopt`）后，在项目目录执行 `ai-sdlc run`，把需求交给你选择的 AI 代理。`run` 只读取当前状态，返回 `Result`、`Next` 和 `Applicable Rules`，不会自行创建 Loop、写代码或提交。宿主 Agent 按 `Next` 新建量化 Loop，并消费返回的规则；没有新建动作时就沿当前实例继续，不重复初始化。

新建阶段的 `Next` 会带上 `--decision-mode adaptive-quantified --decision-capability stage-simulation-v1`。这些是宿主执行的命令参数，不是要求你填写评分表。宿主负责从需求、项目事实和上游结果形成目标、可验收义务、评分锚点与完整时间计划，准备少量不同机制的草案，并调用独立只读上下文判断。你不需要手写候选 JSON、分数、权重或预算；有期限、范围或授权约束时直接在需求中说明。缺少独立上下文或必需工具时，宿主应说明缺口，不能由原作者自签通过。

### 五个 Loop、六个评分视角

| Loop | 评分视角 | 主要关注 |
| --- | --- | --- |
| Requirement | 需求分析 | 已知目标是否进入验收要求，边界、未知和冲突是否交代清楚。 |
| Design Contract | 设计合同 | 义务是否映射到接口、数据和行为，代表场景与失败路径是否闭合。 |
| Implementation | 实施方案、代码成果 | 实施前比较任务与依赖方案；需要改善时，将当前真实代码作为基线，与改善草案按代码视角重新比较。 |
| Frontend Evidence | 前端证据 | 关键操作、页面状态和交互障碍；仍须满足方案确认和当前真实浏览器证据门禁。 |
| Local PR Review | 交付就绪 | 只检查当前暂存树的证据、未解决发现与上游一致性，不选择实现路线或凭模拟分提交。 |

六个视角使用同一套计算规则，但各有对应的评价模板；不同视角的分数不能相加或直接比较。Implementation 的计划与代码视角在开始时一起冻结，共享目标与时间窗口；普通实际评审仍逐条检查真实义务，不是强制再跑一遍所有模拟评分。

### 分数从哪里来

宿主先冻结去重的目标、义务、场景等共同计数集合。独立判断逐项区分 `supported`、`contradicted`、`unknown`，提供来源及理由，再按冻结的 0–4 级锚点给出等级区间。候选自身的组件清单只作数量诊断，不能靠多拆任务或扩展分母刷分。框架检查集合完整性和结构化锚点，复算 0–100 的模拟区间分 `S`；未知保留为区间，不取中点冒充确定结论。

`S` 是预测偏好，不是测试通过率。执行选中路线后，实际专家依据当前工件和真实证据逐条给出 `PASS`、`FAIL` 或 `UNKNOWN`；框架保守合并，计算必达未通过数 `H` 以及通过、未知、失败的义务权重 `Q/U/F`。第二轮还记录同合同的质量变化与回归。文件摘要证明材料对应，不能独自证明行为正确；高模拟分、模型同意或命令退出 0 都不能代替必需的业务证据和原有 Close。

### 有限时间内怎样继续或停止

每阶段最多两批模拟比较，每批至多三个候选，由一个独立模拟判断处理同批草案；第二批也可能用于执行前再比较，用完后不能另开可选改善批次。框架先检查执行前提、必达冲突和完整未来时间上界，再比较质量区间；质量不可区分时比较同口径未来时间，仍重叠则保留当前方案或按稳定规则回退。无法区分不等于已经找到最优解。

时间计划包含实施、评审、验证、已知返工及直接下游交接。候选须满足“首次开始以来的历时 + 完整未来时间上界不超过原窗口”；重启、换候选、纠正格式或第二批比较都不刷新时间。实际成果就绪且 R1 尚未开始时，宿主可用剩余的一批比较有依据的改善；预测有意义的改善胜出后，也必须等实际 R1 达标、原基线与时间准入仍有效才能执行。实际评审沿用 R1/R2：必要修复或条件改善至多一次，R2 仍有缺口就停止，不能增开 R3 或借换 Loop 重置次数。

这不是无人值守保证、估时准确性或 ROI 证明，也不是对所有可能方案求全局最优。框架约束自己的状态、准入与下一步，不会中断协议外的 IDE 操作；不会自动开发多套真实实现、保留历史最高分代码或回滚。旧实例不自动迁移为新能力，已有 B1/D1 与正常 legacy 实例按原合同继续；被标记为已退出支持的旧续办实例只保留历史，不恢复执行。无论时间是否用完，当前真实成果未达必需门槛都不能 Close。

## 用 v3.2.0 验证关键业务规则

在需求中说明必须保持的业务规则和合法行为，例如：“只有 `ready_for_canary` 可以变为 `canary_approved`；`blocked`、`stopped` 和已经批准的状态保持原样；输入文件不能被修改。”代理先固定可检查的规则和验收方法，在原生量化入口比较可行方案，只实现实际选中的方案。

对于需要反例验收的任务，代理使用当前 `stage-simulation-v1` 设计与实现流程，准备绑定本次任务、源码、资源、检查器和预算的反例合同及执行计划。计划分别运行当前实现、隔离错误变体和合法对照，保存实际命令、观察结果与资源清理回执。必要规则被违反时，任务不能以这份失败证据关闭。

实际修改业务实现后，应保留原失败，绑定新源码并使用同一检查器复验。合法行为也要继续通过；隔离副本中的同类回归仍应被保留的防护拒绝。隔离变体不应覆盖正式交付树。普通验收原本就能检出的错误可以保留原检查，不必为了展示改进而削弱它。

代理使用的原生入口如下，参数来自本次已准入的真实任务及执行计划：

```text
ai-sdlc loop implementation verify --loop-id <当前实现Loop> --task-id <当前任务> --counterexample-plan <本次计划.json> --json
```

用户不必手写执行计划或执行回执；代理按命令返回的 `Next` 和运行规则组织输入、独立评审与正常 Close。缺件、源码不符或必要清理未完成时，应按真实阻塞处理，不能把技术执行失败解释为检出了业务错误。

反例验收覆盖所选规则与已运行样本，不保证自动发现未知缺陷，也不证明普遍质量收益。未完成旧计划的跨计划接管及无有效判断的历史评审自动恢复不在本版本支持范围；原始失败和资源清理责任仍须保留。
