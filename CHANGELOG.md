# Changelog

## 0.2.0

- 支持最新 **Tripo H3.5 Preview (`v3.5-20261108`)** 模型、实时计价公式（`GenerateBaseH35=30` / 原价 `100` + `detailed` 几何 `+15`）与仅三角面约束，国际站（`--region global`）默认自动使用 `v3.5-20261108`，国内站（`--region cn`）保持实装最新 `v3.1-20260211`（上线后可通过 `--hd-version h3.5` 无缝切换）。
- 支持国际站最新 **Rigging v3.0 (`v3.0-20260909`)** 自动绑定（`--rig-version` 可显式指定），Smart Mesh 保持默认最新 **P2.0 (`Nexus-v2.0-20260801`)**。
- 升级 `StudioUI` 支持中英双语控件标签（含 `H3.5 Preview` 下拉框与 `Generate` 按钮），并通过 DOM 去划线提取及 `min(tokens)` 兼容预览折扣划线价（如 `Generate 60 130`）与试用划线价（如 `生成模型 100 0`）。
- 清理技能文档中残留的本机 profile 示例名，并新增 H3.5/P2.0/Rigging v3.0 计价与 UI 离线回归测试。

## 0.1.1

- 修复GitHub干净Windows环境中，Bridge调用账本因默认charmap无法输出中文JSON的问题。
- 账本和driver的CLI输出显式UTF-8，新增强制ASCII父环境的2项回归。
- 原0.1.0及其CI失败记录保留，预算和云调用保护不变。

## 0.1.0

- 首次公开受限预览：自研OpenCLI适配器、代理技能和本地脚本。
- 将发布者路径/profile改为显式配置，保留预算、unknown与单次提交保护。
- 打包不含官方Bridge、运行时、资产、账号或私有审计。
- 附离线安全回归及已有实测/未验范围说明；没有新增云验证。
