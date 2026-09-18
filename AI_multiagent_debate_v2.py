"""
AI_multiagent_debate_v2.py
=================================================================================
v2（2026-08-18 重构）：「四学者投票 + 计票员把关」智能体。

v1 的问题：组合经理一个人独断门控 → 前面三位学者吵得再凶也不算数（用户判“开仓系统损坏”）。
v2 改法：
  - 组合经理岗位释放为「第四位自由投票学者」，四学者平等，均可参与唇枪舌战并投票。
  - 另设【计票员】（纯代码聚合，确定性、零 API）：汇总四张票、按多数原则把关，
    触发 → 开仓程序（既有 send_order）或 平仓程序（既有 auto_close_position）。

三处锁死的投票参数（用户拍板）：
  - 开仓：至少 3 位学者各自给出 ≥【1111】的同向强票（意愿强度≥4）才开仓（=75% 强一致≥70%，用户 2026-08-20 从【111】拉高）；低自信(【1】~【111】)或人数不足一律不开；方向冲突→不开；无破例（单人【111111】不开）。
  - 平仓：至少 3 位学者各自给出 ≥【22】的同向平仓票（意愿强度≥2）才平；平仓符用【2】（退出≠反手）。
  - 四位学者固定人设：技术分析师(deepseek) / 民间高手(kimi) / 业余K线研究员(doubao,标准方舟) / 组合经理(zhipu)。

【结构】
  - 上半「纯大脑」MultiAgentDebate：辩论 + 计票员聚合，返回可执行 decision，纯 stdlib 可独立跑。
  - 讨论流程（v2 修正）：四学者话往后传各投一票 → 【系统】显式提醒“对话结束” → 把纸条递给【计票员】（代码）计票把关。
  - 下半「策略骨架」DebateStrategy：接 pythongo SDK 完整交易闭环（含导演作息/亏损熔断/会计小兵）。
"""
import json
import os
import time
import threading
import urllib.request
import urllib.error
from collections import deque

# 冷静期 15 问（预热期自检用，现成文件；无则跳过）
# 【重要】pythongo 沙箱 sys.path 不含策略目录，常规 from cooling_15 import 会失败 →
# 必须按本文件同目录绝对路径 importlib 加载（同 api_keys_config 手法，2026-08-24 实盘暴露）
build_cooling_questions = None
try:
    import importlib.util as _ilu
    _cooling_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cooling_15.py")
    if os.path.exists(_cooling_path):
        _cspec = _ilu.spec_from_file_location("local_cooling_15", _cooling_path)
        _cmod = _ilu.module_from_spec(_cspec)
        _cspec.loader.exec_module(_cmod)
        build_cooling_questions = _cmod.build_cooling_questions
except Exception:
    build_cooling_questions = None
_REAL_TIME = time.time   # 捕获真实时钟；回放脚本会 monkey-patch time.time 为假时钟，网络层需临时恢复它以避免 Windows Errno 22
# 强制国内直连：四家 API 均为国内服务（deepseek/moonshot/dashscope/bigmodel），
# 不走系统/VPN 代理。断开 VPN 后系统代理残留（如 127.0.0.1:7890）会致 10061 拒绝连接。
# 用空 ProxyHandler 的 opener 让 urllib 直连，规避机器代理设置影响。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ---------------------------------------------------------------------------
# pythongo 导入（真机有，本地副本缺 core/ui 时退化为桩，不阻塞语法校验）
# ---------------------------------------------------------------------------
try:
    from pythongo.ui import BaseStrategy
    from pythongo.base import BaseParams, Field
    from pythongo.classdef import TickData, KLineData
    from pythongo.utils import KLineGenerator
    from pythongo.core import KLineStyleType
    _PYTHONGO_OK = True
except Exception:  # 本地副本缺 core.py/ui.py → 桩，仅供语法/离线可读性
    class BaseStrategy:
        def __init__(self, *a, **k):
            self.strategy_name = "DebateStrategy"
            self.trading = False
        def output(self, *m):
            print("[output]", *m)
        def on_tick(self, tick): pass
        def on_start(self): pass
        def on_stop(self): pass
        def on_trade(self, trade, log=False): pass
        def send_order(self, *a, **k): return None
        def auto_close_position(self, *a, **k): return None
        def cancel_order(self, oid): return None
        def sub_market_data(self, *a, **k): pass
    class BaseParams:
        pass
    def Field(*a, **k):
        return None
    TickData = KLineData = object
    KLineGenerator = None
    KLineStyleType = "M1"
    _PYTHONGO_OK = False


# 四家 OpenAI 兼容端点（换模型只改 model，端点一般不动）
# 注：原 qwen 走的是某个「个人套餐」网关(仅交互工具,禁自动化)，已弃用；
#     改由豆包「标准方舟 API」(/api/v3 按量付费,后台调用合规)顶替业余K线研究员角色
#     【已脱敏 2026-09-18】注释里原本写了该网关的租户名，属私人信息，已删除。
ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "doubao":   "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    "zhipu":    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    "kimi":     "https://api.moonshot.cn/v1/chat/completions",
}
DEFAULT_MODELS = {"deepseek": "deepseek-chat", "doubao": "doubao-seed-2-0-lite-260215",
                  "zhipu": "glm-4", "kimi": "kimi-k3"}

# 每点金额（甲醇 MA 合约 10 吨/手 → 每点约 ¥10，用于红绿灯盈亏分级）
POINT_VALUE = 10.0
# 硬止损线（元/手）：浮亏达到 -60元/手 → 会计小兵通知计票员强制平仓（真实硬止损，市价平）【2026-08-21 用户 -50→-60，给 AI 更大扛单空间】
HARD_STOP_YUAN = 60.0
# 止盈线（点/手）：浮盈达到该点数（价格单位，1点=POINT_VALUE元）→ 会计小兵通知计票员强制止盈平仓 → 黄灯冷静
TAKE_PROFIT_POINTS = 20.0
# 亏损/止盈平仓后的黄灯冷静期（秒）
COOLDOWN_SEC = 600.0
# 开仓挂单未成交超时（秒）：超过即主动撤单（防挂单堆积/系统硬处理伤人）
PENDING_TIMEOUT = 60.0

# 【逆反弹保护】价格从日内低点反弹 ≥ 该点数 → 禁开空；从日内高点回落 ≥ 该点数 → 禁开多
# （2026-08-24 用户拍板：追反弹顶做空=逆势接刀，硬控拦截；20点=约200元/手缓冲）
REVERSAL_GUARD_POINTS = 20.0

# —— 官方 token 单价（元/百万token，输入/输出）——预算闸真 token 计价用；官方调价只改这张表 ——
# 校准日 2026-08-24（deepseek 8/17 起峰谷计价，本表取高峰价保守估算）
TOKEN_PRICE_YUAN_PER_M = {
    "deepseek": (3.0, 9.0),     # deepseek-chat(=v4-flash 非思考)；高峰 3.0/9.0，空闲减半
    "doubao":   (0.6, 3.6),     # doubao-seed-2.0-lite，输入≤32k（缓存命中仅0.12，未计）
    "zhipu":    (5.0, 15.0),    # glm-4(Plus档) 输入5/输出15
    "kimi":     (20.0, 100.0),  # kimi-k3：输入(未命中缓存)20 / 输出100（贵，输出为主）
}

# —— 投票开仓 / 平仓阈值（计票员把关，用户 2026-08-18 拍板：百分比支持率）——
# 开仓门槛（用户 2026-08-20 再拉高：从【111】提到【1111】，高自信度）
# 计票员只数 conviction 串里「1」的个数 = 意愿强度；【111】=强度3, 【1111】=强度4 …
OPEN_STRONG_CONV   = 4     # 每个同向投票者的最低意愿强度（≥【1111】才计入“强票”）
OPEN_STRONG_VOTERS = 3     # 至少 3 位学者各自给出 ≥【1111】的同向强票，才开仓（=75% 强一致 ≥70%）
CLOSE_STRONG_CONV   = 2     # 平仓：每张同向平仓票最低意愿强度 ≥【22】（数「2」个数≥2）才计入“强平票”
CLOSE_STRONG_VOTERS = 3     # 至少 3 位学者各自给出 ≥【22】的同向平仓票，才平仓（=75% 强一致）
MAX_CONV = 6     # conviction 上限（【111111】 / 【222222】）
VOTER_ROLES = ["技术分析师", "民间高手", "业余K线研究员", "组合经理"]   # 四位投票学者（含原组合经理）


def _extract_content(resp):
    """从 OpenAI 兼容返回里取发言文本。KIMI K3 等推理模型常把结论放在
    reasoning_content（思考链），content 可能为空，这里做兜底。"""
    try:
        msg = resp["choices"][0]["message"]
    except Exception:
        raise RuntimeError("API 返回缺少 choices[0].message")
    if not isinstance(msg, dict):
        return str(msg)
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or "").strip()  # K3 思考链兜底
    return content


class ChatClient:
    """一个 OpenAI 兼容客户端，带 1 次重试。纯 urllib，无 openai 依赖。"""

    def __init__(self, provider, api_key, model=None, timeout=45,
                 reasoning_effort=None, temperature=0.3):
        self.base = ENDPOINTS[provider]
        self.key = api_key
        self.model = model or DEFAULT_MODELS[provider]
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort   # KIMI K3 顶层推理强度(low/high/max)；其余模型忽略
        # 每模型默认温度；KIMI K3 推理模型只接受 temperature=1，由调用方显式传 1
        self.temperature = temperature
        self.last_usage = {}   # 最近一次调用的 usage（真实 token 用量，供预算闸精确计价）

    def chat(self, messages, temperature=None, max_tokens=1200):
        t = temperature if temperature is not None else self.temperature
        payload = {"model": self.model, "messages": messages,
                   "temperature": t, "max_tokens": max_tokens, "stream": False}
        if self.reasoning_effort:   # KIMI K3 顶层推理强度；其余模型 reasoning_effort 为 None，跳过
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"})

        # 线程级硬超时：urllib 在 Windows / 某些网络下 SSL 握手或连接阶段
        # 不尊重 timeout，会永久挂起主线程。用 daemon 子线程包住 urlopen，
        # 主线程 join 硬超时强制返回——超时或异常都判为本票弃权，
        # 不让单个模型拖死整条链（也不冒泡 raise 卡住上层）。
        out = {}
        def _worker():
            _saved = time.time
            time.time = _REAL_TIME   # 暂复原真实时钟，避开回放全局假时钟 monkey-patch 触发的 Windows Errno 22
            try:
                with _NO_PROXY_OPENER.open(req, timeout=self.timeout) as r:
                    _raw = json.loads(r.read().decode("utf-8"))
                    out["usage"] = _raw.get("usage", {})     # 真实 token 用量（预算闸计价用）
                    out["val"] = _extract_content(_raw)
            except Exception as e:
                out["err"] = e
            finally:
                time.time = _saved   # 还原为（可能是假时钟的）全局 time.time，不影响策略层节流/预热/冷却
        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(self.timeout + 5)
        self.last_usage = out.get("usage", {}) or {}
        if "val" in out:
            return out["val"]
        err = out.get("err")
        return json.dumps({"direction": "hold", "conviction": "0", "order_volume": 0,
                           "reasoning": f"[模型调用超时/失败,本票弃权] {err}"})


# 四个角色的系统提示（期货版，只基于给出的数据，不喊单幻觉）
# v2：四个 AI 都是「投票学者」，平等一票；组合经理已释放为自由投票人（不再是独断门控者）。
# 计票员（代码）独立统计四票 → 开仓/平仓程序。

