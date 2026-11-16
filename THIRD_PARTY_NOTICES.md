# 来源与外部依赖

本仓库自研的技能、适配器和编排脚本按GPL-3.0-or-later发布。代码为已验证本地工具的公开发行适配，没有捆绑生产模型、参考图、私有会话、财务账本或原始网络捕获。

- [OpenCLI](https://github.com/jackwener/opencli)：外部Node依赖，已核版本1.8.7的包元数据许可为Apache-2.0。本仓使用其公开registry/errors/browser Page接口，不捆绑其源码或Browser Bridge扩展。
- [Blender](https://www.blender.org/)：外部运行时。`bpy`、`bmesh`与`mathutils`由使用者已有Blender提供，不在普通Python环境中安装这些模块。
- [Tripo Studio](https://studio.tripo3d.com/)与官方Tripo Bridge：第三方服务和接收插件，不包含在本仓，不能将本仓称为官方Bridge或独立API生成插件。账号、下载配额、价格和服务条款需由使用者核对。
- [blenderctl](https://github.com/tsist/blenderctl)：可选外部GPL-3.0-or-later工具，磁盘交接脚本依赖其已有兼容接口。本仓不复制其完整工具链。
- `smart-download.ps1`：下载分支需要用户明确指定的已有兼容引擎。引擎不捆绑、不自动寻找私人插件缓存或安装替代工具。本仓只含stdin保密包装器与调用协议。

网页DOM、前端构建标识、参数和价格规则记录用于说明互操作行为，不意味着获得Tripo官方授权、保证未来价格或重新分发其前端代码。本仓不包含归档的前端bundle。

软件许可与任何输入图像、第三方素材或生成资产许可分别处理。
