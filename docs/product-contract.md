# AI-SDLC 3.2.1 产品能力契约

本文为 `v3.2.1` 发布文档，描述本版代码的能力与边界；正式发布、安装资产及平台验收是否完成，以对应 Release 和发布回执为准。

## 产品定位

AI-SDLC 是面向 AI 代理与工程团队的本地研发治理框架。它负责读取项目事实、固化工程规则、组织可恢复流水线、运行质量门禁，并把每次推进转化为可验证的本地证据。

项目地址：<https://github.com/panguosong/AI-SDLC>

## 核心原则

1. 本地项目事实优先于会话记忆。
2. 需求、设计、任务、实现和测试必须可追踪。
3. 门禁失败必须保留为明确状态，不得伪造完成。
4. 高影响动作必须支持预演、确认或恢复。
5. 代码外发默认关闭，凭据不得写入仓库。
6. 自动化结论必须能由命令、工件或测试复核。

## 能力边界

### 项目入口

- 初始化新项目并生成项目配置；
- 只读接入已有项目；
- 扫描语言、依赖、测试、入口和风险；
- 为 Codex 生成 `AGENTS.md` 项目入口；
- 持久化 PowerShell、Bash、Zsh、Cmd 或自动选择；
- 安装版 CLI 在业务命令前读取更新缓存：外部 stable shim 与 `python -m ai_sdlc` 仅在 TTY 用户明确确认后升级并重放原命令；Windows runtime-local direct `Scripts\ai-sdlc.exe` 只给出迁移提示、零安装并继续当前业务，显式 direct self-update 非零退出；Agent、非 TTY 和 JSON 路径只在 stderr 输出稳定单行提示，不污染业务 stdout。

### 流水线与恢复

- 以 checkpoint 表达阶段、分支、开放门禁和执行模式；
- `run` 只读当前五 Loop 的 Result、Next、Blockers 和 `Applicable Rules`，不自行创建 Loop、调用模型、执行任务或提交；
- 具体写入与执行入口按各自合同支持 dry-run、确认执行与事实对齐；
- 支持 Codex handoff，避免跨会话丢失关键上下文；
- 对过期分支、工件缺失和状态漂移给出阻断或修复指引。

### Loop Engineering

- Requirement Loop：目标、范围、验收标准和风险；
- Design Contract Loop：接口、数据、边界和验证策略；
- Implementation Loop：任务、代码、测试和关闭证据；
- Frontend Evidence Loop：页面契约、浏览器证据、视觉与可访问性；
- Local PR Review：提交前由独立本地只读代理执行跨阶段审查。

五个 Loop 的实质结果均由 CLI 按内容选择最多两种专家角色，再由 Codex、Claude Code、Cursor 或 VS Code 中的当前宿主 Agent 按协议为每个角色启动一个全新只读上下文；CLI 自身不调度模型。专家只读取与 `input_digest` 同次获取的内联 `review_snapshot`，不得在复核期间重新打开可变工件路径。宿主 Agent 用 `loop review-record` 汇总当前轮结果；有重要发现或必达缺口时由原实现代理按当前准入修复，并只允许一次复审；通过后调用既有 close。专家执行失败保留真实失败与未关闭状态，不得解释为通过或要求用户手动触发专家。

框架只在原 Loop 目录保存固定的 `review-outcome-round-1.json`，修复后至多再保存 `review-outcome-round-2.json`，用于防止缺失结果、角色不完整和摘要漂移。它不保存专家上下文或长期身份，也不创建 session、ledger、certificate、attestation、authority/store 或第三轮结果。

代码精简只提供非阻断建议。它不改变 Loop 状态，不产生强制修复、receipt、例外或 No-Go，也不阻止 close。

#### 正常新需求：全阶段模拟量化

普通用户先执行 `init`；已有项目还需执行 `adopt` 接入项目事实，再按 Result/Next 进入 AI 对话。宿主读取 `ai-sdlc run` 返回的 Next 和 Applicable Rules，为新需求显式创建 `--decision-mode adaptive-quantified --decision-capability stage-simulation-v1` 实例，并按 Next 完成准备与实际工作。`run` 本身不创建实例；直接调用底层 start/check 而省略量化参数仍使用兼容的 legacy 默认值。用户不必手填 JSON、分数、权重或预算。

本地 `stage-simulation-v1` 为五个 Loop 提供六个评分视角：

