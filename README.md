# visual-gen-benchmark

**图片与视频生成质量评测基准**

这是一套用于批量测试图片/视频生成模型质量的自动化评测工具。从提示词优化、批量生成、到要求拆解、逐项评分、最终报告，全流程脚本化。

---

## 特性

- **提示词优化**：自动将用户原始提示词优化为更规范的格式（通过大模型改写）
- **批量生成**：支持对接 ComfyUI 和其他视频平台，批量提交任务并自动下载结果
- **模型切换**：按模型分目录存储结果，每次运行独立副本，历史结果永久保存
- **要求拆解**：从提示词自动拆出一条条评测要求，并匹配到评测体系的维度
- **评分表预填**：生成 Excel 评测记录表，评测人员只需填「满足/不满足」和维度等级
- **结果文件链接**：评测表自动加超链接列，点击即可打开对应图片/视频
- **自动化报告**：解析评测记录，生成 Markdown 格式的报告，含总览、逐样本结果、高频失败维度

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

依赖：`openpyxl`（Excel 读写）、`Pillow`（图片处理）

### 2. 配置平台地址

生成平台地址通过环境变量配置（Windows）：

```cmd
setx EVAL_COMFY_URL http://<你的ComfyUI地址>:8188
setx EVAL_PGX_URL http://<你的视频平台地址>:8082
```

也可以直接在 `models/*.json` 里指定 `comfyui_url` 或 `pgx_h3_url`。

### 3. 配置大模型 API（用于提示词优化）

创建 `_lib/config.json`：

```json
{
  "base_url": "https://你的API地址/v1",
  "model": "claude-opus-5-5",
  "api_key": "sk-你的密钥",
  "temperature": 0.0,
  "timeout": 180,
  "max_tokens": 4096
}
```

### 4. 配置视频平台凭据（如需要）

从浏览器导出 Cookie，保存为 `testset/_凭据/pgx-h3.cookies`（Mozilla cookies.txt 格式）。

### 5. 准备测试集

在 `testset/提示词测试集.xlsx` 中填写：
- **用例ID**：每条的唯一标识，如 IMG-01、VID-01
- **初始提示词**：用户原始输入
- **参考图**：首帧生视频任务需要，放在 `testset/参考图/` 下
- 其他列：任务类型、画面比例、视频时长等

### 6. 运行一次完整评测

```bash
python run_bench.py --file testset/提示词测试集.xlsx --out runs/run_001
```

运行后的目录结构：

```
runs/run_001/
  ├─ 提示词测试集.xlsx      # 本次运行的工作副本
  ├─ status.json            # 进度文件
  ├─ 生成情况.md             # 生成结果摘要（含模型信息）
  ├─ 评测记录.xlsx           # 评测表（②表最后一列可点击打开文件）
  └─ outputs/
      ├─ qwen_image_2_1/    # 图片结果
      └─ minimax_h3/        # 视频结果
```

### 7. 填写评测记录并生成报告

1. 打开 `runs/run_001/评测记录.xlsx` 的 ②表
2. 在「要求结论」列选择：满足 / 不满足 / 待复核
3. 在「维度等级」列选择：L1~L4（L1 最好）或 待复核
4. 保存后运行：

```bash
python report.py --eval runs/run_001/评测记录.xlsx
```

报告生成为 `runs/run_001/report.md`。

---

## 常见用法

### 只运行指定用例

```bash
python run_bench.py --file testset/提示词测试集.xlsx --out runs/test --ids IMG-01,VID-01
```

### 跳过提示词优化（已优化过）

```bash
python run_bench.py --file testset/提示词测试集.xlsx --out runs/test --skip-optimize
```

### 换模型重跑

```bash
python run_bench.py --file testset/提示词测试集.xlsx --out runs/new_model --model-img new_model_id
```

**注意**：换模型必须用新的 `--out` 目录，否则会报错（模型锁保护）。

### 续跑（中断后恢复）

再次运行**相同命令**，脚本会自动跳过已完成的用例，继续未完成的。

---

## 目录结构

```
visual-gen-benchmark/
│
├─ run_bench.py              ← 主入口：一条命令完成完整评测流程
├─ auto_optimize.py          提示词优化（调大模型改写）
├─ auto_generate.py          批量生成图片/视频
├─ auto_decompose.py         要求拆解（从提示词拆出评测要求）
├─ report.py                 读取评测记录，生成 Markdown 报告
├─ build_prompt_testset.py   工具：从零构建提示词测试集模板
│
├─ models/                   模型配置目录
│   ├─ qwen_image_2_1.json   ComfyUI 图片模型示例
│   ├─ minimax_h3.json       pgx-h3 视频模型示例
│   └─ README.md             字段说明和新模型接入方式
│
├─ comfy_templates/          ComfyUI 工作流模板（.json）
│
├─ _lib/                     提示词优化库（auto_optimize 的依赖）
│   ├─ optimize.py           视频提示词优化逻辑（MiniMax H3 格式）
│   ├─ optimize_img.py       图片提示词优化逻辑（Qwen-Image 格式）
│   ├─ llm.py                大模型调用封装（OpenAI 兼容接口）
│   ├─ store.py              配置读写（_lib/config.json）
│   ├─ export.py             拆解结果导出到 Excel
│   ├─ rules.py              从评测体系 Excel 读取维度规则
│   ├─ server.py             拆解助手的本地 Web 服务（内部工具）
│   ├─ h3_skill/             视频提示词优化技能（MiniMax H3）
│   └─ qwen_image_skill/     图片提示词优化技能（Qwen-Image）
│
├─ testset/                  示例测试集（可替换为自己的）
│   ├─ 提示词测试集.xlsx      3 条示例用例（IMG-01、VID-01、VID-02）
│   ├─ 参考图/               首帧生视频用的参考图
│   ├─ 评测记录/             拆解好的评测表（auto_decompose 生成）
│   └─ 报告样例/             示例生成结果和真实填写的评测表
│
├─ docs/
│   └─ 评测体系_v1.5.xlsx    评测体系主模板（评测维度和评分规则）
│
├─ tests/                    自动化测试（不依赖真实平台，用假服务器）
│   ├─ test_auto_optimize.py
│   ├─ test_auto_generate.py
│   └─ test_auto_generate_video.py
│
├─ 一键评测.bat              Windows 快捷启动（双击运行完整流程）
├─ 自动生成.bat              只运行生成步骤
├─ 自动优化提示词.bat        只运行优化步骤
├─ 自动拆解.bat              只运行拆解步骤
├─ 启动文件服务.bat          手动启动 HTTP 文件服务（临时）
├─ 安装文件服务.bat          注册开机自启的文件服务（需管理员）
├─ 卸载文件服务.bat          卸载文件服务
└─ 文件服务.ps1              文件服务的 PowerShell 实现
```

**入口文件**：`run_bench.py`，其他 `auto_*.py` 也可单独调用。

**运行结果**默认存在 `runs/` 目录下（已在 .gitignore 中排除）：
```
runs/
└─ run_20261110/
    ├─ status.json        进度
    ├─ 生成情况.md         结果摘要
    ├─ 评测记录.xlsx       评测表（②表最后一列有结果文件超链接）
    ├─ report.md          评测报告
    └─ outputs/
        ├─ qwen_image_2_1/   图片结果
        └─ minimax_h3/       视频结果
```

---

## 相关资源

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