# ========== 期货市场规则（通用，拼到每个角色 prompt 前，让 AI 明确交易环境）==========
# 数值以 MA610(甲醇)标准合约为准；手续费/保证金随账户不同，标注“以账户实际为准”，可按需修改。
MARKET_RULES = (
    "【交易环境·国内期货市场】| 你们的操作作为自主投研的示范研究对象 | \n"
    "· 品种：配置不同的品种，其对应不同的费率和增长单位；\n"
    "· 双向交易：『买开』为做多行为————认为未来一段时间上涨，『卖开』为做空行为————认为未来一段时间下跌；\n"
    "· T+0：日内交易，当日盘中即可买卖；\n"
    "· 手续费：单边，按实际账户的费率3折计算；以实际费率为准。每次买卖动作对应相应的扣费；\n"
    "· 保证金：对应杠杆约 10 倍，以账户实际为准，注意仓位与强平风险。\n"
    "· 涨跌停板：约 ±4%，极端行情可能封板导致无法成交或无法平仓。\n"
    "· 交易时段：日盘 09:00-10:15、10:30-11:30、13:30-15:00；夜盘 21:00-23:00/21:00-23:30/21:00-01:00。\n"
    "·请基于以上真实规则，请发表你们做多/做空/观望的看法；或数据不足时请告知需要哪方面数据，工作人员后续会补充！\n"
    "·甲醇可以根据大方向顺势去做，逆市单要提前预判挂单；\n"
    "·并在 reasoning 中简要说明方向依据与成本考量。\n\n"
)
ROLES = {
    "技术分析师": MARKET_RULES + "你是期货技术分析师，通过娴熟的技术手段来推测预演未来价格走势，技术派；"
                 "并列出【做多】与【做空】两方的主要论据（必须引用数据）。依次讨论发表意见（认同/不认同/反驳观点），四位代表性的人物请出示你的投票（硬性）："
                 '只返回 JSON：{"direction":"buy/sell/hold","conviction":"【N】或【0】","order_volume":int,"reasoning":"≤40字"}'
                 "（conviction=你的意愿强度，【N】中连1的个数=1~6，【0】=弃权）。",
    "民间高手":  MARKET_RULES + "你是期货圈民间高手，你自学成才，凭着自己一身独特的交易方法鹤立鸡群，有自己独特的判断；"
                 "看完数据用大白话给方向，依次讨论发表意见（认同/不认同/反驳观点），四位代表性的人物请出示你的投票（硬性）："
                 '只返回 JSON：{"direction":"buy/sell/hold","conviction":"【N】或【0】","order_volume":int,"reasoning":"≤40字"}'
                 "（conviction=你的意愿强度，【N】中连1的个数=1~6，【0】=弃权）。",
    "业余K线研究员":    MARKET_RULES + "你是业余K线研究员，多年来在行业立足，中规中矩；"
                 "认为前两位的观点是否和你的观点相同/背道而驰？依次讨论发表意见（认同/不认同/反驳观点），四位代表性的人物请出示你的投票（硬性）："
                 '只返回 JSON：{"direction":"buy/sell/hold","conviction":"【N】或【0】","order_volume":int,"reasoning":"≤40字"}'
                 "（conviction=你的意愿强度，【N】中连1的个数=1~6，【0】=弃权）。",
    "组合经理":  MARKET_RULES + "你是投资委员会成员（原组合经理岗位已释放为自由投票人）。你已看到前面三位全部发言，"
                 "认为前三位的观点是否和你的观点相同/背道而驰？依次讨论发表意见（认同/不认同/反驳观点），四位代表性的人物请出示你的投票（硬性）："
                 "注意：开仓/平仓由独立【计票员】统计四票后把关触发，你不再独断。"
                 '只返回 JSON：{"direction":"buy/sell/hold","conviction":"【N】或【0】","order_volume":int,"reasoning":"≤40字"}'
                 "（conviction=你的意愿强度，【N】中连1的个数=1~6，【0】=弃权）。",
}

# 角色 → 首选 provider（deepseek/doubao/zhipu/kimi 固定人设）；缺省时回退到第一个可用模型
ROLE_PROVIDER = {
    "技术分析师": "deepseek",  # deepseek：打头阵（用户 2026-08-20 拍板换回）
    "民间高手":   "kimi",      # kimi：盘感派（口语化大白话）
    "业余K线研究员":     "doubao",    # 豆包：综合建议
    "组合经理":   "zhipu",     # 智谱：放最后综合位
}


