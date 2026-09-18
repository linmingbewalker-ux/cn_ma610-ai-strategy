"""
api_keys_config.py
=================================================================================
统一 API Key 加载器：从 `<你的API目录>/API`（候选目录列表，自动兜底旧路径）下的明文 .txt 读取
deepseek / doubao(豆包·标准方舟) / zhipu(智谱) / kimi 的 key，桥接到各 AI 系统。

设计原则（安全 + 好用）：
- 明文 key 只存在对应的 .txt 里，本 .py 不含任何 key，入库也不泄密。
- 任何日志/打印都经 mask_key() 脱敏，绝不回显明文。
- 文件缺失/为空则跳过该 provider（不报错），方便缺哪个少哪个。
- 路径带空格也能用（os.path.join + 显式目录）。

用法：
    from api_keys_config import load_keys, mask_key, available_providers
    keys = load_keys()                       # -> {"deepseek": "...", "qwen": "...", "zhipu": "...", "kimi": "..."}
    print({k: mask_key(v) for k, v in keys.items()})   # 只显示 sk-0da0…55a6
"""
import os
import re

# 从「可能含说明文档」的 key 文件里抽取真正的 key。
# - deepseek / zhipu / kimi 用 sk- 开头；doubao(豆包)用 ark- 开头且为 UUID 格式。
# - 注意：豆包_mini.txt 里有一行示例域名 ark-project.tos-cn-beijing.volces.com（陷阱），
#   必须用 UUID 精确正则跳过它，只命中真正的 ark-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx-xxxx key。
_KEY_RE = re.compile(r"sk-[A-Za-z0-9._\-]+")
_ARK_RE = re.compile(r"ark-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{4,8}")


def _extract_key(text: str) -> str:
    """从文件全文里抽取 key：优先 ark- UUID（豆包），其次 sk-（其他三家）；找不到则整段 strip。"""
    m = _ARK_RE.search(text or "")
    if m:
        return m.group(0)
    m = _KEY_RE.search(text or "")
    if m:
        return m.group(0)
    return (text or "").strip()


# provider -> 文件名（与 API 目录下实际文件名一致）
KEY_FILES = {
    "deepseek": "deepseek_api.txt",
    "doubao":   "豆包_mini.txt",          # 交易员角色（标准方舟 API，ark- UUID 格式）
    "zhipu":    "智谱api.txt",
    "kimi":     ["kimi_api.txt", "KIMI.txt"],
}

# 候选 API 目录（按优先级逐个尝试；目录改名/迁移后仍能自动找到 key）
# 【已脱敏 2026-09-18】原本这里写的是本机真实路径（含目录名），属于私人信息，已换成占位符。
# 用这个文件之前，把下面的 <你的API目录> 换成你自己的实际路径即可 ——
# 没换之前 load_keys() 找不到目录会返回空，策略会提示"未找到 API Key"，这是正常的。
DEFAULT_API_DIRS = [
    r"<你的API目录>/API",
]
DEFAULT_API_DIR = DEFAULT_API_DIRS[0]   # 兼容旧调用（可用 provider 显示用）

# 四家 OpenAI 兼容端点（与 AI_multiagent_debate_v1.ENDPOINTS 保持一致）
# 【已脱敏 2026-09-18】qwen 这条原本写的是"阿里云百炼专属网关域名"（域名里带租户标识，属私人信息），
#   已换成官方公开端点。如果你自己用的是专属网关，把这一行改回你自己的地址即可。
ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "qwen":     "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "zhipu":    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "kimi":     "https://api.moonshot.cn/v1/chat/completions",
}
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "qwen": "qwen3.7-plus",
                  "zhipu": "glm-4", "kimi": "kimi-k3"}


def mask_key(key: str, keep: int = 4) -> str:
    """脱敏显示：sk-0da0…55a6（只露前缀+末尾4位）。"""
    if not key:
        return "<empty>"
    key = key.strip()
    if len(key) <= keep * 2:
        return "*" * len(key)
    return f"{key[:keep]}…{key[-keep:]}"


def load_keys(api_dir: str = None) -> dict:
    """
    读取 key 文件，返回 {provider: key}。
    api_dir 为空时遍历 DEFAULT_API_DIRS 候选目录，任一目录命中即用。
    文件不存在或为空则跳过该 provider（不报错）。
    """
    dirs = list(DEFAULT_API_DIRS) if api_dir is None else [api_dir]
    keys = {}
    for provider, fnames in KEY_FILES.items():
        if isinstance(fnames, str):
            fnames = [fnames]
        key = None
        for d in dirs:
            for fn in fnames:
                path = os.path.join(d, fn)
                try:
                    with open(path, encoding="utf-8") as f:
                        raw = f.read()
                except FileNotFoundError:
                    continue
                k = _extract_key(raw)
                if k:
                    key = k
                    break
            if key:
                break
        if key:
            keys[provider] = key
    return keys


def available_providers(api_dir: str = DEFAULT_API_DIR) -> list:
    return list(load_keys(api_dir).keys())


if __name__ == "__main__":
    # 自检：只打印脱敏后的可用 provider，绝不回显明文
    ks = load_keys()
    print(f"API 目录: {DEFAULT_API_DIR}")
    print(f"可用 provider: {available_providers()}")
    for p, k in ks.items():
        print(f"  {p:>8}: {mask_key(k)}")