| Loop | 评分视角 | 比较对象 |
| --- | --- | --- |
| Requirement | `requirement-analysis-v1` | 需求解释、场景与验收草案 |
| Design Contract | `design-contract-v1` | 设计契约草案 |
| Implementation | `implementation-plan-v1`、`code-result-v1` | 执行前路线；当前代码成果与有界改善草案 |
| Frontend Evidence | `frontend-evidence-v1` | 交互与证据方案草案 |
| Local PR Review | `delivery-readiness-v1` | 唯一的当前暂存树及其风险、反例 |

宿主根据用户目标、项目事实和上游产物实例化合同，在首批前冻结原始计数集合、分项锚点、目标权重和时间计划，再生成候选并封存比较输入。独立只读判断者逐项提交 supported、contradicted 或 unknown 及依据，框架校验覆盖、来源与摘要，确定性复算预测分 `S` 的精确区间并做时间准入。未知不取中点伪装确定值。profile 是语义模板，需宿主实例化和独立判断，不是自动理解需求、代码或业务正确性的六套评分器。

每实例最多两批比较，每批最多三个候选、一个独立模拟判断者；Implementation 的初始续搜与成果改善共用该限额。Implementation 的两个视角同时冻结、共享 GoalContract 和时间窗口，只在同一视角内比较。预计完整后续时间与已用历时共同约束准入，后续时间包含实施、评审、验证、已知返工和直接下游交接；它不是特定模型的实测 token 账单或成本保证。

宿主只落实选中一路，不完整执行多套候选后再选最优。代码成果的可选改善在真实 R1 前比较当前成果与草案，并绑定当前 baseline；封存后不能继续比较。Local PR 不支持改善搜索，只分析 `current-staged-tree`，真实 verify、独立评审、精确树 commit 和 Close 仍走原入口，不能用 Git 范围或预测分代替当前暂存树交付。

#### 实际评价与关闭

模拟 `S` 是预测偏好，不是实际验收。独立专家依据本轮真实材料逐义务给出 PASS、FAIL 或 UNKNOWN，量化结果须绑定当前 input/context/route 和证据 manifest；框架保守合并并计算 `H`（未满足必达数）、`Q/U/F`（满足、未知、失败权重）。作者自报总分、预测 context、旧 PASS、哈希或 exit 0 均不能单独证明当前业务义务完成。

实际 R1 满足必达底线且无重要发现时，通常停止修改；只有已封存且当前基线、剩余时间仍获准的改善提案才可执行一次。真正缺口只在授权、事实与可行验证均具备时允许一次必要修复。必要修复或条件改善至多进入原 R2；R2 复核当前成果后停止或阻断，不开 R3。实际 `ΔQ` 等变化用于诊断，不按历史评分自动选回旧成果，也不自动回滚代码。

原 Requirement freeze、真实命令、当前浏览器/视觉证据、精确 Git 树和 Close 门禁保持。Frontend Evidence 会核对当前浏览器来源与保存快照，旧通过结果不能覆盖当前失败。模拟高分、H/Q 计算或实际评审通过都不能单独替代完整 Close，更不授予生产写入、合并或发布权限。

#### 规则与兼容边界

`ai-sdlc run` 返回当前 Loop 最多两个内置规则片段。规则选择只依赖结构化 Loop 类型与状态，不扫描需求关键词、不联网、不写项目状态，也不建立规则平台。通用规则不得硬编码具体前端框架、组件库、provider 或 style pack；前端实现前必须根据项目事实给出推荐方案、可选方案并等待用户确认。

旧 legacy、Implementation B1 或早期模拟实例不自动升级、降级或迁移，已有失败、预算和终态义务保持；不得另建 Loop 刷新次数。已退役的 legacy continuation 实例保留历史原件并明确拒绝续跑，不能静默转为普通两轮。该退役边界不是普通用户 Close 的替代开关。

`ai-sdlc status` 默认只显示 Result、Next、Blockers；详细人类诊断保留在 `--details`，详细机器合同保留在 `--json`。顶层帮助只展示正常用户入口；已经退役的平行 Program、Telemetry、Provenance、AgentOps、Studio 和 Host Runtime 命令不再注册，显式调用返回未知命令。

### 质量治理

- 项目规则与 Git 分支约束；
- 任务级验收与门禁一致性；
- 前端契约、交付上下文和浏览器探针；
- 本地独立对抗 PR 审查；
- 发布身份、文档、离线包和工作流一致性。