class MultiAgentDebate:
    """多模型辩论门控智能体。至少提供一个 API Key 即可跑。"""

    def __init__(self, deepseek_key="", doubao_key="", zhipu_key="", kimi_key="",
                 debate_rounds=2, memory_file="", temperature=0.3):
        # 兜底：未显式传 key 且未设环境变量时，自动从 api_keys_config 加载（开箱即用）
        if not (deepseek_key or doubao_key or zhipu_key or kimi_key
                or os.getenv("DEEPSEEK_API_KEY") or os.getenv("ARK_API_KEY")
                or os.getenv("ZHIPU_API_KEY") or os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")):
            _cfg = {}
            _load_err = None
            # ① 优先按【本文件同目录】绝对路径加载 api_keys_config，避免被同名模块/旧 .pyc 遮蔽
            try:
                import importlib.util as _ilu
                _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys_config.py")
                if os.path.exists(_cfg_path):
                    _spec = _ilu.spec_from_file_location("local_api_keys_config", _cfg_path)
                    _mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    _cfg = _mod.load_keys()
            except Exception as e:
                _load_err = e
            # ② 退路：常规 import（仍可能被遮蔽，仅兜底）
            if not _cfg:
                try:
                    from api_keys_config import load_keys as _lk
                    _cfg = _lk()
                except Exception as e:
                    _load_err = _load_err or e
            if not _cfg:
                raise RuntimeError(
                    "未找到任何 API Key（已按本策略同目录 api_keys_config 与环境变量尝试均失败）。"
                    "请确认 <你的API目录>/API 下存在：deepseek_api.txt / 豆包_mini.txt / 智谱api.txt / KIMI.txt，"
                    "或设置环境变量 DEEPSEEK_API_KEY / ARK_API_KEY / ZHIPU_API_KEY / KIMI_API_KEY。"
                    f"内部错误：{_load_err}"
                )
            deepseek_key = deepseek_key or _cfg.get("deepseek", "")
            doubao_key = doubao_key or _cfg.get("doubao", "")
            zhipu_key = zhipu_key or _cfg.get("zhipu", "")
            kimi_key = kimi_key or _cfg.get("kimi", "")
        self.clients = {}
        if deepseek_key or os.getenv("DEEPSEEK_API_KEY"):
            self.clients["deepseek"] = ChatClient("deepseek", deepseek_key or os.getenv("DEEPSEEK_API_KEY"))
        if doubao_key or os.getenv("ARK_API_KEY"):
            self.clients["doubao"] = ChatClient("doubao", doubao_key or os.getenv("ARK_API_KEY"))
        if zhipu_key or os.getenv("ZHIPU_API_KEY"):
            self.clients["zhipu"] = ChatClient("zhipu", zhipu_key or os.getenv("ZHIPU_API_KEY"))
        if kimi_key or os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY"):
            self.clients["kimi"] = ChatClient(
                "kimi", kimi_key or os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY"),
                reasoning_effort="low", temperature=1)   # K3 推理模型只接受 temperature=1
        if not self.clients:
            raise RuntimeError("至少提供一个 API Key（deepseek / doubao / zhipu / kimi）")
        self.debate_rounds = debate_rounds
        self.memory_file = memory_file or os.path.join(
            os.path.expanduser("~"), ".tradingagents_memory.md")
        self.temperature = temperature
        self.round_no = 0          # 辩论轮次计数（反馈循环用）
        self.last_result = ""      # 上轮计票员结论（回灌下一轮；调用方可用带持仓的反馈覆盖）

    def _call(self, provider, system, user, ledger=None):
        r = self.clients[provider].chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=None)   # 各客户端用各自默认温度（kimi=1，其余=0.3）
        if ledger is not None:
            ledger.append(provider)
        return r

    def _load_memory(self, n=7):
        """回放最近 n 条记忆（默认7：前5条常是hold/无效决策，多回放2条才能抓到
        上一笔真正的开平仓与复盘结果，给 AI 更完整的因果闭环）。"""
        try:
            with open(self.memory_file, encoding="utf-8") as f:
                lines = f.read().splitlines()
            return "\n".join(lines[-n:])
        except Exception:
            return ""

    def _save_memory(self, text):
        try:
            with open(self.memory_file, "a", encoding="utf-8") as f:
                f.write(f"\n- {time.strftime('%Y-%m-%d %H:%M')} {text}")
        except Exception:
            pass

    def _parse(self, text):
        text = (text or "").strip()
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            return None
        try:
            j = json.loads(text[s:e + 1])
        except Exception:
            return None
        if "direction" not in j:
            return None
        return {"direction": str(j.get("direction", "hold")).lower(),
                "offset_ticks": int(j.get("offset_ticks", 0) or 0),
                "order_volume": int(j.get("order_volume", 0) or 0),
                "conviction": str(j.get("conviction", "0") or "0"),
                "target_price": j.get("target_price"),
                "reasoning": j.get("reasoning", "")}

    # ------------------------- 计票员（代码聚合，零 API） -------------------------
    def _conv_weight(self, conv, kind="open"):
        """把 conviction 串换算成权重。kind='open' 数【1】个数，kind='close' 数【2】个数。"""
        conv = str(conv or "0")
        return min(conv.count("1" if kind == "open" else "2"), MAX_CONV)

    def _tally_votes(self, parses, mode="open", close_dir=None):
        """【计票员】汇总四学者投票，按「≥3 位强票同向(各≥【1111】)」把关开仓/平仓。返回 verdict dict。
        - 开仓 mode：只认「方向相同 且 意愿强度(_conv_weight)≥OPEN_STRONG_CONV(4)」的强票；
            · 强票数≥OPEN_STRONG_VOTERS(3) → 开（=75% 强一致≥70%）；低于此一律 hold
            · 买卖两边都有 ≥3 强票 → 方向冲突，不开
        - 平仓 mode：仅统计「方向==close_dir 且 意愿强度≥CLOSE_STRONG_CONV(2,即【22】)」的强平票；强平票数≥CLOSE_STRONG_VOTERS(3) → 平（=75% 强一致）
        - 【0】弃权 / 低自信票不计入强票
        """
        N = len(parses) or 4   # 总票数（4 位学者；【0】弃权也计入分母）
        if mode == "open":
            # —— 开仓把关（用户 2026-08-20 拍板：三个【1111】以上才可开，从【111】拉高）——
            # 计票员只数 conviction 串里「1」的个数 = 意愿强度（【111】=3,【1111】=4…）；
            # 只有「方向相同 且 意愿强度≥OPEN_STRONG_CONV」的票才算“强票”。
            buy_strong  = [p for p in parses
                           if p.get("direction") == "buy"
                           and self._conv_weight(p.get("conviction", "0"), "open") >= OPEN_STRONG_CONV]
            sell_strong = [p for p in parses
                           if p.get("direction") == "sell"
                           and self._conv_weight(p.get("conviction", "0"), "open") >= OPEN_STRONG_CONV]
            n_buy_s, n_sell_s = len(buy_strong), len(sell_strong)
            buy_w  = sum(self._conv_weight(p.get("conviction", "0"), "open") for p in buy_strong)
            sell_w = sum(self._conv_weight(p.get("conviction", "0"), "open") for p in sell_strong)
            # 方向冲突：两边都有 ≥3 位强票 → 不开（优先保一致）
            if n_buy_s >= OPEN_STRONG_VOTERS and n_sell_s >= OPEN_STRONG_VOTERS:
                return {"action": "hold", "conflict": True, "weight": 0, "voters": 0,
                        "buy_w": buy_w, "sell_w": sell_w}
            if n_sell_s >= OPEN_STRONG_VOTERS:
                return {"action": "open", "direction": "sell", "weight": sell_w,
                        "voters": n_sell_s, "support_rate": n_sell_s / N,
                        "target_price": self._consensus_price(sell_strong),
                        "buy_w": buy_w, "sell_w": sell_w}
            if n_buy_s >= OPEN_STRONG_VOTERS:
                return {"action": "open", "direction": "buy", "weight": buy_w,
                        "voters": n_buy_s, "support_rate": n_buy_s / N,
                        "target_price": self._consensus_price(buy_strong),
                        "buy_w": buy_w, "sell_w": sell_w}
            return {"action": "hold", "weight": max(buy_w, sell_w), "voters": max(n_buy_s, n_sell_s),
                    "buy_w": buy_w, "sell_w": sell_w, "buy_rate": n_buy_s / N, "sell_rate": n_sell_s / N}
        else:  # 平仓 mode
            # 只认「方向==close_dir 且 意愿强度≥CLOSE_STRONG_CONV(2,即【22】)」的强平票；
            # 强平票数≥CLOSE_STRONG_VOTERS(3) → 平（=75% 强一致）；低于此一律 hold
            close_strong = [p for p in parses
                            if p.get("direction") == close_dir
                            and self._conv_weight(p.get("conviction", "0"), "close") >= CLOSE_STRONG_CONV]
            n_close = len(close_strong)
            close_w = sum(self._conv_weight(p.get("conviction", "0"), "close") for p in close_strong)
            close_rate = n_close / N
            if n_close >= CLOSE_STRONG_VOTERS:
                return {"action": "close", "weight": close_w, "voters": n_close,
                        "support_rate": close_rate, "close_w": close_w,
                        "target_price": self._consensus_price(close_strong)}
            return {"action": "hold", "weight": close_w, "voters": n_close,
                    "close_w": close_w, "close_rate": close_rate}

    def _consensus_price(self, strong_parses):
        """【计票员·预挂单价】从强票里取 target_price 的中位数，四舍五入到最小变动价位(1元)。
        无有效价格返回 None（调用方回退现价）。"""
        prices = []
        for p in strong_parses:
            tp = p.get("target_price")
            try:
                tp = float(tp)
                if tp > 0:
                    prices.append(tp)
            except (TypeError, ValueError):
                continue
        if not prices:
            return None
        prices.sort()
        mid = prices[len(prices) // 2]          # 中位数（抗极端值）
        return round(mid)                        # 甲醇最小变动价位=1元 → 四舍五入

    def decide(self, snapshot, position=0):
        """输入行情快照文本，返回最终可执行 decision dict。position 用于区分开/平投票。"""
        sig, _, _ = self.decide_verbose(snapshot, position)
        return sig

    def decide_verbose(self, snapshot, position=0, last_result=""):
        """v2 核心：四学者唇枪舌战各投一票 → 计票员（代码）聚合把关 → 返回可执行 decision。
        反馈循环：上轮「计票员结论」由调用方通过 last_result 回灌，本轮 AI 据此知道自己开/平了没有。

        返回 (decision, log, ledger)
          decision = {"action":"open/close/hold", "direction", "order_volume", "reasoning", "tally":verdict}
          log      = [(role, text), ...]  唇枪舌战逐角色发言
          ledger   = [provider, ...]       本次实际调用的 API（供分 API 计数/成本表）
        """
        self.round_no += 1
        mem = self._load_memory()
        log = []  # (role_label, text)
        ledger = []
        feedback = last_result or self.last_result or "（首轮，暂无上轮结论）"

        # —— 任务上下文（决定本轮是“开仓投票”还是“平仓投票”）——
        if position == 0:
            mode = "open"
            task_ctx = ("\n【本轮任务】当前空仓，四位就「是否开仓」投票：明确方向(buy/sell)与意愿强度 "
                        "conviction=【N】(连1的个数,1~6,数值开仓的意愿就越大)，【0】=弃权。"
                        "【预挂单】除了投票，请再输出 target_price=你想要的成交价（数字，不加引号）："
                        "做多挂【低于现价】的价位等回调买入、做空挂【高于现价】的价位等反弹卖出；"
                        "认为现价合适可 target_price=现价附近。计票员会取同向强票价格的中位数挂单。"
                        "计票员要求：至少 3 位学者各自给出 ≥【1111】的同向强票(意愿强度≥4)才开仓；低自信(【1】~【111】)或人数不足一律不开；方向冲突不开；无破例。"
                        f"\n【反馈循环·第{self.round_no}轮】上轮计票员结论：{feedback} "
                        f"→ 你应基于上轮结论+本轮新数据继续；你要清楚自己当前是否已持仓。")
        else:
            close_dir = "sell" if position > 0 else "buy"
            mode = "close"
            task_ctx = (f"\n【本轮任务】当前持有{'多' if position>0 else '空'}单{abs(position)}手，"
                        f"四位就「是否平仓」投票：想平仓则方向写 {close_dir}(平"
                        f"{'多' if position>0 else '空'})、conviction=【2N】(连2的个数,1~6)；"
                        f"不想平写 hold、conviction=【0】。"
                        f"【预挂单平仓】想平仓请输出 target_price=你想要的平仓价（数字）："
                        f"平多挂【高于现价】等反弹卖、平空挂【低于现价】等回调买；不急可挂远点等更优价。"
                        f"计票员要求：至少 3 位学者各自给出 ≥【22】的同向平仓票(意愿强度≥2)才平。"
                        f"\n【反馈循环·第{self.round_no}轮】上轮计票员结论：{feedback} "
                        f"→ 你应基于上轮结论+本轮新数据继续；你要清楚自己当前是否已持仓。")

        # —— 任务页头：每次都提醒学者做多/做空/观望三选一，打断多头默认 ——
        QUEST_HEADER = ("你们正在做一项为期货推理性的投票工作————这对我们人类有很大的帮助；请发表你们做多/做空/观望的想法；或数据不足时及时提出;"
                         "现在是角色代入环节，有“技术分析师”、“民间高手”、“业余K线研究员”、“组合经理”四位贡献脑洞盛宴，希望以各位专业的角度给出合适的推理判断;"
                         "【1】（开仓自信度）&【2】（平仓自信度）的程度有六种，分别是：【1】、【11】、【111】、【1111】、【11111】、【111111】&【2】、【22】、【222】、【2222】、【22222】、【222222】;"
                         "甲醇流动性好，做甲醇尽可能顺势去做，尽量不要扛单；如果要做逆市单要提前预判和挂单；"
                         "欢迎你们用渊博的知识为投研工作做贡献，非常感谢！")

        # ================= 第一阶段：四学者唇枪舌战（话往后传）=================
        # 每位学者都看得到前面人的发言，依次把话往后传，各自独立投出一票。
        # 1) 技术分析师（客观 + 多空论据 + 投票）
        analyst = self._call(self._resolve("技术分析师"), ROLES["技术分析师"],
                            f"{QUEST_HEADER}\n{snapshot}\n历史决策记忆(供参考,别盲从):\n{mem}{task_ctx}", ledger=ledger)
        log.append(("技术分析师", analyst))
        # 2) 民间高手（实战盘感派，独立于学院派视角 + 投票）
        folk = self._call(self._resolve("民间高手"), ROLES["民间高手"],
                         f"{QUEST_HEADER}\n{snapshot}\n历史决策记忆(供参考,别盲从):\n{mem}\n技术分析师看法:{analyst}{task_ctx}", ledger=ledger)
        log.append(("民间高手", folk))
        # 3) 业余K线研究员综合多空 + 民间高手盘感 + 投票
        trader = self._call(self._resolve("业余K线研究员"), ROLES["业余K线研究员"],
                           f"{QUEST_HEADER}\n数据:{snapshot}\n历史决策记忆(供参考,别盲从):\n{mem}\n技术分析师:{analyst}\n民间高手:{folk}{task_ctx}", ledger=ledger)
        log.append(("业余K线研究员", trader))
        # 4) 组合经理（已释放为第四位自由投票学者，看到前面全部发言 + 投票）
        pm = self._call(self._resolve("组合经理"), ROLES["组合经理"],
                       f"{QUEST_HEADER}\n{snapshot}\n历史决策记忆(供参考,别盲从):\n{mem}\n技术分析师:{analyst}\n民间高手:{folk}\n业余K线研究员建议:{trader}{task_ctx}", ledger=ledger)
        log.append(("组合经理", pm))

        # —— 把四份投票收进【纸条】（结构化，待递交给计票员）——
        raw_notes = [
            ("技术分析师", self._parse(analyst)),
            ("民间高手", self._parse(folk)),
            ("业余K线研究员", self._parse(trader)),
            ("组合经理", self._parse(pm)),
        ]
        note = [p for _, p in raw_notes if p]
        # 记录员用：四家各自票型（带角色标签），供结构化记录"谁投了啥"
        votes_struct = [{"role": role, "direction": p["direction"],
                         "conv": str(p.get("conviction", "0") or "0"),
                         "weight": self._conv_weight(p.get("conviction", "0"), mode),
                         "target_price": p.get("target_price"),
                         "reasoning": p.get("reasoning", "")}
                        for role, p in raw_notes if p]
        # 【预挂单·讨论打印】每个成员报的目标价都打印出来，方便人核对"谁想挂什么价"
        _price_lines = ", ".join(
            f"{r['role']}={r['target_price']}" if r.get("target_price") else f"{r['role']}=未报"
            for r in votes_struct)
        log.append(("■报价", f"各成员目标价：{_price_lines}"))

        # ================= 【对话结束】提醒：本轮讨论终止 =================
        # 四学者发言完毕，必须显式告知“对话结束”，这一轮唇枪舌战到此为止。
        log.append(("■系统", f"【第{self.round_no}轮·对话结束】四位学者发言完毕，本轮唇枪舌战到此终止。"
                              "现将讨论纸条（含四份投票）郑重递交给【计票员】统计把关。"
                              f"（上轮结论回灌：{feedback}）"))

        # ================= 第二阶段：把纸条递给【计票员】（代码聚合，零 API）=================
        # 计票员（代码）收到纸条 → 按多数原则计票把关（开仓/平仓/不开）。
        close_dir = "sell" if position > 0 else "buy" if position < 0 else None
        verdict = self._tally_votes(note, mode, close_dir=close_dir)
        _rate = verdict.get('support_rate') or verdict.get('buy_rate') or verdict.get('sell_rate') or verdict.get('close_rate')
        log.append(("■计票员",
                    f"【计票员】收到纸条，开始计票：四票中"
                    f"{' 买Σ【1】='+str(verdict.get('buy_w','-')) if mode=='open' else ''}"
                    f"{' 卖Σ【1】='+str(verdict.get('sell_w','-')) if mode=='open' else ''}"
                    f"{' 平Σ【2】='+str(verdict.get('close_w','-')) if mode=='close' else ''}"
                    f"{(' 同向支持率='+format(_rate,'.2f')) if _rate is not None else ''}"
                    f" → 结论：{verdict['action']}"))
        # 记录本轮结论，作为「上轮结果」回灌下一轮（调用方可在得到真实持仓后覆盖为带持仓的反馈）
        self.last_result = (f"{verdict['action']}"
                            + (f" {verdict['direction']}" if verdict.get('direction') not in (None, 'hold') else ""))

        if verdict["action"] == "open":
            decision = {"action": "open", "direction": verdict["direction"],
                        "order_volume": 1, "reasoning": f"计票员:开仓共识 W={verdict['weight']} N={verdict['voters']}",
                        "target_price": verdict.get("target_price"),
                        "tally": verdict, "votes": votes_struct}
        elif verdict["action"] == "close":
            decision = {"action": "close", "direction": close_dir,
                        "order_volume": abs(position), "reasoning": f"计票员:平仓共识 C={verdict['weight']} N={verdict['voters']}",
                        "target_price": verdict.get("target_price"),
                        "tally": verdict, "votes": votes_struct}
        else:
            decision = {"action": "hold", "direction": "hold", "order_volume": 0,
                        "reasoning": f"计票员:共识不足(开W={verdict.get('weight',0)}/方向冲突={verdict.get('conflict',False)})",
                        "tally": verdict, "votes": votes_struct}
        self._save_memory(f"{decision['action']} {decision.get('direction')} {decision['reasoning']}")
        return decision, log, ledger

    def _resolve(self, role):
        """把角色映射到实际可用的 provider；首选 ROLE_PROVIDER，缺失则回退到第一个可用模型。"""
        pref = ROLE_PROVIDER.get(role)
        if pref in self.clients:
            return pref
        return list(self.clients)[0]


# ===========================================================================
# 下半部分：策略骨架（继承 pythongo BaseStrategy，接上完整交易闭环）
# ===========================================================================

class Light:
    """红绿灯状态（用户定义语义）"""
    GREEN  = "绿灯"   # 无持仓 → 可交易
    RED    = "红灯"   # 持仓盈利 → 只 close / hold
    BLUE   = "蓝灯"   # 持仓亏损 → 闪烁提示（深度分级），只平/持
    YELLOW = "黄灯"   # 亏损平仓后冷静期 → 只观察不开仓（思考+扣1）


class LightConfig(BaseParams):
    """参数映射模型（真机由 pythonGo 界面填 exchange / instrument_id）"""
    exchange: str = Field(default="", title="交易所代码")
    instrument_id: str = Field(default="", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")


# —— 会计小兵（幕后记账，不干预交易决策）——
try:
    from accountant import TradeAccountant
except Exception:
    TradeAccountant = None


class AI_multiagent_debate_v2(BaseStrategy):
    """多 agent 辩论策略：tick→K线→快照→辩论→红绿灯门控→下单/平/撤/反手。"""

    def __init__(self):
        super().__init__()
        self.params_map = LightConfig()

        self.max_volume = 1                      # ★ 最大开仓数 = 1 手
        self.debater = MultiAgentDebate(debate_rounds=1)  # 自动读 key

        # —— 终极停止闸：四位学者必须齐备，缺一即停（不静默降级成 1 个 AI）——
        if len(self.debater.clients) < 4:
            self._halted = True
            self.output(f"[停止闸] 仅 {len(self.debater.clients)} 个AI可用(需4位学者齐备) → 停止，不降级")

        # 持仓状态（本地维护，on_trade 同步）
        self.position = 0                        # 净持仓：+多 / -空
        self.entry_price = 0.0
        self.entry_ts = 0.0                       # 开仓时戳（持票时长用；平仓归零）
        self.pending_order_id = None             # 挂单号（供撤单；成交/撤单后清空）
        self.pending_order_ts = 0.0              # 挂单时间戳（超时自动撤单用）
        self._open_note = None                   # 本轮开仓结果备注（拦截/成功/失败，回灌反馈用）
        self._event_note = None                  # 【事件部门】后台事件（成交/撤单/手动平仓等），回灌下一轮 AI
        self._hard_stop_fired = False            # 硬止损哨兵（防重复下单，平仓后复位）
        self._tp_fired = False                   # 止盈哨兵（防重复下单，平仓后复位）
        self._pos_synced_by_reconcile = False    # 对账哨兵已抢先同步持仓（防止 on_trade 延迟回报双重记账）

        # 红绿灯
        self.light = Light.GREEN
        self.cooldown_until = 0.0                # 黄灯冷静期截止时间戳
        self.cooldown_score = 0                  # 黄灯扣 1 分累计
        self._cooling_done = False               # 预热期 15 问自检只做一次（防每 tick 重复烧钱）
        self._hard_stop_fired = False            # 会计小兵硬止损哨兵：本笔持仓已触发强制平仓（防重复下单）

        self.kline_generator = None

        # 价格/K线滚动缓冲（供 50 维特征 / 五档深度 / 盘面图）
        self.price_buf = deque(maxlen=240)   # 最新价时间序列
        self.kline_buf = deque(maxlen=60)    # 合成 K 线（on_bar 写入）
        self._vol_window = deque(maxlen=20)  # 每 tick 成交量增量
        self._prev_volume = 0.0              # 上 tick 累计成交量
        self.day_high = -1e18                # 日内最高价（逆反弹保护用，首 tick 起跟踪）
        self.day_low = 1e18                  # 日内最低价（逆反弹保护用）

        # ——— 安全闸配置（节流 / 预算 / killfile / 输出截断）———
        self.ai_interval = 30.0          # 节流：每隔 N 秒才辩论一次，中间沿用上次决策
        self.max_api_calls = 1000        # 预算闸：累计 API 调用上限（成本闸先到则以此为准）
        self.max_cost_yuan = 15.0        # 预算闸：累计估算成本上限(元)【2026-08-24 5→15，真 token 计价后 5 元只够约40分钟辩论】
        self.COST_PER_CALL = 0.005       # 单次辩论调用的估算成本(元)，用于上限判断
        self._api_calls = 0
        self._est_cost = 0.0
        self._api_calls_by_provider = {}   # 分 API 计数：{provider: 调用次数}
        self._last_decision_time = 0.0
        self._cached_sig = {"direction": "hold", "offset_ticks": 0,
                            "order_volume": 0, "reasoning": ""}
        self._last_round_result = ""   # 上轮计票员结论+净持仓（回灌下一轮辩论，让 AI 知道开/平了没）
        self._halted = False
        self.max_print_chars = 240       # 控制台单条发言截断长度（全量仍落盘）

        # ——— 导演（作息管理 + 亏损熔断）———
        self.warmup_sec = 300.0          # ① 开盘准备期：首 tick 起 5 分钟内只布景不开仓
        self._first_ts = None            # 首 tick 数据时间（回放=假数据钟 / live=真钟）
        self._status_ts = 0.0            # 节流：持续状态日志上次打印时间戳
        self._status_tag = ""            # 节流：上次打印的状态标签（状态切换即立即打）
        self.session_windows = [         # ③ 中场休息：仅这些时段内可交易（MA609）
            ("21:00", "23:00"),          #   夜盘
            ("09:00", "10:15"),          #   日盘第一段
            ("10:30", "11:30"),          #   日盘第二段
            ("13:30", "15:00"),          #   日盘第三段
        ]
        self.max_daily_loss = 100.0      # ⑤ 亏损硬闸阈值（元）· 会计小兵触发后全停
        self.director_breached = False   # ⑤ 熔断标志镜像（会计小兵置位后这里也置位）
        try:
            _here = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            _here = os.getcwd()
        self.killfile = os.path.join(_here, "STOP_debate.flag")        # 建空文件即停
        self.debate_log_path = os.path.join(_here, "debate_log.txt")   # 辩论全量落盘
        self.vote_log_path = os.path.join(_here, "vote_log.csv")        # 投票账（记录员）落盘

        # 【盘前观点】人类参考（daily_note.txt，每天开盘前可改；AI 当背景知识，非开仓信号）
        # 只取「【今日日期】」起的正文，过滤 # 注释行（模板说明是给人看的，不进 AI 纸条）
        self.daily_note = ""
        try:
            _note_path = os.path.join(_here, "daily_note.txt")
            if os.path.exists(_note_path):
                with open(_note_path, encoding="utf-8") as _f:
                    _txt = _f.read()
                if "【今日日期】" in _txt:
                    _txt = _txt.split("【今日日期】", 1)[1]
                _lines = [ln for ln in _txt.splitlines() if ln.strip() and not ln.strip().startswith("#")]
                self.daily_note = "\n".join(_lines).strip()
        except Exception:
            pass

        # 会计小兵：幕后记录盈亏/交易（不参与决策，record_fill 自带 try 兜底）
        self.accountant = (TradeAccountant(point_value=POINT_VALUE,
                                           fee_per_leg=5.0,
                                           instrument=self.params_map.instrument_id or "MA",
                                           log_fn=self.output,
                                           max_daily_loss=self.max_daily_loss)
                           if TradeAccountant else None)

    # ------------------------- 生命周期 -------------------------
    def on_start(self) -> None:
        if KLineGenerator is not None and self.params_map.instrument_id:
            self.kline_generator = KLineGenerator(
                callback=self.on_bar,
                real_time_callback=self.on_bar,
                exchange=self.params_map.exchange,
                instrument_id=self.params_map.instrument_id,
                style=self.params_map.kline_style or "M1")
            # push_history_data 须在 super().on_start() 之前，避免历史数据触发下单
            self.kline_generator.push_history_data()
        super().on_start()
        self.output(f"【启动】最大开仓={self.max_volume}手 | 红绿灯={self.light} | "
                    f"辩论源={list(self.debater.clients.keys())} | SDK={'真机' if _PYTHONGO_OK else '本地桩'}")
        self.output(f"【安全闸】节流={self.ai_interval:.0f}s 预算上限=¥{self.max_cost_yuan:.2f}/{self.max_api_calls}调用 "
                    f"| killfile={os.path.basename(self.killfile)} | 控制台截断={self.max_print_chars}字 "
                    f"| 辩论落盘={os.path.basename(self.debate_log_path)}")

    def on_stop(self) -> None:
        super().on_stop()
        # 记录员：策略停时导出投票账（与成交台账同目录）
        if self.accountant is not None:
            try:
                self.accountant.export_vote_csv(self.vote_log_path)
                self.output(f"[会计·投票账] 已导出 {os.path.basename(self.vote_log_path)}")
            except Exception:
                pass

    # ------------------------- tick / K线 -------------------------
    def _status(self, tag, msg, interval=30.0):
        """持续状态类日志节流：状态切换（tag 变化）立即打；否则每 interval 秒最多打一次。
        用于预热/中场/黄灯冷静等每 tick 都触发的状态，避免刷屏。"""
        now = time.time()
        if tag != self._status_tag or (now - self._status_ts) >= interval:
            self.output(msg)
            self._status_ts = now
            self._status_tag = tag

    def on_tick(self, tick) -> None:
        try:
            super().on_tick(tick)
            # —— 导演（提前下班/熔断）：停止信息传递 ——
            if self._halted:
                return
            if self._first_ts is None:
                self._first_ts = time.time()   # 记录开盘首 tick（回放假钟 / live 真钟）
            # —— 滚动缓冲（供 50 维特征 / 五档深度 / 盘面图）——
            self.price_buf.append(tick.last_price)
            # 日内高低点跟踪（逆反弹保护用；tick 自带 high/low 时取两者较大/较小）
            _th = getattr(tick, "high_price", None)
            _tl = getattr(tick, "low_price", None)
            if _th:
                self.day_high = max(self.day_high, float(_th))
            if _tl:
                self.day_low = min(self.day_low, float(_tl))
            self.day_high = max(self.day_high, tick.last_price)
            self.day_low = min(self.day_low, tick.last_price)
            vdelta = tick.volume - self._prev_volume
            if vdelta < 0:                      # 跨日累计量归零 → 负增量按 0 处理
                vdelta = 0.0
            self._prev_volume = tick.volume
            self._vol_window.append(vdelta)
            if self.kline_generator is not None:
                self.kline_generator.tick_to_kline(tick)   # K线系统：tick 累积成 bar
            self._drive(tick)                              # 每个 tick 驱动一次决策
        except (OSError, TimeoutError) as e:
            # 网络层偶发错（VPN/SSL 握手 Errno 22/超时等）：v2 内部 _worker 已尽量兜底，
            # 这里再兜一层，本 tick 跳过不冒泡——避免回放主循环"报错>=40闸"把偶发网络错当致命停掉整场。
            # 真正白烧钱由预算闸(¥5)拦截；真程序致命错走下方 except 冒泡给 40 闸。
            import traceback as _tb
            _tb.print_exc()
            self.output(f"[on_tick 网络错·本tick跳过] {type(e).__name__}: {e}")
        except Exception as e:
            # 真正的程序致命错：重新抛出，交给回放主循环"报错>=40闸"处理
            raise

    def on_bar(self, kline) -> None:
        """K线合成回调（新分钟K产生时触发）。把合成 K 线存入缓冲，真正接入 AI 决策。"""
        try:
            self.kline_buf.append({
                "open":   float(getattr(kline, "open_price", 0) or 0),
                "high":   float(getattr(kline, "high_price", 0) or 0),
                "low":    float(getattr(kline, "low_price", 0) or 0),
                "close":  float(getattr(kline, "close_price", 0) or 0),
                "volume": float(getattr(kline, "volume", 0) or 0),
            })
        except Exception:
            pass

    # ------------------------- 成交回调 -------------------------
    def on_trade(self, trade, log: bool = False) -> None:
        super().on_trade(trade, log)
        d = str(trade.direction).lower()
        off = str(trade.offset).lower()
        v = trade.volume
        is_open = (off in ("0", "open", "open_today", "open_yesterday"))
        # pythongo 方向编码："0"=买(buy) / "1"=卖(sell)（const.py OrderDirectionEnum：BUY="0" SELL="1"）
        is_buy = d in ("0", "buy", "b", "long")
        sign = 1 if is_buy else -1
        if is_open:
            self._pos_synced_by_reconcile = False   # 开仓是新仓，清对账标记（防旧标记残留）
            if self.position == 0:
                self.entry_price = trade.price
                self.entry_ts = time.time()     # 记录员：开仓记时戳
            self.position += sign * v
            self.pending_order_id = None        # 挂单已成交，清掉挂单号（防重复撤/开）
            self.pending_order_ts = 0.0         # 同时清挂单时间戳
            self.output(f"[成交·开] {d} {v}手 @ {trade.price} → 净持仓={self.position}")
            # 【事件部门】开仓成交 → 告知 AI
            self._notify("开仓成交", f"{d} {v}手 @ {trade.price}，净持仓={self.position}")
        else:  # 平仓
            # 计算该笔平仓盈亏（元）
            _prev_pos = self.position          # 记录平仓前持仓（复盘文案用）
            if is_buy:          # 平空(买平空仓)
                pnl = (self.entry_price - trade.price) * v * POINT_VALUE
            else:               # 平多(卖平多仓)
                pnl = (trade.price - self.entry_price) * v * POINT_VALUE
            # 【标记法·根治双重记账】若对账哨兵已抢先同步过持仓（position 已是柜台真实值），
            # 这笔延迟的成交回报【不再叠加 position】，只算盈亏/事件——否则 0+(-1)=-1 / 0+(+1)=+1 假持仓。
            if self._pos_synced_by_reconcile:
                self._pos_synced_by_reconcile = False
                self.output(f"[成交·平] {d} {v}手 @ {trade.price} 笔盈亏≈{pnl:.1f}元 → 净持仓={self.position}(对账已同步,不重复记账)")
            else:
                self.position += sign * v   # 正常路径：平仓按方向增减(买+卖-)，归零而非翻倍
                self.output(f"[成交·平] {d} {v}手 @ {trade.price} 笔盈亏≈{pnl:.1f}元 → 净持仓={self.position}")
            self.pending_order_id = None    # 平仓单已成交，清掉挂单状态（对账哨兵不再拦截）
            self.pending_order_ts = 0.0
            _was_tp = self._tp_fired    # 记下是否为止盈平仓（复位前读取）
            _was_stop = self._hard_stop_fired
            if self.position == 0:
                self.entry_ts = 0.0     # 记录员：已空仓，持票时长清零
                self._hard_stop_fired = False   # 硬止损哨兵复位（等下一笔持仓）
                self._tp_fired = False          # 止盈哨兵复位（等下一笔持仓）
            if pnl < 0 or _was_tp:
                # 亏损平仓 或 止盈平仓 → 触发黄灯冷静期（思考+扣1）
                self.cooldown_until = time.time() + COOLDOWN_SEC
                self.cooldown_score += 1
                _why = "止盈平仓" if _was_tp else "亏损平仓"
                self.output(f"[黄灯触发] {_why} {pnl:.1f}元 → 冷静{int(COOLDOWN_SEC)}s 扣1分(累计{self.cooldown_score})")
            # 【事件部门】平仓成交 → 统一告知 AI（含用户手动平仓）
            if self.position == 0:
                _src = "会计止盈" if _was_tp else ("会计硬止损" if _was_stop else "平仓")
                self._notify(_src, f"持仓已平（{d} {v}手 @ {trade.price}，盈亏≈{pnl:.1f}元），净持仓=0")
                # 【招一·复盘回灌】平仓结果写回记忆文件，下轮 AI 回放能看到
                # 「当时决策 → 这笔盈亏」的因果闭环，从自己的错误里长记性
                try:
                    _pn = "平多单" if _prev_pos > 0 else "平空单"
                    self.debater._save_memory(
                        f"复盘[{_src}]: {_pn}{v}手@{trade.price} 盈亏{pnl:+.1f}元 → 记住这笔的教训")
                except Exception:
                    pass

        # —— 会计小兵记账（幕后，绝不影响交易流程）——
        if self.accountant is not None:
            try:
                self.accountant.record_fill(
                    kind="open" if is_open else "close",
                    direction="buy" if is_buy else "sell", volume=v, price=trade.price, ts=time.time())
            except Exception:
                pass

    # ------------------------- 红绿灯 -------------------------
    def _update_light(self, tick) -> None:
        if self.position == 0:
            if time.time() < self.cooldown_until:
                self.light = Light.YELLOW
            else:
                self.light = Light.GREEN
        else:
            pnl = (tick.last_price - self.entry_price) * self.position * POINT_VALUE
            if pnl > 0:
                self.light = Light.RED
            else:
                self.light = Light.BLUE
                # 蓝灯分级（贴合 -60 硬止损：<20可扛 / 20-40警惕 / >40逼近）
                depth = abs(pnl)
                level = "L1" if depth < 20 else ("L3" if depth < 40 else "L5")
                # 蓝灯节流：等级切换立即打，同等级每 5s 最多一次（防每 tick 刷屏）
                self._status(f"blue_{level}", f"[蓝灯·{level}] 持仓亏损 {depth:.1f}元（距硬止损约{HARD_STOP_YUAN-depth:.1f}元）", interval=5.0)

    # ------------------------- 指标计算（50 维 + 五档深度） -------------------------
    def _ma(self, buf, k):
        b = list(buf)[-k:]
        return sum(b) / len(b) if b else 0.0

    def _stats(self, buf):
        n = len(buf)
        if n == 0:
            return 0.0, 0.0
        mean = sum(buf) / n
        var = sum((x - mean) ** 2 for x in buf) / n
        return mean, var ** 0.5

    def calc_features(self, tick):
        """返回 50 维 (标签, 值) 列表，作为 AI 决策的盘面输入（含五档深度）。"""
        P = list(self.price_buf)
        n = len(P)
        last = tick.last_price
        openp = tick.open_price
        high_d = tick.high_price
        low_d = tick.low_price
        bid1 = tick.bid_price1
        ask1 = tick.ask_price1
        mid = (bid1 + ask1) / 2.0 if (bid1 and ask1) else last
        spread = (ask1 - bid1) if (bid1 and ask1) else 0.0
        # —— 五档深度（交易平台实盘提供 1~5 档；回测样本无此数据，仅实盘生效）——
        bp = [tick.bid_price1, tick.bid_price2, tick.bid_price3, tick.bid_price4, tick.bid_price5]
        ap = [tick.ask_price1, tick.ask_price2, tick.ask_price3, tick.ask_price4, tick.ask_price5]
        bv = [tick.bid_volume1, tick.bid_volume2, tick.bid_volume3, tick.bid_volume4, tick.bid_volume5]
        av = [tick.ask_volume1, tick.ask_volume2, tick.ask_volume3, tick.ask_volume4, tick.ask_volume5]
        _sbv, _sav = sum(bv), sum(av)
        _sv = _sbv + _sav
        mid5 = ((sum(b * p for b, p in zip(bv, bp)) + sum(a * p for a, p in zip(av, ap))) / _sv) if _sv else mid
        book_span = (ap[4] - bp[4]) if (ap[4] and bp[4]) else 0.0
        depth_total = _sv
        depth_imb5 = ((_sbv - _sav) / _sv) if _sv else 0.0
        bid1_share = (bv[0] / _sbv) if _sbv else 0.0
        ask1_share = (av[0] / _sav) if _sav else 0.0
        mean, std = self._stats(P)
        lo_w = min(P) if P else last
        hi_w = max(P) if P else last
        rng = (hi_w - lo_w) or 1.0
        prev = P[-2] if n >= 2 else last
        ma5 = self._ma(P, 5)
        ma10 = self._ma(P, 10)
        ma20 = self._ma(P, 20)
        ma60 = self._ma(P, 60)
        ma240 = mean
        ma5_lag = self._ma(list(P)[:-1], 5) if n >= 6 else ma5
        spread_bps = (spread / mid * 1e4) if mid else 0.0
        ob_skew = ((mid - last) / last) if last else 0.0
        ob_imbalance = ((bid1 - ask1) / (bid1 + ask1)) if (bid1 + ask1) else 0.0
        vdelta = self._vol_window[-1] if self._vol_window else 0.0
        v10 = self._ma(self._vol_window, 10)
        v20 = self._ma(self._vol_window, 20)
        vol_ratio = (v10 / v20) if v20 else 1.0
        # K 线衍生
        kb = list(self.kline_buf)
        nk = len(kb)
        if nk:
            kc = kb[-1]
            k_open, k_high, k_low, k_close = kc["open"], kc["high"], kc["low"], kc["close"]
            krng = (k_high - k_low) or 1.0
            k_body = abs(k_close - k_open) / krng
            k_up = (k_high - max(k_open, k_close)) / krng
            k_dn = (min(k_open, k_close) - k_low) / krng
            k_close_prev = kb[-6]["close"] if nk >= 6 else kc["open"]
            k_trend = (k_close - k_close_prev) / k_close_prev if k_close_prev else 0.0
        else:
            k_open = k_high = k_low = k_close = last
            k_body = k_up = k_dn = 0.0
            k_trend = 0.0
        ret_tick = (last - prev) / prev if prev else 0.0
        ret5 = (last - P[-5]) / P[-5] if n >= 5 else 0.0
        ret15 = (last - P[-15]) / P[-15] if n >= 15 else 0.0
        intraday = (last - openp) / openp if openp else 0.0
        ma_diff = ma5 - ma20
        ma_slope = (ma5 - ma5_lag) / ma5_lag if ma5_lag else 0.0
        return [
            ("最新价", last), ("今开", openp), ("当日高", high_d), ("当日低", low_d),
            ("买一", bid1), ("卖一", ask1), ("中间价", mid), ("买卖价差", spread),
            ("窗口均值", mean), ("窗口标准差", std), ("窗口最低", lo_w),
            ("窗口最高", hi_w), ("窗口振幅", rng), ("距低点%", (last - lo_w) / rng),
            ("距高点%", (hi_w - last) / rng), ("tick收益", ret_tick), ("5tick收益", ret5),
            ("15tick收益", ret15), ("日内涨跌%", intraday), ("MA5", ma5), ("MA10", ma10),
            ("MA20", ma20), ("MA60", ma60), ("MA240", ma240), ("MA差5-20", ma_diff),
            ("MA5斜率", ma_slope), ("价差bps", spread_bps), ("盘口偏度", ob_skew),
            ("距买一", last - bid1), ("距卖一", ask1 - last), ("盘口失衡", ob_imbalance),
            ("成交量", tick.volume), ("tick量增", vdelta), ("近20tick均量", v20),
            ("量比", vol_ratio), ("K线根数", nk), ("K收", k_close), ("K高", k_high),
            ("K低", k_low), ("K实体占比", k_body), ("K上影", k_up), ("K下影", k_dn),
            ("K线趋势", k_trend),
            ("五档量加权中价", mid5), ("盘口跨度", book_span), ("五档总厚度", depth_total),
            ("五档买卖失衡", depth_imb5), ("买一量占比", bid1_share), ("卖一量占比", ask1_share),
        ]

    # ------------------------- ASCII 盘面图 / 盘口阶梯 -------------------------
    def draw_ascii_chart(self, n=18, height=8):
        kb = list(self.kline_buf)
        if len(kb) >= 2:
            series = [b["close"] for b in kb[-n:]]
            src = "K线收盘"
        else:
            series = list(self.price_buf)[-n:]
            src = "tick最新价"
        if len(series) < 2:
            return "(盘面图: 数据不足)"
        lo, hi = min(series), max(series)
        rng = (hi - lo) or 1.0
        grid = [[" "] * len(series) for _ in range(height)]
        for i, p in enumerate(series):
            r = int(round((p - lo) / rng * (height - 1)))
            grid[height - 1 - r][i] = "*"
        lines = []
        for r in range(height):
            price_at = lo + (height - 1 - r) / (height - 1) * rng
            lines.append(f"{price_at:8.1f} |" + "".join(grid[r]))
        axis = "          +" + "".join(str(i % 10) for i in range(len(series)))
        return f"(来源:{src})\n" + "\n".join(lines) + "\n" + axis

    def draw_book_ladder(self, tick):
        bp = [tick.bid_price1, tick.bid_price2, tick.bid_price3, tick.bid_price4, tick.bid_price5]
        ap = [tick.ask_price1, tick.ask_price2, tick.ask_price3, tick.ask_price4, tick.ask_price5]
        bv = [tick.bid_volume1, tick.bid_volume2, tick.bid_volume3, tick.bid_volume4, tick.bid_volume5]
        av = [tick.ask_volume1, tick.ask_volume2, tick.ask_volume3, tick.ask_volume4, tick.ask_volume5]
        bid_lad = " ".join(f"买{i+1} {bp[i]:.1f}/{bv[i]}" for i in range(5))
        ask_lad = " ".join(f"卖{i+1} {ap[i]:.1f}/{av[i]}" for i in range(5))
        return f"{ask_lad}  | 最新 {tick.last_price:.1f} |  {bid_lad}"

    # ------------------------- 快照（50 维 + 五档深度 + 图） -------------------------
    def _snapshot(self, tick) -> str:
        feats = self.calc_features(tick)
        chart = self.draw_ascii_chart()
        book = self.draw_book_ladder(tick)
        def _fmt(x):
            return f"{x:.2f}" if isinstance(x, float) else str(x)
        feat_line = "  ".join(f"{lbl}={_fmt(v)}" for lbl, v in feats)
        return (
            f"\n【50维特征】\n{feat_line}\n"
            f"【ASCII盘面图】\n{chart}\n"
            f"【盘口阶梯】 {book}\n"
            f"净持仓={self.position}手 入场价={self.entry_price:.1f} "
            f"红绿灯={self.light} 时间={tick.update_time}"
        )

    # ------------------------- 安全闸（节流 / 预算 / killfile / 输出截断） -------------------------
    def _cost_of_call(self, provider) -> float:
        """按【真实 token 用量 × 官方单价】估本次调用成本(元)。
        usage 缺失（弃权/超时/旧返回）时回退 COST_PER_CALL 粗估。"""
        try:
            _cl = getattr(self.debater, "clients", {})
            _usage = getattr(_cl.get(provider, None), "last_usage", None) or {}
            pt = int(_usage.get("prompt_tokens", 0) or 0)
            ct = int(_usage.get("completion_tokens", 0) or 0)
            if pt or ct:
                _pr, _po = TOKEN_PRICE_YUAN_PER_M.get(provider, (self.COST_PER_CALL, 0.0))
                return (pt * _pr + ct * _po) / 1_000_000.0
        except Exception:
            pass
        return self.COST_PER_CALL

    def _budget_exceeded(self) -> bool:
        """预算闸：调用次数或估算成本任一达上限即停。"""
        return self._api_calls >= self.max_api_calls or self._est_cost >= self.max_cost_yuan

    def _light_block(self, tick) -> str:
        """把灯态机结构化接入奏折：权限 + 浮亏/浮盈 + 距硬止损 + 可扛锚点 + 纪律。"""
        perm = {Light.GREEN: "可开/可平", Light.RED: "只平/持",
                Light.BLUE: "只平/持", Light.YELLOW: "冷静期·只观察不开"}[self.light]
        if self.position == 0:
            return f"[灯态]{self.light} [权限]{perm} [时间]{tick.update_time}"
        pnl = (tick.last_price - self.entry_price) * self.position * POINT_VALUE
        depth = abs(pnl)
        if self.light == Light.BLUE:
            if depth < 20:
                zone = "负方向观察区间"
            elif depth < 40:
                zone = "警惕但仍有缓冲"
            else:
                zone = "逼近硬止损"
            return (f"[灯态]{self.light} [权限]{perm}\n"
                    f"  浮亏 {depth:.1f}元（≈{-pnl / POINT_VALUE:.1f}跳）\n"
                    f"  距硬止损(-{HARD_STOP_YUAN:.0f}元) 还差 {HARD_STOP_YUAN - depth:.1f}元 → {zone}\n"
                    f"  提示：已设置好可接受的回撤值，可待行情确认再止损或持仓。")
        if self.light == Light.RED:
            return (f"[灯态]{self.light} [权限]{perm}\n"
                    f"  浮盈 {pnl:.1f}元（≈{pnl / POINT_VALUE:.1f}跳） [时间]{tick.update_time}")
        return f"[灯态]{self.light} [权限]{perm} [时间]{tick.update_time}"

    def _market_env(self, feats) -> str:
        """先判环境（招二）：基于 MA5/MA20/MA60 排列与斜率，输出 趋势/震荡/单边 分类。
        让 AI 看菜下饭——趋势市追方向、震荡市做区间、单边市顺大周期。"""
        try:
            d = dict(feats)
            ma5, ma20, ma60 = d.get("MA5", 0), d.get("MA20", 0), d.get("MA60", 0)
            slope = d.get("MA5斜率", 0)
            if not ma5 or not ma20:
                return "震荡"
            up = ma5 > ma20 > ma60        # 多头排列
            dn = ma5 < ma20 < ma60        # 空头排列
            if up and slope > 0.0005:     # 多头排列 + 斜率向上 → 单边多头
                return "单边·多头"
            if dn and slope < -0.0005:    # 空头排列 + 斜率向下 → 单边空头
                return "单边·空头"
            if up or dn:
                return "趋势" + ("·多头" if up else "·空头")
            return "震荡"                  # 均线纠缠 → 横盘
        except Exception:
            return "震荡"

    def _compact_snapshot(self, tick) -> str:
        """喂给模型的快照：50 维特征 + Z 解读 + 市场环境 + 盘前观点 + ASCII 盘面图 + 盘口阶梯 + 灯态 + 持仓。"""
        feats = self.calc_features(tick)
        line = " ".join(f"{lbl}:{v:.2f}" for lbl, v in feats)
        mean, std = self._stats(list(self.price_buf))
        z = (tick.last_price - mean) / std if std else 0.0
        z_note = "超买(Z>2)" if z > 2 else "超卖(Z<-2)" if z < -2 else "中性区间(|Z|≤2)"
        env = self._market_env(feats)
        chart = self.draw_ascii_chart()
        book = self.draw_book_ladder(tick)
        note_block = f"[盘前观点·人类参考(非信号)] {self.daily_note}\n" if self.daily_note else ""
        return (f"[50维] {line}\n"
                f"[价值回归Z] Z={z:+.2f} → {z_note}\n"
                f"[市场环境] {env}（趋势市追方向 / 震荡市做区间 / 单边市顺大周期）\n"
                f"{note_block}"
                f"[ASCII盘面图]\n{chart}\n"
                f"[盘口阶梯] {book}\n"
                f"{self._light_block(tick)} "
                f"[持仓]{self.position}手 [入场]{self.entry_price:.1f}")

    def _log_debate(self, text) -> None:
        """辩论全量落盘（不被截断），供事后翻阅；失败静默。"""
        try:
            with open(self.debate_log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _cancel_pending_before_market_close(self, why="强制平仓"):
        """【预挂单·安全】止损/止盈要市价平仓前，若已有预挂单在途（未成交），必须先撤掉，
        再发市价单——否则「预挂单 + 市价单」双单同挂会冲突（错单：报单已全成交或已撤销不能再撤）。
        撤单失败（已成交/已撤）也清状态，交给市价单处理。"""
        if self.pending_order_id is not None:
            oid = self.pending_order_id
            try:
                self.cancel_order(oid)
                self.output(f"[撤预挂单] {why}前撤回预挂单 id={oid}")
            except Exception as _e:
                self.output(f"[撤预挂单] id={oid} 撤单失败(忽略): {_e}")
            self.pending_order_id = None
            self.pending_order_ts = 0.0

    def _run_warmup_questions(self, tick) -> None:
        """【15问·预热期自检】策略启动预热期只做一次：四学者各回答一轮 15 问（cooling_15.py），
        帮助它们理解当前市场/品种/自己的架构，答案打印 + 写进记忆。
        只做一次（_cooling_done），不每 tick 烧钱；15问失败静默不影响交易。"""
        if self._cooling_done or build_cooling_questions is None:
            return
        self._cooling_done = True
        try:
            snapshot = self._compact_snapshot(tick)
            profile = {
                "signal_label": "你的多空信号",
                "signal_explain": "50维特征+Z值+市场环境+盘口阶梯综合判断",
                "key_level": "当日高/低点与硬止损-60元位置",
                "market_hint": "趋势/震荡/单边",
                "orderbook_hint": "五档买卖量",
                "max_position": f"净持仓上限={self.max_volume}手（代码强制，AI 改不了）",
                "instrument_hint": getattr(self.params_map, "instrument_id", "") or "见下方行情数据",
            }
            questions = build_cooling_questions(profile, snapshot,
                                                params=getattr(self, "params_map", None))
            self.output(f"[15问·预热自检] 启动一次，四学者各答一轮（约¥0.2）")
            for role in ("技术分析师", "民间高手", "业余K线研究员", "组合经理"):
                try:
                    ans = self.debater._call(self.debater._resolve(role),
                                             ROLES.get(role, ""),   # ROLES 是模块级字典，不是 debater 的属性（08-24 实盘暴露）
                                             f"{questions}\n你是{role}，请逐题回答上面的自检题，"
                                             f"本环节不要输出交易JSON，只需畅所欲言。")
                    self.output(f"[15问·{role}] {str(ans)[:200]}")
                    try:
                        self.debater._save_memory(f"预热自检[{role}]: {str(ans)[:120]}")
                    except Exception:
                        pass
                except Exception as _e:
                    self.output(f"[15问·{role}] 失败(忽略): {_e}")
        except Exception as _e:
            self.output(f"[15问] 预热自检异常(忽略): {_e}")

    # ------------------------- 主驱动 -------------------------
    def _in_session(self, ts) -> bool:
        """③ 中场休息：判断 ts 是否落在交易时段内（MA609 配置）。"""
        try:
            hm = time.strftime("%H:%M", time.localtime(ts))
            cur = int(hm[:2]) * 60 + int(hm[3:5])
        except Exception:
            return True
        for s, e in self.session_windows:
            sh, sm = (int(x) for x in s.split(":"))
            eh, em = (int(x) for x in e.split(":"))
            if sh * 60 + sm <= cur <= eh * 60 + em:
                return True
        return False

    def _drive(self, tick) -> None:
        self._update_light(tick)

        # ===== 导演（作息管理 + 亏损熔断）=====
        # ⑤ 亏损硬闸（最高优先）：会计小兵报当日已实现亏损≥阈值 → 全停 + 强平
        breached = bool(self.accountant and getattr(self.accountant, "breached", False))
        if breached:
            if not self.director_breached:
                self.director_breached = True
                self._halted = True
                self.output(f"[导演·熔断] 当日已实现亏损达阈值 → 全停：停止信息传递 / 暂停API / 立即平仓")
                if self.position != 0:
                    d = "buy" if self.position < 0 else "sell"   # 平空用buy / 平多用sell
                    self._close(tick, d)
            return

        # ===== 会计小兵硬止损（哨兵，最高优先于预热/时段/黄灯）：浮亏触及 -60元 → 立即强制平仓 → 黄灯 =====
        if self.position != 0 and not self._hard_stop_fired:
            _fl = (tick.last_price - self.entry_price) * self.position * POINT_VALUE   # 策略自身浮亏（权威兜底）
            _hit = _fl <= -HARD_STOP_YUAN
            if self.accountant is not None:
                try:
                    _hit = _hit or self.accountant.check_hard_stop(tick.last_price, HARD_STOP_YUAN)
                except Exception:
                    pass
            if _hit:
                self._hard_stop_fired = True
                _d = "buy" if self.position < 0 else "sell"   # 平空用buy / 平多用sell
                self.output(f"[会计小兵→计票员] 浮亏 {_fl:.1f}元 触及 -{HARD_STOP_YUAN:.0f}元 硬止损线 → 立即强制平仓 → 进入黄灯冷静期")
                self._cancel_pending_before_market_close("硬止损")   # ⚠️ 先撤预挂单再市价平，防双单冲突
                self._close(tick, _d, market=True)
                self._last_round_result = (f"会计小兵硬止损强制平仓（浮亏触及-{HARD_STOP_YUAN:.0f}元）"
                                           f"；净持仓=0手（空仓，可开新仓），进入黄灯冷静期")
                return

        # ===== 会计小兵止盈（哨兵）：浮盈达到 TAKE_PROFIT_POINTS 个点 → 强制止盈平仓 → 黄灯 =====
        if self.position != 0 and not self._tp_fired:
            _pts = (tick.last_price - self.entry_price) * self.position   # 浮盈点数（正=盈利方向）
            _hit = _pts >= TAKE_PROFIT_POINTS
            if self.accountant is not None:
                try:
                    _hit = _hit or self.accountant.check_take_profit(tick.last_price, TAKE_PROFIT_POINTS)
                except Exception:
                    pass
            if _hit:
                self._tp_fired = True
                _d = "buy" if self.position < 0 else "sell"
                self.output(f"[会计小兵→计票员] 浮盈 {_pts:.1f}点 触及 +{TAKE_PROFIT_POINTS:.0f}点 止盈线 → 立即止盈平仓 → 黄灯冷静")
                self._cancel_pending_before_market_close("止盈")   # ⚠️ 先撤预挂单再市价平，防双单冲突
                self._close(tick, _d, market=True)
                self._last_round_result = (f"会计小兵止盈强制平仓（浮盈触及+{TAKE_PROFIT_POINTS:.0f}点）"
                                           f"；净持仓=0手（空仓，可开新仓），进入黄灯冷静期")
                return

        # ===== 挂单超时主动撤单（防挂单堆积；若系统硬处理会很伤）=====
        if self.pending_order_id is not None and self.pending_order_ts > 0:
            if time.time() - self.pending_order_ts > PENDING_TIMEOUT:
                self.cancel_order(self.pending_order_id)
                self.pending_order_id = None
                self.pending_order_ts = 0.0
                self._notify("撤单", f"开仓挂单超{PENDING_TIMEOUT:.0f}s未成交已主动撤，可基于新行情重新决策")
                return

        # ①/② 开盘准备期：首 tick 起 warmup_sec 内只布景不开仓
        if self._first_ts is not None and (time.time() - self._first_ts) < self.warmup_sec:
            remain = int(self.warmup_sec - (time.time() - self._first_ts))
            self._status("warmup", f"[导演] 开盘准备中（预热剩{remain}s）仅布景不拍")
            self._run_warmup_questions(tick)   # 预热期四学者各答一次 15 问（只做一次）
            return

        # ③ 中场休息：非交易时段跳过（休市段不拍）
        if not self._in_session(time.time()):
            self._status("rest", "[导演] 中场休息（休市）仅观察")
            return

        # 黄灯：只观察不开仓
        if self.light == Light.YELLOW:
            remain = max(0, int(self.cooldown_until - time.time()))
            self._status("yellow", f"[黄灯] 冷静期 剩余{remain}s 扣1分(累计{self.cooldown_score}) 仅观察不开仓")
            return

        # —— 安全闸：killfile / 预算 ——
        if self._halted or os.path.exists(self.killfile):
            if not self._halted:
                self._halted = True
                self.output(f"[安全闸] 命中 {self.killfile} → 停止辩论与下单，仅 hold")
            return
        if self._budget_exceeded():
            self.output(f"[预算闸] 已达上限(调用{self._api_calls}/估算¥{self._est_cost:.3f}) → 仅 hold")
            return

        # —— 节流：区间内沿用上次决策，不调 API、不重复下单 ——
        now = time.time()
        if now - self._last_decision_time < self.ai_interval and self._cached_sig is not None:
            return

        # —— 持仓对账哨兵：外部操作（手动平仓/外部开仓）不推 on_trade 回调，只能主动问柜台 ——
        # 每轮辩论前对一次账：柜台真实净持仓 ≠ 策略记录 → 同步 + 事件部门回灌 AI
        # 【重要】策略自己有挂单在途（pending_order_id 非空）时跳过对账——
        # 自己下单的成交回报会推 on_trade，由 on_trade 记账；对账抢先会双重记账（11:13/22:16 两次事故根因）
        # 【标记法】对账同步后置 _pos_synced_by_reconcile=True，on_trade 收到延迟的平仓回报时
        # 看到标记就跳过 position 叠加（position 已是对账后的真实值），只算盈亏/事件 → 根治双重记账。
        # 注意：这里【不再清 entry_price】——若马上有 on_trade 回报到达，它要用 entry_price 算真实盈亏。
        try:
            _gp = getattr(self, "get_position", None)      # replay 桩无此接口，跳过
            if (_gp is not None and self.pending_order_id is None
                    and getattr(self.params_map, "instrument_id", "")):
                _real = _gp(self.params_map.instrument_id).net_position
                if _real != self.position:
                    _old = self.position
                    self.position = _real
                    self._pos_synced_by_reconcile = True   # 标记：已对账同步，防 on_trade 重复记账
                    if _real == 0:                         # 外部平仓 → 复位入场状态（entry_price 保留给 on_trade 算盈亏）
                        self.entry_ts = 0.0
                        self._hard_stop_fired = False
                        self._tp_fired = False
                    self._notify("持仓对账",
                                 f"柜台净持仓={_real}手 与策略记录={_old}手 不一致，已同步"
                                 f"（可能是外部手动平仓/开仓），请基于实际持仓重新评估")
                    self.output(f"[持仓对账] 柜台={_real} 策略={_old} → 已同步，事件已回灌AI")
        except Exception as _e:
            self.output(f"[持仓对账] 查询失败(忽略): {_e}")

        # 触发新一轮辩论（四学者各一票 = 4 次 API 调用；缺 key 已被停止闸拦截）
        # 反馈循环：把「上轮计票员结论+净持仓」回灌本轮，AI 才知道自己开/平了没有
        sig, debate_log, providers = self.debater.decide_verbose(
            self._compact_snapshot(tick), self.position, self._last_round_result)
        for p in providers:   # 分 API 计数 + 成本（修 v1 写死 3 的漏算）
            self._api_calls_by_provider[p] = self._api_calls_by_provider.get(p, 0) + 1
            self._api_calls += 1
            self._est_cost += self._cost_of_call(p)   # 按真实 token 用量 × 官方单价计价
        self._print_debate(debate_log, sig)
        self._cached_sig = sig
        self._last_decision_time = now

        # —— 记录员（坐在计票员旁）：每轮辩论落一笔投票账 ——
        if self.accountant is not None:
            try:
                _pos = self.position
                _hold = int(time.time() - self.entry_ts) if (self.entry_ts and _pos != 0) else 0
                self.accountant.record_vote(
                    round_no=self.debater.round_no, ts=time.time(),
                    price=tick.last_price, position=_pos, hold_seconds=_hold,
                    votes=sig.get("votes", []), verdict=sig.get("tally", {}),
                    mode=("close" if _pos != 0 else "open"))
            except Exception:
                pass

        # 计票员决策落库
        action = sig.get("action", "hold")
        d = sig.get("direction", "hold")

        # 红/蓝灯（持仓中）：只平不开新仓
        if self.light in (Light.RED, Light.BLUE):
            if action == "close":
                self._close(tick, d, target_price=sig.get("target_price"))   # AI 共识平仓 → 预挂平仓单
            else:
                self.output(f"[{self.light}] 计票员未达成平仓共识 → 仅持")
        else:
            # 绿灯：执行开仓（计票员 action=open 才开）
            self._execute(sig, tick)

        # —— 反馈循环收尾：执行后 self.position 已是真实净持仓（on_trade/虚拟撮合同步更新）——
        # 构建「上轮结果」回灌下一轮，让 AI 清楚自己开/平了没有。
        self._last_round_result = self._build_feedback(sig, self.position)

    # ------------------------- 执行（开/平/反手/挂单/撤单） -------------------------
    def _execute(self, sig, tick) -> None:
        action = sig.get("action", "hold")
        d = sig.get("direction", "hold")
        ex, inst = self.params_map.exchange, self.params_map.instrument_id
        if action != "open" or d == "hold" or not ex or not inst:
            return
        pos = self.position

        target = 1 if d == "buy" else -1
        if pos == target:
            return  # 已是目标仓位，不动

        # 【逆反弹保护·硬控】从日内低点反弹 ≥N 点禁开空；从日内高点回落 ≥N 点禁开多。
        # 防"追反弹顶做空/追回落底做多"这类逆势接刀（2026-08-24 用户拍板，AI 讨论无法绕过）。
        _now_px = tick.last_price
        if d == "sell" and self.day_low > -1e17 and (_now_px - self.day_low) >= REVERSAL_GUARD_POINTS:
            self.output(f"[逆反弹拦截] 价{_now_px:.0f} 已从日内低{self.day_low:.0f}反弹"
                        f"{_now_px-self.day_low:.0f}点≥{REVERSAL_GUARD_POINTS:.0f} → 禁开空（防追反弹顶）")
            self._open_note = (f"开仓被逆反弹保护拦截：价格已从日内低点反弹≥{REVERSAL_GUARD_POINTS:.0f}点，"
                               f"禁开空；可考虑等回落或转多思路")
            return
        if d == "buy" and self.day_high < 1e17 and (self.day_high - _now_px) >= REVERSAL_GUARD_POINTS:
            self.output(f"[逆反弹拦截] 价{_now_px:.0f} 已从日内高{self.day_high:.0f}回落"
                        f"{self.day_high-_now_px:.0f}点≥{REVERSAL_GUARD_POINTS:.0f} → 禁开多（防追回落底）")
            self._open_note = (f"开仓被逆反弹保护拦截：价格已从日内高点回落≥{REVERSAL_GUARD_POINTS:.0f}点，"
                               f"禁开多；可考虑等反弹或转空思路")
            return

        # 已有挂单未成交：拦截重复开仓，并把状态回灌计票员→AI（避免重复开仓）
        if self.pending_order_id is not None:
            self.output(f"[开仓拦截] 已有挂单 id={self.pending_order_id} 未成交 → 本轮不开新仓（等成交/撤单结果）")
            self._open_note = f"开仓未执行：上一轮挂单 id={self.pending_order_id} 尚未成交，等待结果"
            return

        # 反手：若已有反向持仓，先平
        if pos != 0:
            self._close(tick, "buy" if pos < 0 else "sell")

        # 【预挂单】挂单价 = 计票员共识价（强票中位数），方向校验：做多挂低/做空挂高
        px = self._validated_target_price(sig, tick, d, fallback=tick.last_price)
        oid = self.send_order(ex, inst, self.max_volume, px, d, market=False)
        if oid is not None:
            self.pending_order_id = oid
            self.pending_order_ts = time.time()   # 记录挂单时刻（超时自动撤单用）
            _tag = "预挂" if px != tick.last_price else "现价"
            self.output(f"[挂单·开] {d} {self.max_volume}手 @ {px:.1f} id={oid} [{_tag}]")
            self._open_note = f"已下开仓挂单 {d} {self.max_volume}手 @ {px:.1f}（id={oid}，{_tag}），等待成交"
        else:
            # 挂单未被平台接受（没挂上）：明确告知，避免下轮重复开仓
            self.output(f"[挂单·开] 失败（平台未接受挂单）→ 本轮未开仓")
            self._open_note = "开仓未成功：挂单未被平台接受（未挂上），请勿在状态未明时重复开仓"

    def _validated_target_price(self, sig, tick, d, fallback=None):
        """【预挂单·校验】取计票员共识价，做方向与范围校验：
        - 做多必须挂 < 现价（等回调）；做空必须挂 > 现价（等反弹）——矛盾则回退现价；
        - 超出当日高低点 → 回退现价（防 AI 瞎报价/挂单被拒）。"""
        px = fallback if fallback is not None else tick.last_price
        try:
            tp = float(sig.get("target_price") or 0)
            if tp <= 0:
                return px
            lo = float(getattr(tick, "low_price", 0) or 0)
            hi = float(getattr(tick, "high_price", 0) or 0)
            if lo and hi and not (lo <= tp <= hi):
                return px                      # 超当日高低点 → 回退现价
            if d == "buy" and tp >= tick.last_price:
                return px                      # 做多挂高=追价，回退现价
            if d == "sell" and tp <= tick.last_price:
                return px                      # 做空挂低=追空，回退现价
            return round(tp)                   # 通过 → 用共识价（对齐最小变动价位）
        except (TypeError, ValueError):
            return px

    def _close(self, tick, d, market=False, target_price=None) -> None:
        """平仓。d='sell' 平多 / d='buy' 平空（受当前净持仓限制）。
        market=True 时以市价平（会计小兵硬止损用，保证成交），否则限价平。
        AI 共识平仓走【预挂平仓单】（auto_close_position 平今/平昨由框架处理），
        可传 target_price 挂目标价；不传则用现价。"""
        ex, inst = self.params_map.exchange, self.params_map.instrument_id
        vol = abs(self.position)
        if vol == 0:
            return
        px = tick.last_price
        if not market and target_price:
            # 平仓预挂：方向校验（平多挂高/平空挂低），超当日高低点回退现价
            try:
                tp = float(target_price)
                lo = float(getattr(tick, "low_price", 0) or 0)
                hi = float(getattr(tick, "high_price", 0) or 0)
                if lo and hi and lo <= tp <= hi:
                    if d == "sell" and tp > tick.last_price:    # 平多：等反弹卖
                        px = round(tp)
                    elif d == "buy" and tp < tick.last_price:   # 平空：等回调买
                        px = round(tp)
            except (TypeError, ValueError):
                pass
        oid = self.auto_close_position(ex, inst, vol, px, d, market=market)
        if oid is not None:
            # 平仓单也记入挂单状态：挡住「持仓对账哨兵」抢先同步（成交回报会由 on_trade 处理）
            self.pending_order_id = oid
            self.pending_order_ts = time.time()
        _tag = "预挂" if (not market and px != tick.last_price) else ("市价" if market else "现价")
        self.output(f"[平仓] {d} {vol}手 @ {px:.1f} id={oid} [{_tag}]")

    # ------------------------- 反馈循环：构建「上轮结果」 -------------------------
    def _notify(self, event, detail):
        """【事件部门】后台事件统一分发：写入纸条回灌 AI（下一轮 feedback）+ 通知会计小兵 + 输出日志。
        event: 事件名（开仓成交/平仓成交/撤单/错单等）；detail: 事件描述。"""
        self._event_note = f"[{event}] {detail}"
        if self.accountant is not None:
            try:
                self.accountant._log(f"[事件·{event}] {detail}")
            except Exception:
                pass
        self.output(f"[事件·{event}] {detail}")

    def _build_feedback(self, sig, pos_after):
        """构建「上轮计票员结论」反馈串，回灌下一轮对话，让 AI 清楚自己开/平了没有。"""
        # 开仓执行结果优先（拦截/成功/失败），让 AI 知道挂单状态，避免重复开仓
        if getattr(self, "_open_note", None):
            note, self._open_note = self._open_note, None
            return f"{note}；净持仓={pos_after}手"
        # 【事件部门】后台事件（成交/撤单/手动平仓等）其次
        if getattr(self, "_event_note", None):
            note, self._event_note = self._event_note, None
            return f"{note}；净持仓={pos_after}手"
        a = sig.get("action", "hold")
        d = sig.get("direction", "hold")
        if a == "open" and d in ("buy", "sell"):
            verdict = f"开仓 {d}（建立{'多' if d == 'buy' else '空'}头）"
        elif a == "close":
            verdict = "平仓（已平掉持仓）"
        else:
            verdict = "不开仓/持有（共识不足）"
        pos_txt = "净持仓=%d手（%s）" % (
            pos_after, "多头" if pos_after > 0 else "空头" if pos_after < 0 else "空仓，可开新仓")
        return f"{verdict}；{pos_txt}"

    # ------------------------- 控制台（唇枪舌战） -------------------------
    def _print_debate(self, debate_log, sig) -> None:
        bar = "=" * 30
        self.output(f"{bar} 多空辩论 {bar}")
        for role, text in debate_log:
            if len(text) > self.max_print_chars:
                shown = text[:self.max_print_chars] + \
                        f"...(截断,全量见 {os.path.basename(self.debate_log_path)})"
            else:
                shown = text
            self.output(f"▌{role}▐ {shown}")
            self._log_debate(f"[{role}] {text}")
        self._log_debate("-" * 40)
        # —— 计票员唱票展示 ——
        tally = sig.get("tally")
        if tally:
            if tally.get("action") == "open":
                self.output(f"【计票员·开仓唱票】 方向={tally.get('direction')} "
                            f"同向支持率={tally.get('support_rate', 0):.2f} "
                            f"W(共识权重)={tally.get('weight')} N(人数)={tally.get('voters')}")
            elif tally.get("action") == "close":
                self.output(f"【计票员·平仓唱票】 平仓支持率={tally.get('support_rate', 0):.2f} "
                            f"C(平仓共识)={tally.get('weight')} N(人数)={tally.get('voters')}")
            else:
                _r = tally.get('support_rate') or tally.get('buy_rate') or tally.get('sell_rate') or tally.get('close_rate') or 0
                self.output(f"【计票员·唱票】 共识不足 支持率={_r:.2f} "
                            f"W/C={tally.get('weight')} N={tally.get('voters')} 方向冲突={tally.get('conflict', False)}")
            self._log_debate(f"[计票员] {tally}")
        self.output(f"★ 最终决策: action={sig.get('action')} direction={sig.get('direction')} "
                    f"volume={sig.get('order_volume')} 理由={sig.get('reasoning')} | 当前灯={self.light} "
                    f"| 累计调用={self._api_calls} 估算¥{self._est_cost:.3f}")
        # —— 分 API 计数（成本表雏形）——
        ct = "  ".join(f"{p}:{self._api_calls_by_provider.get(p, 0)}" for p in self.debater.clients)
        self.output(f"【分API计数】 {ct}")
        self.output(bar)
        # 本地 __main__ 自检也想看时，额外 print 到 stdout（完整不截断）
        if not _PYTHONGO_OK:
            print(f"\n{bar} 多空辩论 {bar}")
            for role, text in debate_log:
                print(f"▌{role}▐ {text}")
            print(f"★ 最终信号: {sig}\n{bar}\n")


# 平台按"文件名=类名"反射加载策略，类名须与文件名一致（AI_multiagent_debate_v2）。
# 保留 DebateStrategy 别名，防止任何外部旧引用断裂。
DebateStrategy = AI_multiagent_debate_v2


if __name__ == "__main__":
    # 独立自检：不需要 pythonGo，只要有任一 API Key 就能跑通整条辩论链并看到「唇枪舌战」。
    # Key 来源优先级：① api_keys_config.load_keys() 自动读 <你的API目录>/API 下三个 .txt
    #                ② 环境变量 DEEPSEEK_API_KEY / ARK_API_KEY / ZHIPU_API_KEY
    # 用法：python AI_multiagent_debate_v2.py   （key 已就绪时直接跑）
    try:
        _ks = {}
        try:
            from api_keys_config import load_keys
            _ks = load_keys()
        except Exception:
            pass  # 无加载器则用环境变量
        m = MultiAgentDebate(
            deepseek_key=_ks.get("deepseek", "") or os.getenv("DEEPSEEK_API_KEY", ""),
            doubao_key=_ks.get("doubao", "") or os.getenv("ARK_API_KEY", ""),
            zhipu_key=_ks.get("zhipu", "") or os.getenv("ZHIPU_API_KEY", ""),
            debate_rounds=1,
        )
        snap = "MA610 最近30根收盘价均值2650，现2645，z=-1.8，净持仓0手，红绿灯=绿灯。"
        print("===== 多空辩论开始（唇枪舌战）=====")
        sig, log, _ = m.decide_verbose(snap)
        for role, text in log:
            print(f"▌{role}▐ {text}")
        print("===== 最终决策（计票员把关）=====")
        print("决策:", sig)
    except Exception as e:
        print("自检跳过（未配置 API Key 或网络不可用）：", e)
