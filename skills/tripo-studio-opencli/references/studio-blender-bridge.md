# 官方 Studio Bridge 发送与到件提取

官方 Studio Bridge 把网页里选定的自有静态模型直接发进正在运行的 Blender 接收窗口，再用 `bridge-handoff` 提取成干净的独立工作副本交给建模部门。通过公共目录查当前工作区入口与宿主路径，按下面顺序操作。

## 查入口与做离线计划

先用 `discover` 取出工作区维护版的 `bridge` 启动器，跑一次离线计划核对任务、步骤和项目编号，全程零消耗。

```powershell
$ctl = './blenderctl.ps1'
$catalog = & $ctl discover --product tripo-studio --action bridge.send | ConvertFrom-Json
$item = $catalog.data.actions.items | Where-Object { $_.launchers.Count -gt 0 } | Select-Object -Last 1
$cmd = $item.launchers[0].command_argv
$bridgePython = $cmd[0]
$driverArgs = @($cmd | Select-Object -Skip 1)

& $bridgePython @driverArgs `
  --task-id <task_id> --step-key <step_key> --project-id <project_uuid>
```

需要核对参数详情时，运行 `& $bridgePython @driverArgs --help`。

## 发送前核对接收器与原标签页

正式点击发送前，逐项查清以下五件事：

1. **查预算与额度**：读取原账本，确认用户已批准本次 1 次操作与 1 次下载额度（自有模型虽为 0 积分，仍计操作与下载次数）。
2. **查官方接收器**：只读确认 `127.0.0.1:60600` 的进程编号（PID）、当前打开的工作副本和活动场景。整台机器只保持一个活动的 Bridge 接收器，避免端口争用；Blender Lab MCP 监听在 `9876`，两者端口彼此独立。
3. **备好独占空场景与检查点**：在目标 Blender 进程里存好一份独占的空场景工作副本（`--work-file`），并在磁盘另存一份字节完全相同的检查点备份（`--checkpoint`）。从发送到交接完成期间由当前委托独占该场景。
4. **保留原模型标签页与官方画中画**：复用当前打开的模型页（`--session` 与 `--page-id`），保持官方画中画（PiP）处于打开状态，读出 `Blender 已连接`、`currentDCC=blender`、`status=OPEN` 且无正在进行的传输。若目标窗口被最小化，先用窗口激活能力恢复该窗口再刷新观察。
5. **用导出抽屉定死参数**：通过模型页「导出抽屉 → FBX → Blender 预设 → 8K（或指定分辨率） → 发送至 → Blender」路径发送。每次打开抽屉时重新核实格式、预设与分辨率，等「发送至 Blender」菜单项实际出现并可命中后再触发单次点击。

## 原页单次发送

所有前置检查通过后，带上完整上下文执行一次发送。全局选项 `--profile`、`--runtime-dir`、`--ledger` 放在 `bridge` 子命令前面：

```powershell
& $bridgePython @driverArgs `
  --task-id <task_id> --step-key <step_key> --project-id <project_uuid> `
  --source-operator-id <source_operator_uuid> `
  --session <session_id> --page-id <page_id> `
  --receiver-pid <blender_pid> --mcp-port 9876 `
  --work-file "<空场景工作副本绝对路径>" --scene "<活动场景名>" `
  --checkpoint "<检查点备份绝对路径>" `
  --execute-spend
```

工作区驱动会在准备页面前和点击发送前分别复查指定接收器状态，并在同一把跨进程浏览器锁（`studio-browser.lock`）和账本锁下串行完成准备、点击与回执落盘。

## 回读回执与查看状态

发送阶段保持原标签页与画中画完好，等传输结束。若会话中断或需要复核，用只读命令查原步骤，保持单次提交：

- **查本地账本状态**：加 `--status`，只读原账本里的 `submitting`、`submitted` 或 `unknown` 状态。
- **回读原导出回执**：加 `--read-receipt`，优先读取账本旁 `.bridge-receipts` 目录中已存的捕获文件，按 HTTP `method` 与 `request_id` 合并筛出唯一的 `POST /v2/studio/operation/export` 回执（自动忽略 `OPTIONS` 预检），把证实的导出 UUID 写回原步骤。捕获文件只留白名单字段和业务状态码，跳过 Cookie、认证头、完整签名链接与二进制体。

确认接收器完成且待处理队列归零后，在目标 Blender 中检查收到的对象、UV、材质和内嵌贴图，保存工作副本，准备提取交接。

## 提取独立候选交给建模部门

确认原步骤已有导出回执且接收工程已保存后，从公共目录取 `bridge.handoff` 入口，把本次收到的模型单独提取到新的绝对输出目录：

```powershell
$found = & $ctl discover --product tripo-studio --action bridge.handoff | ConvertFrom-Json
$hCmd = $found.data.actions.items[0].launchers[0].command_argv
$hPython = $hCmd[0]
$hArgs = @($hCmd | Select-Object -Skip 1)

& $hPython @hArgs `
  --task-id <task_id> --step-key <step_key> --project-id <project_uuid> `
  --source-operator-id <source_operator_uuid> `
  --output-dir "<尚不存在的绝对交付目录>"
```

提取完成后，交付目录中会生成：

- `candidate.blend`：只保留本次接收的目标场景、网格、材质与打包贴图，并经过独立进程冷重开校验。
- `handoff.json`：记录原项目编号、选定变体、导出操作号、对象清单与 `candidate.blend` 的 SHA-256。

把交付目录路径、`candidate.blend`、`handoff.json` 及它们的 SHA-256 交给 [blender-modeling-workflow](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/SKILL.md) 执行 `modeling review-candidate`、`modeling adopt-candidate` 与 `model inspect`；后续编辑一律在新工作副本上进行，原始接收工程留作追溯。
