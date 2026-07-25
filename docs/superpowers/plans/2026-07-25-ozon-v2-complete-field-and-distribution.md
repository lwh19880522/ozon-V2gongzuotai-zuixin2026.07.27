# Ozon V2 Complete Field and Distribution Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复字段 Skill 的任务包、证据路由与固定业务字段，并把工作台、一键安装/启动、生图 Skill、字段 Skill、使用与安装文档作为一个可验证版本完整提交和推送。

**Architecture:** 工作台继续负责采集、真实 SKU/主体确认、价格与包装证据、Seller API 草稿和任务包承载；字段 Skill 只处理需要模型判断的字段，但必须收到完整的结构化 Ozon、1688、锁定 SKU、图片和用户确认事实。固定店铺/业务字段由服务端确定性映射，不浪费模型任务；生图 Skill 保持独立的产品后置任务箱。安装器仅配置本地运行环境、快捷方式和浏览器扩展说明，不执行任何真实业务动作。

**Tech Stack:** Python 3.11+, PowerShell, stdlib HTTP server, pytest, Edge Manifest V3, Codex plugin/skills.

---

### Task 1: Freeze the current integration boundary

**Files:**
- Modify: `.gitignore`
- Test: repository status inspection

- [ ] Classify all modified/untracked files as source, documentation, test, or runtime-only.
- [ ] Exclude `.wrangler`, `artifacts`, `work`, runtime databases, logs, and generated media.
- [ ] Preserve all existing user/project changes inside the Ozon V2 scope.

### Task 2: Repair deterministic field mapping and evidence routing

**Files:**
- Modify: `src/ozon_v2/services/attribute_mapping_service.py`
- Modify: `src/ozon_v2/services/workbench_service.py`
- Modify: `skills/ozon-intelligent-field-drafter/SKILL.md`
- Modify: `skills/ozon-intelligent-field-drafter/references/field-policy.md`
- Test: `tests/test_attribute_mapping_service.py`
- Test: `tests/test_ozon_field_drafter_skill.py`
- Test: `tests/test_workbench_local_server.py`

- [ ] Add failing tests for fixed workflow fields and customer-facing mapped-field audit.
- [ ] Add failing tests proving structured Ozon and supplier evidence reach the correct task field.
- [ ] Implement store/workflow defaults before model drafting.
- [ ] Route customer-facing values polluted by supplier fulfillment language through normalization.
- [ ] Keep immutable raw evidence and reject unsupported facts instead of inventing values.
- [ ] Verify targeted tests pass.

### Task 3: Package workbench, plugin, field Skill, and image Skill

**Files:**
- Modify: `.codex-plugin/plugin.json`
- Create: `scripts/install_ozon_v2.ps1`
- Modify: `scripts/create_workbench_shortcut.ps1`
- Modify: `scripts/workbench_control.ps1`
- Modify: `README.md`
- Test: `tests/test_installation_contract.py`
- Test: `tests/test_workbench_control.py`
- Test: `tests/test_ozon_field_drafter_skill.py`
- Test: `tests/test_ozon_image_controller_skill.py`

- [ ] Add failing portability and installer contract tests.
- [ ] Build an idempotent installation script using only repository-relative paths.
- [ ] Validate the one-click launcher and shortcut contract.
- [ ] Make the plugin manifest schema-valid and expose both Skills as one product.
- [ ] Verify installer contract tests and launcher tests pass.

### Task 4: Document complete installation and workflow

**Files:**
- Create: `docs/INSTALLATION.md`
- Create: `docs/USER_GUIDE.md`
- Modify: `README.md`

- [ ] Document prerequisites, clone/install/update, Edge extension load, credentials, first launch, and troubleshooting.
- [ ] Document end-to-end workflow from batch creation through field drafting, product submission, and separate image-task processing.
- [ ] State production boundaries and exactly where user confirmation is required.
- [ ] Document safe update/rollback and log locations.

### Task 5: Verify the complete release

**Files:**
- Test: full repository tests and plugin/Skill validators

- [ ] Run focused regression tests for field mapping, workbench, installer, image tasks, Seller API, and SKU selection.
- [ ] Run the full pytest suite with a sufficient timeout.
- [ ] Validate `.codex-plugin/plugin.json`.
- [ ] Quick-validate every repository Skill.
- [ ] Run installer in dry-run/non-business mode and validate start/status/stop on an isolated port.
- [ ] Confirm no real collection, image generation, upload, publish, or approval occurred.

### Task 6: Commit and push the integrated release

**Files:**
- Modify: all verified Ozon V2 source, tests, Skills, plugin metadata, and docs in this release.

- [ ] Review `git diff`, staged paths, and untracked exclusions.
- [ ] Create one integrated release commit on `main`.
- [ ] Push `main` using the repository-scoped SSH-over-443 route without changing the HTTPS origin.
- [ ] Report the commit, push result, verification counts, and any remaining manual installation step.
