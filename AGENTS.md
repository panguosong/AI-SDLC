# AI-SDLC（Codex / OpenAI Codex CLI 提示）

本工程使用 **AI-SDLC** 自动化流水线。

- 宪章：`.ai-sdlc/memory/constitution.md`
- **终端约定**：优先使用用户已安装好的 `ai-sdlc`；若裸命令不可用，使用 `python -m ai_sdlc ...`。不要要求普通用户手动安装 Python、venv、pip 或依赖，除非 CLI 明确报错并给出修复命令。
- 初始化入口（普通用户先执行）：`ai-sdlc init .` 或 `python -m ai_sdlc init .`
- `init` 会在用户选择 AI 代理入口和 shell 后自动执行必要检查与安全预演；正常输出会给出“当前结果 / Result”和“下一步 / Next”。
- 排查入口（仅当 CLI 明确要求时执行）：`ai-sdlc adapter status`、`ai-sdlc run --dry-run` 或对应 `python -m ai_sdlc ...` 写法。
- 当前五 Loop 路由（只读）：`ai-sdlc run`
- 正常路径直接使用 `ai-sdlc run` 返回的 `Applicable Rules`；不要求用户手动执行 `rules show` 或 `stage show`。

当前 Codex adapter 以 `AGENTS.md` 作为 canonical path。规则安装后，写代码前以当前可执行任务为准；内部诊断详情只在排查命令的 `--details` / `--json` 输出中查看。

当用户在聊天中输入任何需求/任务描述时，如果项目尚未初始化，先引导用户执行 `init`。如果 `init` 已完成且 CLI 输出的下一步是切换到 AI 对话，则不要再要求用户手动执行 `adapter status` 或 `run --dry-run`；直接进入需求细化与分解。`run --dry-run` 仅作为异常排查入口，普通需求路径无需手动运行。

<!-- AI-SDLC managed review guidance start -->
## 五类结果的内置动态专家复核

Requirement、Design Contract、Implementation、Frontend Evidence、Local PR Review 产出实质结果后，由当前 AI 代理自动执行以下边界内复核，不要求用户选择专家、管理复核文件或裁决普通分歧：

新建实例只消费当前 CLI 的 supported_capabilities 与 Next，选择该阶段最新已支持能力；legacy、implementation-b1、implementation-simulation-v1 已建实例保持原身份。stage-simulation-v1 由宿主按 profile 实例化目标/义务、锚点和完整时间计划，用户不填写分数、权重、候选或预算。按 decision-prepare 的 schema 完成 begin、freeze-comparison、独立判断、record-comparison；写操作先 dry-run，再应用返回摘要，不要求真实探针或多套实现。Requirement/Design 判断当前文档，Frontend 保留真实浏览器证据；Implementation 同时冻结计划/代码两个视角，不跨视角比较评分。仅在 R1 前依 Next 作有依据的有限改善，封存后不再搜索；Local PR 用 pr-review decision-prepare 的 delivery-readiness-v1，只评唯一 current-staged-tree 风险/反例，不选产品路线、不执行改善。正式 review 前 seal-for-review；预测 S 永不替代实际 H/Q、PASS 或原 Close。

1. 对当前 Loop 执行 `ai-sdlc loop review --type <类型> --loop-id <ID> --json`，读取 `expert_roles`、`expert_reasons`、`round_number`、`input_digest` 和实质结果。专家检查每个相关工件时，必须执行同一命令并追加 `--expect-digest <input_digest> --read-path <artifact_path>`，只审查返回的 `review_snapshot`；`artifact_paths` 仅用于选择工件，不得重新读取 `artifact_paths` 指向的可变文件。Local PR Review 在此之前先按当前 CLI 返回的 Next，用 `ai-sdlc pr-review verify --cwd . -- <argv...>` 执行并记录真实验证命令；不得把人工字符串作为验证证据。
2. 宿主 Agent 必须严格消费 `expert_roles`，为每个角色启动一个全新且只读的独立上下文；角色数量由 CLI 限定为最多两名。不得要求用户手动触发专家、选择角色或搬运结果。
3. 每个上下文遵守 CLI 返回的 expert_result_schema：旧模式输出该角色的 `ReviewExecution` JSON；量化模式同时提供当前证据绑定的逐义务 assessment，作者不得代填 PASS。执行状态、角色、选择原因及带位置的 findings 均须保留，不得修改代码或工件。宿主随后自动调用 `ai-sdlc loop review-record --type <类型> --loop-id <ID> --expect-digest <input_digest> --result <专家结果.json> --json`，每名角色各传一次 `--result`。
4. 若记录结果为 `needs_fix`，交回原结果编写者修复并重跑正常检查，再次执行 `loop review`；只有输入确实变化时才进入 `round_number=2`。同一结果最多进行一次修复后复审，第二轮后禁止继续生成第三轮。
5. 专家不可用、超时或输出无效时，也必须把该角色的失败执行记录给 `review-record`；不得解释为无问题或调用 close。原 Loop 保持 `needs_review`，只有查明暂态原因或修正具体技术故障后，才按 CLI Next 在未改输入及剩余次数内恢复同一轮。平台策略拒绝不属于普通技术重试；保留真实失败，不得改名、换角色或换渠道绕过，也不得要求用户反复授权。
6. 只有 `review-record` 返回 `passed` 且摘要未漂移时，当前代理才调用既有 close / freeze，并传入 `--loop-id <ID>` 与 `--expect-review-digest <input_digest>`；Local PR Review 还必须传 `--review-id <ID>`。
7. Local PR Review 在 close 前读取 Review Pack、Findings、resolution、验证证据和当前 HEAD/index/staged diff；完成这次跨阶段复核后即停止，禁止继续评审该复核结果或最终报告。

专家上下文和身份不持久化；框架只在原 Loop 目录保留 `review-outcome-round-1.json` 和至多一个 `review-outcome-round-2.json`，不建立长期身份、历史评分或第二套状态流。

## 非阻断代码精简

实现代码应优先保持简单、聚焦和可维护。代码体积、复杂度、重复或拆分分析只提供建议，可以基于正确性和交付价值灵活突破；不得把精简建议升级为 BLOCKER、改变 Loop 状态、阻止 close，或要求任何额外治理工件。
<!-- AI-SDLC managed review guidance end -->


若需求涉及前端需求、UI、页面、组件、浏览器交互或前端工程，进入实现前必须基于项目已有技术栈、约束和交付目标给出一个推荐方案，并同时提供至少一个可选 / 自定义方案，明确区分“规范正文”“可选建议”“已经落地”，并等待用户明确确认。确认前不得进入 execute、不得生成前端实现代码或执行交付应用。通用规则不得硬编码框架、组件库、provider 或 style pack；只有项目事实或用户明确选择才能确定具体方案。

代码实现时，新增注释必须跟随当前或近期用户主要沟通语言；当前对话或近期对话以英文为主则使用英文，否则默认简体中文。保留原有注释；确需删除时，必须在同一变更附近补充等价说明，或在 execution log / handoff 记录删除原因。注释只解释复杂意图、边界、兼容、并发、缓存、错误处理和非显然业务约束，不复述命名已经表达清楚的代码。凡是后续 agent 或人工需要维护的脚本/模块，尤其包含认证、XHR/API 调用、payload 字段映射、加密、阶段流程、重试或副作用边界时，必须补维护契约、关键函数 docstring 或边界注释，并在验证/交付说明中确认这些注释已覆盖。

请在修改 `specs/` 与 `.ai-sdlc/` 下文档时遵守上述入口。

（自动安装；不覆盖已有同名自定义文件。）
