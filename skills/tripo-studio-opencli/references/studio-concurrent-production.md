# 国区 Studio 多资产并发与对账

同配方、同交期的参考图优先一次批量生成。逐件短提交适合配方不同、需要独立恢复或分批到件的资产。两件同配方的国区样本中，扣除批量组映射项目的停顿后，逐件路线多用约 42 秒。逐件路线观察到有作业在途时推进其他工序，未采到两个云端作业同时运行的快照。批量生成接错峰后处理的整批用时需要单独测量。

1. 使用同一个国区 OpenCLI 配置和运行目录。预检余额、试用和下载配额，按已获批准的预算、提交次数和下载次数初始化任务。并发批次用该运行目录的共享账本，同一浏览器配置在同一账本内只开一个在途并发批次。把同钱包的其他收费操作安排在这批结束后。
2. 生成可用 `--execute-spend --submit-only` 保存原项目与操作回执后返回，每件独立资产使用自己的 `step-key`。用 `--mode batch` 时，一组结果共用一个步骤，各操作的状态分别显示，整组到达终态后结算。
3. 用 `collect --task-id ...` 查看一次快照。已成功的项目可以进入下一工序。未决、传输错误和未知结果保留原回执后重查。钱包下降超出已提交价格上界时暂停整批。余额变化用于批次核对，逐笔费用依赖服务端凭据。
4. 支持的后处理可用短提交。指定 `--source-operator-id` 绑定该项目的当前成功变体。Smart UV 预览及应用、贴图编辑预览、绑定和动画保留各自的页面验收流程。
5. 自有资产导出可用 `--submit-only`。提交前后都会核对当前变体，期间保持该项目内容和选中版本稳定。用相同参数和 `--resume-export` 找回原操作及下载作业，再用 `--download-job` 核对清单、文件及 SHA-256。
6. 记录整批与首件 Blender 冷开耗时。验收时将项目、源变体、导出操作和下载文件一一对应，检查实际网格、UV 和嵌入贴图。

从仓库根目录运行以下示例。先填入自己已获批的额度、配置及源变体，再执行收费操作。

```powershell
$env:TRIPO_STUDIO_RUNTIME_DIR = Join-Path $PWD '.tripo-studio'
$scripts = Join-Path $PWD 'skills/tripo-studio-opencli/scripts'
$driver = Join-Path $scripts 'tripo_studio_driver.py'
$ledger = Join-Path $scripts 'tripo_ledger.py'
$profile = '<existing-profile>'
$taskId = '<batch-task-id>'
$studio = @('--region', 'cn', '--profile', $profile)

python $ledger init-task --task-id $taskId --region cn --profile $profile `
  --budget-cap $approvedCredits --max-submissions $approvedSubmissions --download-cap $approvedDownloads
python $driver @studio generate --mode single --tier smartmesh --lp-version p1 `
  --visibility private --face-limit 5000 --images $referenceImage `
  --task-id $taskId --step-key a_generate --execute-spend --submit-only
python $driver @studio collect --task-id $taskId

$exportArgs = @('export', '--task-id', $taskId, '--step-key', 'a_export', `
  '--project-id', $projectId, '--source-operator-id', $sourceOperatorId, `
  '--model-format', 'fbx', '--fbx-preset', 'blender', '--texture-packaging', 'embedded')
python $driver @studio @exportArgs --execute-spend --submit-only
python $driver @studio @exportArgs --resume-export
python $driver @studio @exportArgs --download-job $downloadJob
```

模型下拉框关闭后控件仍无响应时，检查 OpenCLI 专用标签页是否为 `visible`。将专用窗口置前并最大化后，再运行无提交的预检。空批量表单可能没有总价，上传经批准的参考图后由驱动重新核对按钮实价。
