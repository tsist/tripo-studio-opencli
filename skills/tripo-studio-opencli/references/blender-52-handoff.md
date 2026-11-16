# Blender磁盘交接

官方Bridge是常规一键路线；磁盘用于独立存档或获授权的后备。脚本需要已有兼容blenderctl.ps1与Blender，使用环境TRIPO_BLENDERCTL/TRIPO_BLENDER_EXE，不捆绑运行时。

`blender_handoff.ps1 -InputFile <absolute> -TaskDir <new-absolute-dir> -Mode auto -FrontAxis +X`对已有handoff/jobs目标拒绝覆盖。GLB由兼容blenderctl modeling import_local路径处理；FBX由隔离Blender原生导入，再冷读检查。

FBX use_image_search=false，避免全盘递归找缺图；正常相对/内嵌图保留，真缺依赖仍报错。检查四边面、骨架、权重、Action与内嵌图，不隐式三角化或将静态预设当动作。

原用户场景、源文件保持，重要交接保存工作副本。跨Scene冷检查激活真实Window Scene/ViewLayer、固定帧并更新依赖图；作者TRS/父级/父逆等另保护，原可见与共同显现条件分别声明覆盖。未活动缓存matrix_world不能替代实际几何。

本例语义精确通过仅覆盖指定版本/保存帧/原15对象，旧缓存矩阵约4.371e-8严格失败保留，不相互改签。不要将有界审计脚本当通用资产保护器。
