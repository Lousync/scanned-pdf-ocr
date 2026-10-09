#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scanned PDF -> Markdown, via a vision LLM (OpenAI-compatible chat/completions).

Why: a scanned PDF has no text layer, so normal extraction yields nothing.
This script renders each page to an image, sends it to a vision model, and
writes the model's Markdown transcription. The output is meant for the AI to
read (not for humans): text/formulas/tables are transcribed faithfully, while
merely illustrative figures (e.g. function graphs) are skipped. The .md files
can be registered as teaching materials and read by the in-app AI directly.

Usage:
    python ocr_pdf.py --config config.json
    python ocr_pdf.py --pdf "D:/book.pdf" --out "D:/out" \
        --base-url "https://dashscope.aliyuncs.com/compatible-mode/v1" \
        --api-key "sk-xxx" --model "qwen-vl-max"

Only standard library + PyMuPDF + requests. Python 3.8+.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

try:
    import fitz  # PyMuPDF
except Exception:
    print("[FAIL] missing PyMuPDF. run: pip install -r requirements.txt")
    sys.exit(1)

try:
    import requests
except Exception:
    print("[FAIL] missing requests. run: pip install -r requirements.txt")
    sys.exit(1)

# Force UTF-8 on Windows console (default GBK would crash on Chinese output).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Same instruction (slightly adapted) the Phrontis app uses for vision
# transcription. Key adaptation: the output is ONLY for the AI to read, humans
# almost never read it, so illustrative figures need not be reproduced.
VISION_SYSTEM = (
    "你是教材视觉转写助手。产出仅供 AI 阅读与教学复用，读者几乎不会直接看这份稿子，"
    "因此**不必追求视觉还原**，只求文字信息完整准确。请把页面转写为结构化 Markdown："
    "① 正文文字、标题层级、题号与题干完整转写；"
    "② 公式一律用 LaTeX（行内 $…$，独立公式 $$…$$）；"
    "③ 表格转 Markdown 表格（表格承载关键信息，必须转写）；"
    "④ 纯辅助理解的示意图 / 函数图像 / 装饰性插图**默认省略**，无需描述其外观；"
    "**但有一类必须保住：若某道题（尤其选择题）的题干或选项本身就是图**（例如 A/B/C/D 四幅函数图像），"
    "必须为每个图形选项各给一句极简客观描述（如「A：开口向上的抛物线，顶点在原点，对称轴为 y 轴」），"
    "保证题目完整可判读——绝不能省略成空或占位符；"
    "其余承载正文未说明关键信息的插图，用一句【图：…】客观点明要点；"
    "⑤ 页面上的**水印一律不转写、不提及**（半透明文字、LOGO、印章水印、重复出现的机构名/网址水印等），"
    "把它们当作不存在，绝不能让水印内容混进正文；"
    "⑥ **禁止输出任何图片语法**（`![...](...)` 或 `<img>` 标签）——产物里没有图片文件，写了就是坏图；"
    "需要表达图形一律用文字描述或上面的【图：…】。"
    "看不清的内容标注（不清晰），**严禁编造或补全**。直接输出该页 Markdown，不要任何开场白或评论。"
)

_print_lock = threading.Lock()
# PyMuPDF: a single Document is NOT thread-safe. Rendering happens under this
# lock; the (slow) HTTP call stays outside it, so parallelism is preserved.
_doc_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# Global adaptive concurrency limiter, installed in main(). It caps the number of
# in-flight HTTP requests and shrinks on HTTP 429, then slowly recovers, so the
# pipeline backs off automatically instead of hammering a rate-limited endpoint.
_limiter = None


