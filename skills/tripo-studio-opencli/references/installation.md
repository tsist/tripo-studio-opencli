# 安装与配置

对应仓库根`docs/INSTALL.md`与README。已有Python3.12+、Node20+、OpenCLI1.8.7及Browser Bridge；交接可选Blender5.2.x、PowerShell7（pwsh在PATH）、官方Tripo Bridge、兼容blenderctl及smart-download引擎。所有依赖单独安装，不捆绑运行时或账户。

profile显式`--profile`或`TRIPO_STUDIO_PROFILE`；runtime显式`--runtime-dir`或`TRIPO_STUDIO_RUNTIME_DIR`，默认当前目录`.tripo-studio`。Node账本helper用`TRIPO_LEDGER_PYTHON`，默认python。下载用`TRIPO_SMART_DOWNLOAD_ENGINE`绝对路径；磁盘交接用`TRIPO_BLENDERCTL`和`TRIPO_BLENDER_EXE`。缺失时拒绝，不搜索发布者私有插件缓存。

技能ZIP包含完整scripts/references；skills-only插件包含portable与Codex兼容manifest。OpenCLI插件适配器需要另将仓库`adapters/tripo-studio/`放到用户clis目录，不能假设技能安装就已安装适配器。仓库安装脚本遇到冲突拒绝覆盖。
