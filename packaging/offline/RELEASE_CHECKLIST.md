# AI-SDLC 3.2.0 离线发布检查清单

本次目标版本为 `3.2.0` / `v3.2.0`。候选源码版本不代表已经公开发布；只有本清单的发布后验证完成，才能宣布发布完成。

## 版本与源码

- [ ] `pyproject.toml` 为 `3.2.0`；
- [ ] 两个 `ai_sdlc/__init__.py` 回退版本均为 `3.2.0`；
- [ ] `uv.lock` 中本项目版本为 `3.2.0`；
- [ ] Git 地址为 `https://github.com/panguosong/AI-SDLC`；
- [ ] 工作树只包含本次授权变更。

## 质量门禁

- [ ] 若候选版本高于 `3.0.0`，用户手册 12 路径合同检查通过；
- [ ] `uv run python scripts/validate_user_guide_standard.py .` 通过；
- [ ] `uv run pytest -q` 通过；
- [ ] `uv run ruff check src tests scripts` 通过；
- [ ] `uv run ai-sdlc verify constraints` 通过；
- [ ] `uv run python scripts/validate_public_release_identity.py .` 通过；
- [ ] `uv build` 通过。
- [ ] 当前 wheel 与从当前 sdist 构建的 wheel 均在独立非 editable 安装环境中通过既有五 Loop 与普通 Close 的协议测试，绑定候选、包身份、实际导入位置和真实回执；源码环境或历史包的通过不能替代。该项不代替下述公开资产安装及真实业务验收。

## 制品构建

- [ ] Windows AMD64 zip 已生成；
- [ ] macOS ARM64 tar.gz 已生成；
- [ ] Linux AMD64 tar.gz 已生成；
- [ ] 每个制品包含 AI-SDLC wheel 与完整依赖 wheelhouse；
- [ ] 每个制品包含安装脚本和 `bundle-manifest.json`；
- [ ] 每个制品包含 `SHA256SUMS`，正式压缩包带同名 `.sha256` 文件；
- [ ] 每个制品包含可执行的 Python 3.11+ 运行时；
- [ ] 制品名称、目录名、manifest 与 wheel 版本一致。

## 完整性验证

- [ ] `verify_offline_bundle.py` 通过；
- [ ] 包内 `SHA256SUMS` 与压缩包 `.sha256` 均校验通过；
- [ ] 无逃逸符号链接；
- [ ] 运行时平台与制品平台一致；
- [ ] 安装日志被验证器接受。

## 平台 smoke

- [ ] Windows 解压与 `install_offline.ps1 -AddToPath` 成功；
- [ ] macOS 解压与 `install_offline.sh --add-to-path` 成功；
- [ ] Linux 解压与 `install_offline.sh --add-to-path` 成功；
- [ ] 三个平台 `ai-sdlc --version` 输出 `3.2.0`；
- [ ] 三个平台 `ai-sdlc --help` 成功；
- [ ] Codex + PowerShell 初始化成功；
- [ ] `ai-sdlc adapter status` 成功；
- [ ] `ai-sdlc run --dry-run` 产生明确 Result 与 Next。

## 发布与复验

- [ ] README、用户指南和打包说明中的包名一致；
- [ ] 精确 main SHA 上存在 annotated tag `v3.2.0` 和全新的空 Draft Release；
- [ ] `release-build` 仅在 `upload_to_release` 为字符串 `"true"` 时，把已通过 smoke 的六个文件上传到该 Draft；
- [ ] Release Build 的三平台安装 smoke 全部通过；下载 Draft 中全部六个资产，验证精确名称、sidecar 文件名和 SHA256；
- [ ] `release-artifact-smoke` 使用只读 token，无法读取 Draft 时不得把可见性失败记作通过，也不增加权限或重复请求；保留上述构建 smoke 和已上传资产校验，重新核对 main、annotated tag、Draft 与六资产身份后发布为非预发布 Latest；
- [ ] 发布事件触发的 Release Artifact Smoke 三平台全部通过，正式下载并安装的 CLI 均报告 `3.2.0`；
- [ ] 发布后分别手动触发现有 `windows-user-guide-e2e.yml` 和 `posix-user-guide-e2e.yml`，输入 `tag=v3.2.0`，要求全部成功；这两份流程没有 release 事件触发器，不等待自动触发；
- [ ] 所有旧 tag 对象、Release ID、旧草稿状态及资产身份保持不变；只允许 Latest 切换到 `v3.2.0`；
- [ ] 平台工作流 artifact 完整；
- [ ] 从全新目录安装正式制品并重复 smoke；
- [ ] 日志、制品和仓库不包含令牌或本地绝对路径。
