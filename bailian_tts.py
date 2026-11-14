#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阿里百炼（DashScope）语音合成 CLI —— Hermes TTS 接入层。

用法速览：
    py -3.12 bailian_tts.py status
    py -3.12 bailian_tts.py say --text "你好" --out out.wav
    py -3.12 bailian_tts.py switch                    # 列出全部音色（编号）
    py -3.12 bailian_tts.py switch 3 --language ja    # 给当前 profile 指定音色+语言
    py -3.12 bailian_tts.py cache                     # 各 profile 的音频缓存目录与大小
    py -3.12 bailian_tts.py cache --clear             # 清理当前 profile 的音频缓存
    py -3.12 bailian_tts.py where                     # 配置文件/缓存目录一览
    py -3.12 bailian_tts.py voices
    py -3.12 bailian_tts.py use longanhuan_v3.1 --language zh
    py -3.12 bailian_tts.py design --prompt "沉稳的中年男性播音员..." --preview-text "各位听众朋友，大家好。" --name news
    py -3.12 bailian_tts.py clone --audio sample.wav --name brand

设计约束（与百炼 API 保持一致，勿随意改动）：
  * 非实时合成端点：{base}/services/audio/tts/SpeechSynthesizer
  * 音色管理端点（复刻 / 设计 / 查询 / 删除）：{base}/services/audio/tts/customization
  * base 默认用旧域名 https://dashscope.aliyuncs.com/api/v1（官方文档称旧域名仍可用，
    可省去 WorkspaceId）；也可通过环境变量 BAILIAN_TTS_BASE 或 voices.json 的 "endpoint"
    切到 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1。
  * 复刻/设计音色创建时绑定 target_model，后续合成必须用同一个 model，脚本会按音色自动选模型。
仅使用标准库，无第三方依赖。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = Path(os.environ.get("BAILIAN_TTS_REGISTRY") or (SCRIPT_DIR / "voices.json"))
PROFILE_STATE_PATH = Path(os.environ.get("BAILIAN_TTS_PROFILES") or (SCRIPT_DIR / "profiles.json"))
CACHE_VERSION = "tts-cache-v1"      # 改提示词/标签规则时递增，整体失效
CACHE_SUBDIR = "tts_cache"
DEFAULT_CACHE_MAX_MB = 200
DEFAULT_CACHE_MAX_AGE_DAYS = 30
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_MODEL = "qwen-audio-3.1-tts-flash"
DEFAULT_FORMAT = "mp3"
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_MT_MODEL = "qwen-mt-plus"

# 情感/富语言标签：只有这三个模型支持，其它模型会把标签念出来，必须先判断。
TAGGABLE_MODELS = {
    "qwen-audio-3.1-tts-flash", "qwen-audio-3.0-tts-plus", "qwen-audio-3.0-tts-flash",
}
# 控制类：作用于其后的文本，直到遇到下一个控制类标签或长句被切断
CONTROL_TAGS = {
    "sad", "amazed", "deep and loud shouting", "trembling", "angry", "excited", "sarcastic",
    "curious", "like dracula", "bored", "tired", "scornful", "shouting", "asmr", "panicked",
    "mischievously", "empathetic", "whispers", "reluctantly", "crying", "serious",
    "very slowly", "very fast",
}
# 富语言类：在该位置插入拟声/生理音，不影响前后情感
RICH_TAGS = {"gasp", "sighing", "clears throat", "giggles", "laughing", "cough", "snorts"}
ALLOWED_TAGS = CONTROL_TAGS | RICH_TAGS

# 括号里的动作/神态描写（（蹭蹭）（尾巴摇了摇））：不朗读，只作为情感标签的语气依据
ACTION_PAIRS = {"\uff08": "\uff09", "(": ")"}
_ASCII_TOKEN = re.compile(r"^[A-Za-z0-9_@./:\\%+\-]+$")


def strip_action_notes(text: str) -> tuple:
    """去掉括号里的动作描写 -> (可朗读文本, 动作列表)。

    纯 ASCII 的括号内容（URL、IPv4(IPv6)、文件名）不算动作，原样保留。
    """
    if not text:
        return text, []
    kept, actions, buf, depth = [], [], [], 0
    for ch in text:
        if ch in ACTION_PAIRS:
            depth += 1
            if depth == 1:
                buf = []
                continue
        elif depth and ch in ("\uff09", ")"):
            depth -= 1
            if depth == 0:
                note = "".join(buf).strip()
                if note and not _ASCII_TOKEN.match(note):
                    actions.append(note)
                continue
        if depth:
            buf.append(ch)
        else:
            kept.append(ch)
    if not actions:
        return text, []
    clean = "".join(kept)
    clean = re.sub(r"[ \t]+([\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u3001\uff09\u300b\u300d])", r"\1", clean)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = "\n".join(ln.strip() for ln in clean.split("\n") if ln.strip()).strip()
    return (clean or text), actions
# language_hints 代码 → qwen-mt 的 target_lang 名称（官方示例用英文全名）
MT_LANG_NAMES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "fr": "French",
    "de": "German", "ru": "Russian", "pt": "Portuguese", "th": "Thai", "id": "Indonesian",
    "vi": "Vietnamese", "es": "Spanish", "it": "Italian", "ms": "Malay", "fil": "Filipino",
    "ar": "Arabic",
}
SUPPORTED_FORMATS = {"mp3", "wav", "pcm", "opus"}
TIMEOUT = 180

# 系统音色（摘自《Qwen-Audio-TTS 音色列表》）。voice 参数与模型强绑定，跨模型混用会报
# InvalidParameter。可在 voices.json 里补充自己的条目，或直接 --voice 传任意系统音色 id。
SYSTEM_VOICES = [
    {"id": "longanhuan_v3.1", "display": "龙安欢_v3.1", "gender": "女", "model": "qwen-audio-3.1-tts-flash",
     "langs": ["zh", "en", "ja", "ko", "fr", "de", "pt", "it", "vi", "id"],
     "dialects": ["重庆话", "宁波话"],
     "note": "活泼、偏快；方言与多语种需配合 --language 或 --instruction"},
    {"id": "longanlingxin_v3.1", "display": "龙安灵心_v3.1", "gender": "女", "model": "qwen-audio-3.1-tts-flash",
     "langs": ["zh", "en", "ja", "ko", "fr", "de", "pt", "it", "vi", "id"],
     "dialects": ["云南话", "陕西话", "上海话"],
     "note": "温柔知性"},
    {"id": "longanfengyue_v3.1", "display": "龙安风悦_v3.1", "gender": "女", "model": "qwen-audio-3.1-tts-flash",
     "langs": ["zh", "en", "ja", "ko", "fr", "de", "pt", "it", "vi", "id"],
     "dialects": ["东北话", "越南语支持"],
     "note": "播报感强"},
    {"id": "xunanchuan_v3.1", "display": "许南川", "gender": "男", "model": "qwen-audio-3.1-tts-flash",
     "langs": ["zh", "en", "ja", "ko", "fr", "de", "pt", "it", "vi", "id"],
     "dialects": ["甘肃话", "东北话"],
     "note": "磁性男声"},
    {"id": "Bella_v3.1", "display": "Bella_v3.1", "gender": "女", "model": "qwen-audio-3.1-tts-flash",
     "langs": ["zh", "en"], "dialects": [], "note": "中英双语音色"},
]

LANG_ALIASES = {
    "zh": "zh", "中文": "zh", "普通话": "zh", "国语": "zh", "chinese": "zh", "cn": "zh",
    "en": "en", "英文": "en", "英语": "en", "english": "en",
    "fr": "fr", "法语": "fr", "法文": "fr", "de": "de", "德语": "de", "德文": "de",
    "ja": "ja", "日语": "ja", "日文": "ja", "jp": "ja",
    "ko": "ko", "韩语": "ko", "韩文": "ko", "kr": "ko",
    "ru": "ru", "俄语": "ru", "pt": "pt", "葡萄牙语": "pt", "th": "th", "泰语": "th",
    "id": "id", "印尼语": "id", "vi": "vi", "越南语": "vi", "es": "es", "西班牙语": "es",
    "it": "it", "意大利语": "it", "ms": "ms", "马来语": "ms", "马来西亚语": "ms",
    "fil": "fil", "菲律宾语": "fil", "ar": "ar", "阿拉伯语": "ar",
}

