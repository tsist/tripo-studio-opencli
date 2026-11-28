---
name: tripo-studio-opencli
description: 通过 OpenCLI 驱动 Tripo Studio 网页端完成余额预检、预算锁账、三维模型生成、后处理、本地导出与官方 Bridge 到件提取。接到复杂有机体、雕塑造型的角色或高面数主体，需要先用 Studio 会员算力出候选再交给 Blender 建模部门时用它。
metadata:
  version: "1.2.0"
---

# Tripo Studio 网页生成与交接

本技能属于[建模部门](https://github.com/tsist/blender-ai-workflow/blob/main/skills/blender-work-methodology/references/departments/modeling.md)，负责通过本机已登录的 Chrome 与 OpenCLI 操作 Tripo Studio 网页端，完成算力核对、模型生成、网页后处理、导出归档和官方 Bridge 到件提取，再把带齐清单与 SHA-256 的候选交给 [blender-modeling-workflow](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/SKILL.md) 导入和审核。需要走独立 API 钱包生成时，改由建模流程里的独立接口路线处理。

## 高模禁止直接导出

- 高细节原模只作为烘焙源。所有高模必须先做 Studio 智能重拓扑，再以具备合格 UV 的低模为目标做高低模投射烘焙。按镜头距离、轮廓和细枝等结构定目标面数，逐件核对实际面数和源、目标变体。导出、本地下载及 Bridge 发送一律选择低模变体。
- 正式交付必须包含实际投射得到的烘焙贴图，至少带切线空间法线（OpenGL +Y、Non-Color）。按用途交付高度、AO 或曲率等数据图。Base Color 等外观贴图可按任务需要省略。Studio 的自动重绘或 PBR 生成无法证明完成了高低模投射。
- 优先以 Studio 可核验的高低模烘焙结果配套导出。若 Studio 无法使用指定的烘焙源，仅在已有合法本地高模时，可将低模带到 Blender 通过 [blender-material-authoring](https://github.com/tsist/blender-material-workflow/blob/main/skills/blender-material-authoring/SKILL.md) 投射，并随低模交付贴图。已有本地源缺失且 Studio 无法烘焙时，记录阻塞，高模继续留在 Studio。
- 放行前冷重开低模与贴图，核对低模 UV、色彩空间、关联路径和实际着色，并在同机位检查轮廓与贴图接缝。记录高低模身份、面数、烘焙通道与产物指纹。

## 怎么做

- **国区多资产**。同配方优先一次批量生成。独立配方、分批到件或需要按原回执恢复时，用短提交和批量回读。贴图、导出按各项目的已验源变体推进。 [国区并发与对账](references/studio-concurrent-production.md)
- **Agentic Canvas**。按地区核对入口和权益，再在私有模板副本上确认节点设置及导出。 [画布操作与接入](references/agentic-canvas.md)

### 第一步：查当前入口，做零消耗预检

动手前先查公共目录，拿到工作区维护版驱动的准确参数和当前宿主路径。再用只读命令核对浏览器连接、会员积分、免费试用次数与任务账本。

```powershell
$ctl = './blenderctl.ps1'
& $ctl discover --product tripo-studio --actions

opencli --profile <your-profile> tripo-studio balance --region cn -f json
opencli --profile <your-profile> tripo-studio quota --region cn -f json
python scripts/tripo_studio_driver.py --profile <your-profile> preflight
```

国内会员默认走 `--region cn`（`https://studio.tripo3d.com`），用户明确点名国际站时再切 `--region global`（`https://studio.tripo3d.ai`）。认证全程复用用户打开的 Chrome 会话，Cookie、登录令牌和签名下载链接留在内存中处理，保持磁盘干净。

### 第二步：先估价再锁预算，获批后才提交

所有收费操作先跑不带 `--execute-spend` 的干跑估算，核清实际积分、免费试用资格和操作次数。把预估明细报给用户拿到本次明确授权后，再用 `tripo_ledger.py init-task` 按批准额度锁死任务账本。

```powershell
# 干跑估算，零扣费
python scripts/tripo_studio_driver.py --profile <your-profile> generate `
  --mode single --images "<参考图路径>" --tier smartmesh --quad --face-limit 10000

# 按用户批准的额度初始化任务账本
python scripts/tripo_ledger.py init-task `
  --task-id <task_id> --budget-cap <积分上限> `
  --max-submissions <操作次数上限> --download-cap <下载次数上限> `
  --region cn --note "<制作目标>"
```

需要动用免费试用时，在 `init-task` 里显式加 `--trial-cap NAME=COUNT`。提交前对比页面实时标价与本地估算，两者完全相等再继续；遇到缺价、标价变化或额度不足，立刻停下把实测结果报给用户。

### 第三步：单次提交生成

按主体需求选好生成模式和档位，带上任务编号、唯一 `step-key` 和 `--execute-spend` 执行单次提交。

- **结构规整、要保四边面的主体**：选 `--tier smartmesh --quad --face-limit <面数>`，默认使用最新 **Tripo Smart Mesh P2.0（`Nexus-v2.0-20260801`）**，支持原生四边面与多面数变体，后续优先导出 FBX 保留四边面拓扑。
- **高细节雕塑或原画主体**：选 `--tier hd`，默认按目标站点自动选取最新实装版本——国际站（`--region global`）默认使用最新 **Tripo H3.5 Preview（`v3.5-20261108`，仅三角面、最高 500 万面）**，国内站（`--region cn`）当前实装最新为 **Tripo H3.1（`v3.1-20260211`）**（待国内站推送 H3.5 后可直接传 `--hd-version h3.5` / `v3.5-20261108` 启用）；按需要配 `--texture-quality`、`--geometry-quality` 和 `--pbr`。
- **输入方式**：单图用 `--mode single --images`，四视图用 `--mode multiview --front --left --right --back`，批量图用 `--mode batch`，纯文字用 `--mode text --prompt`。

```powershell
python scripts/tripo_studio_driver.py --profile <your-profile> generate `
  --mode single --images "<参考图路径>" `
  --tier smartmesh --quad --face-limit 10000 `
  --task-id <task_id> --step-key gen_01 --execute-spend
```

点击提交前脚本会把状态落盘为 `submitting`，拿到准确的项目与操作回执才转为 `submitted`。遇到超时、断网或页面响应不明时停在现场，先读原账本和网页项目历史核实是否已建单，按原回执续查，保持单次提交纪律。

### 第四步：按需做网页后处理

已有项目编号后，用 `postprocess` 跑拆件、补全、重拓扑、智能 UV、重做贴图、提示词重绘、PBR、贴图放大或自动绑定。

```powershell
python scripts/tripo_studio_driver.py --profile <your-profile> postprocess `
  --operation rigging --rig-type biped `
  --project-id <project_id> --task-id <task_id> --step-key rig_01 --execute-spend
```

- **两步预览保存**：`smart-uv` 和 `texture-edit` 先出预览，记录精确候选和原标签页信息；确认满意后再在原页面另起一步保存，保留原标签页直到保存确认完成。3D 画笔绘制留给人工在网页里完成，之后重新观察项目状态。
- **贴图放大与重绘**：先跑 `--prepare-only` 核对当前页面控件与价格；提示词重绘预览按实测标价执行，保存操作留待价格契约核清后启用。
- **动画与绑定**：绑定默认按站点选用最新实装版本（国际站人型默认 **Rigging v3.0 `v3.0-20260909`**，国内站人型当前为 `v1.0-20240301`，四足动物为 `v2.5-20260210`，可用 `--rig-version` 显式指定）。绑定完成后回读 `rigging.type` 和 `is_rigged`；选定动作卡片另起一步并按批准上限记录。细则见 [预览、保存与动画](references/preview-apply-and-animation.md)。

### 第五步：导出到本地或用官方 Bridge 接回

已经有落盘文件时，直接核对原清单与 SHA-256 交接，省去重复导出。需要从云端取件时，按场景二选一：

- **本地归档与批处理交接（推荐）**：用 `export` 按批准的下载额度先发起导出并下载到任务目录，自动计算 SHA-256 并写入 `manifest.json`。静态四边面或带动作的模型优先用 `--model-format fbx --fbx-preset blender --texture-packaging embedded`；高细节带 PBR 贴图的静态模型可选 `--model-format gltf`。
  ```powershell
  python scripts/tripo_studio_driver.py --profile <your-profile> export `
    --task-id <task_id> --step-key exp_01 --project-id <project_id> `
    --model-format fbx --fbx-preset blender --texture-packaging embedded `
    --execute-spend
  ```
  导出或下载中途中断时，先跑 `--resume-export` 检查本机已有下载作业；文件下完后用 `--download-job <job_id>` 核对校验并补齐 `manifest.json`。
- **官方 Studio Bridge 现场发送与提取**。这两个工作区命令需要对应的 Blender 5.3 工作区驱动。公开驱动可通过 `export` 和本地交接脚本验收文件。能在本机工作区调出 `bridge` 与 `bridge-handoff` 时，按文档使用单一接收器和原模型标签页。 [官方 Bridge 交接](references/studio-blender-bridge.md)

### 第六步：本地初步导入并核对

交付给建模部门前，根据文件类型选定本地导入入口，确认文件可读、贴图完整、面数与朝向正常：

- **已落盘 GLB / FBX**：运行 `scripts/blender_handoff.ps1`，传入 `-InputFile`、`-TaskDir`、`-ProjectId`，有下载清单时一并传入 `-DownloadManifest` 与 `-ExpectedDownloadManifestSha256`。
- **静态 FBX 通用交换**：也可通过公共 CLI `blenderctl.ps1 exchange import` 导入生成工作副本。细则见 [本地归档与导入](references/local-handoff.md)。

## 交给下游

把下面几项整理齐全，交给 [blender-modeling-workflow](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/SKILL.md) 开展多候选比选、冷重开体检和部件采用：

- **交付文件与指纹**：落盘的 `.fbx` / `.glb` 或 `bridge-handoff` 生成的 `candidate.blend`，以及对应小写 SHA-256。
- **来源清单**：任务目录下的 `manifest.json` 或 `handoff.json`（含其 SHA-256），写清 `project_id`、选中的 `operator_id`、导出格式、贴图打包方式和下载作业号。
- **几何与材质备注**：标明模型正面朝向（Studio 导出常见为 `+X` 正面、`+Z` 朝上、高度约 1 米）、四边面比例、是否含骨架与动作、是否有待补的 UV 或 PBR 贴图。
- **预算与账本摘要**：说明本次任务已用积分、操作与下载次数，以及账本路径，方便后续返修追溯。

## 例子：从单张原画生成四边面道具并交给建模部门

接到上游前期参考部门给的去背正视图 `work/lantern/front.png`，要求先出低中面四边面候选供建模部门精修。

1. **查入口并预检**：跑 `blenderctl.ps1 discover --product tripo-studio` 核对脚本路径，再执行 `opencli --profile <your-profile> tripo-studio balance --region cn -f json` 和 `preflight`，确认国内站已登录、余额充足。
2. **估价并请用户批预算**：干跑 `generate --mode single --images "work/lantern/front.png" --tier smartmesh --quad --face-limit 10000`，读出预估 100 积分（或 1 次 Smart Mesh 试用）加 1 次导出。报给用户获批后，跑 `tripo_ledger.py init-task --task-id lantern-v01 --budget-cap 100 --max-submissions 2 --download-cap 1 --region cn --note "lantern smartmesh"`。
3. **单次生成**：带上 `--task-id lantern-v01 --step-key gen_01 --execute-spend` 提交生成，等操作完成并记下 `project_id` 和选中的变体 `operator_id`。
4. **导出本地 FBX**：运行 `export --task-id lantern-v01 --step-key exp_01 --project-id <project_id> --model-format fbx --fbx-preset blender --texture-packaging embedded --execute-spend`，在任务目录拿到 `.fbx`、SHA-256 与 `manifest.json`。
5. **本地初验与交接**：跑 `scripts/blender_handoff.ps1` 导入生成初始 `.blend` 报告，确认四边面完整、内嵌贴图可读、正面朝向为 `+X`，把 `.fbx`、`manifest.json` 和两个 SHA-256 交给建模部门进入 `blender-modeling-workflow` 比选与体检，白模整修完后再请用户做审看①。

## 参考

- [站点区域、UI 锚点与九个后处理模块](references/studio-ui-and-api-map.md)
- [算力计价与预算门禁](references/pricing-and-budget-gates.md)
- [预览、保存应用与动画导出](references/preview-apply-and-animation.md)
- [官方 Studio Bridge 发送与到件提取](references/studio-blender-bridge.md)
- [本地归档与 Blender 导入交接](references/local-handoff.md)
- [故障定位、中断恢复与人工边界](references/troubleshooting-and-recovery.md)
- [国区多资产并发与对账](references/studio-concurrent-production.md)
- [Agentic Canvas 操作与接入](references/agentic-canvas.md)
- [公共路由、脚本清册与验证证据索引](references/sources.md)
