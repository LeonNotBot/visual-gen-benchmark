# 模型配置

每个 `.json` 文件对应一个模型版本。`run_bench.py --model-img <id> --model-vid <id>` 指定要用哪个。
不指定时，图片默认 `qwen_image_2_1`，视频默认 `minimax_h3`。

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 和文件名一致（不含 .json），也是命令行参数的值 |
| `name` | 是 | 显示名称，写进任务记录、进度文件和报告 |
| `task_types` | 是 | 这个模型支持的任务类型，如 `["文生图"]` |
| `platform` | 是 | `comfyui` 或 `pgx_h3`；新平台要在 `auto_generate.py` 里加适配 |
| `comfyui_url` | comfyui | ComfyUI 地址 |
| `workflow_template` | comfyui | 工作流 JSON，路径相对于脚本所在目录 |
| `megapixels` | 否 | 出图像素量（百万），0.92 ≈ 1312×736 |
| `pgx_h3_url` | pgx_h3 | pgx-h3 地址 |
| `default_resolution` | 否 | `720p`（默认）或 `480p` |

## 换模型

1. 新建 `<新id>.json`，`platform` 填现有的平台就只需改地址和参数。
2. 如果是全新平台，在 `auto_generate.py` 里照 ComfyUI / pgx-h3 的写法加三个函数：提交、查进度、下载。
3. 运行时：`python run_bench.py --model-img <新id> --file ... --out ...`
