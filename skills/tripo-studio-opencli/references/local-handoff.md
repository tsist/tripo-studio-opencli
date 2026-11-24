# 本地归档与 Blender 导入交接

本页说明 Tripo Studio 导出文件的本地目录布局、通过 `scripts/blender_handoff.ps1` 或公共 CLI 导入当前项目宿主的方法，以及交付给建模部门前的核对清单。

## 本地任务归档布局

运行 `tripo_studio_driver.py export` 后，导出的模型默认保存在 `<PROJECT_ROOT>/runtime\tripo-studio\<task_id>\` 下。脚本在下载晋升阶段自动执行三项核验并写入 `<task_dir>\manifest.json`：

1. 计算流式小写 SHA-256 指纹。
2. 核对文件魔数头（`.glb` 为 `glTF`，二进制 `.fbx` 为 `Kaydara FBX Binary`，压缩包 `.zip` 为 `PK`）。
3. 追加结构化出处，含 `project_id`、导出 `operator_id`、下载作业编号、格式和贴图打包方式。

如果手头已有落盘模型和同次导出清单，直接拿原清单和 SHA-256 做本地交接，省去额外的云端导出。

## 双路径导入脚本（`scripts/blender_handoff.ps1`）

使用工作区交接脚本把落盘的静态 GLB 或 FBX 导入当前项目宿主并生成初步核验报告：

```powershell
pwsh -NoProfile -File "./.agents/skills\tripo-studio-opencli\scripts\blender_handoff.ps1" `
  -InputFile "<已落盘模型路径>" `
  -TaskDir "<PROJECT_ROOT>/runtime\tripo-studio\<task_id>" `
  -ProjectId "<project_uuid>" `
  -Mode auto `
  -FrontAxis +X `
  -DownloadManifest "<task_dir>\manifest.json" `
  -ExpectedDownloadManifestSha256 "<manifest_sha256>"
```

按文件格式自动分流到两条路径：

### 路径 A：静态 GLB（`-Mode glb` 或 `.glb` / `.gltf`）

- 在 `<task_dir>\handoff\` 下生成 `import_local` 参数文件，写入输入文件、SHA-256、Studio 出处和默认三视图预览参数（跳过预览可加 `-SkipPreview`）。
- 调用公共 `blenderctl.ps1 modeling run` 完成导入、贴图打包、保存和独立冷重开检查，再对产出的候选 `.blend` 跑一次 `blenderctl.ps1 inspect`。
- 传入 `-DownloadManifest` 与 `-ExpectedDownloadManifestSha256` 后，脚本会先核验清单与原 GLB 一致，并把项目、导出操作和下载作业一并写进交接报告，方便下游直接跑 `modeling review-candidate` 和 `modeling adopt-candidate`。

### 路径 B：Smart Mesh 四边面或带动作 FBX（`-Mode fbx` 或 `.fbx`）

- 调用独立后台进程运行 `scripts/fbx_import_headless.py`，关闭递归全盘搜图（`use_image_search=False`）以防耗时误找外部目录，同时保留正常相对路径与内嵌贴图加载并如实报告缺图。
- 原样保留四边面拓扑（统计 `overall_quad_ratio`）、骨架层级、顶点权重组、打包贴图（`pack_all()`）与动作帧域，写入 Studio 出处元数据并另存未压缩的 `<stem>_fbx.blend`。
- 随后运行 `blenderctl.ps1 inspect` 做独立冷重开检查。

对于无纹理块的纯静态几何 FBX，也可以直接通过公共 CLI 导入：

```powershell
& './blenderctl.ps1' exchange import "<fbx路径>" --expected-sha256 "<fbx_sha256>" --manifest "<static-import.json>"
```

更多接收与采用细则见 [本地模型交接到 Blender](https://github.com/tsist/blender-modeling-workflow/blob/main/skills/blender-modeling-workflow/references/blender-handoff.md)。

## 交给建模部门前的核对要点

交出候选前先顺手核清下面四项，帮建模部门省去返工：

- **默认朝向与尺寸**：Tripo Studio 导出件通常正面朝 `+X`、`+Z` 朝上、高度接近 1 米。在备注里写清实测朝向和尺寸，带骨架的模型保持原变换交给下游处理。
- **贴图色彩空间**：确认基础色（Base Color）设为 `sRGB`，法线、粗糙度、金属度或拆包后的通道贴图设为 `Non-Color`，有缺失或待重做贴图时直接标明。
- **GLB 缝线拆点现象**：glTF 在 UV 缝线和硬边法线处会拆开顶点，原始边界边计数往往偏高。把这个特征写进交接备注，下游会用只读几何体检区分真实破洞与格式拆点。
- **端口与接收器卫生**：本机只保留一个活动的官方 Studio Bridge 接收器（`127.0.0.1:60600`），与 Blender Lab MCP（`9876`）并行工作互不干扰；交接脚本自带的插件扫描告警按实际端口占用核实即可。