class AdaptiveConcurrency:
    def __init__(self, initial: int, minimum: int = 1, maximum=None):
        self.minimum = max(1, minimum)
        self.maximum = max(self.minimum, maximum or max(initial, 1) * 2)
        self._limit = max(self.minimum, min(initial, self.maximum))
        self._active = 0
        self._cond = threading.Condition()
        self._last_penalty = 0.0

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    def __enter__(self):
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1
        return self

    def __exit__(self, *exc):
        with self._cond:
            self._active -= 1
            self._cond.notify()
        return False

    def penalize(self) -> None:
        with self._cond:
            self._last_penalty = time.time()
            if self._limit > self.minimum:
                self._limit -= 1
                log("[THROTTLE] 命中 429 → 并发降到 %d" % self._limit)

    def reward(self) -> None:
        with self._cond:
            # only recover after a quiet period, to avoid flapping
            if time.time() - self._last_penalty < 10.0:
                return
            if self._limit < self.maximum:
                self._limit += 1
                self._cond.notify()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _default_config() -> dict:
    return {
        "pdf": "",
        "out_dir": "",
        "provider": {
            "base_url": "",
            "api_key": "",
            "model": "",
            "headers": {},
        },
        # 事后审查（清洗残留水印）用的模型——**纯文本**调用，与看图转写分工。
        # 留空（model 为空）则复用上面的 provider（此时仅是不传图，仍是同一模型）。
        "review": {
            "base_url": "",
            "api_key": "",
            "model": "",
            "headers": {},
        },
        "dpi": 170,
        "concurrency": 6,
        "max_retries": 3,
        "timeout": 240,
        "max_tokens": 8000,
        # 额外请求体字段，按需透传给 /chat/completions。例：Qwen-VL 思考模型关掉思考
        # {"enable_thinking": false}（可显著提速）；不确定就留空 {}。
        "extra_body": {},
        # 书籍前置内容处理：默认从「目录」页开始转写，跳过封面 / 编者的话 / 前言 / 版权页等。
        # start_page 若非 null 则优先使用（显式指定起始页，1 起）；否则自动探测目录页。
        "skip_front_matter": True,
        "start_page": None,
        # 自动探测目录页时最多向前扫描多少页
        "toc_scan_limit": 30,
        # 目录页最多连续探测多少页（供自动生成章节使用）
        "toc_max_pages": 6,
        # 从目录页自动生成 chapters（默认关，可用 --auto-chapters 打开）
        "auto_chapters": False,
        # 有文本层的页直接抽取，跳过视觉调用（省时省钱）；--force-ocr 可强制全视觉
        "use_text_layer": True,
        # 文本层去空白后至少这么多字符，才认定为「文本页」
        "text_layer_min_chars": 30,
        # 低质量页（过短 / 含「不清晰」）自动提高 DPI 重跑一次
        "requality": True,
        "requality_min_chars": 30,
        "requality_dpi_boost": 1.4,
        # 导出页内「非整页」插图到 out_dir/images/（默认关）
        "figures": False,
        # 转写后清洗残留水印/噪声：off / rules（查重，免费）/ llm（模型复查）/ both
        "cleanup": "llm",
        # rules 模式：某行在 >= max(cleanup_min_pages, ratio*页数) 页重复出现，则视为噪声行删掉
        "cleanup_repeat_ratio": 0.3,
        "cleanup_min_pages": 5,
        # If empty -> whole PDF becomes a single .md (with ## pN markers).
        "chapters": [],
    }


def load_config(args: argparse.Namespace) -> dict:
    cfg = _default_config()
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            user = json.load(f)
        _deep_update(cfg, user)
    # CLI overrides
    if args.pdf:
        cfg["pdf"] = args.pdf
    if args.out:
        cfg["out_dir"] = args.out
    if args.base_url:
        cfg["provider"]["base_url"] = args.base_url
    if args.api_key:
        cfg["provider"]["api_key"] = args.api_key
    if args.model:
        cfg["provider"]["model"] = args.model
    if args.dpi:
        cfg["dpi"] = args.dpi
    if args.concurrency:
        cfg["concurrency"] = args.concurrency
    if args.pages:
        cfg["chapters"] = [{"name": "", "from": args.pages[0], "to": args.pages[1]}]
        cfg["start_page"] = args.pages[0]      # 显式区间：不再跳过前置内容
        cfg["skip_front_matter"] = False
    if getattr(args, "start_page", None):
        cfg["start_page"] = args.start_page
    if getattr(args, "keep_front_matter", False):
        cfg["skip_front_matter"] = False
    if getattr(args, "no_cleanup", False):
        cfg["cleanup"] = "off"
    if getattr(args, "cleanup", None):
        cfg["cleanup"] = args.cleanup
    if getattr(args, "no_thinking", False):
        eb = cfg.setdefault("extra_body", {})
        if not isinstance(eb, dict):
            eb = {}
            cfg["extra_body"] = eb
        eb["enable_thinking"] = False
    if getattr(args, "force_ocr", False):
        cfg["use_text_layer"] = False
    if getattr(args, "auto_chapters", False):
        cfg["auto_chapters"] = True
    if getattr(args, "figures", False):
        cfg["figures"] = True
    if getattr(args, "no_requality", False):
        cfg["requality"] = False
    if getattr(args, "reset_cache", False):
        cfg["_reset_cache"] = True
    # env fallback for the key (avoids putting it in a file)
    if not cfg["provider"]["api_key"]:
        cfg["provider"]["api_key"] = os.environ.get("VISION_API_KEY", "")
    if not cfg["out_dir"]:
        base = os.path.splitext(os.path.basename(cfg["pdf"]))[0] if cfg["pdf"] else "ocr-out"
        cfg["out_dir"] = os.path.join(os.path.dirname(os.path.abspath(cfg["pdf"] or ".")), base + "-md")
    return cfg


