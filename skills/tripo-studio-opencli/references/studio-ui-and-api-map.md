# 站点区域、OpenCLI 适配器与模块对照

本页整理 Tripo Studio 网页端站点隔离规则、只读与导出适配器命令，以及生成与后处理的路由映射。实际可执行参数以 `blenderctl.ps1 discover --product tripo-studio --actions` 和各脚本 `--help` 为准。

## 区域隔离

| 参数 | 网页入口 | REST 接口 | Micro RPC | 适用场景 |
|---|---|---|---|---|
| `--region cn`（默认） | `https://studio.tripo3d.com` | `https://api-cn.tripo3d.com` | `https://msapi.tripo3d.com/ms/web/api` | 国内站会员账号会话 |
| `--region global` | `https://studio.tripo3d.ai` | `https://api.tripo3d.ai` | `https://msapi.tripo3d.ai/ms/web/api` | 用户明确指定国际站时使用 |

国内与国际两站账号、积分余额和项目编号彼此独立。全程固定用户指定的 `--region`，域名保持使用上面两行已验证地址。

## 只读与导出 OpenCLI 适配器

本地适配器位于 `~/.opencli\clis\tripo-studio\`，所有调用都复用当前已登录的 Chrome 会话，通过 `--profile` 指定配置档（默认 `un6amgxk`）。

| 命令 | 后端接口 | 用途与关键参数 |
|---|---|---|
| `opencli --profile <p> tripo-studio balance --region cn -f json` | `$micro GetAssets` + `config-store` | 查询会员积分余额、过期积分、下载配额与免费试用计数 |
| `opencli --profile <p> tripo-studio quota --region cn -f json` | `POST /v2/studio/operation/quota` | 查询 Smart Mesh、智能 UV 等专项剩余额度 |
| `opencli --profile <p> tripo-studio assets --region cn -f json` | `GET /v2/studio/assets/v2` | 列出个人或收藏资产，支持 `--asset-type mine\|collected`、`--type`、`--offset`、`--limit` |
| `opencli --profile <p> tripo-studio project <project_id> --region cn -f json` | `GET /v2/studio/project/detail/v3/{id}` | 读取项目详情、历史操作、`is_owner`、`is_quad`、`is_rigged`、`is_pbr` |
| `opencli --profile <p> tripo-studio progress --ids <op_id> --region cn -f json` | `POST /v2/studio/progress` | 轮询操作进度（`prepare\|queued\|running\|success\|failed\|expired\|cancelled\|banned\|unknown`） |
| `opencli --profile <p> tripo-studio export-url <project_id> --confirm-export -f json` | 自有：`/v2/studio/operation/export`<br>非自有：`/v2/studio/operation/export-public-model` | 传 `--model-format gltf\|fbx\|obj\|stl\|usdz\|3mf` 指定模型格式，`-f json` 指定终端输出格式；FBX 可配 `--fbx-preset blender\|3dsmax\|mixamo`、`--texture-packaging embedded\|zip`、`--texture-size` |

发起导出前先完成账本预留并核对 `--confirm-export`。非自有公开模型导出会产生积分消耗，需用户明确同意后加 `--allow-nonowner-spend`。

## 生成工作区（`/workspace/generate`）

由 `scripts/tripo_studio_driver.py generate` 驱动，支持四种输入模式和两种主力档位：

- **单图（`--mode single`）**：传 `--images <图片路径>`，图片走网页原图上传与审核流程。
- **多视图（`--mode multiview`）**：传 `--front` 加至少一张侧视图或背视图（`--left`、`--right`、`--back`）。网页槽位显示顺序是前/左/右/后，内部接口索引是前 0 / 左 1 / 后 2 / 右 3，驱动会自动完成槽位映射并在每次上传后重新核对输入状态。
- **批量图（`--mode batch`）**：传 `--images img1 img2 ...`，按输入张数计费。
- **文本生成（`--mode text`）**：传 `--prompt "<描述>"`，角色类可加 `--t-pose`。

档位选择：

- **Smart Mesh（`--tier smartmesh`）**：面数可控的拓扑优先生成，配合 `--quad` 和 `--face-limit`（四边面 500–25000，三角面 500–50000）。非批量模式下支持 `--variations 1|2|4` 同价出多档面数候选，后续优先导 FBX（Blender 预设）保留四边面。
- **高细节（`--tier hd`）**：偏重造型与贴图细节，原生输出 GLB。切换精度或贴图选项时页面会重置面数，驱动会在最后写入面数并回读核对。

## 九个后处理模块（`/workspace/<route>/<project-id>`）

由 `scripts/tripo_studio_driver.py postprocess --operation <名> --project-id <id>` 驱动：

| `--operation` | 网页路由 | 后端操作端点 | 使用要点 |
|---|---|---|---|
| `segmentation` | `/workspace/segmentation` | `/v2/studio/operation/ai_segmentation` | 自动拆分模型部件，可选 `--granularity coarse\|medium\|fine` |
| `fill-parts` | `/workspace/fill-parts` | `/v2/studio/operation/ai_completion` | 补全拆分后的部件缺口 |
| `retopology` | `/workspace/retopology` | `/v2/studio/operation/remesh` | 重拓扑，默认四边面，可切 `--triangle`、`--smart-poly` 与 `--face-limit` |
| `smart-uv` | `/workspace/smart-uv` | `/v2/studio/operation/uv_edit/generate` | 生成 UV 候选预览；确认后再用 `--apply-preview-step` 保存 |
| `texture` | `/workspace/texture` | `/v2/studio/operation/texture_model` | 重新生成贴图，支持 `--texture-quality`、`--prompt` 和独立风格参考图 `--ref-image` |
| `texture-edit` | `/workspace/texture-edit` | `/v2/studio/operation/retexture_generate` | 驱动当前视图的提示词生成模式（`--prompt`）；3D 画笔绘制留给人工在网页操作 |
| `texture-pbr` | `/workspace/texture-pbr` | `/v2/studio/operation/pbr_generate` | 补齐法线、粗糙度、金属度等 PBR 通道 |
| `texture-upscale` | `/workspace/texture-upscale` | `/v2/studio/operation/texture_upscaler` | 贴图放大，默认标准 4K，加 `--upscale-extreme` 升至 8K |
| `rigging` | `/workspace/rigging` | `/v2/studio/operation/rigging_model` | 自动绑定，`--rig-type biped\|animal`；绑定完成后另起一步选 `--animation` |

## 页面交互守则

- **先读后点**：所有页面值通过只读方式读取，表单交互走受控点击、填写和文件上传，操作完立刻回读控件真实状态。
- **弹窗过滤**：关闭的对话框节点有时留在页面树里，定位控件时限定可见状态（`data-state=open`），状态变更后刷新页面树再查引用。
- **会话隔离**：新生成和常规后处理使用独占前台页并固定标签页；预览保存和官方 Bridge 绑定原标签页就地继续，保护未保存状态。
