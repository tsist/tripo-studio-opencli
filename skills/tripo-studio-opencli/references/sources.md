# 公共路由、脚本清册与验证证据索引

本页集中存放 `tripo-studio-opencli` 的公共 CLI 入口查询方式、目录内脚本清册以及历史验证证据路径。日常操作流程看 [SKILL.md](../SKILL.md) 与对应专题参考页。

## 公共目录实时查询

运行下面命令查看当前项目宿主、工作区维护版入口与各动作实跑状态：

```powershell
& './blenderctl.ps1' discover --product tripo-studio
& './blenderctl.ps1' discover --product tripo-studio --actions --limit 100
```

## 目录内脚本清册（`scripts/`）

- `tripo_studio_driver.py`：统一编排入口，包含 `preflight`、`generate`、`postprocess`、`export`、`bridge`、`bridge-handoff` 六个子命令。
- `tripo_pricing.py`：前端计价模型与免费试用估算器。
- `tripo_ledger.py`：跨进程加锁的预算与幂等账本，管理积分、操作、下载、试用上限与 `halt` 保护。
- `tripo_ui.py`、`tripo_post_ui.py`、`tripo_actions.py`、`tripo_preview.py`：生成页、九个后处理页及预览候选的受控 UI 准备与状态回读。
- `tripo_submit.py`：提交前状态持久化、单次点击与操作回执捕获。
- `tripo_export.py`、`tripo_download.py`、`secure_download_bridge.ps1`：两阶段导出、断点恢复与后台下载校验（签名链接通过标准输入传递）。
- `tripo_bridge.py`、`tripo_bridge_ui.mjs`、`tripo_bridge_command.mjs`、`tripo_bridge_receiver.py`、`tripo_bridge_candidate.py`、`tripo_bridge_handoff.py`：官方 Studio Bridge 计划、原页绑定与单次点击、接收器检查、回执分类和到件提取。
- `blender_handoff.ps1`、`fbx_import_headless.py`：已落盘静态 GLB / FBX 导入当前项目宿主并跑独立冷重开检查。

OpenCLI 网页适配器位于 `~/.opencli\clis\tripo-studio\`（`balance`、`quota`、`assets`、`project`、`progress`、`export-url`），站点记录位于 `~/.opencli\sites\tripo-studio\`。

## 维护源与交叉链接

- 技能唯一维护源：`./.agents/skills/tripo-studio-opencli/`
- 所属部门卡：[建模部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/modeling.md)
- 下游建模接手：[blender-modeling-workflow](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/SKILL.md) · [本地模型交接到 Blender](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/references/blender-handoff.md)
- 公共 CLI 手册：[blender-cli](https://github.com/tsist/blenderctl/blob/main/skills/blender-cli/SKILL.md)

## 验证与审计证据索引

- 动作覆盖范围矩阵：`coverage.md`
- 网页只读适配器核验：`studio-read verification.json`
- 私有单图生成与自有静态 FBX 导出回执：`tripo-generation verification.json`
- 静态 FBX 本地导入与冷重开核验：`local-handoff verification.json`
- 已结算导出丢失清单离线恢复核验：`maintained-candidate verification.json`
- 工作区导出恢复与 `--resume-export` / `--download-job` 核验：`export-resume README.md` · `manifest-handoff README.md` · `partial-resume README.md`
- 静态 GLB 公共交接与冷重开核验：`modeling-studio verification.json` · `public-r2 verification.json`
- 官方 Bridge 计划、接收器检查与到件提取核验：`bridge-plan validation.json` · `bridge-handoff validation.json` · `bridge-receiver verification.json` · `handoff-extract verification.json` · `modeling-adopt verification.json`
- 工具能力验收与历史 Bridge 实单归档：`CAPABILITY-ACCEPTANCE.json` · `auto-bridge REPORT.md` · `EXPORT-PACKAGING-CONTRACT.json` · `semantic-close RESULT.json`
