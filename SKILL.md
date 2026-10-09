---
name: scanned-pdf-ocr
description: 把扫描版 PDF（无文本层）整本/整章用视觉大模型 OCR 转成 Markdown，供 AI 教学当素材。含前置内容跳过、目录页自动生成章节、残留水印清洗、断点续跑。
---

# Skill：扫描版 PDF → Markdown（视觉大模型 OCR）

> 给外部编码 agent（有 shell、能跑 Python 的）执行；也给人看。
> 目标：把一本**扫描版 PDF**（无文本层）整本/整章一次性转成 `.md` 文本稿，再交给 Phrontis「AI 教学」区当素材用，免去在软件里一个区间一个区间地手动视觉转写。

---

## 0. 它解决什么

扫描版 PDF 本质是**页面图片**，没有文本层，普通提取会得到空，必须靠 OCR「看图识字」。本 skill 把每页渲染成图片，发给**视觉大模型**（OpenAI 兼容接口）逐页转写成 Markdown：

- 公式 → LaTeX（行内 `$…$`，独立 `$$…$$`）
- 表格 → Markdown 表格（承载关键信息，必须转写）
- 纯辅助理解的示意图 / 函数图像 / 装饰性插图 → **默认省略，不输出任何图片语法**；**但若题干或选项本身就是图**（如 A/B/C/D 四幅函数图像），**必须逐项用一句文字描述**（「A：开口向上的抛物线…」），保证题目可判读；其余承载关键信息的插图才用 `【图：…】` 点明要点
- 页面上的**水印 → 一律不转写、不提及**（半透明文字、LOGO、印章、重复的机构名/网址水印等），当作不存在，绝不让水印混进正文；转写后还有一道**残留水印清洗**（见 §4 `cleanup`）
- 书籍默认**从「目录」页开始转写**，自动跳过封面 / 编者的话 / 前言 / 版权页等前置内容（见 §4）
- 保留标题层级与题号，**严禁编造**

> **定位**：产物是**喂给 AI 的教材文本**，不是给人阅读的电子书——**重文字 / 公式 / 表格 / 题目，轻插图外观**。这些稿子用户几乎不会直接看，所以像函数图像、示意图这类主要帮人理解的插图不必还原。

输出格式与 Phrontis 软件内的「视觉转写」同款，所以产出的 `.md` 可以**直接登记为教材素材**，AI 教学中用 `vault.read` 就能读，不必在软件里重新逐页转写。

---

## 1. 环境要求

- Windows + Python 3.8+（本机为 3.8.10）
- 一个 **OpenAI 兼容**的视觉模型（见 §3）
- 依赖：`PyMuPDF`、`requests`（见 `requirements.txt`）

---

## 2. 快速开始（agent 照做）

在本目录（`桌面\pdf-ocr-skill`）执行：

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json
```

然后编辑 `config.json`（见 §4），再运行：

```bat
python ocr_pdf.py --config config.json
```

> Windows 一键：直接双击 / 运行 `run.bat`（自动建 venv、装依赖、按 `config.json` 跑）。
> 也可 `pip install -e .` 安装，之后用 `scanned-pdf-ocr` 命令；列供应商用 `ocr-providers`。
> ⚠️ `config.json` 含明文 API Key，**已在 `.gitignore` 排除，切勿提交**（只提交 `config.example.json`）。

也可以不写配置文件，用命令行直接跑（适合单次、小范围）：

```bat
python ocr_pdf.py --pdf "D:\book.pdf" --out "D:\book-md" ^
  --base-url "https://dashscope.aliyuncs.com/compatible-mode/v1" ^
  --api-key "sk-xxxx" --model "qwen-vl-max" --pages 1-72