### 运行集成

- 本地 Continuity handoff 与五 Loop 状态恢复；
- 受支持安装入口的命令前升级提示、离线升级与失败恢复；
- Windows、macOS、Linux 离线交付。

## 非目标

- 不替代源代码托管、CI 平台或制品仓库；
- 不在缺少证据时自动宣告项目完成；
- 不绕过组织权限执行合并、发布或生产变更；
- 不默认向远程模型发送代码；
- 不在项目文件中保存密钥或令牌值；
- 不承诺全局最优、跨业务通用质量分、实测商业 ROI 提升、改善后实际质量必然更高或无人值守交付；
- 不提供旧 Shadow/Enforce 激活体系、close certificate、review session/ledger、离线优化平台、资源治理平台或阻断式 Lean governance；这些旧能力已删除，不是隐藏开关或后续默认路线。当前有界模拟准入不是恢复这些平台。

## 验证证据与故障恢复

验收要求与执行渠道分开：框架要求当前候选的真实验证证据和完整独立评审，不要求某一家平台的专项审查或资格。宿主是否具备独立上下文、能否读取所需材料，应在承诺完整交付路径前确认；这项执行纪律不等于源码已经提供自动能力探测。执行失败不代表业务质量不合格，也不产生通过结果。

- 失败的 `loop review --json` 返回原始 `execution_failure`，包含原结果的输入摘要、类型和详情；部分完成的专家结果单独保留。Next 要求先查明原因，同输入技术恢复受原次数约束；策略拒绝不得改名、换角色或换渠道绕过。
- `implementation verify` 保存真实命令结果后，宿主可据证据 `record --status done`，不必让用户重复手填“测试通过”。命令成功与业务任务完成仍分开判断，不能自动把全部任务标为完成。
- Implementation 的 Review/Close 消费已有验证证据，本身不重跑测试；Local PR 的 Review/Commit/Close 同样消费其已有证据。Local PR 的最终 `verify` 绑定真实暂存树，不得拿其他候选的历史结果代替。
- 不提供通用跨阶段结果缓存或替代 Close 的外部审查调度平台。当前回执不含完整环境与外部状态指纹，HEAD/index 变化也会改变源码摘要，不能只凭相同命令文本复用结果。

## 本版发行目标与历史版本

### v3.2.1 发行目标

- 目标源码版本及安装后输出：`3.2.1`；目标正式 tag：`v3.2.1`；
- Git 仓库：`https://github.com/panguosong/AI-SDLC`；
- 安装与校验入口见 `USER_GUIDE.zh-CN.md` 的完整 12 条路线；只有对应正式 Release、资产和摘要已就绪后才能按正式渠道安装；
- 目标离线产物：`ai-sdlc-offline-3.2.1-windows-amd64.zip`、`ai-sdlc-offline-3.2.1-macos-arm64.tar.gz`、`ai-sdlc-offline-3.2.1-linux-amd64.tar.gz`，每个归档同时提供同名 `.sha256`；
- 本版范围为五 Loop / 六视角的有界模拟量化、原生实际评审与关闭接线，以及对应公开文档和安装包；不因源码测试或协议夹具通过就宣称模型效果、全平台安装或发行已完成。

版本号、tag、下载链接、资产名、摘要与安装后版本必须对应同一发行候选。Windows AMD64、macOS Apple Silicon、Linux AMD64 的在线/离线与新建/已有项目路径必须独立验证。Linux 有 Python 3.11+ 的在线路径保持发行版无关；缺少 Python 时，在线自动 bootstrap 只认证 Debian GNU/Linux 12 (bookworm) + amd64/x86_64 + glibc，其他兼容 AMD64/glibc 主机使用本版 Linux 离线包；非 AMD64 或非 glibc 主机没有兼容 Linux 发行资产。

新版本只通过普通 GitHub Release、tag、跨平台安装验收和分支保护发布，不建立 Release Proof、Certificate、attestation、generation burn 或额外 authority/store。

### 历史版本事实

