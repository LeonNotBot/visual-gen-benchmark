---
name: qwen-image-prompt-writing
description: Write Qwen-Image prompts for text-to-image (portrait, text-containing, general images) and image editing. Use when rewriting a user's image request into a Qwen-Image prompt, adding on-screen text, choosing negative_prompt and prompt_extend settings, or writing add/delete/replace, text-edit, style-transfer, inpainting/outpainting and multi-image edit instructions.
compatibility: Plain Markdown + reference files; no external API calls. Rules are taken from the official Qwen-Image open-source rewrite prompts and the Alibaba Cloud Model Studio API reference; see references/official/ for the verbatim sources.
---

## Qwen-Image 提示词写作

### 流程
1. 判断任务：文生图，还是图片编辑（有输入图）。
2. 文生图：读 `references/t2i-guide.md`。先把画面归为 **人像 / 含文字图 / 通用图** 三类之一，再按该类的要素清单写。
3. 图片编辑：读 `references/edit-guide.md`。先判断编辑类型（增删改、文字编辑、人物编辑、风格转换、补全扩图、多图），再按该类型的写法写。
4. 定好调用参数：`negative_prompt` 写什么、`prompt_extend` 开不开（见 t2i-guide 第 6 节）。
5. 按 t2i-guide 第 7 节的自检清单检查一遍再交付。

### 和 H3 skill 的主要区别
- Qwen-Image **没有固定字段**。官方要求写成一段连贯的描述性文字，不用列表、编号、标题；只有信息图、图表类可以用换行分区。
- 不想要的内容放 `negative_prompt`，正向提示词里不写否定词。
- 输出语言跟随输入：输入里有汉字就写中文，否则写英文（官方改写工具的判定方式）。

### 输出规则
- 只输出改写后的提示词（以及需要时的 `negative_prompt` 和参数建议），不解释。
- 不改变用户原意；专有名词、用户给定的文字原样保留。
- 用户没要求文字的画面，不添加任何文字。

### 规则来源标记
正文每条规则后标注来源：**[2512]** = 官方改写规则 2512 版；**[v1]** = 官方改写规则早期版；**[编辑]** = 官方编辑改写规则；**[API]** = 阿里云 API 文档；**[补充]** = 本 skill 根据评测需要补充，官方没有原文。