def _deep_update(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def validate(cfg: dict) -> None:
    problems = []
    if not cfg["pdf"] or not os.path.isfile(cfg["pdf"]):
        problems.append(f"pdf 不存在：{cfg['pdf']!r}")
    if not cfg["provider"]["base_url"]:
        problems.append("provider.base_url 为空")
    if not cfg["provider"]["api_key"]:
        problems.append("provider.api_key 为空（可在 config.json 填，或设环境变量 VISION_API_KEY）")
    if not cfg["provider"]["model"]:
        problems.append("provider.model 为空（如 qwen-vl-max / glm-4v / gpt-4o）")
    if problems:
        log("[FAIL] 配置有问题：")
        for p in problems:
            log("  - " + p)
        sys.exit(2)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_page_dataurl(doc, page_no: int, dpi: int) -> str:
    """Render 1-based page_no to a JPEG data URL."""
    with _doc_lock:
        page = doc.load_page(page_no - 1)
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    try:
        data = pix.tobytes("jpeg")
        mime = "image/jpeg"
    except Exception:
        data = pix.tobytes("png")
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return "data:%s;base64,%s" % (mime, b64)


def page_text_layer(doc, page_no: int) -> str:
    """Embedded text layer of a page ('' for a pure scan)."""
    with _doc_lock:
        page = doc.load_page(page_no - 1)
        return page.get_text("text")


def extract_figures(cfg: dict, doc, page_no: int) -> list:
    """Export a page's *non-full-page* embedded images. Returns relative paths.

    For a pure scan the whole page is a single full-page image -> filtered out by
    the area-ratio test, so nothing is exported. Hybrid / digital PDFs that embed
    real figures do get them.
    """
    rels = []
    try:
        with _doc_lock:
            page = doc.load_page(page_no - 1)
            page_area = abs(page.rect.width * page.rect.height) or 1.0
            infos = page.get_image_info(xrefs=True)
            img_dir = os.path.join(cfg["out_dir"], "images")
            for i, info in enumerate(infos, 1):
                xref = int(info.get("xref") or 0)
                bbox = info.get("bbox") or (0, 0, 0, 0)
                area = abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                if not xref or area <= 0 or area / page_area >= 0.9:
                    continue  # full-page background scan -> skip
                base = doc.extract_image(xref)
                if not base or not base.get("image"):
                    continue
                ext = base.get("ext", "png")
                os.makedirs(img_dir, exist_ok=True)
                rel = "images/p%04d-%d.%s" % (page_no, i, ext)
                with open(os.path.join(cfg["out_dir"], rel), "wb") as f:
                    f.write(base["image"])
                rels.append(rel)
    except Exception as e:  # noqa: BLE001
        log("[WARN] 第 %d 页插图导出失败：%s" % (page_no, e))
    return rels


def _src_path(cache_dir: str, page_no: int) -> str:
    return os.path.join(cache_dir, "p%04d.src" % page_no)


def _write_src(cache_dir: str, page_no: int, source: str) -> None:
    try:
        with open(_src_path(cache_dir, page_no), "w", encoding="utf-8") as f:
            f.write(source)
    except Exception:
        pass


def _read_src(cache_dir: str, page_no: int) -> str:
    p = _src_path(cache_dir, page_no)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return "vision"


def _write_fig_fragment(cache_dir: str, page_no: int, rels: list) -> None:
    dst = os.path.join(cache_dir, "p%04d.fig.md" % page_no)
    if not rels:
        if os.path.isfile(dst):
            try:
                os.remove(dst)
            except Exception:
                pass
        return
    try:
        with open(dst, "w", encoding="utf-8") as f:
            f.write("（本页插图）\n" + "\n".join("- ![](%s)" % r for r in rels))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# cache fingerprint: warn when config changes invalidate the page cache
# ---------------------------------------------------------------------------

def _fingerprint(cfg: dict) -> dict:
    return {
        "dpi": int(cfg["dpi"]),
        "model": cfg["provider"]["model"],
        "base_url": cfg["provider"]["base_url"],
        "max_tokens": int(cfg["max_tokens"]),
        "system_sha1": hashlib.sha1(VISION_SYSTEM.encode("utf-8")).hexdigest(),
        "use_text_layer": bool(cfg.get("use_text_layer", True)),
        "figures": bool(cfg.get("figures", False)),
    }


def check_fingerprint(cfg: dict, cache_dir: str) -> None:
    fp_file = os.path.join(cache_dir, "_fingerprint.json")
    cur = _fingerprint(cfg)
    old = None
    if os.path.isfile(fp_file):
        try:
            with open(fp_file, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = None
    if isinstance(old, dict):
        changed = {k: (old.get(k), cur.get(k)) for k in cur if old.get(k) != cur.get(k)}
        if changed:
            log("[WARN] 缓存指纹不匹配（配置已改动）——旧缓存可能不再适用：")
            for k, (a, b) in changed.items():
                log("       %s: %s -> %s" % (k, a, b))
            log("       想按新配置重跑就加 --reset-cache；否则忽略本警告，继续复用旧缓存。")
            return  # keep the old fingerprint so the warning repeats until resolved
    try:
        with open(fp_file, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# vision call
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # drop the first line (```markdown / ```md / ```) and trailing ```
        lines = t.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_IMG_HTML_RE = re.compile(r"<img\b[^>]*>", re.I)
_IMG_PLACEHOLDER_RE = re.compile(r"^\s*(\[(image|图片|图)\]|image|图片)\s*$", re.I)


def _strip_images(text: str) -> str:
    """Drop image markdown / <img> (there are no image files -> they'd render as broken
    images) and bare 'image' placeholder lines."""
    lines = []
    for ln in text.split("\n"):
        if _IMG_PLACEHOLDER_RE.match(ln):
            continue  # whole line is just a placeholder -> drop it
        if _IMG_MD_RE.search(ln) or _IMG_HTML_RE.search(ln):
            rest = _IMG_HTML_RE.sub("", _IMG_MD_RE.sub("", ln)).strip()
            if rest:  # keep any real text that shared the line with the image
                lines.append(rest)
            continue
        lines.append(ln)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out


def _vision_request(cfg: dict, system: str, user_text: str, data_urls, session,
                    max_tokens=None, provider=None) -> str:
    p = provider or cfg["provider"]
    url = p["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + p["api_key"],
        "Content-Type": "application/json",
    }
    if p.get("headers"):
        headers.update(p["headers"])
    content = [{"type": "text", "text": user_text}]
    for u in data_urls:
        content.append({"type": "image_url", "image_url": {"url": u}})
    payload = {
        "model": p["model"],
        "max_tokens": int(max_tokens or cfg["max_tokens"]),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    if cfg.get("extra_body"):
        for k, v in cfg["extra_body"].items():
            payload[k] = v
    retries = int(cfg["max_retries"])
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            if _limiter is not None:
                with _limiter:
                    resp = session.post(url, headers=headers, json=payload, timeout=int(cfg["timeout"]))
            else:
                resp = session.post(url, headers=headers, json=payload, timeout=int(cfg["timeout"]))
            if resp.status_code == 429:
                if _limiter is not None:
                    _limiter.penalize()
                wait = 5 * attempt
                try:
                    wait = max(wait, int(resp.headers.get("Retry-After", wait)))
                except Exception:
                    pass
                last_err = "HTTP 429 (rate limited)"
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                last_err = "HTTP %d: %s" % (resp.status_code, resp.text[:300])
                # 5xx / transient -> retry; 4xx -> retry a couple times then give up
                time.sleep(2 * attempt)
                continue
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                last_err = "no choices in response: %s" % json.dumps(data)[:300]
                time.sleep(2 * attempt)
                continue
            out = choices[0].get("message", {}).get("content", "")
            if isinstance(out, list):
                out = "".join(
                    part.get("text", "") for part in out if isinstance(part, dict)
                )
            text = str(out).strip()
            if not text:
                last_err = "empty response"
                time.sleep(2 * attempt)
                continue
            if _limiter is not None:
                _limiter.reward()
            return text
        except Exception as e:  # noqa: BLE001
            last_err = "%s: %s" % (type(e).__name__, e)
            time.sleep(2 * attempt)
    raise RuntimeError(last_err or "unknown error")


def call_vision(cfg: dict, data_url: str, page_no: int, session: requests.Session) -> str:
    text = _vision_request(cfg, VISION_SYSTEM, "这是教材第 %d 页，请转写整页。" % page_no,
                           [data_url], session)
    return _strip_images(_strip_fences(text))


TOC_SYSTEM = "你是版面判别助手。只依据图片内容回答一个词，不要解释、不要转写页面内容。"
TOC_PROMPT = ("这一页是不是书籍的「目录」页？目录页的典型特征是：标题为「目录」「目 录」「Contents」等，"
              "正文是多行「章节名 …… 页码」的条目列表。封面 / 扉页 / 前言 / 编者的话 / 版权页 / 正文页都不是目录页。"
              "只回答：是 或 否。")


def is_toc_page(cfg: dict, data_url: str, page_no: int, session: requests.Session) -> bool:
    """Return True if this page is the book's table-of-contents page."""
    r = _vision_request(cfg, TOC_SYSTEM, TOC_PROMPT + "（第 %d 页）" % page_no,
                        [data_url], session, max_tokens=16).strip()
    low = r.lower()
    if "目录" in r or "contents" in low:
        return True
    if r.startswith("是") or low.startswith("yes"):
        return True
    return False


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def cache_path(cache_dir: str, page_no: int) -> str:
    return os.path.join(cache_dir, "p%04d.md" % page_no)


def process_page(cfg: dict, cache_dir: str, page_no: int, doc, session) -> tuple:
    """Returns (page_no, ok, error). Cached pages are skipped."""
    dst = cache_path(cache_dir, page_no)
    if os.path.isfile(dst):
        return (page_no, True, "")
    try:
        if cfg.get("figures"):
            _write_fig_fragment(cache_dir, page_no, extract_figures(cfg, doc, page_no))

        text = None
        source = "vision"
        if cfg.get("use_text_layer", True):
            raw = page_text_layer(doc, page_no)
            if len(re.sub(r"\s+", "", raw)) >= int(cfg.get("text_layer_min_chars", 30)):
                text = _strip_images(raw).strip()
                source = "text-layer"
        if text is None:
            data_url = render_page_dataurl(doc, page_no, int(cfg["dpi"]))
            text = call_vision(cfg, data_url, page_no, session)

        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dst)
        _write_src(cache_dir, page_no, source)
        return (page_no, True, "")
    except Exception as e:  # noqa: BLE001
        return (page_no, False, str(e))


def chapter_ranges(cfg: dict, total: int, start: int = 1) -> list:
    chs = cfg.get("chapters") or []
    out = []
    for i, c in enumerate(chs):
        frm = max(1, start, int(c.get("from", 1)))
        to = min(total, int(c.get("to", total)))
        if frm > to:
            continue
        name = str(c.get("name", "")).strip() or ("第%d部分" % (i + 1))
        out.append({"name": name, "from": frm, "to": to})
    if not out:
        out = [{"name": os.path.splitext(os.path.basename(cfg["pdf"]))[0], "from": max(1, start), "to": total}]
    return out


def resolve_start_page(cfg: dict, doc, total: int, cache_dir: str) -> int:
    """First page (1-based) to transcribe.

    Explicit ``start_page`` wins; else auto-detect the 目录 page so cover /
    preface / editor's-note / copyright front matter is skipped by default.
    """
    sp = cfg.get("start_page")
    if sp:
        return max(1, min(int(sp), total))
    if not cfg.get("skip_front_matter", True):
        return 1
    # 用户已用 chapters 明确从目录之后开始（首章 from > 1）→ 不再自动探测
    chs = cfg.get("chapters") or []
    if chs and min(int(c.get("from", 1)) for c in chs) > 1:
        return 1
    cache_file = os.path.join(cache_dir, "_start_page.json")
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                v = int(json.load(f).get("start_page", 0))
            if v > 0:
                log("[INFO] 前置内容：起始页 = p%d（缓存）" % v)
                return max(1, min(v, total))
        except Exception:
            pass
    limit = min(total, max(1, int(cfg.get("toc_scan_limit", 30))))
    conc = max(1, min(int(cfg.get("concurrency", 6)), 8))
    start, found = 1, False
    n0 = 1
    while n0 <= limit:
        batch = list(range(n0, min(n0 + conc - 1, limit) + 1))
        urls = [(n, render_page_dataurl(doc, n, int(cfg["dpi"]))) for n in batch]
        hits = []
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futs = {pool.submit(is_toc_page, cfg, u, n, requests.Session()): n for n, u in urls}
            for fut in as_completed(futs):
                n = futs[fut]
                try:
                    if fut.result():
                        hits.append(n)
                except Exception as e:  # noqa: BLE001
                    log("[WARN] 第 %d 页目录判别失败：%s" % (n, e))
        if hits:
            start, found = min(hits), True
            log("[INFO] 检测到目录页：p%d → 从该页开始转写（跳过其前的前置内容）" % start)
            break
        n0 += conc
    if not found:
        log("[INFO] 前 %d 页未找到目录页 → 从 p1 开始" % limit)
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"start_page": start}, f)
    except Exception:
        pass
    return start


# ---------------------------------------------------------------------------
# auto chapters: read the TOC page(s) and derive chapter ranges
# ---------------------------------------------------------------------------

TOC_EXTRACT_SYSTEM = "你是目录解析助手。只输出 JSON，不要解释、不要代码围栏、不要任何额外文字。"
TOC_EXTRACT_PROMPT = (
    "这是一本书目录页的图片。请提取其中的**一级章节条目**（最左侧、层级最高的标题；"
    "不要子小节 / 子标题），按出现顺序输出 JSON 数组："
    '[{"title":"章节名","page":印刷页码整数}, ...]。'
    "page 用该条目在目录里标注的页码（阿拉伯数字）。若某项没有页码，page 填 0。只输出 JSON 数组。"
)


def _parse_toc_entries(text: str) -> list:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    if isinstance(arr, list):
        for e in arr:
            if not isinstance(e, dict):
                continue
            title = str(e.get("title", "")).strip()
            try:
                page = int(e.get("page", 0))
            except Exception:
                page = 0
            if title:
                out.append({"title": title, "page": page})
    return out


def detect_toc_pages(cfg: dict, doc, total: int, start: int) -> list:
    """Consecutive 目录 pages starting at ``start``."""
    max_pages = max(1, int(cfg.get("toc_max_pages", 6)))
    pages = []
    for n in range(start, min(total, start + max_pages - 1) + 1):
        session = requests.Session()
        try:
            url = render_page_dataurl(doc, n, int(cfg["dpi"]))
            if is_toc_page(cfg, url, n, session):
                pages.append(n)
            else:
                break
        except Exception as e:  # noqa: BLE001
            log("[WARN] 目录页判别失败 p%d：%s" % (n, e))
            break
    return pages or [start]


def auto_chapters(cfg: dict, doc, total: int, start: int, cache_dir: str) -> list:
    """Read the TOC page(s) and derive chapter ranges (physical pages).

    Offset heuristic: the first content page (right after the TOC pages) is
    assumed to carry the first entry's printed page number, giving
    ``physical = printed + offset``.
    """
    cache_file = os.path.join(cache_dir, "_auto_chapters.json")
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            chs = data.get("chapters") or []
            if chs:
                log("[INFO] 自动章节（缓存）：%d 章" % len(chs))
                return chs
        except Exception:
            pass
    toc_pages = detect_toc_pages(cfg, doc, total, start)
    log("[INFO] 目录页：%s" % ", ".join("p%d" % p for p in toc_pages))
    entries = []
    for n in toc_pages:
        session = requests.Session()
        try:
            url = render_page_dataurl(doc, n, int(cfg["dpi"]))
            raw = _vision_request(cfg, TOC_EXTRACT_SYSTEM,
                                  TOC_EXTRACT_PROMPT + "（第 %d 页）" % n,
                                  [url], session, max_tokens=2000)
            entries.extend(_parse_toc_entries(raw))
        except Exception as e:  # noqa: BLE001
            log("[WARN] 目录解析失败 p%d：%s" % (n, e))
    valid = [e for e in entries if e["page"] > 0]
    if not valid:
        log("[WARN] 未能从目录页解析出章节 → 回退整本单文件")
        return []
    content_start = toc_pages[-1] + 1
    offset = content_start - valid[0]["page"]
    chapters = []
    for i, e in enumerate(valid):
        frm = e["page"] + offset
        to = (valid[i + 1]["page"] + offset - 1) if i + 1 < len(valid) else total
        frm = max(1, min(frm, total))
        to = max(frm, min(to, total))
        chapters.append({"name": e["title"], "from": frm, "to": to})
    log("[INFO] 自动章节：offset=%d（印刷页码 + %d = 物理页）" % (offset, offset))
    for c in chapters:
        log("       %s（p%d-%d）" % (c["name"], c["from"], c["to"]))
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"chapters": chapters, "offset": offset, "toc_pages": toc_pages},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return chapters