- `v3.1.0` 引入五 Loop / 六视角的有界模拟量化，预测评分与实际验收保持分开；该版本的安装包不能作为 `v3.2.1` 的安装证据；
- `v3.0.1` 是维护补丁版本，仅启用面向全新用户的 12 条安装、初始化与恢复路线并修正文档合同，运行时能力与 `v3.0.0` 保持一致；其版本绑定的安装包和指南不能作为 `v3.2.1` 安装证据；
- `v2.0.0` 相对 `v1.0.2` 删除了旧审查治理入口，历史升级说明见 `docs/v2-migration.zh-CN.md`；
- `v3.0.0` 删除了 `v2.0.0` 中公开但已退出产品边界的旧顶层命令；升级说明见 `docs/v3-migration.zh-CN.md`。

## 验收接口

### Requirement 修复依据补录

对于 `stage-simulation-v1` 的 Requirement，已完成的 R1 仅因准备度缺资料进入 `repair-unavailable`，且尚未产生 R2 或冻结凭据时，宿主可按 Next 使用 `loop review-repair-prepare` 准备补充材料，再由原 R1 各角色的全新独立只读上下文核验修复前提。材料放在项目内 `.ai-sdlc/reviews/`，每次读取必须绑定准备摘要；原 R1、冻结合同、所选路线和原始业务材料须保持未漂移。

三项准备度（已有修复授权、事实依据、验证办法）都获证据支持后，`loop review-repair-record` 只追加一次 `repair-readiness-supplement.json`。专家的只读权限不等于宿主没有修复授权；需求文档修复的验证办法也不要求尚未开发的系统已通过运行验收。准备度存在明确 FAIL、平台拒绝、未知项、角色缺失或证据漂移时不允许补录放行。

补录不修改 R1 的问题、评分、结果、原时间计划或正式轮次，也不表示质量通过。作者随后修复原需求，仍须通过原 R2 才能正常冻结；R2 摘要同时绑定补录材料，篡改或删除会阻止继续消费。补充证据是不可变依赖，须与原 Loop 一同保留。这不是通用历史恢复入口，其他 Loop 和已退役的 Implementation continuation 不受此能力影响。

原生 PR 目录中的框架产物不能作为新增修复授权依据；实际阶段评审也拒绝其中未列入原合同的元数据。用户原始材料不按文件同名误拒。下游 Design 检查和同实例重检会复验补录及其原始依据；删除补录不能将已有依赖伪装成从未补录。量化 Requirement 的原 R1 与已封存 context 是关闭后的必需记录，同时删除这些足迹也会拒绝消费；正常 R1 已通过时不要求额外 R2。没有补录的量化结果同样逐轮重算实际判断、检查 findings 与合法续轮关系，并核对最终轮的完整材料及末次读取，不能仅凭保留的 action 字段认定普通通过。关闭回读和下游消费都保持原 R1、R2 与同次材料绑定。

### Design 首次评审前的文档修正

`stage-simulation-v1` 的首次 Design 确定性检查若返回 `needs_fix` 或 `needs_review`，且尚未开始量化决策或正式评审，作者可以修正文档和来源摘要后重跑同一 `design-contract check`。检查通过但尚未 begin 不等于已封存量化合同；原报告、run 和唯一轮次的状态须一致，blocker 数量须与 findings 相符。仅文档摘要及其反例合同来源摘要可更新，旧新反例合同除来源文件、条目与预算引用的摘要外必须语义一致，不能更换义务、判据、资源权限或来源身份；Loop、工作项、上游 Requirement、授权范围、材料路径和能力选择保持不变。原生开始标记、已有评审或 Close 足迹存在时不适用，删除 context 不恢复准入。

既有发布日志保留检查前后的 input、report 和 run 等原件；旧反例合同保持原始字节，新合同继续执行完整来源校验并按摘要另存。该修正不增加或重置轮次，不修改历史检查失败，也不提供正式封存后的目标或预算变更入口。后续仍须完成量化、独立评审和原生 Close。

以下是诊断与验收命令参考，不是普通用户进入 AI 对话前必须逐项执行的清单；正常推进读取 `ai-sdlc run` 返回的 Next。`adapter status` 和 `run --dry-run` 仅在需要排查时使用，发行身份脚本仅存在于框架源码仓库。

```powershell
ai-sdlc --version
ai-sdlc adapter status
ai-sdlc status
ai-sdlc run --dry-run
ai-sdlc verify constraints
python scripts/validate_public_release_identity.py .
```

上述命令默认验证普通用户项目；仅 AI-SDLC 仓库自身维护使用
`ai-sdlc verify constraints --profile self-development`。

交付关闭还必须通过测试、lint、构建、离线包完整性校验和目标平台 smoke。
