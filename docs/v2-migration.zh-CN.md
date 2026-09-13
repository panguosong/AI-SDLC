# AI-SDLC v2 迁移说明

`v2.0.0` 是相对公开稳定版 `v1.0.2` 的破坏性精简版本。升级不会自动恢复已经删除的旧治理子系统。

## 保留的三项能力

1. 提交前由独立本地只读代理执行 Local PR Review。
2. 代码精简分析只给出建议，可因正确性和交付价值灵活突破，不作为硬门禁。
3. Requirement、Design Contract、Implementation、Frontend Evidence、Local PR Review 五类结果可按内容临时选择最多两名只读专家；有发现时最多修复并复审一次。

## 已删除的旧入口

- `loop implementation lean-check`
- `loop implementation lean-verify`
- `loop implementation lean-regression`
- `loop implementation lean-no-go`
- `pr-review attest`
- Shadow/Enforce 激活、CI certificate、review session/ledger、Release Proof 与持久 authority/store

这些入口没有兼容别名。依赖它们的脚本应删除对应调用，改用普通测试、`ai-sdlc verify constraints`、Local PR Review 与现有 Loop close。

## 升级验证

在全新目录安装 `v2.0.0`，然后执行：

```powershell
ai-sdlc --version
ai-sdlc --help
ai-sdlc init .
```

版本输出必须为 `2.0.0`。公开安装和离线包校验命令见 `USER_GUIDE.zh-CN.md`。