# ---------------------------------------------------------------------------
# cleanup: remove residual watermark / scan noise from the transcription
# ---------------------------------------------------------------------------

CLEAN_SYSTEM = (
    "你是扫描书稿的清洗助手。下面这段 Markdown 是 OCR 转写结果，可能残留**扫描水印/噪声**文字"
    "（例如：半透明印章字、机构或出版社水印、重复出现的网址/学校名、'xx 影印'、装饰性重复符号、孤立的乱码字符）。"
    "你的任务：**只删除这些水印/噪声**，其它一切内容——正文、标题、公式（LaTeX）、表格、题号与题干、标点——**必须一字不改地保留**。"
    "严禁改写、翻译、润色、总结、补全或调整顺序。任何你不确定是否属于水印的文字，一律保留。"
    "只输出清洗后的 Markdown 正文本身，不要任何解释、说明或代码围栏。若本段没有水印，就原样返回。"
)


def clean_path(cache_dir: str, page_no: int) -> str:
    return os.path.join(cache_dir, "p%04d.clean.md" % page_no)


def _write_clean(cache_dir: str, page_no: int, text: str) -> None:
    dst = clean_path(cache_dir, page_no)
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, dst)


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _rule_drop_lines(page_texts: dict, ratio: float, min_pages: int) -> set:
    """Lines repeated across many pages that look like boilerplate -> watermark candidates."""
    counts = {}
    for t in page_texts.values():
        for ln in {_norm_line(l) for l in t.split("\n") if l.strip()}:
            counts[ln] = counts.get(ln, 0) + 1
    n = len(page_texts)
    threshold = max(int(min_pages), int(ratio * n))
    drop = set()
    for ln, c in counts.items():
        if not ln or c < threshold:
            continue
        if len(ln) > 30:                         # long lines are unlikely to be a watermark
            continue
        if ln.startswith("#") or "|" in ln or "`" in ln or "\\" in ln:
            continue                             # keep headings / tables / code / LaTeX
        drop.add(ln)
    return drop


