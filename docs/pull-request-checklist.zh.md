# Pull Request 检查清单

## 范围与契约

- [ ] 变更目标、范围和验收标准明确；
- [ ] 需求、设计、任务与实现可以相互追踪；
- [ ] 未修改授权范围外的文件；
- [ ] 用户可见行为与文档一致。

## 量化 Loop 说明与边界

- [ ] 普通用户从 `run` 的 Result / Next 进入新量化实例，宿主承担结构化准备与独立判断，不要求用户手填 JSON、分数或预算；
- [ ] 五 Loop、六视角及原始计数、预测评分、实际 H/Q/U/F 的边界与实现一致；
- [ ] 最多两批候选、每批一次独立模拟判断、R1/R2 和原时间窗口不因技术重试或更新工件重置；
- [ ] 可选改善须通过预测比较和实际 R1 条件，Local PR 只评价当前暂存树；
- [ ] 旧实例不迁移，正常 Close 不以预测分替代实际证据；没有全局最优或无人值守保证。

## 代码与测试

- [ ] 新行为有自动化测试；
- [ ] 修复包含可复现的回归测试；
- [ ] `uv run pytest -q` 通过；
- [ ] `uv run ruff check src tests scripts` 通过；
- [ ] 没有提交密钥、令牌、环境文件或本地绝对路径。

## AI-SDLC 门禁

- [ ] `ai-sdlc run --dry-run` 没有未解释的失败；
- [ ] `uv run ai-sdlc verify constraints --profile self-development` 没有 BLOCKER；
- [ ] checkpoint 与当前分支、任务和证据一致；
- [ ] 前端变更包含浏览器、视觉或可访问性证据；
- [ ] 高影响动作具备确认、回滚或恢复路径。

## 对抗审查

- [ ] 已执行 `ai-sdlc pr-review doctor`；
- [ ] 审查输入来源和范围正确；
- [ ] BLOCKER 与 REQUIRED 发现均已处理；
- [ ] 独立本地 reviewer 已在 close 前完成最终复核；
- [ ] 复审未超过一轮，最终报告只在 clean 后生成；
- [ ] 代码外发策略符合项目要求。

## 发布相关变更

- [ ] 若候选版本高于 `3.0.0`，`USER_GUIDE.zh-CN.md` 已按 `docs/user-guide-release-standard.zh-CN.md` 覆盖 12 条自包含路线；
- [ ] 12 条路线的在线/离线安装、全新/已有项目和三平台命令均有真实环境证据；
- [ ] `uv run python scripts/validate_user_guide_standard.py .` 通过；
- [ ] `README.md`、`USER_GUIDE.zh-CN.md` 与 `packaging/offline/README.md` 描述一致；
- [ ] 源码、发布文档目标、workflow 默认 tag 与制品名称均为 `3.1.0` / `v3.1.0`；正式发布状态另有对应 Release 与发布后验证证据；
- [ ] 发布变更只使用普通 GitHub Release、tag、跨平台 smoke 和分支保护；
- [ ] 包版本、源码版本、锁文件和工作流一致；
- [ ] README、用户指南和打包说明一致；
- [ ] 离线包名称与 manifest 一致；
- [ ] Windows、macOS、Linux smoke 覆盖对应平台；
- [ ] 当前候选 wheel 与 sdist 构建的 wheel 在隔离安装环境通过五 Loop 既有流程与普通 Close 验收，导入来源为已安装包而不是产品源码；
- [ ] `python scripts/validate_public_release_identity.py .` 通过。
