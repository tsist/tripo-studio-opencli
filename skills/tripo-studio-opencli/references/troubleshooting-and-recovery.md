# 故障定位、中断恢复与人工边界

本页整理操作 Tripo Studio 网页端和导出下载时最常见的故障排查顺序，以及中断后的标准恢复命令。核心原则只有一句：出了异常先查原账本和原回执，查明事实再决定下一步。

## 域名、认证与命令参数排查

- **站点与登录**：国内站固定 `--region cn`（`https://studio.tripo3d.com`），国际站按用户明确要求用 `--region global`（`https://studio.tripo3d.ai`）。认证全程留在用户已登录的 Chrome / OpenCLI 会话内，原始 HTTP 被 WAF 拦截时回到浏览器会话执行。
- **参数区分**：在 `opencli tripo-studio export-url` 中，`--model-format` 指定导出模型格式（`fbx`、`gltf` 等），`-f json` 指定终端输出格式。
- **诊断先查状态**：遇到连接异常，先跑只读的 `opencli --profile <p> tripo-studio balance --region cn -f json` 和 `preflight` 检查实际状态，由用户决定是否重启浏览器、OpenCLI 守护进程或 Blender。
- **Windows 调用方式**：在 PowerShell 里复用脚本内置的 Node 与 Python 路径直接传参，避免经由 `cmd /c` 二次拼接转义 JSON。

## 点击后页面没变化

遇到提交或切页后界面无反应时，按下面三步排查：

1. **刷新观察再取引用**：先检查前台窗口、当前 `profile / session / page` 是否匹配、弹窗有没有遮挡、目标按钮是否处于可用状态。状态变化后先重新拉取页面状态树再查控件引用，避免复用旧的引用指纹。
2. **区分标签页生命周期**：新生成和普通后处理走独占前台标签页并固定 `--tab`；预览保存和官方 Bridge 绑定原标签页就地操作，保留未保存状态。
3. **三轮止损**：同一种界面问题连续两轮排查无进展就换取证方式；累计三轮仍未推进，立刻停下把现场截图、日志和原因报给用户拍板。

## 超时与 `unknown` 状态核查

提交后的回执等待和资源就绪等待默认每阶段为 120 秒。超过窗口或响应残缺时，驱动会把当前步骤标为 `unknown` 并触发任务 `halt`，保护预算。按下面四步处理：

1. **先读原账本和原项目**：运行 `python scripts/tripo_ledger.py status --task-id <task_id>` 查看已落盘的 `operator_id`、参数和 `input_attempted` 字段，再用只读 `opencli tripo-studio project <project_id>` 核实云端是否真正建单。
2. **原地恢复导出与下载**：只要原步骤已经产生导出操作或本地下载作业，直接沿用原任务、原 `step-key` 和原导出参数跑恢复命令，零重复建单：
   ```powershell
   # 先查本地已有下载作业；只有需要追查原云端操作时才会联系 Chrome
   python scripts/tripo_studio_driver.py --profile un6amgxk export `
     --task-id <task_id> --step-key <原step_key> --project-id <project_id> `
     --model-format fbx --fbx-preset blender --texture-packaging embedded `
     --resume-export

   # 本地下载完成后，核对原操作、参数、文件魔数与 SHA-256 并写入/恢复 manifest.json
   python scripts/tripo_studio_driver.py --profile un6amgxk export `
     --task-id <task_id> --step-key <原step_key> --project-id <project_id> `
     --model-format fbx --fbx-preset blender --texture-packaging embedded `
     --download-job <download_job_id>
   ```
   已结算但丢失本地 `manifest.json` 的旧导出，同样直接运行带 `--download-job` 的命令即可离线复核并恢复清单。
3. **处理 `.partial` 残片**：下载中途断开留下的 `.partial` 文件会由维护版脚本按原下载作业记录的 `aria2` 或 `curl` 后端继续处理。遇到残片归属不明、控制文件冲突或目标越出工作目录时，脚本会停在现场保留文件等待人工核实。
4. **经人工复核后解锁 `halt`**：查清现场并由用户明确同意解除该条 `halt` 后，再运行：
   ```powershell
   python scripts/tripo_ledger.py clear-halt --task-id <task_id> --human-review --reason "<核查结论与用户批准说明>"
   ```
   若用户期间在网页端手动触发了云端操作，把这笔手动操作单列计入累计对账。

## 留给人工或专项核验的范围

- **3D 画笔绘制**：`texture-edit` 的自动路径面向当前视图的提示词生成模式；`--brush-mode` 会被脚本直接拒收，3D WebGL 笔刷绘制请由人工在网页内完成，保存后重新读取项目最新操作继续下游流程。
- **重绘预览保存**：提示词重绘保存待价格契约核清后再启用自动提交，现阶段通过 `--prepare-only` 检查候选与页面状态。
- **非常用导出格式**：日常交付优先选用静态/动作 FBX 或静态 GLB；OBJ、STL、USDZ、3MF 与 3dsMax 预设按下游目标软件另做原生导入验收。