def _apply_rule_drop(text: str, drop: set) -> str:
    kept = [l for l in text.split("\n") if _norm_line(l) not in drop]
    return "\n".join(kept).strip()


def _review_provider(cfg: dict) -> dict:
    """Provider for the post-hoc review (cleanup). Uses an explicit text model from
    ``cfg['review']`` when set, otherwise falls back to the transcription provider
    (image omitted -> the call is text-only)."""
    r = cfg.get("review") or {}
    if str(r.get("model", "")).strip():
        base = cfg["provider"]
        return {
            "base_url": str(r.get("base_url") or base.get("base_url") or ""),
            "api_key": str(r.get("api_key") or base.get("api_key") or ""),
            "model": str(r["model"]),
            "headers": r.get("headers") or base.get("headers") or {},
        }
    return cfg["provider"]


def call_cleanup(cfg: dict, page_md: str, session: requests.Session) -> str:
    out = _vision_request(cfg, CLEAN_SYSTEM, page_md, [], session, provider=_review_provider(cfg))
    return _strip_images(_strip_fences(out))


def _clean_one(cfg: dict, cache_dir: str, page_no: int, text: str, session) -> tuple:
    try:
        out = call_cleanup(cfg, text, session)
        if not out:
            return (page_no, False, "empty cleanup output")
        _write_clean(cache_dir, page_no, out)
        return (page_no, True, "")
    except Exception as e:  # noqa: BLE001
        return (page_no, False, str(e))


