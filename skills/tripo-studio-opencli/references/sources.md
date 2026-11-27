# 公共路由、脚本清册与验证证据索引

本页记录 `tripo-studio-opencli` 的公共入口、脚本清册及验证索引。日常操作看 [SKILL.md](../SKILL.md) 与对应专题参考页。

## 公共目录实时查询

运行下面命令查看当前项目宿主、工作区维护版入口与各动作实跑状态：

```powershell
& './blenderctl.ps1' discover --product tripo-studio
& './blenderctl.ps1' discover --product tripo-studio --actions --limit 100
```

## 目录内脚本清册（`scripts/`）

- `tripo_studio_driver.py` 提供 `preflight`、`generate`、`postprocess`、`collect` 和 `export`。以当前版本的 `--help` 为准。
- `tripo_pricing.py`：前端计价模型与免费试用估算器。
- `tripo_ledger.py`：跨进程加锁的预算与幂等账本，管理积分、操作、下载、试用上限与 `halt` 保护。
- `tripo_ui.py`、`tripo_post_ui.py`、`tripo_actions.py`、`tripo_preview.py`：生成页、九个后处理页及预览候选的受控 UI 准备与状态回读。
- `tripo_submit.py`：提交前状态持久化、单次点击与操作回执捕获。
- `tripo_export.py`、`tripo_download.py`、`secure_download_bridge.ps1`：两阶段导出、断点恢复与后台下载校验（签名链接通过标准输入传递）。
- `tripo_bridge.py`、`tripo_bridge_ui.mjs`、`tripo_bridge_command.mjs`、`tripo_bridge_receiver.py`、`tripo_bridge_candidate.py`、`tripo_bridge_handoff.py`：官方 Studio Bridge 计划、原页绑定与单次点击、接收器检查、回执分类和到件提取。
- `blender_handoff.ps1`、`fbx_import_headless.py`：已落盘静态 GLB / FBX 导入当前项目宿主并跑独立冷重开检查。

OpenCLI 网页适配器位于 `~/.opencli\clis\tripo-studio\`（`balance`、`quota`、`assets`、`project`、`progress`、`export-url`），站点记录位于 `~/.opencli\sites\tripo-studio\`。

## 维护源与交叉链接

- 本仓库的技能目录为 `skills/tripo-studio-opencli/`。
- 所属部门卡：[建模部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/modeling.md)
- 下游建模接手：[blender-modeling-workflow](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/SKILL.md) · [本地模型交接到 Blender](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/references/blender-handoff.md)
- 公共 CLI 手册：[blender-cli](https://github.com/tsist/blenderctl/blob/main/skills/blender-cli/SKILL.md)

## 验证

- [版本实测范围](https://github.com/tsist/tripo-studio-opencli/blob/main/docs/VERIFICATION.md)
- [国区并发和原回执操作](studio-concurrent-production.md)
- [从下载清单到 Blender 冷开](local-handoff.md)
