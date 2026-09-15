# AI-SDLC 新用户手册发布标准

状态：强制发布合同

生效范围：`3.0.0` 之后的首个版本及所有后续版本

历史边界：不追溯改写已经发布的 `v3.0.0` 手册

## 目标

正式用户手册必须面向第一次接触 AI-SDLC、不了解 Python 环境和仓库内部结构的普通用户。用户只根据自己的项目状态、安装渠道和操作系统，即可选择一条完整路线并完成下载、安装、初始化或已有项目接入。

手册不得把源码开发入口、仓库 worktree、`uv`、手工创建 venv 或手工安装依赖作为普通用户前置条件。普通入口优先使用已安装的 `ai-sdlc`；裸命令不可用时使用安装器给出的 `python -m ai_sdlc ...`。只有 CLI 明确报错并给出修复命令时，才允许要求用户处理 Python、pip、venv 或依赖。

## 固定路线矩阵

路线矩阵固定为 `2 × 2 × 3 = 12`：

| 项目状态 | 安装渠道 | Windows AMD64 | macOS Apple Silicon | Linux AMD64 |
|---|---|---|---|---|
| 全新空项目 | 在线安装 | `new|online|windows-amd64` | `new|online|macos-arm64` | `new|online|linux-amd64` |
| 全新空项目 | 离线安装 | `new|offline|windows-amd64` | `new|offline|macos-arm64` | `new|offline|linux-amd64` |
| 已有项目 | 在线安装 | `existing|online|windows-amd64` | `existing|online|macos-arm64` | `existing|online|linux-amd64` |
| 已有项目 | 离线安装 | `existing|offline|windows-amd64` | `existing|offline|macos-arm64` | `existing|offline|linux-amd64` |

正式手册开头必须先给出路线选择器，再给出以上 12 条路线。不能只写“两个大场景”后把在线安装省略，也不能把一个系统的命令当作其他系统的替代。

## 每条路线必须自包含

每条路线必须按下列顺序完整出现，用户只阅读这一条路线也能完成操作：

1. `prerequisites`：支持的平台、权限、网络或离线包前提，以及即将创建或修改的目录；
2. `acquire`：从当前正式 Release 获取在线安装器或离线包，链接绑定目标版本；
3. `verify`：在线路径核对安装来源和最终版本，离线路径同时核对 sidecar 文件名和 SHA256；
4. `install`：可复制执行的当前平台安装命令，以及命令成功的明确输出；
5. `initialize`：全新项目执行 `init`；已有项目先执行 `init` 再执行 `adopt`，并完成 AI 代理入口和 shell 选择；
6. `success`：明确展示版本、`当前结果 / Result`、`下一步 / Next`、项目未被意外覆盖等成功证据；
7. `recover`：在本路线内给出摘要不一致、命令不可用、权限、网络、初始化门禁等常见失败的恢复命令和停止条件。

公共章节可以解释概念，但上述七步不能只写“参见其他路线”“同上”或依赖公共排障章节才能执行。命令块必须能从全新终端复制执行，变量定义、工作目录和路径引用不得依赖前文其他路线。

## 平台与渠道合同

- 在线安装必须使用目标正式版本绑定的官方安装器，并在安装后核对 `ai-sdlc --version` 或等价 module 入口；不得默认安装 `main` 或未发布分支。
- 离线安装必须给出归档文件、同名 `.sha256`、平台校验命令、解压命令和离线安装器命令；不得把浏览器显示“下载完成”当作完整性证明。
- Windows 使用 PowerShell 语法；macOS 与 Linux 使用各自真实可用的 shell 命令，不得把一种语法机械复制到另一平台。
- Windows 正常入口优先使用外部 stable shim 或 `python -m ai_sdlc`。运行时目录内 direct `Scripts\\ai-sdlc.exe` 的自动更新限制、迁移提示和显式更新非零行为必须在相关路线中就地说明。
- `-AddToPath` 或 `--add-to-path` 成功后，重开终端的主路径必须是裸 `ai-sdlc`；当前窗口的 module 命令仅作为明确标注的安装后或排障入口。
- Linux 在线路线中，已有 Python 3.11+ 的路径保持发行版无关；缺少 Python 3.11+ 的自动 bootstrap 仅认证 Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc。其他缺少 Python 的 amd64/x86_64 + glibc 主机必须就地指向路线 6/12 的 `ai-sdlc-offline-3.2.0-linux-amd64.tar.gz`；非 AMD64 或非 glibc 主机必须明确 v3.2.0 没有兼容的 Linux 发行资产，且不得使用路线 6/12 的 AMD64 离线包。该边界必须同时出现在每条 Linux 在线路线的 `prerequisites` 和 `recover`，不得用共享文本或“同上”替代。