def run_cleanup(cfg: dict, cache_dir: str, pages: list) -> None:
    """After transcription: strip residual watermarks. Writes `pNNNN.clean.md`."""
    mode = str(cfg.get("cleanup", "llm")).lower()
    if mode not in ("rules", "llm", "both"):
        return
    raw = {}
    for n in pages:
        cp = cache_path(cache_dir, n)
        if os.path.isfile(cp):
            with open(cp, "r", encoding="utf-8") as f:
                raw[n] = f.read().strip()
    if not raw:
        return
    pre = dict(raw)
    if mode in ("rules", "both"):
        drop = _rule_drop_lines(raw, float(cfg.get("cleanup_repeat_ratio", 0.3)),
                                int(cfg.get("cleanup_min_pages", 5)))
        for n in pre:
            pre[n] = _apply_rule_drop(pre[n], drop)
        log("[INFO] cleanup(rules): 删除重复噪声行 %d 条" % len(drop))
    if mode == "rules":
        for n, t in pre.items():
            _write_clean(cache_dir, n, t)
        log("[INFO] cleanup(rules): 完成 %d 页" % len(pre))
        return
    # llm / both: model review per page (resume-safe via .clean.md cache)
    todo = [n for n in raw if not os.path.isfile(clean_path(cache_dir, n))]
    if not todo:
        log("[INFO] cleanup(llm): 全部已清洗（缓存）")
        return
    log("[INFO] cleanup(llm): 审查模型=%s · 待清洗 %d 页" % (_review_provider(cfg)["model"], len(todo)))
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["concurrency"]))) as pool:
        futures = {}
        for n in todo:
            session = requests.Session()
            futures[pool.submit(_clean_one, cfg, cache_dir, n, pre.get(n, raw.get(n, "")), session)] = n
        for fut in as_completed(futures):
            n, ok, err = fut.result()
            done += 1
            if ok:
                log("[cleanup %d/%d] p%d ok" % (done, len(todo), n))
            else:
                log("[cleanup %d/%d] p%d FAILED: %s -> 保留清洗前文本" % (done, len(todo), n, err))
                _write_clean(cache_dir, n, pre.get(n, raw.get(n, "")))


