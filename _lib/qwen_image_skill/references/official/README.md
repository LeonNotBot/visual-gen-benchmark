# 官方原文（未改动）

本目录文件是从官方来源逐字抽取的原文，只用于核对。正文规范见上一级目录的 `t2i-guide.md`、`edit-guide.md`。

| 文件 | 来源 | 说明 |
|---|---|---|
| `t2i-rewrite-2512-zh.txt` | Qwen-Image 仓库 `src/examples/tools/prompt_utils_2512.py` 的 `polish_prompt_zh` | 文生图改写 system prompt（中文）。README 写明 Qwen-Image-2512 推荐用这一版 |
| `t2i-rewrite-2512-en.txt` | 同上，`polish_prompt_en` | 文生图改写 system prompt（英文），比中文版多了较长的示例 |
| `t2i-rewrite-v1-zh.txt` | `src/examples/tools/prompt_utils.py` 的 `polish_prompt_zh` | 早期版本（中文），含“用户输入 → 改写输出”示例和“不出现否定词”规则 |
| `t2i-rewrite-v1-en.txt` | 同上，`polish_prompt_en` | 早期版本（英文） |
| `edit-rewrite-en.txt` | `src/examples/tools/prompt_utils.py` 的 `polish_edit_prompt` | 图片编辑改写 system prompt（英文，交给 Qwen-VL-Max） |

- 仓库：https://github.com/QwenLM/Qwen-Image （抽取时 `src/examples/tools` 最近一次提交 d755887db3f6，2025-12-31）
- API 参数来源：阿里云百炼《千问-文生图 API 参考》https://help.aliyun.com/zh/model-studio/qwen-image-api （2026-11-02 读取；模型列表中为 qwen-image-2.0 / qwen-image-2.0-pro）
- 官方没有公开 API 中 `prompt_extend` 实际使用的改写规则，不能认定与上面的开源规则相同。

官方更新后，替换本目录文件，并对照修改正文规范。
