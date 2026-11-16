# UI与参数

生成模式single/multiview/batch/text；tier及face-limit按当前CLI帮助和UI确认。国内默认com，global显式选择。T姿势是按钮；HD精度或贴图会重置面数，面数最后填并完整读回。

多视图DOM前/左/右/后，store/API索引前0/左1/后2/右3。需要前图加另一张指定视图，uploading可能是对象，不能Boolean(object)误判永远上传中。上传后重新定位input。

后处理：segmentation、fill-parts、retopology、smart-uv、texture、texture-edit、texture-pbr、texture-upscale、rigging。先对应路由与适用参数；不识别参数直接拒绝。不根据旧菜单假设当前组合提供。P1quad/某些HD SmartPoly组合当前禁用。

Reka dialog可能关闭后仍留DOM，限定data-state=open。每次状态变化重取观察和fingerprint，不用旧find结果提交。eval保持只读，真正动作使用已有结构化输入。

适配器读取缺失is_quad字段返回未知null，不能Boolean转false；四边拓扑用原生网格确认。Browser Page必须来自已安装兼容OpenCLI，不构造未经核实的接口。