# ---------------------------------------------------------------------------
# requality: retry suspicious pages at a higher DPI
# ---------------------------------------------------------------------------

def _is_low_quality(text: str, min_chars: int) -> bool:
    if "不清晰" in text:
        return True
    return len(re.sub(r"\s+", "", text)) < int(min_chars)


def requality_pages(cfg: dict, cache_dir: str, pages: list, doc) -> list:
    if not cfg.get("requality", True):
        return []
    min_chars = int(cfg.get("requality_min_chars", 30))
    boost = float(cfg.get("requality_dpi_boost", 1.4))
    targets = []
    for n in pages:
        cp = cache_path(cache_dir, n)
        if not os.path.isfile(cp) or _read_src(cache_dir, n) != "vision":
            continue
        try:
            with open(cp, "r", encoding="utf-8") as f:
                t = f.read()
        except Exception:
            continue
        if _is_low_quality(t, min_chars):
            targets.append(n)
    if not targets:
        return []
    hi_dpi = max(1, int(int(cfg["dpi"]) * boost))
    log("[INFO] requality: %d 页疑似低质量 → 以 DPI %d 重跑一次" % (len(targets), hi_dpi))

    def _redo(n):
        session = requests.Session()
        try:
            for p in (cache_path(cache_dir, n), clean_path(cache_dir, n)):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            data_url = render_page_dataurl(doc, n, hi_dpi)
            text = call_vision(cfg, data_url, n, session)
            tmp = cache_path(cache_dir, n) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, cache_path(cache_dir, n))
            _write_src(cache_dir, n, "vision")
            return (n, True, "")
        except Exception as e:  # noqa: BLE001
            return (n, False, str(e))

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["concurrency"]))) as pool:
        futs = {pool.submit(_redo, n): n for n in targets}
        for fut in as_completed(futs):
            n, ok, err = fut.result()
            if ok:
                done += 1
            else:
                log("[WARN] requality p%d 重跑失败：%s" % (n, err))
    log("[INFO] requality: 完成 %d/%d 页" % (done, len(targets)))
    return targets


