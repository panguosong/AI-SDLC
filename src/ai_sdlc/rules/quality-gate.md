# 质量门禁

<!-- ai-sdlc:normal-path:start -->
只用与当前 Loop 和变更风险直接相关的可执行证据决定是否进入下一步。
失败必须给出具体 blocker 与恢复动作；不要把建议项、历史日志或命令字符串冒充成功证据。
stage-simulation-v1在R1前依Next可比较一次有依据的改善；只执行已纳入R1且仍准入的提案，不因还有批次强制修改或重置窗口。Frontend按frontend-evidence-v1推演操作链和状态，但最终仍需真实浏览器/视觉证据及既有前端方案确认；Local PR评分不授权改树。
反例必达仅支持新stage-simulation-v1任务；宿主在Design首次context前用check --verification-contract绑定已有规范判据、最多3义务×2机制与合法控制，用户不填内部JSON。Implementation按verification规则准备计划；必达UNKNOWN、误拒或真实业务未修复均不能Close，旧legacy/B1/D1不静默启用。
<!-- ai-sdlc:normal-path:end -->

> 本规则在每个 SDLC 阶段结束时激活，判断是否允许进入下一阶段。

## 门禁检查时机

每个阶段执行完毕后、进入下一阶段前，必须运行门禁检查。门禁检查结果只有三种：

- **PASS**：所有条件满足 → 检查当前阶段的执行模式（`pipeline.yml` 的 `stage_overrides` 或全局 `execution_mode`）。如果是 `auto` → 自动进入下一阶段；如果是 `confirm` → 输出确认卡，暂停等待用户指令
- **RETRY**：条件不满足但可自动修复 → 修复后重新检查（最多重试次数见 pipeline.yml）
- **HALT**：条件不满足且无法自动修复 → 阻断流水线，输出阻断面板并通知

## 各阶段门禁条件

### 反例能力与原门禁

支持的源码入口由宿主使用：`ai-sdlc loop design-contract check --verification-contract <项目内合同路径>` 必须在首次 decision context 前绑定合同；后续保持上游捕获字节与原义务引用，不在已冻结输入补字段或移除能力降级。Implementation 使用 `ai-sdlc loop implementation verify --task-id <任务> --counterexample-plan <项目内计划路径>`，不能同时提供尾部命令 argv、显式 `--cwd` 或 `--timeout-seconds`；逐步命令和资源约束来自冻结计划。普通质量命令验证和原 review/Close 仍需完成。

验收要求见 `verification.md` 的宿主职责。反例结果必须与当前候选、合同、计划、尝试和原始观察绑定；删除、替换、过期或缺失的必需证据应阻断，普通质量命令成功不能掩盖必达失败。意见、模拟选择、已应用变更、实际业务观察与 Close 是不同状态，预测分或测试数量不证明效果收益。执行能力、平台证据或授权不足时说明具体缺口，不能静默改为建议后声称必达通过。

目标验收须对应实际观察过的同一资源状态；任何必需操作失败、未解决的历史合法控制误拒或历史资源未清理，都不能因当前其他步骤通过而消失。原预算内的中断清理只解除对应资源义务，不改写旧业务结果，也不增加尝试额度或 V1 强化次数。

### INIT 阶段门禁（Stage 0）

```
PASS 条件（全部满足）:
  ✓ .ai-sdlc/memory/constitution.md 存在且非空
  ✓ .ai-sdlc/profiles/tech-stack.yml 存在且非空
  ✓ .ai-sdlc/profiles/decisions.yml 存在且非空
  ✓ 宪章包含至少 3 条核心原则
  ✓ 技术栈标注了来源（preset / detected / user-selected）

存量项目（Step 0 检查 G=是）且走标准 SDLC 时，额外满足:
  ✓ .ai-sdlc/memory/engineering-corpus.md 存在且非空
  ✓ 对应探索深度（Tier 1/2/3）的必填章节有实质内容（非模板占位符）
  ✓ §10 Open Questions 章节存在（可无条目）

RETRY 触发:
  - constitution.md 生成不完整 → 基于 PRD 重新推导
  - tech-stack.yml 缺少 backend 或 frontend 字段 → 补充推导
  - engineering-corpus.md 必填章节缺失或为模板占位 → 按 brownfield-corpus.md 补充探索后写入

HALT 触发:
  - PRD 内容过于模糊，无法提取项目名称或产品形态
  - 技术栈存在无法自动解决的冲突（如老项目检测到 Vue 2 和 Vue 3 共存）
  - 探索预算耗尽仍无法产出 §1-§3 → 项目过于复杂或权限不足，需人工介入
```

