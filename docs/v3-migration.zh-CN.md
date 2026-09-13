# AI-SDLC v3 迁移说明

`v3.0.0` 是相对公开稳定版 `v2.0.0` 的破坏性精简版本。本版本物理删除了已退出产品边界的旧运行时、平行权威与历史前端治理家族；升级不会自动恢复已删除命令。

## 保留的核心能力

1. 提交前由独立本地只读代理对精确候选树执行 Local PR Review。
2. 代码精简分析只给出建议，可因正确性和交付价值灵活突破，不作为硬门禁。
3. Requirement、Design Contract、Implementation、Frontend Evidence、Local PR Review 五类 Loop 结果按内容临时选择最多两名只读专家；有发现时最多修复并复审一次。

## 已删除的 v2 顶层命令

- `program`
- `agentops`
- `enterprise`
- `telemetry`
- `provenance`
- `trace`
- `studio`
- `host-runtime`
- `stage`
- `rules`
- `gate`

上述入口没有兼容别名，显式调用会返回未知命令。

## 替代路径

- 原 `rules` / `stage` 的正常开发路由 `ai-sdlc run` 返回当前五 Loop 路由和 Applicable Rules，具体状态仍由各 `ai-sdlc loop ...` 命令管理。
- 原 `program` 的前端保留动作迁入 `ai-sdlc loop frontend-evidence solution-confirm` / `apply` / `capture` / `baseline`。
- 原 `gate` 的交付判断由普通测试、`ai-sdlc verify constraints`、Loop close 与 Local PR Review 共同承接。
- `agentops`、`enterprise`、`telemetry`、`provenance`、`trace`、`studio` 和 `host-runtime` 没有隐藏替代命令；如仍需对应能力，请使用现有外部 CI、日志、观测或开发平台。

## v3 正常命令路径

- `ai-sdlc run` 是五 Loop 的只读正常入口，直接返回 Result、Next、Blockers 和最多两个 Applicable Rules 片段。
- `ai-sdlc status` 默认只显示 Result、Next、Blockers；详细人类诊断在 `status --details`，机器合同在 `status --json`。
- Windows 上的立即更新和原命令重放支持经验证的外部 stable shim 与 `python -m ai_sdlc`。运行时目录内的 direct `Scripts\ai-sdlc.exe` 无法在正在运行时安全替换：自动检查会输出可执行迁移提示并让当前业务命令继续一次，显式 direct self-update 则不修改安装且非零退出。

## 升级验证

在全新目录安装 `v3.0.0`，然后执行：

```powershell
ai-sdlc --version
ai-sdlc --help
ai-sdlc init .
```

版本输出必须为 `3.0.0`。`ai-sdlc program --help` 等已删命令应返回未知命令。公开安装、离线包与校验命令见 `USER_GUIDE.zh-CN.md`。