def assemble(cfg: dict, cache_dir: str, chapters: list, failed: list, model: str) -> list:
    written = []
    base_name = os.path.splitext(os.path.basename(cfg["pdf"]))[0]
    today = date.today().isoformat()
    for idx, ch in enumerate(chapters):
        parts = [
            "# %s · %s（p%d-%d）" % (base_name, ch["name"], ch["from"], ch["to"]),
            "",
            "> 由视觉模型逐页转写（外部 ocr_pdf skill） · %s · 模型 %s" % (today, model or "-"),
            "> 页码为 PDF 物理页序。本文件可直接编辑修正，AI 教学按修正版引用。",
            "",
        ]
        for n in range(ch["from"], ch["to"] + 1):
            parts.append("## p%d" % n)
            parts.append("")
            ccp = clean_path(cache_dir, n)
            cp = ccp if os.path.isfile(ccp) else cache_path(cache_dir, n)
            if os.path.isfile(cp):
                with open(cp, "r", encoding="utf-8") as f:
                    parts.append(f.read().strip())
            else:
                parts.append("（第 %d 页转写失败/未完成）" % n)
            figp = os.path.join(cache_dir, "p%04d.fig.md" % n)
            if os.path.isfile(figp):
                with open(figp, "r", encoding="utf-8") as f:
                    parts.append(f.read().strip())
            parts.append("")
        fname = "%02d-%s.md" % (idx + 1, _safe_name(ch["name"])) if len(chapters) > 1 \
            else "%s-p%d-%d.md" % (_safe_name(base_name), ch["from"], ch["to"])
        path = os.path.join(cfg["out_dir"], fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        written.append(path)
        log("[OK] wrote %s" % path)
    # index
    idx_lines = [
        "# %s · 转写目录" % base_name,
        "",
        "> 生成于 %s · 模型 %s · 共 %d 页" % (today, model or "-", sum(c["to"] - c["from"] + 1 for c in chapters)),
        "",
    ]
    for idx, ch in enumerate(chapters):
        idx_lines.append("- %s（p%d-%d）" % (ch["name"], ch["from"], ch["to"]))
    if failed:
        idx_lines.append("")
        idx_lines.append("## 失败页")
        idx_lines.append("")
        idx_lines.append(", ".join("p%d" % n for n in sorted(failed)))
    with open(os.path.join(cfg["out_dir"], "00-目录.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx_lines))
    return written


def _safe_name(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "untitled"


def main() -> None:
    ap = argparse.ArgumentParser(description="Scanned PDF -> Markdown via vision LLM")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--pdf", help="path to the scanned PDF")
    ap.add_argument("--out", help="output directory")
    ap.add_argument("--base-url", dest="base_url", help="OpenAI-compatible base URL")
    ap.add_argument("--api-key", dest="api_key", help="API key (or env VISION_API_KEY)")
    ap.add_argument("--model", help="vision model name")
    ap.add_argument("--dpi", type=int, help="render DPI (default 170)")
    ap.add_argument("--concurrency", type=int, help="parallel requests (default 6)")
    ap.add_argument("--pages", help="only process page range, e.g. 1-70")
    ap.add_argument("--start-page", dest="start_page", type=int, help="first page to transcribe (skip front matter explicitly)")
    ap.add_argument("--keep-front-matter", dest="keep_front_matter", action="store_true", help="do NOT skip cover/preface; transcribe from page 1")
    ap.add_argument("--auto-chapters", dest="auto_chapters", action="store_true", help="read the TOC page(s) and auto-generate chapter ranges")
    ap.add_argument("--force-ocr", dest="force_ocr", action="store_true", help="ignore any embedded text layer; always use the vision model")
    ap.add_argument("--figures", action="store_true", help="export non-full-page figures to out_dir/images/ and reference them")
    ap.add_argument("--no-requality", dest="no_requality", action="store_true", help="do NOT retry suspiciously short/flagged pages at higher DPI")
    ap.add_argument("--reset-cache", dest="reset_cache", action="store_true", help="clear the per-page cache and re-transcribe everything")
    ap.add_argument("--cleanup", choices=["off", "rules", "llm", "both"], help="post-transcription watermark cleanup mode")
    ap.add_argument("--no-cleanup", dest="no_cleanup", action="store_true", help="disable post-transcription cleanup")
    ap.add_argument("--no-thinking", dest="no_thinking", action="store_true", help="set enable_thinking=false (faster for Qwen-VL thinking models)")
    args = ap.parse_args()
    if args.pages:
        a, b = args.pages.split("-")
        args.pages = (int(a), int(b))

    cfg = load_config(args)
    validate(cfg)

    os.makedirs(cfg["out_dir"], exist_ok=True)
    cache_dir = os.path.join(cfg["out_dir"], ".cache")
    os.makedirs(cache_dir, exist_ok=True)

    if cfg.get("_reset_cache"):
        log("[INFO] --reset-cache：清空逐页缓存 %s" % cache_dir)
        for fn in os.listdir(cache_dir):
            fp = os.path.join(cache_dir, fn)
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass

    global _limiter
    _limiter = AdaptiveConcurrency(int(cfg["concurrency"]))

    check_fingerprint(cfg, cache_dir)

    doc = fitz.open(cfg["pdf"])
    total = doc.page_count
    start = resolve_start_page(cfg, doc, total, cache_dir)

    if cfg.get("auto_chapters") and not (cfg.get("chapters") or []):
        generated = auto_chapters(cfg, doc, total, start, cache_dir)
        if generated:
            cfg["chapters"] = generated

    chapters = chapter_ranges(cfg, total, start)
    log("[INFO] pdf=%s pages=%d start=p%d chapters=%d dpi=%d concurrency=%d vision=%s review=%s"
        % (cfg["pdf"], total, start, len(chapters), int(cfg["dpi"]), int(cfg["concurrency"]),
           cfg["provider"]["model"], _review_provider(cfg)["model"]))

    pages = []
    seen = set()
    for ch in chapters:
        for n in range(ch["from"], ch["to"] + 1):
            if n not in seen:
                seen.add(n)
                pages.append(n)
    todo = [n for n in pages if not os.path.isfile(cache_path(cache_dir, n))]
    log("[INFO] pages=%d cached=%d to_transcribe=%d" % (len(pages), len(pages) - len(todo), len(todo)))

    if todo:
        failed = []
        done_count = 0
        started = time.time()
        with ThreadPoolExecutor(max_workers=max(1, int(cfg["concurrency"]))) as pool:
            futures = {}
            for n in todo:
                session = requests.Session()
                futures[pool.submit(process_page, cfg, cache_dir, n, doc, session)] = n
            for fut in as_completed(futures):
                n, ok, err = fut.result()
                done_count += 1
                if ok:
                    log("[%d/%d] p%d ok" % (done_count, len(todo), n))
                else:
                    failed.append(n)
                    log("[%d/%d] p%d FAILED: %s" % (done_count, len(todo), n, err))
            elapsed = time.time() - started
            log("[INFO] transcription done: %d pages in %.1fs (avg %.1fs/page)"
                % (len(todo), elapsed, elapsed / max(1, len(todo))))
    else:
        failed = []

    requality_pages(cfg, cache_dir, pages, doc)

    if str(cfg.get("cleanup", "llm")).lower() != "off":
        t_clean = time.time()
        run_cleanup(cfg, cache_dir, pages)
        log("[INFO] cleanup done in %.1fs" % (time.time() - t_clean))
    else:
        run_cleanup(cfg, cache_dir, pages)

    written = assemble(cfg, cache_dir, chapters, failed, cfg["provider"]["model"])
    log("[DONE] %d file(s) written under %s" % (len(written), cfg["out_dir"]))
    if failed:
        log("[WARN] %d page(s) failed: %s" % (len(failed), ", ".join("p%d" % n for n in sorted(failed))))
        log("[WARN] re-run the same command to retry only the failed/missing pages (cache skips the rest)")


if __name__ == "__main__":
    main()