### REFINE 阶段门禁

```
PASS 条件（全部满足）:
  ✓ spec.md 存在
  ✓ 用户故事数量 >= 1
  ✓ 每个用户故事有 >= 1 个验收场景（推荐 `场景 1:` / `Scenario 1:`；`**场景 1**`、`#### 场景 1`、`- 场景 1` 也算合法场景标题）
  ✓ 功能需求列表非空
  ✓ 每条功能需求有可测试的描述
  ✓ 无未解决的 [NEEDS CLARIFICATION] 标记
  ✓ 成功标准非空且可度量

RETRY 触发:
  - 有 [NEEDS CLARIFICATION] 但可通过 decisions.yml 或 AI 判断解决
  - 验收场景不完整但可从 PRD 补充

HALT 触发:
  - PRD 中存在自相矛盾的需求
  - 歧义涉及安全/合规/资金
  - 无法从 PRD 中提取任何用户故事
```

### DESIGN 阶段门禁

```
PASS 条件:
  ✓ plan.md 存在且包含阶段计划
  ✓ research.md 存在且无 "未解决" 标记
  ✓ data-model.md 存在且有表定义
  ✓ contracts/ 目录有 >= 1 个契约文件（如果 spec 有 HTTP 接口）
  ✓ 宪章检查通过（plan 不违反宪章原则）

RETRY 触发:
  - research.md 有少量未冻结决策但可用默认值
  - data-model.md 缺少索引定义但可自动补充

HALT 触发:
  - plan 与宪章冲突（如引入了禁止的复杂度）
  - 技术栈选型与 tech-stack.yml 矛盾
```

### DECOMPOSE 阶段门禁

```
PASS 条件:
  ✓ tasks.md 存在
  ✓ 任务总数 > 0
  ✓ 所有任务有精确文件路径
  ✓ 每个用户故事至少有 1 个任务覆盖
  ✓ 任务 ID 连续且唯一
  ✓ Phase 划分合理（有准备阶段和收尾阶段）

RETRY 触发:
  - 部分任务缺少文件路径 → 自动补充
  - 遗漏了某个用户故事 → 自动补充

HALT 触发:
  - 任务依赖关系成环
  - 生成了超出 spec 范围的任务
```

### VERIFY 阶段门禁

```
PASS 条件:
  ✓ CRITICAL 级别问题 = 0
  ✓ HIGH 级别问题 <= 3（或已被自动修复）

RETRY 触发:
  - 存在 CRITICAL 问题但可通过修改 tasks.md 修复
  - 覆盖缺口可通过追加任务修复

HALT 触发:
  - CRITICAL 问题在 2 次修复后仍存在
  - spec 和 plan 之间存在根本矛盾
```

### EXECUTE 阶段门禁（每批次检查）

验证范围依 `rules/verification.md` 的「批次验证范围」确定：原失败优先，覆盖受影响的既有行为及必要兼容检查；项目/合同必跑项不能省略，影响不明或跨模块时扩大检查。批次通过不能替代最终完整门禁。

```
PASS 条件:
  ✓ 当前批次所有测试通过
  ✓ 按影响面选定的回归与项目/合同必跑项通过，选择依据已记录
  ✓ 构建成功（如适用）
  ✓ 已归档到 task-execution-log.md
  ✓ 已 git commit

RETRY 触发:
  - 测试失败但可定位原因 → 修复后重试

HALT 触发:
  - 连续 2 个任务失败
  - 调试修复超过 3 轮
  - 必需回归失败且原因不明
  - 构建失败且非依赖问题
```

### CLOSE 阶段门禁

```
PASS 条件:
  ✓ development-summary.md 存在且非空
  ✓ 最终全量测试通过
  ✓ 最终构建成功（如适用）
  ✓ 所有 tasks.md 中的任务已标记为 [x]
  ✓ 最终 git commit 完成

RETRY 触发:
  - development-summary.md 生成不完整 → 重新生成
  - 最终测试/构建失败但原因可定位 → 修复后重试

HALT 触发:
  - 最终测试/构建失败且原因不明
  - tasks.md 中有未完成任务
```

## 熔断器

以下条件触发全局熔断（不管当前在哪个阶段）：

- 流水线中 AI 自主决策累计超过 15 次 → 暂停，输出所有决策供人工审查
- 单个阶段重试超过 3 次 → 阻断
- 生成的文件总数超过 tasks.md 中任务数的 3 倍 → 暂停检查是否范围蔓延
- 单个类/文件超过 500 行 → 暂停建议拆分
