## 图片编辑提示词写作规范（Qwen-Image-Edit）

规则来自官方编辑改写 system prompt（`official/edit-rewrite-en.txt`，标记 [编辑]），原文为英文，以下为中文整理，示例保留英文原文。官方 README：不做提示词改写时编辑结果可能不稳定，强烈建议改写。

说明：这份规则在早期版 `prompt_utils.py` 里，2512 版文件没有编辑规则。阿里云《千问-图像编辑》API 文档本 skill 未核对。

### 1. 总原则 [编辑]

- 指令**直接、具体**。
- 指令矛盾、含糊或做不到时，优先合理推断和修正，必要时补细节。
- 原指令的核心意图不变，只提高清晰度、合理性和可实现性。
- 新加的物体或修改，要和输入图整体场景的逻辑、风格一致。

### 2. 按编辑类型写 [编辑]

| 类型 | 写法 | 官方示例 |
|---|---|---|
| 增 / 删 / 替换 | 指令已清楚（类型、目标、位置、数量、属性都有）就只理顺语法；含糊时补最少但够用的细节（类别、颜色、大小、朝向、位置）；无意义指令（如“加 0 个物体”）忽略；替换写成“把 Y 换成 X”，并简述 X 的关键外观 | "Add an animal" → "Add a light-gray cat in the bottom-right corner, sitting and facing the camera" |
| 文字编辑 | 文字一律放英文双引号 " "，保留原语言和大小写；新增文字和替换文字都按“替换”写；位置、颜色、排版只在用户要求时写；指定字体时字体名保留原语言 | Replace "xx" to "yy"；Replace the mask / bounding box to "yy"；Replace the visual object to "yy" |
| 人物编辑 | 强调保持人物核心一致（种族、性别、年龄、发型、表情、服装）；改外观时新元素和原风格一致；**表情、美颜、妆容的修改要自然、细微，不能夸张** | "Change the person's hat" → "Replace the man's hat with a dark brown beret; keep smile, short hair, and gray jacket unchanged" |
| 风格转换 | 指定了风格就用关键视觉特征简述；参考图风格要分析并提取色彩、构图、质感、光照、艺术风格写进指令；明确改哪个对象；有其他修改时风格描述放最后 | "Disco style" → "1970s disco style: flashing lights, disco ball, mirrored walls, colorful tones" |
| 上色 / 老照片修复 | **固定模板** | "Restore and colorize the photo." |
| 局部补全（inpainting） | **固定模板** | "Perform inpainting on this image. The original caption is: " + 原图描述 |
| 扩图（outpainting） | **固定模板** | "Extend the image beyond its boundaries using outpainting. The original caption is: " + 原图描述 |
| 多图 | 写明改的是哪张图的哪个元素；风格化任务要描述参考图风格，同时保留源图内容 | "Replace the subject of picture 1 with the subject of picture 2" → "Replace the girl of picture 1 with the boy of picture 2, keeping picture 2's background unchanged" |

### 3. 合理性检查 [编辑]

- 矛盾指令要修正，如“删掉所有树但保留所有树”。
- 缺关键信息要补：没写位置时，按构图选合理区域（主体附近、空白处、中心或边缘）。

### 4. 交付前自检

- [ ] 编辑类型判断正确；上色、补全、扩图用了固定模板
- [ ] 指令直接具体，核心意图没变
- [ ] 文字放在英文双引号里，原语言、大小写未改
- [ ] 人物编辑写明了要保持不变的特征；表情、妆容改动是细微的
- [ ] 多图任务写明了图号和元素
- [ ] 没有自相矛盾的要求；缺的位置已补
- [ ] 不想改动的区域明确写了保持不变 [补充，依据官方人物编辑和多图示例里的 “keep … unchanged” 写法]
