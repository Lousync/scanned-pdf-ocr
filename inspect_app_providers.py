#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Helper: read the Phrontis app settings and list model providers so you can fill
config.json quickly. READ-ONLY. It NEVER prints the API key (the app stores it
encrypted with Electron safeStorage; it simply cannot be read here).

Scans %APPDATA%\\knowbase*\\settings.json, reads the `modelProviders` setting,
and prints each provider's baseUrl + likely vision models.

Usage:
    python inspect_app_providers.py
    python inspect_app_providers.py --settings "C:/Users/me/AppData/Roaming/knowbase/settings.json"
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Same heuristic the app uses to guess vision-capable models.
VISION_RE = re.compile(
    r"(vision|\bvl\b|-vl[-._]|vl[-._]?\d|4o|omni|multimodal|glm-4v|gemini|claude-(|\d)|kimi.*vision|ocr)",
    re.I,
)


def candidate_settings() -> list:
    appdata = os.environ.get("APPDATA", "")
    found = sorted(glob.glob(os.path.join(appdata, "knowbase*", "settings.json")))
    # prod (no suffix) first, then alphabetical
    found.sort(key=lambda p: (os.path.basename(os.path.dirname(p)) != "knowbase", p))
    return found


def read_providers(path: str):
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    raw = d.get("modelProviders")
    if raw is None:
        return []
    arr = json.loads(raw) if isinstance(raw, str) else raw
    return arr if isinstance(arr, list) else []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", help="path to a specific settings.json")
    args = ap.parse_args()

    files = [args.settings] if args.settings else candidate_settings()
    if not files:
        print("[FAIL] no settings.json found under %APPDATA%\\knowbase*")
        return

    for path in files:
        if not os.path.isfile(path):
            print("[FAIL] not found: %s" % path)
            continue
        print("=" * 70)
        print("settings: %s" % path)
        try:
            providers = read_providers(path)
        except Exception as e:  # noqa: BLE001
            print("  [FAIL] parse error: %s" % e)
            continue
        if not providers:
            print("  (no modelProviders)")
            continue
        for p in providers:
            enabled = "ON " if p.get("enabled") else "off"
            key_state = "key=已配置(加密,读不出)" if p.get("apiKeyEncrypted") else "key=未配置"
            print("\n  [%s] %s" % (enabled, p.get("name", "")))
            print("    base_url : %s" % p.get("baseUrl", ""))
            print("    type     : %s" % p.get("type", ""))
            print("    %s" % key_state)
            models = p.get("models") or []
            vision = [m for m in models if VISION_RE.search(str(m))]
            if vision:
                print("    视觉候选模型 (%d): %s" % (len(vision), ", ".join(vision[:12])))
            else:
                print("    视觉候选模型: 未识别到（可手动指定）")
    print("=" * 70)
    print("提示：把上面某个 base_url 与视觉模型名填进 config.json；API Key 需你从")
    print("     软件设置界面或供应商后台单独复制（脚本读不到明文）。")


if __name__ == "__main__":
    main()
