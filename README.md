# scanned-pdf-ocr

把**扫描版 PDF**（无文本层）整本 / 整章一次性转成 Markdown 的命令行工具 —— 用视觉大模型逐页「看图识字」。

- 公式 → LaTeX，表格 → Markdown 表格，题号题干完整保留
- 自动跳过封面 / 前言等前置内容（从「目录」页开始）
- 从目录页**自动生成章节切分**
- 转写后**清洗残留水印**
- **断点续跑**：中断后重跑只补缺失页，已完成页零重复消耗

> 产物定位是**喂给 AI 的教材文本**，不是给人阅读的电子书：重文字 / 公式 / 表格 / 题目，轻插图外观。

面向编码 agent 的详细操作说明见 [`SKILL.md`](./SKILL.md)。

## 环境

- Python 3.8+（Windows / macOS / Linux 均可）
- 一个 OpenAI 兼容的视觉模型（见下）

## 快速开始

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp config.example.json config.json   # 然后编辑 config.json
python ocr_pdf.py --config config.json
```

也可以不写配置文件，命令行直接跑：

```bash
python ocr_pdf.py --pdf "book.pdf" --out "book-md" \
  --base-url "https://dashscope.aliyuncs.com/compatible-mode/v1" \
  --api-key "sk-xxxx" --model "qwen3.5-ocr"
```

Windows 一键脚本：`run.bat`（自动建 venv、装依赖、按 `config.json` 运行）。

## 模型从哪来

| 供应商 | base_url | 视觉模型示例 |
|---|---|---|
| 阿里 DashScope（百炼） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.5-ocr` / `qwen-vl-max` |
| 智谱 BigModel | `https://open.bigmodel.cn/api/paas/v4` | `glm-4v` / `glm-4.6v-flash` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

选型（2026）：**追求权威** → PaddleOCR-VL-1.6（需换 SDK）；**几乎零改动 + 便宜够准** → `qwen3.5-ocr`；**极致省钱** → GLM-OCR（专用接口，需改脚本）。整本 500 页的 API 花费普遍只有**几元**。

API Key 也可用环境变量，不写进文件：`set VISION_API_KEY=sk-xxxx`。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--pages 起-止` | 只处理某一页区间（最省事的按章跑法） |
| `--start-page N` | 显式指定起始页，跳过前置内容 |
| `--keep-front-matter` | 连封面 / 前言一起转 |
| `--auto-chapters` | 读目录页自动生成章节切分 |
| `--cleanup off\|rules\|llm\|both` | 转写后水印清洗模式（默认 `llm`） |
| `--force-ocr` | 忽略文本层，强制全部走视觉模型 |
| `--figures` | 导出页内非整页插图到 `images/` |
| `--no-thinking` | Qwen-VL 思考模型关思考，提速 |
| `--ask-key` | 终端交互输入 API Key（仅内存、不落盘） |
| `--reset-cache` | 清空逐页缓存重跑 |

## 输出

```
{out_dir}/
  00-目录.md            # 章节索引 + 失败页清单
  01-第一章 绪论.md
  .cache/               # 逐页缓存，断点续跑用（勿删）
  images/               # 仅 --figures 时出现
```

## 安全

API Key 支持**不落盘**：`config.json` 里 `api_key` 留空，运行时加 `--ask-key` 交互输入（只进内存、不回显、进程结束即消失），或设环境变量 `VISION_API_KEY`。若仍写进 `config.json`，注意它含明文 Key，**已在 `.gitignore` 排除，切勿提交**；只提交 `config.example.json`。

## License

[MIT](./LICENSE)
