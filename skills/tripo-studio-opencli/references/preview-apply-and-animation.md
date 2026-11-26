# 预览、保存应用与动画导出

智能 UV、提示词重绘、绑定和动作重定向都是分阶段操作。本页说明怎么管好原标签页、两步记账，以及怎么安全导出带骨架动作的 FBX。

## 管好原标签页

预览类操作成功后会保留当前模型标签页，并把 `{profile, session, page}` 写入账本步骤。

- **原页续做**：保存预览前，驱动核对同一标签页的网址、项目编号、源 `operator_id` 和选中的候选标识，在原页直接点击保存。
- **保护未保存候选**：只读适配器在每次调用时保持 `--site-session persistent`，避免清理会话触发页面离站提示。网页弹出离站确认框时先取消以保住候选，确认保存完全落盘后再关闭该标签页。
- **找回历史预览**：遇到缺少浏览器上下文的历史预览，先只读核实原页仍在、候选未变且从未提交保存，再显式绑定原页接续。

## 贴图文本与风格参考图

运行 `postprocess --operation texture` 时：

- **文本贴图**：传 `--prompt "<描述>"`，驱动切换到文本输入页签、填入并回读提示词。
- **风格参考图**：传 `--ref-image "<图片路径>"`，驱动将图片送入独立的风格参考槽位（支持 JPG / PNG / WEBP，单张在 20 MiB 以内）。多视图生成页签下隐藏风格控件，风格参考图留给 `texture` 后处理使用。
- **上传开关**：`--prepare-only` 默认做纯只读页面准备；获批上传测试图时显式加 `--prepare-upload`，确保本次页面展示的是刚上传的参考图。

## 智能 UV 与提示词重绘：两步记账

1. **第一步出预览**：运行 `postprocess --operation smart-uv` 或 `postprocess --operation texture-edit --prompt "<描述>"`。成功后返回 `result_kind=preview_ready` 与 `asset_applied=false`，同时把候选标识和源操作号记进这一步。
2. **第二步单独保存**：确认预览效果满意后，在同一个任务里新建一个 `step-key`，通过 `--apply-preview-step <预览步名>` 指向刚才那一步。

```powershell
# 智能 UV 确认保存（需先获得用户对无标价保存预留上限的明确授权）
python scripts/tripo_studio_driver.py --profile <your-profile> postprocess `
  --operation smart-uv --project-id <project_id> `
  --task-id <task_id> --step-key uv_apply_01 `
  --apply-preview-step uv_prev_01 --unpriced-apply-cap <批准上限> --execute-spend

# 提示词重绘保存目前做只读准备检查，待价格契约核清后启用提交
python scripts/tripo_studio_driver.py --profile <your-profile> postprocess `
  --operation texture-edit --project-id <project_id> `
  --task-id <task_id> --step-key tex_apply_01 `
  --apply-preview-step tex_prev_01 --prepare-only
```

执行智能 UV 还要留意两点：

- **贴图影响**：保存新的 UV 会清空当前贴图，先确认项目已备份或准备重做贴图再保存，历史版本仍保留在项目历史里。
- **生成下一个候选**：页面已有 UV 候选时，默认直接报告现有候选。明确需要再出一版时加 `--uv-retry` 另占一次提交，按页面免费重试窗或 10 积分实时核价。

## 绑定、动作重定向与动画 FBX 导出

1. **自动绑定与回读**：执行 `postprocess --operation rigging --rig-type biped`（或 `animal`）。提交完成后回读项目详情，核实 `is_rigged` 为真且 `rigging.type` 匹配预期（服务端 `quadruped` 会归一化为 `animal`）。
2. **选择动作卡片**：绑定就绪后，先用 `--prepare-only` 核对可见动作卡片名称：
   ```powershell
   python scripts/tripo_studio_driver.py --profile <your-profile> postprocess `
     --operation rigging --project-id <project_id> `
     --animation "<精确卡片名称>" --prepare-only
   ```
   已缓存动作直接做本地预览切换；需要云端重定向的新动作另立 `step-key`，带上用户批准的 `--retarget-credit-cap` 单次提交，完成后回读可导出的 `animation_name`。
3. **导出带动作的 FBX**：使用 `--model-format fbx` 配合 `--with-animation` 和 `--animation-names <已完成动作名...>`。
   ```powershell
   python scripts/tripo_studio_driver.py --profile <your-profile> export `
     --task-id <task_id> --step-key exp_anim_01 --project-id <project_id> `
     --model-format fbx --fbx-preset blender --texture-packaging embedded `
     --with-animation --animation-names "<动作名1>" --execute-spend
   ```
   导出适配器会把可读的动作名称精确映射为对应已完成重定向的 `operator_id` 再发给服务端，确保导出的 FBX 带有真实骨骼权重、Action 和多帧姿态曲线。需要导出静态当前姿态时留待专项核验，常规动画导出保持关闭旧烘焙标志。

## 分层验收口径

每一步结束后按实际完成层级如实汇报：页面准备就绪、云端操作完成、预览保存应用、下载文件校验通过、Blender 独立冷重开可读，五项分开记录。文件下载校验覆盖容器头与 SHA-256 完整性，网格拓扑、贴图和动作表现交由本地导入后的 Blender 独立冷重开检查确认。