# 方言不能通过 language_hints 指定，只能走 instruction（官方文档：方言通过指令控制功能设置）
DIALECTS = {
    "粤语", "广东话", "上海话", "四川话", "东北话", "河南话", "陕西话", "重庆话", "云南话",
    "天津话", "甘肃话", "贵州话", "湖北话", "湖南话", "江西话", "宁夏话", "山西话",
    "山东话", "浙江话", "宁波话", "青岛话", "河北话", "闽南话", "台湾话", "安徽话",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def die(msg: str, code: int = 1):
    print(f"错误: {msg}", file=sys.stderr)
    raise SystemExit(code)


def read_api_key() -> str:
    """DASHSCOPE_API_KEY：环境变量 > Hermes 的 .env（当前 profile > 默认 profile）。"""
    key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if key:
        return key
    candidates = []
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home) / ".env")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "hermes" / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")
    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("DASHSCOPE_API_KEY="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value:
                        return value
        except OSError:
            continue
    die("未找到 DASHSCOPE_API_KEY（请写入环境变量或 Hermes 的 .env）")
    return ""


def base_url() -> str:
    override = (os.environ.get("BAILIAN_TTS_BASE") or "").strip().rstrip("/")
    if override:
        return override
    return load_registry().get("endpoint") or DEFAULT_ENDPOINT


def http_post(url: str, payload: dict, key: str) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = f"{parsed.get('code')}: {parsed.get('message')}"
        except Exception:
            detail = detail[:600]
        die(f"HTTP {exc.code} {detail}")
    except (TimeoutError, socket.timeout):
        die(f"请求超时（>{TIMEOUT}s）。文本过长时请分段合成，或用 Hermes 的分句机制（max_text_length）")
    except urllib.error.URLError as exc:
        die(f"网络错误: {exc.reason}")
    return {}


def download(url: str, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-bailian-tts/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read()
    except (TimeoutError, socket.timeout):
        die(f"下载合成音频超时（>{TIMEOUT}s）")
    except urllib.error.URLError as exc:
        die(f"下载合成音频失败: {exc.reason}")
        return 0
    out.write_bytes(data)
    return len(data)


# agent 主模型（回复用的那个）——翻译直接复用它，不再依赖专用 MT 模型
PROVIDER_KEY_ALIASES = {
    "deepseek": ["DEEPSEEK_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "google": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "moonshot": ["MOONSHOT_API_KEY"],
    "zai": ["ZAI_API_KEY"],
    "zhipu": ["ZHIPU_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "fireworks": ["FIREWORKS_API_KEY"],
    "siliconflow": ["SILICONFLOW_API_KEY"],
}
PROVIDER_DEFAULT_BASE = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "moonshot": "https://api.moonshot.cn/v1",
}


def current_home() -> Path:
    """当前 profile 的 Hermes home（config.yaml / .env 就在这里）。"""
    home = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(home) if home else hermes_root()


def _read_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _model_section(config_path: Path) -> dict:
    """只扫 ``model:`` 块下的第一层标量（不引 YAML 库，够用即可）。"""
    out = {}
    try:
        lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    inside = False
    for line in lines:
        if not inside:
            inside = bool(re.match(r"^model:\s*$", line))
            continue
        if line and not line[0].isspace():
            break
        m = re.match(r"^\s+([A-Za-z_]+):\s*(.*)$", line)
        if m:
            value = m.group(2).strip()
            if value and not value.startswith("#"):
                out[m.group(1)] = value.strip('"').strip("'")
    return out


def main_model_target() -> dict:
    """当前 profile 的回复主模型：model_id / base_url / api_key / provider。"""
    cfg = _model_section(current_home() / "config.yaml")
    if not cfg.get("default"):
        res = _run_hermes(_hermes_cmd(current_profile()) + ["config", "get", "model"])
        for line in (res.stdout or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                cfg.setdefault(k.strip(), v.strip())
    provider = str(cfg.get("provider") or "").strip().lower()
    model = str(cfg.get("default") or "").strip()
    base = str(cfg.get("base_url") or "").strip() or PROVIDER_DEFAULT_BASE.get(provider, "")
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        names = PROVIDER_KEY_ALIASES.get(provider) or [
            f"{provider.upper().replace('-', '_')}_API_KEY"]
        envfile = _read_env_file(current_home() / ".env")
        for name in names:
            key = (os.environ.get(name) or envfile.get(name) or "").strip()
            if key:
                break
    if not model or not base or not key:
        raise RuntimeError(
            f"拿不到主模型配置（model={model or '?'} base_url={base or '?'} "
            f"key={'ok' if key else 'missing'} provider={provider or '?'}）")
    return {"model": model, "base_url": base.rstrip("/"), "key": key, "provider": provider}


def _clean_translation(value: str) -> str:
    """去掉代码块围栏、首尾引号等模型常见的多说话。"""
    out = (value or "").strip()
    out = re.sub(r"^```[A-Za-z]*\s*", "", out)
    out = re.sub(r"\s*```$", "", out).strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'「『“”":
        out = out[1:-1].strip()
    return out


def translate_with_main_model(text: str, source: str, target: str, override_model: str | None = None) -> tuple:
    """用回复主模型整段翻译（一次请求，不切段）→ (译文, 实际模型 id)。失败抛 RuntimeError。"""
    target_name = MT_LANG_NAMES.get(target)
    if not target_name:
        raise RuntimeError(f"不认识的目标语言代码: {target}")
    cfg = main_model_target()
    model = override_model or cfg["model"]
    system = (f"你是专业译者。把用户给出的整段内容完整翻译成{target_name}，"
              f"保留段落结构；译文里不要残留源语言文字（型号、URL、代码、人名品牌可保留原文或按目标语言惯用写法），"
              f"只输出译文本身，不要解释、不要加引号或代码块。")
    payload = {
        "model": model,
        "temperature": 0.2,
        "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text}],
    }
    try:
        resp = http_post(cfg["base_url"] + "/chat/completions", payload, cfg["key"])
    except SystemExit as exc:
        raise RuntimeError(f"主模型翻译请求失败（{exc.code}）") from None
    choices = resp.get("choices") or []
    content = ((choices[0] or {}).get("message") or {}).get("content") if choices else ""
    out = _clean_translation(str(content or ""))
    if not out:
        raise RuntimeError(f"主模型返回为空: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return out, model


def _call_main_model(messages: list, override_model: str | None, temperature: float = 0.2) -> tuple:
    """一次主模型 chat 调用 -> (content, model_id)。失败抛 RuntimeError。"""
    cfg = main_model_target()
    model = override_model or cfg["model"]
    payload = {"model": model, "temperature": temperature, "stream": False, "messages": messages}
    try:
        resp = http_post(cfg["base_url"] + "/chat/completions", payload, cfg["key"])
    except SystemExit as exc:
        raise RuntimeError(f"主模型请求失败（{exc.code}）") from None
    choices = resp.get("choices") or []
    content = ((choices[0] or {}).get("message") or {}).get("content") if choices else ""
    out = _clean_translation(str(content or ""))
    if not out:
        raise RuntimeError(f"主模型返回为空: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return out, model


def _find_tags(value: str) -> list:
    return [t.strip() for t in re.findall(r"\[([^\[\]]{1,32})\]", value)]


def sanitize_tags(value: str) -> tuple:
    """删掉模型自造的英文方括号标签（会被当文字念出来），保留合法标签与非标签方括号。"""
    removed = []

    def _sub(match):
        inner = match.group(1).strip()
        if inner.lower() in ALLOWED_TAGS:
            return match.group(0)
        if re.fullmatch(r"[A-Za-z][A-Za-z \-]{0,24}", inner):
            removed.append(inner)
            return ""
        return match.group(0)

    return re.sub(r"\[([^\[\]]{1,32})\]", _sub, value), removed


def _tag_system_prompt(control_list: str, rich_list: str, actions=None) -> str:
    parts = [
        "你是一个语音合成标注器。在不改变文字内容、不增删信息的前提下，",
        "给下面这段文本插入语音合成用的情感标签与拟声标签，然后只输出标注后的文本，不要解释。",
        "\n\n控制类标签（作用于其后的内容，直到下一个控制类标签或长句被切断）：" + control_list,
        "\n\n富语言标签（插在当前位置，表示笑声/叹息等拟声）：" + rich_list,
    ]
    hint = "；".join(str(a).strip()[:40] for a in (actions or []) if str(a).strip())
    if hint:
        parts.append("\n\n以下括号里的动作/神态描写已从原文删除、不会被朗读，只供你判断语气：" + hint)
    parts += [
        "\n\n规则：",
        "\n1. 只在情绪或语气真的明显的地方加；平铺直叙的技术说明、清单、代码可以不加。",
        "\n2. 一段话最多一个控制类标签；整段最多两三个富语言标签，不要逐句堆叠。",
        "\n3. 标签必须来自上面的列表，大小写与拼写完全一致，不要自造。",
        "\n4. 原文一个字都不要改，只在合适位置插入标签。",
    ]
    if hint:
        parts.append("\n5. 依据动作描写判断情绪（蹭蹭/贴上来→亲昵柔软，尾巴炸毛→不悦，耳朵耷拉/低头→低落，"
                     "打哈欠→困倦），但绝对不要把动作或括号写进文本。")
    return "".join(parts)


def tag_text_with_main_model(text: str, override_model: str | None = None, actions=None) -> tuple:
    """让回复主模型按文本内容插入情感/富语言标签 -> (带标签文本, info)。"""
    control_list = "、".join(f"[{t}]" for t in sorted(CONTROL_TAGS))
    rich_list = "、".join(f"[{t}]" for t in sorted(RICH_TAGS))
    system = _tag_system_prompt(control_list, rich_list, actions)
    out, model = _call_main_model(
        [{"role": "system", "content": system}, {"role": "user", "content": text}],
        override_model, temperature=0.3)
    out, removed = sanitize_tags(out)
    tags = _find_tags(out)
    if len(out) < 0.6 * len(text):           # 模型可能大幅改写，宁可不用
        raise RuntimeError(f"标注结果长度异常（{len(out)} vs {len(text)}），疑似改写")
    return out, {"model": model, "tags": tags, "count": len(tags), "removed": removed}


def compat_chat_url() -> str:
    """OpenAI 兼容端点：<domain>/compatible-mode/v1/chat/completions（域名后不跟 /api/v1）。"""
    base = base_url().rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    return f"{base}/compatible-mode/v1/chat/completions"


_SCRIPT_CHECKS = {
    "ja": (r"[\u3040-\u30ff]", "假名"),
    "ko": (r"[\uac00-\ud7af]", "谚文"),
    "ru": (r"[\u0400-\u04ff]", "西里尔字母"),
    "ar": (r"[\u0600-\u06ff]", "阿拉伯字母"),
    "th": (r"[\u0e00-\u0e7f]", "泰文"),
}


def looks_translated(text: str, target: str) -> bool:
    """粗检译文是否真的是目标语言（qwen-mt 偶发「照着原文重写」而不翻译）。"""
    if not text.strip():
        return False
    if target in _SCRIPT_CHECKS:
        pattern, _ = _SCRIPT_CHECKS[target]
        return re.search(pattern, text) is not None
    if target == "zh":
        return bool(re.search(r"[\u4e00-\u9fff]", text)) and not re.search(r"[\u3040-\u30ff]", text)
    # 拉丁语系：至少 30% 是拉丁字母
    latin = len(re.findall(r"[A-Za-z]", text))
    return latin / max(1, len(re.sub(r"\s", "", text))) > 0.3


def _translate_once(text: str, source: str, target: str, key: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "translation_options": {"source_lang": MT_LANG_NAMES.get(source, "auto"),
                                "target_lang": MT_LANG_NAMES[target]},
    }
    try:
        resp = http_post(compat_chat_url(), payload, key)
    except SystemExit as exc:                      # http_post 内部 die()
        raise RuntimeError(f"翻译请求失败（{exc.code}）") from None
    choices = resp.get("choices") or []
    content = ((choices[0] or {}).get("message") or {}).get("content") if choices else ""
    if not content or not str(content).strip():
        raise RuntimeError(f"翻译返回为空: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return str(content).strip()


def _translate_qwen_mt(text: str, source: str, target: str, key: str, model: str = DEFAULT_MT_MODEL) -> str:
    """备用：百炼 qwen-mt 翻译（仅在 --translator mt 时使用）。"""
    if target not in MT_LANG_NAMES:
        raise RuntimeError(f"qwen-mt 不支持的目标语言代码: {target}")
    out = _translate_once(text, source, target, key, model)
    if looks_translated(out, target):
        return out
    retry = _translate_once(text, source if source in MT_LANG_NAMES else "zh", target, key, model)
    if looks_translated(retry, target):
        return retry
    raise RuntimeError(f"译文疑似未翻译成 {MT_LANG_NAMES[target]}：{out[:60]}")


def translate_text(text: str, source: str, target: str, key: str = "",
                   model: str | None = None, translator: str = "main") -> tuple:
    """整段翻译 → (译文, 实际使用的翻译器标识)。失败抛 RuntimeError。

    默认用回复主模型（与对话同一个模型、同一个 key），``translator="mt"`` 才走 qwen-mt。
    """
    if translator == "mt":
        return _translate_qwen_mt(text, source, target, key, model or DEFAULT_MT_MODEL), \
            model or DEFAULT_MT_MODEL
    out, used_model = translate_with_main_model(text, source, target, override_model=model)
    if looks_translated(out, target):
        return out, used_model
    raise RuntimeError(f"译文疑似未翻译成 {MT_LANG_NAMES.get(target, target)}：{out[:60]}")


def cache_root(profile: str | None = None) -> Path:
    """当前（或指定）profile 的内容寻址音频缓存目录。"""
    return audio_dir_for(home_for_profile(profile or current_profile())) / CACHE_SUBDIR


def cache_is_enabled(args, state: dict) -> bool:
    """命令行 --no-cache > 环境变量 BAILIAN_TTS_CACHE=0 > profile 的 cache 开关 > 默认开。"""
    if getattr(args, "no_cache", False):
        return False
    if str(os.environ.get("BAILIAN_TTS_CACHE", "")).strip() == "0":
        return False
    return state.get("cache") is not False


def _cache_key(payload: dict) -> str:
    """输入侧 key：原文 + 音色 + 模型 + 影响发音的全部参数。"""
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def cache_lookup(key: str, ext: str, profile: str | None = None) -> Path | None:
    path = cache_root(profile) / f"{key}.{ext.lower().lstrip('.')}"
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def cache_meta(key: str, profile: str | None = None) -> dict:
    try:
        return json.loads((cache_root(profile) / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_audio_files(root: Path) -> list:
    try:
        return [f for f in root.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
    except OSError:
        return []


def cache_prune(root: Path) -> dict:
    """按年龄与容量淘汰；只动缓存目录里的音频与其 json 旁注。"""
    max_mb = float(os.environ.get("BAILIAN_TTS_CACHE_MAX_MB") or DEFAULT_CACHE_MAX_MB)
    max_days = float(os.environ.get("BAILIAN_TTS_CACHE_DAYS") or DEFAULT_CACHE_MAX_AGE_DAYS)
    removed, freed = 0, 0
    now = time.time()
    files = _cache_audio_files(root)
    kept = []
    for f in files:
        try:
            age_days = (now - f.stat().st_mtime) / 86400
        except OSError:
            continue
        if age_days > max_days:
            try:
                freed += f.stat().st_size
                f.unlink()
                (root / f"{f.stem}.json").unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        else:
            kept.append(f)
    total = sum(f.stat().st_size for f in kept if f.exists())
    for f in sorted(kept, key=lambda x: x.stat().st_mtime):
        if total <= max_mb * 1048576:
            break
        try:
            total -= f.stat().st_size
            freed += f.stat().st_size
            f.unlink()
            (root / f"{f.stem}.json").unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return {"removed": removed, "freed_mib": round(freed / 1048576, 2)}


def cache_store(key: str, src: Path, meta: dict, profile: str | None = None) -> dict:
    root = cache_root(profile)
    root.mkdir(parents=True, exist_ok=True)
    dst = root / f"{key}{src.suffix.lower()}"
    shutil.copyfile(src, dst)
    payload = dict(meta)
    payload["created"] = time.strftime("%Y-%m-%d %H:%M:%S")
    payload["bytes"] = dst.stat().st_size
    (root / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"path": str(dst), "bytes": payload["bytes"], "pruned": cache_prune(root)}


def cache_stats(profile: str | None = None) -> dict:
    root = cache_root(profile)
    files = _cache_audio_files(root)
    size = 0
    for f in files:
        try:
            size += f.stat().st_size
        except OSError:
            pass
    return {"dir": str(root), "entries": len(files), "mib": round(size / 1048576, 2),
            "max_mib": DEFAULT_CACHE_MAX_MB, "max_days": DEFAULT_CACHE_MAX_AGE_DAYS}


def _clear_cache_dir(root: Path) -> dict:
    files = size = 0
    try:
        for f in list(root.iterdir()):
            if f.is_file() and (f.suffix.lower() in AUDIO_EXTS or f.suffix.lower() == ".json"):
                try:
                    size += f.stat().st_size
                    f.unlink()
                    files += 1
                except OSError:
                    continue
    except OSError:
        pass
    return {"files": files, "mib": round(size / 1048576, 2)}


def _log_run(profile: str, voice: str, model: str, lang, translated, out: Path, chars,
             tagging=None, cache=None) -> None:
    """追加一行运行记录：profile / 音色 / 语言 / 是否翻译 / 产物。"""
    try:
        log = SCRIPT_DIR / "out" / "tts_history.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        flag = "same" if not translated else (
            f"{translated['from']}->{translated['to']}"
            f"@{translated.get('translator', 'main')}:{translated.get('model', '-')}")
        with log.open("a", encoding="utf-8") as fh:
            fh.write("\t".join([
                time.strftime("%Y-%m-%d %H:%M:%S"), profile, voice, model,
                str(lang or "-"), flag,
                f"tags={tagging['count']}" if tagging else "tags=0",
                f"cache={cache or 'off'}",
                str(chars if chars is not None else "-"), str(out),
            ]) + "\n")
    except OSError:
        pass


def _log_translation_failure(msg: str, original: str, target: str) -> None:
    """失败时留痕（chat 路径下 stderr 不可见，事后可查）。"""
    try:
        log = SCRIPT_DIR / "out" / "translation_failures.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{target}\t{msg}\t{original[:120]}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 音色注册表
# --------------------------------------------------------------------------- #
def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("voices", {})
                return data
        except (OSError, json.JSONDecodeError) as exc:
            die(f"voices.json 读取失败 ({REGISTRY_PATH}): {exc}")
    return {"version": 1, "endpoint": DEFAULT_ENDPOINT, "default_model": DEFAULT_MODEL,
            "active": None, "voices": {}}


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


# --------------------------------------------------------------------------- #
# profile 感知层
# Hermes 会把 HERMES_HOME 注入到命令型 provider 的子进程（实测），因此脚本能自己判断
# 当前 profile；音频缓存目录的解析规则与 hermes_constants.get_hermes_dir 保持一致。
# --------------------------------------------------------------------------- #
def hermes_root() -> Path:
    """Hermes 数据根目录；HERMES_HOME 指向某个 profile 时取其 profiles/ 的父级。"""
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if home:
        path = Path(home)
        return path.parent.parent if path.parent.name == "profiles" else path
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    return (Path(local) / "hermes") if local else (Path.home() / ".hermes")


def current_profile() -> str:
    name = (os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_PROFILE_NAME") or "").strip()
    if name:
        return name
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if home and Path(home).parent.name == "profiles":
        return Path(home).name
    return "default"


def home_for_profile(name: str | None) -> Path:
    root = hermes_root()
    if not name or name.strip().lower() in ("default", "main"):
        return root
    return root / "profiles" / name


def audio_dir_for(home: Path) -> Path:
    """遗留 ``audio_cache/`` 只有真的有内容才算在用，否则是 ``cache/audio``。"""
    legacy = home / "audio_cache"
    try:
        if legacy.is_dir() and any(legacy.iterdir()):
            return legacy
    except OSError:
        pass
    return home / "cache" / "audio"


def known_profiles() -> list:
    root = hermes_root()
    names = ["default"]
    pdir = root / "profiles"
    if pdir.is_dir():
        try:
            for d in sorted(pdir.iterdir()):
                if d.is_dir() and not d.name.startswith(".") and d.name not in names:
                    names.append(d.name)
        except OSError:
            pass
    cur = current_profile()
    return names if cur in names else names + [cur]


def _dir_stats(path: Path) -> dict:
    files = size = 0
    try:
        for f in path.iterdir():
            if f.is_file():
                files += 1
                size += f.stat().st_size
    except OSError:
        pass
    return {"files": files, "mib": round(size / 1048576, 2), "exists": path.is_dir()}


AUDIO_EXTS = (".mp3", ".ogg", ".wav", ".opus", ".flac", ".m4a", ".aac", ".pcm")


def load_profiles() -> dict:
    if PROFILE_STATE_PATH.exists():
        try:
            data = json.loads(PROFILE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("profiles", {})
                return data
        except (OSError, json.JSONDecodeError) as exc:
            die(f"profiles.json 读取失败 ({PROFILE_STATE_PATH}): {exc}")
    return {"version": 1, "profiles": {}}


def save_profiles(data: dict) -> None:
    PROFILE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROFILE_STATE_PATH)


def profile_state(name: str) -> dict:
    return (load_profiles().get("profiles") or {}).get(name) or {}


def seed_system_voices(reg: dict) -> dict:
    """首次运行时把内置系统音色写进注册表（不覆盖已有条目）。"""
    for item in SYSTEM_VOICES:
        entry = reg["voices"].setdefault(item["id"], {})
        entry.setdefault("kind", "system")
        entry.setdefault("model", item["model"])
        entry.setdefault("display", item["display"])
        entry.setdefault("gender", item.get("gender"))
        entry.setdefault("langs", item.get("langs", []))
        entry.setdefault("dialects", item.get("dialects", []))
        entry.setdefault("note", item.get("note", ""))
        entry.setdefault("language", None)
        entry.setdefault("instruction", "")
    if not reg.get("active"):
        reg["active"] = "longanhuan_v3.1"
    return reg


def resolve_voice(reg: dict, name: str | None) -> tuple[str, dict]:
    """按 id / 别名 / 唯一子串解析音色。"""
    voices = reg["voices"]
    if not name:
        active = reg.get("active")
        if not active or active not in voices:
            die("当前没有激活的音色，先执行: bailian_tts.py use <voice_id>")
        return active, voices[active]
    if name in voices:
        return name, voices[name]
    low = name.lower()
    hits = [vid for vid, v in voices.items()
            if low in vid.lower() or low in str(v.get("display", "")).lower()]
    if len(hits) == 1:
        return hits[0], voices[hits[0]]
    if not hits:
        die(f"未知音色 '{name}'。用 `voices` 子命令查看已有音色；"
            f"系统音色也可直接 --voice 传原始 id（如 longanhuan_v3.1）")
    die(f"音色 '{name}' 有歧义，候选: {', '.join(sorted(hits))}")
    return "", {}


def normalize_language(raw: str | None) -> tuple[str | None, str]:
    """返回 (language_hints 代码 or None, 由方言派生的 instruction)。"""
    if not raw:
        return None, ""
    value = raw.strip()
    value = {"粤语": "广东话", "粤方言": "广东话", "广东方言": "广东话"}.get(value, value)
    if value in DIALECTS:
        return None, f"请用{value}表达"
    code = LANG_ALIASES.get(value.lower()) or LANG_ALIASES.get(value)
    if code:
        return code, ""
    die(f"不认识的语言/方言 '{raw}'。可用代码: {', '.join(sorted(set(LANG_ALIASES.values())))}；"
        f"方言: {', '.join(sorted(DIALECTS))}")
    return None, ""


def detect_language(text: str) -> str:
    """粗判文本语种（zh/ja/ko/ru/ar/th/…/en），用于 language_hints 与「是否需要翻译」。

    判据按文字系统来，先排除假名/谚文/西里尔等，再由 CJK 占比决定中日。够用即止：
    这是启发式，不确定时调用方可以 --force-translate 或 --no-translate 覆盖。
    """
    sample = text[:2000]
    total = len(re.sub(r"\s", "", sample)) or 1
    counts = {
        "ja": len(re.findall(r"[\u3040-\u30ff]", sample)),          # 平假名 + 片假名
        "ko": len(re.findall(r"[\uac00-\ud7af]", sample)),          # 谚文
        "ru": len(re.findall(r"[\u0400-\u04ff]", sample)),
        "ar": len(re.findall(r"[\u0600-\u06ff]", sample)),
        "th": len(re.findall(r"[\u0e00-\u0e7f]", sample)),
    }
    for code, hit in counts.items():
        if hit and hit / total > 0.05:
            return code
    cjk = len(re.findall(r"[\u4e00-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    if cjk and cjk / max(1, cjk + latin) > 0.15:
        return "zh"
    if latin:
        return "en"
    return "zh" if cjk else "en"


# --------------------------------------------------------------------------- #
# 子命令：say
# --------------------------------------------------------------------------- #
def _as_float(raw, name):
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        die(f"--{name} 需要数字，收到 {raw!r}")
    return None


def cmd_say(args) -> int:
    reg = seed_system_voices(load_registry())
    key = read_api_key()

    text = args.text
    if args.text_file:
        p = Path(args.text_file)
        if not p.exists():
            die(f"文本文件不存在: {p}")
        text = p.read_text(encoding="utf-8")
    if not text or not text.strip():
        die("文本为空")
    raw_chars = len(text)
    actions = []

    # 优先级：命令行 > 当前 profile 指定 > 音色自身默认 > 全局 active
    profile = current_profile()
    state = profile_state(profile)
    voice_id, voice = resolve_voice(reg, args.voice or state.get("voice"))
    # 模型绑定：复刻/设计音色必须用创建时的 target_model
    model = args.model or voice.get("model") or reg.get("default_model") or DEFAULT_MODEL

    lang_raw = args.language or state.get("language") or voice.get("language")
    lang_code, dialect_instr = normalize_language(lang_raw)
    instruction = args.instruction or state.get("instruction") or voice.get("instruction") or ""
    if dialect_instr:
        instruction = f"{instruction}；{dialect_instr}".strip("；") if instruction else dialect_instr

    out = Path(args.out or (SCRIPT_DIR / "out" / f"tts_{int(time.time())}.{DEFAULT_FORMAT}"))
    fmt = (args.format or "").lower().strip().lstrip(".") or out.suffix.lower().lstrip(".") or DEFAULT_FORMAT
    if fmt not in SUPPORTED_FORMATS:
        die(f"百炼非实时接口不支持 {fmt} 格式，可选: {', '.join(sorted(SUPPORTED_FORMATS))}")
    if out.suffix.lower().lstrip(".") != fmt:
        # 让落盘扩展名与真实编码一致，避免下游播放器误判
        out = out.with_suffix("." + fmt)

    # 动作括号不朗读：去掉（蹭蹭）这类描写，但留下来当情感标签的语气依据
    if (not getattr(args, "keep_actions", False) and not args.ssml
            and (state.get("actions") is True or getattr(args, "strip_actions", False))):
        text, actions = strip_action_notes(text)

    # 数值参数：既进缓存 key 也进请求体，统一在这里解析一次
    rate_val = _as_float(args.rate, "rate")
    if rate_val is None:
        rate_val = _as_float(getattr(args, "speed", None), "speed")
    rate_val = None if rate_val is None else max(0.5, min(2.0, rate_val))
    pitch_val = _as_float(args.pitch, "pitch")
    pitch_val = None if pitch_val is None else max(0.5, min(2.0, pitch_val))
    vol_val = None if args.volume is None else max(0, min(100, int(args.volume)))

    # 语言对齐：音频指定语言与文本语言不同时，先把文本翻成该语言（可见回复不动）
    translate_state = state.get("translate")
    do_translate = not args.no_translate and translate_state is not False

    # ---- 内容寻址音频缓存：key 只取输入侧参数，命中就复用首次那份音频 ----
    tags_on = (not args.no_tags) and state.get("tags") is not False and model in TAGGABLE_MODELS
    cache_on = cache_is_enabled(args, state) and not args.translate_only
    ckey = _cache_key({
        "v": CACHE_VERSION,
        "uid": (getattr(args, "uid", None) or "").strip(),
        "text": text, "voice": voice_id, "model": model,
        "format": fmt, "sample_rate": int(args.sample_rate or DEFAULT_SAMPLE_RATE),
        "rate": rate_val, "pitch": pitch_val, "volume": vol_val,
        "bit_rate": int(args.bit_rate) if args.bit_rate else None,
        "seed": int(args.seed) if args.seed is not None else None,
        "ssml": bool(args.ssml), "instruction": instruction,
        "lang": lang_raw or "", "translate": bool(do_translate), "tags": bool(tags_on),
    })
    if cache_on:
        hit = cache_lookup(ckey, fmt)
        if hit is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(hit, out)
            meta = cache_meta(ckey)
            _log_run(profile, voice_id, model, lang_code or "-", None, out, 0, None, cache="hit")
            print(json.dumps({
                "ok": True, "cache_hit": True, "path": str(out), "bytes": out.stat().st_size,
                "voice": voice_id, "model": model, "format": fmt, "language": lang_code,
                "chars": 0, "cache_key": ckey, "cache_dir": str(cache_root(profile)),
                "cached_text": meta.get("final_text"), "cached_at": meta.get("created"),
            }, ensure_ascii=False))
            return 0
    translated = None
    translation_error = None
    if lang_code and (do_translate or args.force_translate) and not args.ssml:
        if re.search(r"\[(?:sad|angry|excited|whispers|laughing|sighing|giggles|serious|"
                     r"panicked|curious|bored|tired|crying|shouting|asmr)\b[^\]]*\]", text, re.I):
            translation_error = "文本含情感/拟声标签，跳过翻译以免破坏标签"
        else:
            source = args.from_language or detect_language(text)
            if args.force_translate or source != lang_code:
                translator = args.translator or "main"
                try:
                    text, used = translate_text(text, source, lang_code, key,
                                                model=args.translate_model, translator=translator)
                    translated = {"from": source, "to": lang_code,
                                  "model": used, "translator": translator,
                                  "whole_reply": True, "chars": len(text)}
                except RuntimeError as exc:
                    translation_error = str(exc)   # 保留原文，宁可有声音也不要静默失败
                    _log_translation_failure(translation_error, text, lang_code)
                    print(f"警告: {translation_error}；改用原文合成", file=sys.stderr)

    # 情感/富语言标签：翻译之后再加，否则标签会被翻译破坏
    tags_state = state.get("tags")
    tagging = None
    tagging_error = None
    want_tags = (not args.no_tags) and tags_state is not False
    found_existing = _find_tags(text)
    already_tagged = any(t.lower() in ALLOWED_TAGS for t in found_existing)
    if want_tags and args.ssml:
        tagging_error = "--ssml 文本不做自动标注"
    elif want_tags and model not in TAGGABLE_MODELS:
        tagging_error = f"模型 {model} 不支持情感/富语言标签，已跳过"
    elif want_tags and already_tagged:
        tagging = {"model": None, "tags": found_existing, "count": len(found_existing),
                   "skipped": "文本已自带标签，跳过自动标注"}
    elif want_tags:
        try:
            text, tagging = tag_text_with_main_model(text, args.tag_model, actions)
        except RuntimeError as exc:
            tagging_error = str(exc)
            _log_translation_failure(f"标注失败: {tagging_error}", text, "tags")
            print(f"警告: 标注失败：{tagging_error}；用无标签文本合成", file=sys.stderr)

    if args.translate_only:
        print(json.dumps({"translated": translated, "error": translation_error,
                          "tagging": tagging, "tagging_error": tagging_error,
                          "actions": actions, "action_count": len(actions),
                          "original_chars": raw_chars, "chars": len(text),
                          "text": text}, ensure_ascii=False))
        return 0

    payload_input = {
        "text": text,
        "voice": voice_id,
        "format": fmt,
        "sample_rate": int(args.sample_rate or DEFAULT_SAMPLE_RATE),
    }
    if lang_code:
        payload_input["language_hints"] = [lang_code]
    elif not lang_raw:
        # 未指定时自动判定，减少数字/缩写读错
        payload_input["language_hints"] = [detect_language(text)]
    if instruction:
        payload_input["instruction"] = instruction
    if rate_val is not None:
        payload_input["rate"] = rate_val
    if pitch_val is not None:
        payload_input["pitch"] = pitch_val
    if vol_val is not None:
        payload_input["volume"] = vol_val
    if args.bit_rate and fmt == "opus":
        payload_input["bit_rate"] = int(args.bit_rate)
    if args.ssml:
        payload_input["enable_ssml"] = True
    if args.seed is not None:
        payload_input["seed"] = int(args.seed)

    payload = {"model": model, "input": payload_input}
    resp = http_post(f"{base_url()}/services/audio/tts/SpeechSynthesizer", payload, key)

    audio = (resp.get("output") or {}).get("audio") or {}
    encoded = audio.get("data") or ""
    url = audio.get("url") or ""
    if encoded:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(encoded))
        size = out.stat().st_size
    elif url:
        size = download(url, out)
    else:
        die(f"响应中没有音频数据: {json.dumps(resp, ensure_ascii=False)[:500]}")
        return 1

    usage = resp.get("usage") or {}
    stored = None
    if cache_on:
        stored = cache_store(ckey, out, {
            "key": ckey, "voice": voice_id, "model": model, "format": fmt,
            "language": (payload_input.get("language_hints") or [None])[0],
            "rate": rate_val, "pitch": pitch_val, "volume": vol_val,
            "instruction": instruction or "", "translate": bool(do_translate),
            "tags": tagging.get("tags") if tagging else [],
            "original_text": args.text or "(from file)",
            "final_text": text,
        })
    _log_run(profile, voice_id, model, (payload_input.get("language_hints") or [None])[0],
             translated, out, usage.get("characters"), tagging,
             cache="miss" if cache_on else "off")
    print(json.dumps({
        "ok": True, "path": str(out), "bytes": size, "voice": voice_id, "model": model,
        "format": fmt, "language": (payload_input.get("language_hints") or [None])[0],
        "instruction": instruction or None, "chars": usage.get("characters"),
        "translated": translated, "translation_error": translation_error,
        "tagging": tagging, "tagging_error": tagging_error,
        "actions": actions, "action_count": len(actions), "original_chars": raw_chars,
        "cache_hit": False, "cache_key": ckey, "cache_stored": stored,
    }, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# 子命令：customization 通用封装
# --------------------------------------------------------------------------- #
def _customization(action_input: dict, key: str) -> dict:
    return http_post(f"{base_url()}/services/audio/tts/customization",
                     {"model": "voice-enrollment", "input": action_input}, key)


def _poll_voice(voice_id: str, key: str, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    status = "DEPLOYING"
    while time.monotonic() < deadline:
        resp = _customization({"action": "query_voice", "voice_id": voice_id}, key)
        status = str((resp.get("output") or {}).get("status") or "")
        if status == "OK":
            return status
        if status != "DEPLOYING":
            return status
        time.sleep(1.5)
    return status


def _remote_list(key: str, prefix: str | None = None, page_size: int = 100) -> list[dict]:
    items: list[dict] = []
    page = 0
    while True:
        req = {"action": "list_voice", "page_index": page, "page_size": page_size}
        if prefix:
            req["prefix"] = prefix
        resp = _customization(req, key)
        out = resp.get("output") or {}
        batch = out.get("voice_list") or []
        items.extend(batch)
        total = int(out.get("total_count") or 0)
        if not batch or len(items) >= total or page > 20:
            break
        page += 1
    return items


def _register_custom(reg: dict, voice_id: str, kind: str, model: str, extra: dict) -> None:
    entry = reg["voices"].setdefault(voice_id, {})
    entry.update({"kind": kind, "model": model, "status": "OK"})
    entry.setdefault("display", extra.get("display") or voice_id)
    entry.setdefault("langs", extra.get("langs") or [])
    entry.setdefault("dialects", [])
    entry.setdefault("language", extra.get("language"))
    entry.setdefault("instruction", extra.get("instruction") or "")
    for k in ("voice_prompt", "preview_text", "sample", "created"):
        if extra.get(k):
            entry[k] = extra[k]
    save_registry(reg)


def cmd_design(args) -> int:
    reg = seed_system_voices(load_registry())
    key = read_api_key()
    model = args.model or reg.get("default_model") or DEFAULT_MODEL
    prompt = args.prompt.strip()
    preview = args.preview_text or "你好，这是一段音色设计预览。"
    if len(preview) < 15:
        preview = "你好，这是一段音色设计预览，用于试听新生成的音色效果。"

    payload_input = {
        "action": "create_voice",
        "target_model": model,
        "voice_prompt": prompt,
        "preview_text": preview,
    }
    if args.name:
        payload_input["prefix"] = re.sub(r"[^0-9A-Za-z]", "", args.name)[:10] or "hermes"
    if args.language:
        code, _ = normalize_language(args.language)
        payload_input["language"] = code or "zh"

    resp = _customization(payload_input, key)
    out = resp.get("output") or {}
    voice_id = out.get("voice_id") or ""
    preview_b64 = ((out.get("preview_audio") or {}).get("data")) or ""

    preview_path = None
    if preview_b64:
        preview_path = Path(args.preview_out or (SCRIPT_DIR / "out" / f"design_{int(time.time())}.wav"))
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(base64.b64decode(preview_b64))

    if not voice_id:
        # 接口在部分版本只回预览音频，靠 prefix 反查新音色
        listed = _remote_list(key, prefix=payload_input.get("prefix"))
        fresh = [v for v in listed if v.get("target_model") == model]
        if fresh:
            fresh.sort(key=lambda v: str(v.get("gmt_create") or ""), reverse=True)
            voice_id = fresh[0].get("voice_id") or ""
    if not voice_id:
        die("创建成功但没能取到 voice_id，请到控制台「音色管理」确认，或用 `voices --remote` 查看")

    status = _poll_voice(voice_id, key)
    _register_custom(reg, voice_id, "design", model, {
        "display": args.name or "设计音色", "voice_prompt": prompt[:200],
        "preview_text": preview, "language": args.language,
    })
    if args.language:
        reg["voices"][voice_id]["language"] = args.language
    if args.use:
        reg["active"] = voice_id
    save_registry(reg)

    print(json.dumps({"ok": status == "OK", "voice_id": voice_id, "model": model, "status": status,
                      "preview": str(preview_path) if preview_path else None,
                      "voice_prompt": prompt, "active": reg.get("active") == voice_id},
                     ensure_ascii=False))
    return 0 if status == "OK" else 2


def _audio_data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        die(f"音频文件不存在: {p}")
    if p.stat().st_size > 10 * 1024 * 1024:
        die("参考音频需 ≤ 10MB（建议 10~20 秒、≥16kHz、单人清晰朗读）")
    suffix = p.suffix.lower().lstrip(".") or "wav"
    mime = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav"}.get(suffix, "audio/wav")
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")


def cmd_clone(args) -> int:
    reg = seed_system_voices(load_registry())
    key = read_api_key()
    model = args.model or reg.get("default_model") or DEFAULT_MODEL
    audio = args.audio
    if not re.match(r"^(https?:|data:)", audio, re.I):
        audio = _audio_data_uri(audio)

    payload_input = {
        "action": "create_voice",
        "target_model": model,
        "prefix": re.sub(r"[^0-9A-Za-z]", "", args.name or "hermes")[:10] or "hermes",
        "audio": audio,
    }
    if args.language:
        code, _ = normalize_language(args.language)
        if code:
            payload_input["language_hints"] = [code]
    if args.max_audio_length is not None:
        payload_input["max_prompt_audio_length"] = max(3.0, min(30.0, float(args.max_audio_length)))
    if args.preprocess:
        payload_input["enable_preprocess"] = True

    resp = _customization(payload_input, key)
    voice_id = (resp.get("output") or {}).get("voice_id") or ""
    if not voice_id:
        die(f"创建复刻音色失败: {json.dumps(resp, ensure_ascii=False)[:500]}")
    status = _poll_voice(voice_id, key)
    _register_custom(reg, voice_id, "clone", model, {
        "display": args.name or "复刻音色", "language": args.language,
        "sample": audio[:120] if not audio.startswith("data:") else "(本地音频)",
    })
    if args.use:
        reg["active"] = voice_id
    save_registry(reg)
    print(json.dumps({"ok": status == "OK", "voice_id": voice_id, "model": model, "status": status,
                      "active": reg.get("active") == voice_id}, ensure_ascii=False))
    return 0 if status == "OK" else 2


# --------------------------------------------------------------------------- #
# 子命令：voices / use / set / current / delete / status
# --------------------------------------------------------------------------- #
def cmd_voices(args) -> int:
    reg = seed_system_voices(load_registry())
    rows = []
    remote_note = ""
    if args.remote:
        try:
            remote = _remote_list(read_api_key())
            remote_ids = {v.get("voice_id") for v in remote}
            for v in remote:
                vid = v.get("voice_id")
                entry = reg["voices"].setdefault(vid, {"kind": "custom", "display": vid, "langs": [],
                                                       "dialects": [], "language": None, "instruction": ""})
                entry["model"] = v.get("target_model") or entry.get("model")
                entry["status"] = v.get("status")
                if v.get("voice_prompt"):
                    entry.setdefault("voice_prompt", v["voice_prompt"])
            save_registry(reg)
            remote_note = f"远程 {len(remote_ids)} 个自定义音色"
        except SystemExit:
            remote_note = "远程查询失败"

    for vid, v in reg["voices"].items():
        if args.kind and args.kind != "all" and v.get("kind") != args.kind:
            continue
        rows.append({
            "id": vid, "kind": v.get("kind"), "model": v.get("model"),
            "display": v.get("display"), "gender": v.get("gender"),
            "language": v.get("language"), "instruction": v.get("instruction") or None,
            "active": vid == reg.get("active"), "status": v.get("status"),
        })

    if args.json:
        print(json.dumps({"active": reg.get("active"), "voices": rows, "remote": remote_note},
                         ensure_ascii=False, indent=2))
        return 0

    prof = current_profile()
    pstate = profile_state(prof)
    print(f"当前 profile: {prof}    生效音色: {pstate.get('voice') or reg.get('active')}"
          f"    生效语言: {pstate.get('language') or '(自动判定)'}    (注册表: {REGISTRY_PATH})")
    if remote_note:
        print(remote_note)
    print(f"{'':2}{'id':<62}{'类型':<8}{'音色默认语言':<14}模型")
    for r in rows:
        mark = "*" if r["active"] else " "
        print(f"{mark:2}{r['id']:<62}{r['kind'] or '':<8}{str(r['language'] or '-'):<14}{r['model'] or ''}")
    return 0


def cmd_use(args) -> int:
    reg = seed_system_voices(load_registry())
    vid, entry = resolve_voice(reg, args.voice)
    reg["active"] = vid
    if args.language is not None:
        entry["language"] = args.language or None
    if args.instruction is not None:
        entry["instruction"] = args.instruction
    if args.model:
        entry["model"] = args.model
    save_registry(reg)
    print(json.dumps({"ok": True, "active": vid, "model": entry.get("model"),
                      "language": entry.get("language"), "instruction": entry.get("instruction") or None},
                     ensure_ascii=False))
    return 0


def cmd_delete(args) -> int:
    reg = seed_system_voices(load_registry())
    key = read_api_key()
    vid, entry = resolve_voice(reg, args.voice)
    if entry.get("kind") == "system" and not args.force:
        die("系统音色不能删除；若要删除自定义音色请确认 id")
    resp = _customization({"action": "delete_voice", "voice_id": vid}, key)
    reg["voices"].pop(vid, None)
    if reg.get("active") == vid:
        reg["active"] = next(iter(reg["voices"]), None)
    save_registry(reg)
    print(json.dumps({"ok": True, "deleted": vid, "response": resp.get("request_id"),
                      "new_active": reg.get("active")}, ensure_ascii=False))
    return 0


def _hermes_exe() -> str:
    exe = shutil.which("hermes")
    if not exe:
        die("找不到 hermes 命令（无法写入 profile 的 config.yaml）")
    return exe


def _hermes_cmd(profile: str | None) -> list:
    cmd = [_hermes_exe()]
    if profile and profile.strip().lower() not in ("default", "main"):
        cmd += ["-p", profile]
    return cmd


def _run_hermes(cmd: list, timeout: int = 90) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        die(f"执行 hermes 失败: {exc}")
    return subprocess.CompletedProcess(cmd, 1, "", "")


def _write_profile_voice(profile: str, voice_id: str) -> dict:
    """把音色写进该 profile 的 config.yaml（tts.providers.bailian.voice），并回读复核。"""
    cmd = _hermes_cmd(profile) + ["config", "set", "tts.providers.bailian.voice", voice_id, "--force"]
    res = _run_hermes(cmd)
    if res.returncode != 0:
        die(f"写配置失败: {(res.stderr or res.stdout or '').strip()[:400]}")
    check = _run_hermes(_hermes_cmd(profile) + ["config", "get", "tts.providers.bailian.voice"])
    got = (check.stdout or "").strip().splitlines()
    verified = bool(got) and got[0].strip() == voice_id
    return {"config": str(home_for_profile(profile) / "config.yaml"), "verified": verified,
            "value": got[0].strip() if got else ""}


def cmd_where(args) -> int:
    """打印所有 profile 的音频缓存目录、配置文件和注册表位置。"""
    cur = current_profile()
    names = [args.profile] if args.profile else known_profiles()
    rows = []
    for name in names:
        home = home_for_profile(name)
        adir = audio_dir_for(home)
        rows.append({
            "profile": name, "current": name == cur, "home": str(home),
            "config": str(home / "config.yaml"), "env": str(home / ".env"),
            "audio_dir": str(adir), "audio": _dir_stats(adir),
        })
    print(json.dumps({
        "hermes_root": str(hermes_root()), "current_profile": cur,
        "voices_json": str(REGISTRY_PATH), "profiles_json": str(PROFILE_STATE_PATH),
        "profile_state": profile_state(cur), "profiles": rows,
    }, ensure_ascii=False, indent=2))
    return 0


def _clear_audio(path: Path) -> dict:
    files = size = 0
    if not path.is_dir():
        return {"files": 0, "mib": 0.0}
    for f in list(path.iterdir()):
        try:
            if f.is_file() and (f.suffix.lower() in AUDIO_EXTS or ".chunk" in f.name):
                size += f.stat().st_size
                f.unlink()
                files += 1
        except OSError:
            continue
    return {"files": files, "mib": round(size / 1048576, 2)}


def cmd_cache(args) -> int:
    """按 profile 解析音频缓存目录；--clear 删除其中的音频文件。"""
    cur = current_profile()
    names = known_profiles() if args.all_profiles else [args.profile or cur]
    rows = []
    for name in names:
        adir = audio_dir_for(home_for_profile(name))
        before = _dir_stats(adir)
        row = {"profile": name, "current": name == cur, "dir": str(adir),
               "before_files": before["files"], "before_mib": before["mib"]}
        crow = cache_root(name)
        cst = cache_stats(name)
        row["tts_cache"] = {"dir": cst["dir"], "entries": cst["entries"], "mib": cst["mib"]}
        if args.clear and not getattr(args, "keep_cache", False):
            cdone = _clear_cache_dir(crow)
            row["tts_cache_deleted"] = cdone
        if args.clear:
            done = _clear_audio(adir)
            after = _dir_stats(adir)
            row.update({"deleted_files": done["files"], "deleted_mib": done["mib"],
                        "after_files": after["files"], "after_mib": after["mib"]})
        rows.append(row)
    if args.include_out:
        out_dir = SCRIPT_DIR / "out"
        before = _dir_stats(out_dir)
        row = {"profile": "(本项目 out/)", "current": False, "dir": str(out_dir),
               "before_files": before["files"], "before_mib": before["mib"]}
        if args.clear:
            done = _clear_audio(out_dir)
            after = _dir_stats(out_dir)
            row.update({"deleted_files": done["files"], "deleted_mib": done["mib"],
                        "after_files": after["files"], "after_mib": after["mib"]})
        rows.append(row)

    if args.json:
        print(json.dumps({"current_profile": cur, "rows": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"当前 profile: {cur}    (数据根: {hermes_root()})")
    if not args.clear:
        print(f"{_pad('profile', 12)}{'文件':>6}{'大小 MiB':>12}  目录")
        for r in rows:
            tc = r.get("tts_cache") or {}
            print(f"{_pad(r['profile'], 12)}{r['before_files']:>6}{r['before_mib']:>12.2f}  {r['dir']}"
                  f"   [缓存 {tc.get('entries', 0)} 条 / {tc.get('mib', 0)} MiB]")
        print("\n清理: cache --clear [--profile NAME | --all-profiles] [--include-out]")
        return 0
    total_c = sum((r.get("tts_cache_deleted") or {}).get("files", 0) for r in rows)
    total_cm = sum((r.get("tts_cache_deleted") or {}).get("mib", 0) for r in rows)
    total_f = sum(r.get("deleted_files", 0) for r in rows)
    total_m = sum(r.get("deleted_mib", 0) for r in rows)
    for r in rows:
        print(f"[{r['profile']}] 删除 {r.get('deleted_files', 0)} 个文件 / {r.get('deleted_mib', 0):.2f} MiB"
              f" → 剩余 {r.get('after_files', 0)} 个 / {r.get('after_mib', 0):.2f} MiB  ({r['dir']})")
    print(f"其中音频缓存 {total_c} 个文件（含 .json 旁注）/ {total_cm:.2f} MiB")
    print(f"合计释放 {total_f + total_c} 个文件 / {total_m + total_cm:.2f} MiB")
    return 0


def _disp_len(value) -> int:
    """粗略显示宽度：CJK/全角按 2 列，其余 1 列。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(value))


def _pad(value, width: int) -> str:
    text = str(value)
    return text + " " * max(0, width - _disp_len(text))


def _voice_list(reg: dict) -> list:
    return list(reg["voices"].items())


def _print_voice_menu(reg: dict, profile: str, state: dict) -> None:
    cur_v = state.get("voice") or active
    active = reg.get("active")
    voice_default_lang = (reg["voices"].get(cur_v) or {}).get("language")
    if state.get("language"):
        eff_lang, lang_src = state["language"], "本 profile 指定"
    elif voice_default_lang:
        eff_lang, lang_src = voice_default_lang, "该音色默认"
    else:
        eff_lang, lang_src = "自动判定(zh/en)", "无指定"

    print(f"当前 profile: {profile}    (生效音色: {cur_v})")
    print(f"  音频语言: {eff_lang}  ← 来源：{lang_src}"
          f"    翻译: {'on' if state.get('translate') is not False else 'off'}"
          f"    标签: {'on' if state.get('tags') is not False else 'off'}"
          f"    缓存: {'on' if state.get('cache') is not False else 'off'}"
          f"    动作: {'on' if state.get('actions') else 'off'}"
          f"    指令: {state.get('instruction') or '-'}")
    if cur_v != active:
        print(f"  全局 active（无 profile 指定时的兜底）: {active}")
    print()
    print(f"{'#':>3}  {_pad('名称', 18)}{_pad('id', 72)}{_pad('类型', 9)}{_pad('模型', 26)}音色默认语言")
    for idx, (vid, v) in enumerate(_voice_list(reg), start=1):
        tag = " ←本profile" if vid == cur_v else ("  (全局)" if vid == active else "")
        print(f"{idx:>3}  {_pad(str(v.get('display') or vid)[:15], 18)}{_pad(vid, 72)}"
              f"{_pad(v.get('kind') or '', 9)}{_pad(v.get('model') or '', 26)}"
              f"{v.get('language') or '-'}{tag}")
    print("  注：最后一列是「该音色自身的默认语言」，不是本 profile 实际生效的语言（见上方 音频语言）。")
    print()
    print('切换: bailian_tts.py switch <编号|id> [--language zh] [--instruction "语气要求"]')
    print("     只想改语言: bailian_tts.py switch --language en")


def cmd_switch(args) -> int:
    """为某个 profile 指定音色/语言/指令；不带音色时列出全部音色。"""
    reg = seed_system_voices(load_registry())
    profile = args.profile or current_profile()
    state = profile_state(profile)

    if (not args.voice and args.language is None and args.instruction is None
            and args.translate is None and args.tags is None and args.actions is None
            and args.cache is None):
        _print_voice_menu(reg, profile, state)
        return 0

    data = load_profiles()
    entry = data["profiles"].setdefault(profile, {})
    changed = {}

    if args.voice:
        wanted = args.voice.strip()
        if wanted.isdigit():
            pool = _voice_list(reg)
            idx = int(wanted)
            if not 1 <= idx <= len(pool):
                die(f"编号越界：1~{len(pool)}")
            vid = pool[idx - 1][0]
        else:
            vid, _ = resolve_voice(reg, wanted)
        entry["voice"] = vid
        changed["voice"] = vid

    if args.language is not None:
        raw = args.language.strip()
        if raw.lower() in ("", "auto", "none", "default"):
            entry.pop("language", None)
            # 只清 profile 层不够：音色自身的默认语言会顶上来，导致“auto 却仍固定某种语言”
            vid_for_lang = entry.get("voice") or reg.get("active")
            if vid_for_lang in reg["voices"] and reg["voices"][vid_for_lang].pop("language", None) is not None:
                save_registry(reg)
                changed["voice_default_language_cleared"] = True
            changed["language"] = "auto"
        else:
            normalize_language(raw)          # 校验，非法直接退出
            entry["language"] = raw
            changed["language"] = raw

    if args.cache is not None:
        entry["cache"] = args.cache.lower() in ("on", "true", "yes", "1")
        changed["cache"] = entry["cache"]

    if args.tags is not None:
        entry["tags"] = args.tags.lower() in ("on", "true", "yes", "1")
        changed["tags"] = entry["tags"]

    if args.actions is not None:
        entry["actions"] = args.actions.lower() in ("on", "true", "yes", "1")
        changed["actions"] = entry["actions"]

    if args.translate is not None:
        if args.translate.lower() in ("on", "true", "yes", "1"):
            entry["translate"] = True
            changed["translate"] = True
        else:
            entry["translate"] = False
            changed["translate"] = False

    if args.instruction is not None:
        if args.instruction.strip():
            entry["instruction"] = args.instruction.strip()
        else:
            entry.pop("instruction", None)
        changed["instruction"] = entry.get("instruction")

    if not entry.get("voice"):
        entry["voice"] = reg.get("active")
    entry["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_profiles(data)

    cfg = _write_profile_voice(profile, entry["voice"])
    voice_meta = reg["voices"].get(entry["voice"], {})
    out = {"ok": True, "profile": profile, "state": entry, "changed": changed,
           "model": voice_meta.get("model"), "config": cfg,
           "profiles_json": str(PROFILE_STATE_PATH)}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        lang = entry.get("language")
        print(f"✓ profile '{profile}' → 音色 {entry['voice']}"
              f"（{voice_meta.get('display') or ''}，模型 {voice_meta.get('model')}）"
              f"  语言 {lang or '自动'}  动作 {'on' if entry.get('actions') else 'off'}"
              f"  指令 {entry.get('instruction') or '-'}")
        print(f"  已写入 {cfg['config']}  (tts.providers.bailian.voice，回读校验={cfg['verified']})")
        print(f"  profile 状态: {PROFILE_STATE_PATH}")
    return 0 if cfg["verified"] else 2


def cmd_status(args) -> int:
    reg = seed_system_voices(load_registry())
    prof = current_profile()
    adir = audio_dir_for(home_for_profile(prof))
    info = {
        "profile": prof,
        "audio_dir": str(adir),
        "audio": _dir_stats(adir),
        "profile_state": profile_state(prof),
        "profiles_json": str(PROFILE_STATE_PATH),
        "tts_cache": cache_stats(prof),
        "registry": str(REGISTRY_PATH),
        "endpoint": base_url(),
        "active": reg.get("active"),
        "default_model": reg.get("default_model"),
        "voices_local": len(reg.get("voices") or {}),
        "api_key": "ok" if (os.environ.get("DASHSCOPE_API_KEY") or read_api_key()) else "missing",
    }
    try:
        key = read_api_key()
        remote = _remote_list(key)
        info["remote_custom_voices"] = len(remote)
        info["remote"] = [{"voice_id": v.get("voice_id"), "status": v.get("status"),
                           "target_model": v.get("target_model")} for v in remote[:10]]
        info["reachable"] = True
    except SystemExit:
        info["reachable"] = False
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bailian_tts", description="阿里百炼 TTS：合成 / 音色定制 / 音色复刻 / 音色切换")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("say", help="文本转语音（Hermes 命令型 provider 调用这个）")
    s.add_argument("--text", "-t")
    s.add_argument("--text-file", "--input-path", dest="text_file", help="UTF-8 文本文件（Hermes 用它传长文本）")
    s.add_argument("--out", "--output-path", "-o", dest="out")
    s.add_argument("--voice", "-v", default="")
    s.add_argument("--model", "-m")
    s.add_argument("--format", "-f", default="")
    s.add_argument("--language", "--lang", "-l", dest="language")
    s.add_argument("--instruction", "-i", help="自然语言指令：情感/风格/方言，如 \"用温柔的语气，语速稍慢\"")
    s.add_argument("--rate", help="语速 0.5~2.0")
    s.add_argument("--speed", help="Hermes 占位符 {speed} 用，等同 --rate")
    s.add_argument("--pitch", help="音调 0.5~2.0")
    s.add_argument("--volume", help="音量 0~100")
    s.add_argument("--sample-rate", dest="sample_rate", help="采样率，默认 24000")
    s.add_argument("--bit-rate", dest="bit_rate", help="opus 码率 kbps")
    s.add_argument("--seed")
    s.add_argument("--ssml", action="store_true", help="文本按 SSML 解析（会跳过翻译）")
    s.add_argument("--no-translate", dest="no_translate", action="store_true",
                   help="不做语言对齐（原文直接合成）")
    s.add_argument("--force-translate", dest="force_translate", action="store_true",
                   help="无视语种检测结果，强制翻译成音频指定语言")
    s.add_argument("--from-language", dest="from_language", help="指定源语言代码，跳过自动检测")
    s.add_argument("--no-cache", dest="no_cache", action="store_true",
                   help="本次不复用音频缓存（默认命中就复用）")
    s.add_argument("--uid", help="稳定标识（如 Hermes 的 message_uid），参与缓存 key")
    s.add_argument("--no-tags", dest="no_tags", action="store_true",
                   help="不加情感/富语言标签（默认由主模型按内容自动标注）")
    s.add_argument("--tag-model", dest="tag_model", help="标注用的模型 id，默认同主模型")
    s.add_argument("--strip-actions", dest="strip_actions", action="store_true",
                   help="本次强制去掉（动作）括号内容（默认跟随 profile 的 actions 开关）")
    s.add_argument("--keep-actions", dest="keep_actions", action="store_true",
                   help="本次保留括号内容并原样朗读")
    s.add_argument("--translator", choices=["main", "mt"], default="main",
                   help="翻译器：main=回复主模型（默认），mt=百炼 qwen-mt 专用翻译模型")
    s.add_argument("--translate-model", dest="translate_model",
                   help=f"覆盖模型 id：main 时指主模型 id，mt 时指 MT 模型（默认 {DEFAULT_MT_MODEL}）")
    s.add_argument("--translate-only", dest="translate_only", action="store_true",
                   help="只翻译不合成，便于核对结果")
    s.set_defaults(func=cmd_say)

    d = sub.add_parser("design", help="音色定制：用一段文字描述生成新音色")
    d.add_argument("--prompt", "-p", required=True, help="声音描述（≤500字符）")
    d.add_argument("--preview-text", dest="preview_text", help="预览文本（15~200字符）")
    d.add_argument("--name", "-n", help="音色名前缀（字母数字，≤10字符）")
    d.add_argument("--model", "-m")
    d.add_argument("--language", "--lang", "-l", dest="language")
    d.add_argument("--preview-out", dest="preview_out")
    d.add_argument("--use", action="store_true", help="创建后立即切换为当前音色")
    d.set_defaults(func=cmd_design)

    c = sub.add_parser("clone", help="音色复刻：用参考音频生成音色")
    c.add_argument("--audio", "-a", required=True, help="本地 WAV/MP3/M4A 路径，或公网 URL")
    c.add_argument("--name", "-n", help="音色名前缀（字母数字，≤10字符）")
    c.add_argument("--model", "-m")
    c.add_argument("--language", "--lang", "-l", dest="language", help="样本语种提示，如 zh/en")
    c.add_argument("--max-audio-length", dest="max_audio_length", help="参考音频最大秒数 3~30，默认 10")
    c.add_argument("--preprocess", action="store_true", help="开启降噪/增强（有噪声时开）")
    c.add_argument("--use", action="store_true", help="创建后立即切换为当前音色")
    c.set_defaults(func=cmd_clone)

    v = sub.add_parser("voices", help="列出音色")
    v.add_argument("--kind", choices=["all", "system", "clone", "design", "custom"], default="all")
    v.add_argument("--remote", action="store_true", help="同时拉取云端自定义音色")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_voices)

    u = sub.add_parser("use", help="切换当前音色（支持 id / 显示名 / 唯一子串）")
    u.add_argument("voice", nargs="?", default=None)
    u.add_argument("--language", "--lang", "-l", dest="language")
    u.add_argument("--instruction", "-i")
    u.add_argument("--model", "-m")
    u.set_defaults(func=cmd_use)

    rm = sub.add_parser("delete", help="删除自定义音色（复刻/设计）")
    rm.add_argument("voice", nargs="?", default=None)
    rm.add_argument("--force", action="store_true")
    rm.set_defaults(func=cmd_delete)

    sw = sub.add_parser("switch", help="为当前 profile 指定音色/语言；不带参数则列出全部音色")
    sw.add_argument("voice", nargs="?", default=None, help="音色编号（先用无参 switch 看列表）或 id")
    sw.add_argument("--profile", help="目标 profile，默认当前 profile")
    sw.add_argument("--language", "--lang", "-l", dest="language",
                    help="该 profile 对该音色的默认语言；写 auto 清除")
    sw.add_argument("--instruction", "-i", help="默认语气指令，如 \"语速稍慢，语气温柔\"")
    sw.add_argument("--cache", choices=["on", "off"],
                    help="是否启用内容寻址音频缓存（默认 on）")
    sw.add_argument("--tags", choices=["on", "off"],
                    help="是否让主模型给合成文本加情感/富语言标签（默认 on）")
    sw.add_argument("--translate", choices=["on", "off"],
                    help="文本语种与指定语言不同时是否先翻译（默认 on）")
    sw.add_argument("--actions", choices=["on", "off"],
                    help="去掉（动作）括号内容不朗读，仅作情感标签依据：on 开 / off 关")
    sw.add_argument("--json", action="store_true")
    sw.set_defaults(func=cmd_switch)

    ch = sub.add_parser("cache", help="按 profile 定位/清理音频缓存目录")
    ch.add_argument("--clear", action="store_true", help="真的删除（默认只统计）")
    ch.add_argument("--profile", help="只看/只清某个 profile")
    ch.add_argument("--all-profiles", dest="all_profiles", action="store_true", help="所有 profile")
    ch.add_argument("--include-out", dest="include_out", action="store_true", help="连带清项目 out/")
    ch.add_argument("--keep-cache", dest="keep_cache", action="store_true",
                    help="--clear 时保留 tts_cache（默认一起清）")
    ch.add_argument("--json", action="store_true")
    ch.set_defaults(func=cmd_cache)

    wh = sub.add_parser("where", help="打印各 profile 的音频缓存目录与配置文件位置")
    wh.add_argument("--profile")
    wh.set_defaults(func=cmd_where)

    st = sub.add_parser("status", help="自检：key / 端点 / 云端音色")
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
