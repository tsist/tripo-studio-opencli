---
name: tripo-studio-opencli
description: 使用已连接的OpenCLI操作Tripo Studio网页生成、后处理和导出，通过官方Bridge或磁盘交接Blender；包含预算、单次回执与失败恢复，适用Studio会员流程，不适用独立API钱包或自动WebGL笔刷。
---

# Tripo Studio → OpenCLI → Blender

用已有浏览器登录和明确授权操作Studio。默认国内`studio.tripo3d.com`，海外`.ai`仅显式选择；不自动跨区，不使用`.cn`域名。独立`TRIPO_API_KEY`钱包属于其他工具。

## 准备

按用户任务恢复当前授权与已有状态；源码/实际页面优先于历史报告。先读[安装与配置](references/installation.md)核已有依赖、目标profile和可写runtime。公开包不携带发布者浏览器配置、账本或云授权。用`python scripts/tripo_studio_driver.py --help`发现参数；不为检查而安装或升级工具。

只读预检：`python scripts/tripo_studio_driver.py --profile YOUR_PROFILE preflight`。生成/后处理默认dry-run；执行需要明确task/step、数值预算和`--execute-spend`。上传也须在授权输入范围内。

## 预算与单次提交

先用`tripo_ledger.py --help`发现并初始化独立任务。积分、下载意图、操作、试用分别计数。调用前保存reserved/submitting，按精确参数输入一次；missing-price、价格不符、失败、超时或unknown即停，不自动重试。旧unknown/halt不清、不回收占用，泛泛“继续”不提高上限。参见[费用和回执](references/pricing-and-budget-gates.md)。

无可核价格/费用上界的重绘预览保存自动执行禁用，任何`--unpriced-apply-cap`均不能解锁；dry-run和非提交准备仍可用。本地cap不是服务端费用上限，钱包净差额不能当单笔报价。

## 页面与输入

eval只读；普通操作用结构化click/fill/select/upload。禁止DOM.click、写Pinia、合成DOM指针、hook fetch。普通生成/后处理使用独占新页；预览应用和Bridge必须绑定原有profile/session/page，不静默新建、导航替换或关闭原页。状态变化后刷新观察，不复用旧引用。

四输入模式和九后处理见[UI与参数](references/studio-ui-and-api-map.md)。不支持的组合拒绝，不强写禁用控件。WebGL笔刷自动入口拒绝，需要人工另行处理。

预览ready不等于资产已应用；UV保存绑定源operator/candidate/project并回读实际状态。动画name先唯一映射完成的retarget operator ID，缺失、歧义或重复拒绝；`enable_bake_animation`不是Action烘焙。参见[预览与动画](references/preview-apply-and-animation.md)。

## 导出与Blender

优先用户请求的官方Studio Bridge，精确入口为导出抽屉→FBX→Blender→8k→发送至→Blender。资产卡箭头不保证继承分辨率。完整原页/构建/接收器/捕获/账本锁规则见[Bridge](references/studio-blender-bridge.md)。

导出是预算内的两阶段动作：UUID先落盘，随后解析与下载。恢复沿用原UUID和partial，不重新export。大文件使用已有兼容下载引擎后台传输，签名URL仅stdin；任务complete且验证后才声明下载成功。磁盘交接见[Blender交接](references/blender-52-handoff.md)。

真实完成需要：唯一POST回执→实际字节/同receiver完成→目标几何/UV/材质/真实图片→保存副本→适用的独立冷读和原内容保护。页面已准备、目录存在、服务success或插件注册均不单独构成通过。

## 验收与故障

当前是代表性限定验收，见[验证边界](references/tooling-validation.md)。颜色8K不等于全PBR8K，语义保护正例不改签旧缓存矩阵失败，未验分支不自动通过。失败后只调查同次任务；两次同类失败换方法，达到停止条件不自行重开。参见[恢复](references/troubleshooting-and-recovery.md)。

凭证只保留在已有会话，禁止落盘Cookie/认证头/完整签名URL。源文件与用户场景保护优先，不关闭未知进程，不抢占端口，不自动改全局安全偏好。
