# Tripo Studio OpenCLI

通过真实浏览器操作 Tripo Studio 的自研 OpenCLI 适配器、Python 工作流和配套代理技能。支持生成、后处理、受预算约束的导出，以及与官方 Tripo Bridge 的 Blender 交接。此仓库同时提供技能包与 skills-only Codex 插件包；没有另造一个 Blender 接收器。

**0.1.0 是受限预览版。** 已有 Windows、OpenCLI 1.8.7、Blender 5.2.1 的代表性实测；网页构建变化可能使操作被安全拒绝。服务成功、导入成功和模型艺术质量分别判断。项目与 Tripo、OpenCLI、Blender 官方无隶属关系。

## 内容

| 目录 | 用途 |
| --- | --- |
| `adapters/tripo-studio/` | OpenCLI 插件：余额、配额、资产、项目、任务进度、导出地址 |
| `skills/tripo-studio-opencli/` | 技能、预算账本、单次提交、UI、导出与交接脚本 |
| `plugin.json`、`.codex-plugin/plugin.json` | skills-only 插件描述；不提供 MCP 服务或托管应用 |
| `tests/` | 临时账本及假浏览器测试，无模型生成或收费请求 |

## 安装与开始

需要已有 Python 3.12+、Node.js 20+、OpenCLI **1.8.7** 和已登录的 Browser Bridge。磁盘交接另需已有 Blender 5.2.x、PowerShell 7（`pwsh`在PATH）；官方 Tripo Bridge、自有兼容 smart-download 引擎及 blenderctl 仅为对应路径的外部依赖。安装包不包含这些程序或任何账号、模型、运行账本。

1. 从 [Releases](https://github.com/tsist/tripo-studio-opencli/releases) 下载技能包或插件包，或者克隆源码。
2. 将技能目录装入代理自己的技能根，或按客户端的本地插件流程加载整个插件包。GitHub 发布不等于应用目录上架或自动安装。
3. 安装 OpenCLI 适配器到自己的配置目录，已有目标时脚本会拒绝覆盖：

```powershell
node scripts/install-adapters.mjs --destination "$env:USERPROFILE\.opencli\clis\tripo-studio"
$env:TRIPO_STUDIO_PROFILE = 'YOUR_EXISTING_PROFILE'
$env:TRIPO_STUDIO_RUNTIME_DIR = Join-Path $PWD '.tripo-studio'
python skills/tripo-studio-opencli/scripts/tripo_studio_driver.py --help
```

安装、版本、路径和可选依赖见 [安装指南](docs/INSTALL.md)。先在浏览器手动登录正确地区，再执行只读预检。普通调用默认 dry-run；真正云提交须有具体任务、步骤和授权预算。

```powershell
python skills/tripo-studio-opencli/scripts/tripo_studio_driver.py preflight
python skills/tripo-studio-opencli/scripts/tripo_studio_driver.py generate --mode text --prompt 'industrial utility vehicle' --tier smartmesh --quad --face-limit 25000
```

这不是独立 `TRIPO_API_KEY` 钱包工具。默认国内 `studio.tripo3d.com`，海外地区必须显式选择，不能自动跨区。

## 验收与边界

- 代表性的生成、重拓扑、拆分/补全、UV、贴图/PBR/放大、绑定和实际动作文件已有有限实测；不保证任意参数组合或资产质量。
- 已有代理单次 FBX/Blender/8192/embedded Bridge 收件、内嵌颜色图与独立冷读正例。只证明该颜色图 8K，不证明全 PBR 通道 8K 或自动重连。
- 一次本地语义检查中，原15对象在5个 Scene/ViewLayer、指定保存帧的作者状态及求值世界几何/拓扑精确保持。旧缓存世界矩阵约4.371e-8的严格失败仍保留，不能用新语义结果改签旧测试。
- **无可核价格上界的重绘预览保存自动执行禁用**，提高本地 cap 不能解锁。WebGL 笔刷人工；3MF仅XML检查；Max预设仅静态检查。

完整说明见 [能力与验证](docs/VERIFICATION.md) 与 [费用及安全](docs/SECURITY.md)。发布适配仅更改路径/配置和文档，未重新进行云端付费验证。离线测试不替代云端正例。

## 本地测试

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
$env:TRIPO_LEDGER_PYTHON = (Get-Command python).Source
node tests/test_bridge_rules.mjs
node tests/test_receipts.mjs
```

运行测试不需要 Tripo 账号或云调用。Blender、浏览器和下载引擎的实际工作流需在自己授权的环境中单独验证。

## 许可

自研代码和技能采用 **GPL-3.0-or-later**，见 [LICENSE](LICENSE)。第三方依赖与归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。软件许可不授予服务访问权、参考图或生成资产的使用权。
