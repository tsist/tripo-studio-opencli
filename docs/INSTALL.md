# 安装与配置

适用已验证基线：Windows、Python3.12+、Node20+、OpenCLI1.8.7、Browser Bridge及可选Blender5.2.1。磁盘交接另需PowerShell7，`pwsh`须在PATH。没有Linux/macOS端到端声明。不要为掩盖版本不匹配自动升级。

## 技能和插件

技能ZIP解压得到`tripo-studio-opencli/SKILL.md`与完整scripts/references，放入实际代理技能根。插件ZIP保留根plugin.json、兼容`.codex-plugin/plugin.json`及`skills/`，使用客户端支持的本地插件加载方式。此发布没有修改你的代理配置，也没有提交任何应用商店。

OpenCLI适配器安装命令见README。脚本预检全部文件后才复制，目标有同名文件时拒绝，不覆盖已有适配器。也可人工将`adapters/tripo-studio/`复制到你自己的OpenCLI clis根。必须由已有OpenCLI加载，不直接用Node运行adapter JS。

## 环境变量

| 配置 | 用途 |
| --- | --- |
| `TRIPO_STUDIO_PROFILE` | 已连接Browser Bridge的profile，或CLI显式`--profile` |
| `TRIPO_STUDIO_RUNTIME_DIR` | 独立可写运行根，默认当前目录`.tripo-studio`；也可显式`--runtime-dir` |
| `TRIPO_LEDGER_PYTHON` | Node Bridge helper调用账本的已有Python可执行路径，默认`python` |
| `TRIPO_SMART_DOWNLOAD_ENGINE` | 下载分支必需：已有兼容smart-download.ps1的绝对路径 |
| `TRIPO_BLENDER_EXE` | 磁盘交接必需：已有Blender可执行文件 |
| `TRIPO_BLENDERCTL` | 磁盘交接必需：已有兼容blenderctl.ps1路径 |

`tripo_studio_driver.py --help`、`tripo_ledger.py --help`可离线检查。不配置profile时，尝试连接前会拒绝，不采用发布者的浏览器会话。

## 可选下载引擎协议

`secure_download_bridge.ps1`不接收命令行URL；Download从stdin读取JSON，含`url/destination`等已校验字段。它在同一PowerShell进程中调用已有引擎的`Doctor/Download/Status`接口，Download支持`-Uri/-Destination/-StateRoot`及可选`-ExpectedBytes/-Sha256`。引擎应返回持久job ID、最终状态/文件及校验信息；不满足时拒绝，不自动重复导出。签名URL不能写日志或公开issue。

## 官方Bridge

安装来自Tripo官方的Studio Bridge，确认唯一接收器端口60600和目标场景；Blender Lab MCP9876是另一功能。保留原Studio页面及PiP连接。帮助脚本仅适用已核网页构建的国内自有静态FBX/Blender/8192/embedded配方，未知构建停止。实际Page从已有OpenCLI公开`browser/page`接口获得；不要把另一工具的Page对象冒充兼容。

常规精确入口：导出抽屉 → FBX → Blender → 8k → 发送至 → Blender。浮窗资产卡箭头可能使用默认2K/4K，不能当精确8K入口。首次用自己的测试工作副本，不对未知用户场景发送。