## 初始化与已有项目保护

- 全新空项目路线必须说明目录应为空或由用户明确选定，并展示 `ai-sdlc init .` 或等价 module 命令。
- 已有项目路线必须先完成 `init`，再说明 `adopt` 的扫描和桥接边界，明确原业务文件不会被静默改写，并给出可核对的结果。
- 每条路线都要展示 AI 代理入口和 shell 的选择步骤；不得把某个代理或 shell 写成唯一可用选项。
- 正常成功输出必须同时解释“当前结果 / Result”和“下一步 / Next”；内部诊断只放在明确的排障步骤中。

## 机器可验证标记

适用本标准的正式 `USER_GUIDE.zh-CN.md` 必须包含一次矩阵标记：

```html
<!-- AI-SDLC-USER-GUIDE-MATRIX: 2x2x3=12 -->
```

每条路线必须包含一次路线标记，并在该路线内部包含七个步骤标记：

```html
<!-- AI-SDLC-USER-GUIDE-ROUTE: new|online|windows-amd64 -->
<!-- AI-SDLC-USER-GUIDE-STEP: prerequisites -->
<!-- AI-SDLC-USER-GUIDE-STEP: acquire -->
<!-- AI-SDLC-USER-GUIDE-STEP: verify -->
<!-- AI-SDLC-USER-GUIDE-STEP: install -->
<!-- AI-SDLC-USER-GUIDE-STEP: initialize -->
<!-- AI-SDLC-USER-GUIDE-STEP: success -->
<!-- AI-SDLC-USER-GUIDE-STEP: recover -->
```

这些注释只用于合同验证，不替代面向用户的标题和正文。

## 安装后的量化使用说明

`v3.2.0` 手册须保留安装后的普通 `run` 入口，说明 Result / Next 与宿主 Agent 的分工：`run` 只读路由，宿主按 Next 新建量化 Loop、准备冻结输入和独立判断，用户不手填 JSON、评分或预算。该说明可放在公共概念章节，不改变任何路线的七个独立步骤。

说明必须覆盖五 Loop、六个评分视角、原始计数与区间评分、预测和实际证据的分离，以及原时间窗口、两批候选与 R1/R2 的上限。代码成果视角仅用于有剩余批次的可选改善比较，Local PR 只评价当前暂存树；预测不替代实际审查或正常 Close，旧实例不迁移。不得把有限候选择路写成全局最优、自动完成或业务 ROI 保证。

## 发布门禁

适用本标准的版本不得发布，除非同时满足：

- 12 个路线标记精确齐全，没有重复或遗漏；
- 每条路线的七个步骤标记齐全且顺序正确；
- 全新项目路线包含实际可执行的 `init`，已有项目路线包含实际可执行的 `adopt`；
- 在线与离线安装分别在 Windows、macOS、Linux 的全新环境中通过；
- 两种项目状态都经过用户指南 E2E，不使用仓库源码、预装依赖或开发者环境假冒全新用户；
- 下载链接、版本、tag、制品名称、checksum 和安装后版本均对应同一个候选版本；
- Windows stable/module/direct 三种入口的公开说明与产品实际行为一致；
- `README.md`、用户手册、在线/离线安装器输出、发布检查清单和 workflow 消费者没有冲突。

任何路线缺失、只能依靠另一章节执行、或只在开发环境中通过，均视为发布阻塞项，不能以文档后补的方式豁免。