```

> 只处理某一章时用 `--pages 起-止` 最省事；不带 `--pages` 就按 `config.json` 的 `chapters` 全部处理。

---

## 3. 视觉模型 / API Key 从哪来

Phrontis 软件「设置 → AI 模型（模型供应商）」里能看到**供应商地址（baseUrl）**和**模型名**。

本目录提供了只读辅助脚本，可直接列出软件里所有供应商的 `base_url` 与**视觉候选模型名**，省得手抄：

```bat
python inspect_app_providers.py
```

⚠️ **API Key 在软件里是加密存储的**（Electron `safeStorage`，文件里的 `apiKeyEncrypted` 外部脚本读不出明文），所以 Key 需要你手动提供——两种途径：
1. 从软件设置界面复制（若界面允许展开查看）；
2. 直接去该供应商后台复制同一个 Key。

常见 OpenAI 兼容视觉模型示例：

| 供应商 | base_url | 视觉模型示例 |
|---|---|---|
| 阿里 DashScope（千问） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-vl-max` / `qwen-vl-plus` / `qwen-vl-ocr` |
| 智谱 BigModel | `https://open.bigmodel.cn/api/paas/v4` | `glm-4v` / `glm-4v-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| 其他兼容网关 | 按供应商文档 | 名字含 `vl` / `vision` / `4o` / `omni` 的多半可以 |

> **推荐：不落盘。** `config.json` 里 `api_key` 留空，运行时三选一：
> - **交互输入（最省心）**：`python ocr_pdf.py --config config.json --ask-key` → 终端提示输入，Key 只进内存、不回显、进程结束即消失。**AI 开终端让你敲 Key 就是这个流程**（agent 跑任务时带上 `--ask-key` 即可，脚本不会在无人应答时自动阻塞）。
> - **环境变量**：`set VISION_API_KEY=sk-xxxx`（或 `setx` 永久），`config.json` 的 `api_key` 留空。
> - **写进 `config.json`**：能用，但明文且**切勿提交**（已被 `.gitignore` 排除）。

### 3.5 模型选型建议（国内，2026）

先明确：转一整本 500 页的 token 费普遍只有**几元**（图片 token 占大头但单价极低），所以选型看**精度 + 接口是否匹配**，不是省钱。

| 场景 | 推荐 | 为什么 |
|---|---|---|
| 要最权威（文档解析 SOTA） | **PaddleOCR-VL-1.6**（百度，0.9B，Apache-2.0） | OmniDocBench v1.6 96.33% 全球第一；公式/表格/图表/古籍强；**可本地免费跑**（CPU 也行，GPU 快） |
| 直接塞进本 skill（通用 chat 接口） | **qwen3.5-ocr**（阿里百炼，¥0.5/¥2 每百万） | OCR 专用、走通用 `chat/completions`，**零改动**；整本约 ¥2 |
| 极致省钱（API） | **GLM-OCR**（智谱，0.2 元/百万，输入=输出） | 专用 OCR，整本约 ¥0.5；⚠️ 是独立 `layout_parsing` 接口，**非 chat 格式**，需改脚本 |
| 免费试水 / 通用视觉 | **GLM-4.6V-Flash**（智谱，免费）；**qwen-vl-max**（¥1.6/¥4） | 通用模型版式/公式不如专用模型 |

> ⚠️ 接口匹配：本脚本走 **OpenAI 通用 `chat/completions`**（自定义系统提示词）。GLM-OCR、PaddleOCR-VL 官方 API 是**专用接口**（`layout_parsing` / 自家 SDK），不能直接套——要用需改脚本或改用其 SDK。

结论：**追求权威 → PaddleOCR-VL-1.6**；**几乎零改动 + 便宜够准 → qwen3.5-ocr**；**极致省钱 → GLM-OCR**。

---

## 4. `config.json` 字段

```jsonc
{
  "pdf": "D:\\book.pdf",              // 扫描版 PDF 的绝对路径
  "out_dir": "D:\\book-md",           // 输出目录（不存在会自动建）
  "provider": {                        // 【视觉模型】负责看图转写
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key": "sk-xxxx",             // 或留空走环境变量 VISION_API_KEY
    "model": "qwen-vl-max",
    "headers": {}                     // 少数网关需要额外请求头时填，如 { "x-opencode-client": "..." }
  },
  "review": {                          // 【文本模型】负责事后审查清洗（纯文本，不看图）；留空则复用 provider
    "base_url": "",                   // 留空 = 用 provider 的 base_url（可换更便宜/更快的小模型，如 qwen3.8-flash）
    "api_key": "",                    // 留空 = 用 provider 的 api_key
    "model": "",                      // 留空 = 复用 provider（此时仅是不传图，仍是同一模型）
    "headers": {}
  },
  "dpi": 170,                         // 页面渲染分辨率：越大越清晰也越费 token/越慢；130~170 够用，慢了就调低
  "concurrency": 6,                   // 并发请求数：越大越快；被 429 就调小（qwen3.5-ocr 限额高，可到 8~10）
  "max_retries": 3,                   // 单页失败重试次数
  "timeout": 240,                     // 单请求超时（秒）
  "max_tokens": 8000,                 // 单页转写最大输出 token
  "extra_body": {},                   // 额外请求体，透传给 /chat/completions；关思考可填 {"enable_thinking": false}

  "skip_front_matter": true,          // 书籍默认：跳过「目录」之前的前置内容（封面/编者的话/前言/版权页），从目录页开始转写
  "start_page": null,                 // 显式指定起始页（1 起）；非 null 时优先于自动探测
  "toc_scan_limit": 30,               // 自动探测目录页时最多向前扫描多少页
  "toc_max_pages": 6,                 // 目录页最多连续探测多少页（供自动生成章节）
  "auto_chapters": false,             // true = 读目录页自动生成 chapters（也可命令行 --auto-chapters）

  "use_text_layer": true,             // 有文本层的页直接抽取，跳过视觉调用（省时省钱）；--force-ocr 强制全视觉
  "text_layer_min_chars": 30,         // 文本层去空白后至少这么多字符，才认定该页为「文本页」
  "requality": true,                  // 低质量页（过短/含「不清晰」）自动提高 DPI 重跑一次
  "requality_min_chars": 30,
  "requality_dpi_boost": 1.4,
  "figures": false,                   // true = 导出页内「非整页」插图到 images/ 并引用（默认关）

  "cleanup": "llm",                   // 转写后清洗残留水印/噪声：off / rules(查重,免费) / llm(模型复查) / both
  "cleanup_repeat_ratio": 0.3,        // rules：某行在 >=max(cleanup_min_pages, 0.3×页数) 页重复出现即判为噪声删掉
  "cleanup_min_pages": 5,

  "chapters": [                       // 按章拆分；留空数组 = 整本合成一个 .md
    { "name": "第一章 绪论", "from": 1, "to": 72 },
    { "name": "第二章 线性表", "from": 73, "to": 140 }
  ]
}
```

> **分工（转写 vs 审查）**：`provider` = **视觉模型**，负责「看图转写」（请求带图片）；`review` = **文本模型**，负责事后「审查清洗」（请求**纯文本、不带图**）。默认审查复用 `provider`（此时只是不传图，仍是同一个模型）；想分工更彻底，就给 `review` 单独配一个**更快更便宜的文本模型**（审查不需要看图能力，例如 `qwen3.8-flash`）。

**前置内容（封面 / 编者的话 / 感谢信之类）默认跳过**：书籍默认 `skip_front_matter: true`，脚本会**自动探测「目录」页**并从该页开始转写，跳过它之前的封面、扉页、前言、编者的话、版权页等。探测结果缓存在 `.cache/_start_page.json`（重跑不重复探测）。

- 想自己指定起点：`"start_page": 9`（第 9 页起）；或命令行 `--start-page 9`。
- 想连前置内容一起转：`"skip_front_matter": false`；或命令行 `--keep-front-matter`。
- 已用 `chapters` 明确从目录之后开始（首章 `from > 1`）时，脚本不再自动探测。

**转写后清洗残留水印（`cleanup`）**：转写时已要求模型别管水印，但扫描件水印形态多，仍有残留。脚本转写完成后会再清洗一遍，结果另存 `.cache/pNNNN.clean.md`，拼装时优先用它。

| 模式 | 做法 | 成本 | 适用 |
|---|---|---|---|
| `off` | 不清洗 | 0 | 不需要 |
| `rules` | 跨页**查重**：某行在多页重复出现且短、无标点 → 判为噪声行删掉 | 免费、离线 | 重复水印/页眉水印 |
| `llm` | 逐页把文本发给模型**复查**，只删明显水印，其余一字不改 | 每页一次纯文本调用（便宜） | 默认，最贴近「复查后删除」 |
| `both` | 先 `rules` 再 `llm` | `rules`+每页调用 | 水印较多时最干净 |

- 命令行：`--cleanup both` / `--cleanup off`；或 `--no-cleanup`（等于 off）。
- 默认 `llm`。模型被严格约束「只删水印、不确定就保留」，一般不会动正文。
- 清洗结果有缓存：重跑不会重复调用。**改了 `cleanup` 模式想重做**，先删掉 `.cache/*.clean.md` 再跑。

**章页区间怎么定？** 扫描版没有可靠的书签目录，最稳的是你自己看书目录/翻页确定每章的**物理页序**（PDF 打开后的第几页，从 1 开始，不是书上的印刷页码）。不确定就先不带 `chapters` 整本跑一份，看 `## pN` 小节里哪页开始是新章，再回来拆。**也可以直接让脚本自动生成（见下）。**

### 4.5 自动化与省流

**自动生成章节（`--auto-chapters`）**：脚本探测连续的目录页 → 让模型把目录读成 `[{"title","page"}]` → 用「正文首页 = 目录页之后第一页」推出 `物理页 = 印刷页码 + offset` → 自动填 `chapters`。结果缓存在 `.cache/_auto_chapters.json`，日志会打印 `offset` 与每章区间**供你核对**。**局限**：offset 是启发式，若目录后还夹着前言/扉页，偏移会偏；核对日志不对就手改 `chapters`（有 `chapters` 时不会自动生成）。

**有文本层的页直接抽取**：默认 `use_text_layer: true`——若某页本就有可提取文本（混合型/电子版 PDF），直接 `get_text()`，省下视觉调用；`--force-ocr` 可强制全部走视觉模型（例如想统一拿到公式 LaTeX）。纯扫描件无文本层，不受影响。

**低质量页自动重跑（`requality`）**：转写后扫描「过短」或含「不清晰」的视觉转写页，自动提高 DPI（`requality_dpi_boost`）重跑一次。默认开；`--no-requality` 关。

**缓存指纹**：`.cache/_fingerprint.json` 记录 `dpi / model / system 提示 / max_tokens / use_text_layer / figures`。改了这些再重跑时脚本会**打印警告**（旧缓存可能不再适用），但**不自动删**；确认要按新配置重跑就加 `--reset-cache`。

**自适应并发**：命中 HTTP 429 时自动**下调并发**，静默一段时间后再逐步回升——不必手动调 `concurrency`。

**导出插图（`--figures`）**：把页内**非整页**的嵌入图片导出到 `{out_dir}/images/`，并在该页正文后附 `- ![](images/pNNNN-1.png)` 引用。纯扫描件整页就是一张图（面积占比 ≥90%）会被跳过，不会导出一堆整页图。默认关。

---

## 5. 输出长什么样

```
{out_dir}/
  00-目录.md                # 章节索引 + 失败页清单
  01-第一章 绪论.md
  02-第二章 线性表.md
  .cache/                   # 逐页缓存 p0001.md ...（断点续跑用，勿删）；p0001.clean.md=清洗后文本
```

每个章节文件形如：

```markdown
# book · 第一章 绪论（p1-72）

> 由视觉模型逐页转写（外部 ocr_pdf skill） · 2026-10-08 · 模型 qwen-vl-max
> 页码为 PDF 物理页序。本文件可直接编辑修正，AI 教学按修正版引用。

## p1

（该页转写的 Markdown……）

## p2
...
```

---

## 6. 断点续跑 / 失败重试（重要）

- 每一页转写完就写进 `.cache/pNNNN.md`。**重跑同一条命令只会补缺失/失败的页**，已完成的页零重复消耗。
- 跑完若日志出现 `[WARN] N page(s) failed`，直接**再跑一次同样命令**即可只补那几页。
- 个别页一直失败（超时/限流/模型拒绝）时：调小 `concurrency`、调大 `timeout`，或换更强的视觉模型。
- 想强制重转某些页：删掉对应的 `.cache/pNNNN.md` 再跑。
- 500 页属于长任务（可能几十分钟到数小时，取决于并发与模型），建议**按章分批**跑、先跑一章验证质量再全量。

---

## 7. 转回 Phrontis AI 教学

1. 打开拼好的章节 `.md` 快速检视（公式/表格是否符合预期，必要时手工修正）。
2. 把 `.md` 文件放进仓库（或任意可读目录），在「AI 教学 → 素材库」里**登记为 `md` 类型素材**（或直接把文件放进工作区文件夹）。
3. 之后 AI 教学中 AI 用 `vault.read` 直接读文本，**不再需要逐页视觉转写**。

> 命名上软件内的自动提取稿是 `{素材名}-p{起}-{终}.md`；本 skill 输出的 `01-第一章 绪论.md` 是给你自己看的独立稿，登记时按你习惯命名即可，不冲突。

---

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `缺少 PyMuPDF / requests` | 先激活 venv 再 `pip install -r requirements.txt` |
| `HTTP 401 / 403` | Key 不对或没有该模型权限 |
| `HTTP 404` | `base_url` 少了/多了 `/v1`；脚本会自动拼 `/chat/completions`，base_url 不要带这截 |
| `HTTP 429` | 并发太高，调小 `concurrency`，脚本对 429 会退避重试 |
| 转写内容乱/编造 | 换更强的视觉模型；模糊页模型会标注「不清晰」 |
| 某页是整页大图无字 | 属正常，纯插图页会跳过或仅一句带过 |
| 题目是「图像选择题」（A/B/C/D 是四幅图） | 不会丢：指令要求模型为每个图形选项各给一句文字描述，保证题目可判读 |
| 输出里出现 `![](image)` 坏图 | 不会——已禁止图片语法，残余的坏图占位会被脚本自动剥离 |
| 中文在控制台报 `UnicodeEncodeError` | 脚本已强制 UTF-8 输出；若仍报错，先 `chcp 65001` |
| 明明改了模型 / DPI 却像没生效 | 逐页缓存被复用了；脚本会打印「缓存指纹不匹配」警告，确认后加 `--reset-cache` 重跑 |
| 自动生成的章节错位 | offset 启发式偏了；看日志里的 `offset`，手动改 `chapters` 覆盖 |
| 想强制全部走视觉（含文本层页） | 加 `--force-ocr` |

---

## 9. 文件清单

| 文件 | 说明 |
|---|---|
| `ocr_pdf.py` | 主脚本（渲染 + 调视觉模型 + 按章拼装 + 断点续跑） |
| `inspect_app_providers.py` | 只读辅助：列出软件里的 base_url 与视觉候选模型名（读不到 Key） |
| `config.example.json` | 配置模板，复制成 `config.json` 再改（`config.json` 已被 gitignore） |
| `requirements.txt` | Python 依赖（PyMuPDF / requests） |
| `pyproject.toml` | 打包 / console_script（`scanned-pdf-ocr`、`ocr-providers`） |
| `run.bat` | Windows 一键（建 venv + 装依赖 + 按 `config.json` 跑） |
| `README.md` | 面向 GitHub 的项目说明 |
| `LICENSE` | MIT |
| `.gitignore` | 排除 `config.json` / `.venv` / `*-md/` / `.cache/` 等 |
| `SKILL.md` | 本说明 |
