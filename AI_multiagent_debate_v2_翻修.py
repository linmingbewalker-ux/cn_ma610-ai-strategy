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
    from pythongo.base import BaseParams, BaseState, Field
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
        def update_status_bar(self): pass          # 官方：刷新 PythonGO 窗口状态栏（本策略当心跳用）
        # —— 以下回调在真 SDK 里由框架调用；桩里给同名空实现，保证离线自测不崩 ——
        def on_error(self, error): pass
        def on_order(self, order): pass
        def on_order_cancel(self, order): pass
        def on_order_trade(self, order): pass
        def on_contract_status(self, status): pass
        def pause_strategy(self): pass
    class BaseState:
        pass
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
# 止盈线（点/手）：浮盈达到该点数（价格单位，1点=POINT_VALUE元）→ 会计小兵通知计票员强制止盈平仓
TAKE_PROFIT_POINTS = 20.0
# 【原始设计文档⑥】四级蓝 🔵🔵🔵🔵 → 冷静 5 分钟，由 api 管理系统触发。
# 这是全文件**唯一**的冷静时长：2026-09-18 按用户指令清掉了连亏停机(600s)、
# 开仓间隔(180s)、风险击穿暂停(600s)，只留这一条。计时器归 ApiManager 管，别处不许自己写时间戳。
BLUE4_COOLDOWN_SEC = 300.0
# 状态栏刷新间隔（秒）。**任何模式下都刷**，让人一眼看出是"在正常跑"还是"进了冷静/熔断/资金不足"。
# 它不是看门狗：按下暂停键之后行情不推、它也不跑，证明不了进程是否还活着（详见 _heartbeat 的 docstring）。
HEARTBEAT_SEC = 30.0
# 资金不足 / 被拒单之后，隔多久再试一次开仓（秒）。
# 设这个是因为：钱不够往往是暂时的（出入金、保证金释放），不能一次被拒就永久锁死。
FUND_RETRY_SEC = 300.0
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
    """
    四色五级 —— 严格按原始设计文档《灯系统、api管理系统、锁系统、重启系统，属于翻.txt》。

      🔴红  未持仓：连续累计涨（走势强）
      🟢绿  未持仓：连续累计跌（走势强）
      🟡黄  持仓后：第 k 根收盘 - 持仓价 为正 → k 级黄
      🔵蓝  持仓后：第 k 根收盘 - 持仓价 为负 → k 级蓝

    每种 1~5 级。红绿互斥、黄蓝互斥，最多五级。

    【撤回 2026-09-17】上一版这里写着"已移除红/绿趋势灯"、额外造了「空仓底座灯
    🟢可开/🟡冷静」和「⚪0级·穿越入场价·转势信号」——那些都是我自己加的戏，不是
    文档里的东西，全部撤回。现在每个颜色只有一个意思：🟡 只表示盈利，
    冷静改用非颜色符号 ⏸（见 _light_block）。
    """
    RED = "红灯"
    GREEN = "绿灯"
    YELLOW = "黄灯"
    BLUE = "蓝灯"


class LightConfig(BaseParams):
    """参数映射模型（真机由 pythonGo 界面填 exchange / instrument_id）"""
    exchange: str = Field(default="", title="交易所代码")
    instrument_id: str = Field(default="", title="合约代码")
    kline_style: str = Field(default="M1", title="K线周期")


class StrategyState(BaseState):
    """状态栏模型 —— 照官方 Demo 的 `State` 写法。

    `update_status_bar()` 会把这些字段显示到交易平台的 PythonGO 窗口，
    本策略直接拿它当**心跳**用：不管处在什么模式（正常/冷静/熔断/已停），
    只要实例还活着就定时刷新，一眼分清「程序死了」和「程序在歇但还活着」。
    """
    mode: str = Field(default="-", title="运行模式")
    light: str = Field(default="-", title="红绿灯")
    position: int = Field(default=0, title="净持仓")
    pnl: str = Field(default="-", title="持仓浮亏")
    available: float = Field(default=0, title="可用资金")
    risk: str = Field(default="-", title="风险度")
    today_pnl: float = Field(default=0, title="今日盈亏")
    last_tick: str = Field(default="-", title="最后行情")


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
        # 状态栏（官方 BaseStrategy.update_status_bar 的载体）→ 本策略当心跳用，见 _heartbeat()
        self.state_map = StrategyState()
        self._heartbeat_ts = 0.0                 # 上次刷新状态栏的时刻
        # 最后一个行情 tick 的时刻，状态栏显示用。**这不是断流检测**：
        # tick 是交易所实时推的，只要不按暂停键它就不会停（2026-09-18 用户纠正，原注释写错了）。
        # 它的唯一用途是让人一眼看出最后那笔行情是几点几分（休市时会停在收盘那一刻）。
        self._last_tick_ts = 0.0
        self._fund_blocked = False               # 资金不足/开仓被拒（由官方 on_error 置位，见 on_error）
        self._fund_blocked_ts = 0.0              # 置位时刻（过了 FUND_RETRY_SEC 自动恢复尝试）

        self.max_volume = 1                      # ★ 最大开仓数 = 1 手
        self.debater = MultiAgentDebate(debate_rounds=1)  # 自动读 key

        # 停止总闸的默认值必须**先**给，再由下面的条件置位。
        # 原写法把这个默认值放在几十行之后，结果把条件置的 True 又抹平了 ——
        # "缺 AI 就停"那条闸等于没接上。规矩：默认值先行，条件修改在后。
        self._halted = False

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
        # 这张在途单**是干什么的**："open"=开仓 / "close"=平仓 / None=没有在途单。
        # 以前只靠 pending_order_id 一个变量，它既装开仓单又装平仓单，读它的三个地方各自猜身份，
        # 于是"超时自动撤单"连平仓单也一起撤了。现在显式标注身份，读的人不用猜。
        self._pending_kind = None
        self._open_note = None                   # 本轮开仓结果备注（拦截/成功/失败，回灌反馈用）
        self._event_note = None                  # 【事件部门】后台事件（成交/撤单/手动平仓等），回灌下一轮 AI
        self._hard_stop_fired = False            # 硬止损哨兵（防重复下单，平仓后复位）
        self._tp_fired = False                   # 止盈哨兵（防重复下单，平仓后复位）
        self._pos_synced_by_reconcile = False    # 对账哨兵已抢先同步持仓（防止 on_trade 延迟回报双重记账）

        # 红绿灯（按原始设计文档《灯系统、api管理系统、锁系统、重启系统，属于翻.txt》的四色五级）
        # 兼容旧字段。**没有灯就是 None**（不许默认成 GREEN / BLUE，理由见 _update_light）
        self.light = None
        self.light_red = 0                       # 🔴红级 0~5（未持仓：连续累计涨）
        self.light_green = 0                     # 🟢绿级 0~5（未持仓：连续累计跌）
        self.light_yellow = 0                    # 🟡黄级 0~5（持仓后第k根收盘 - 入场价 > 0）
        self.light_blue = 0                      # 🔵蓝级 0~5（持仓后第k根收盘 - 入场价 < 0）
        self.light_payload = {}                  # 喂给 AI 的颜色组合（备注③：未持仓1色/持仓2色）
        self._light_pnl = 0.0
        self.light_trend = None                  # 兼容旧字段：趋势灯（=红或绿那个）
        self.light_trend_level = 0               # 兼容旧字段：趋势灯级数
        self.light_pos = None                    # 兼容旧字段：持仓灯（=黄或蓝那个）
        self.light_pos_level = 0                 # 兼容旧字段：持仓灯级数
        # 冷静期时间戳不放在这里 —— 唯一权威是 ApiManager.suspended_until（见 ApiManager.suspend）。
        # 原因是以前 s.cooldown_until 谁都能写（灯写、检测系统也写），状态没有主人，两套冷静期互相打架。
        self._cooling_done = False               # 预热期 15 问自检只做一次
        self._hard_stop_fired = False            # 会计小兵硬止损哨兵：本笔持仓已触发强制平仓（防重复下单）
        self._post_entry_closes = []             # 持仓后每根K收盘（黄/蓝灯分级用）

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
        # （_halted 的默认值已移到 __init__ 前半段、停止闸之前，别再在这里赋 False —— 会把停机判定抹掉）
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

        # 翻修v2：四大子系统实例（灯/检测/API/锁/重启）
        self.api_mgr = ApiManager(self)
        self.locks = LockSystem(self)
        self.acct_health = AccountHealth(self)
        self.restart_sys = RestartSystem(self)
        self._last_account_health = {}            # 检测系统最近一次账户体检快照

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
        self.output(f"【启动】最大开仓={self.max_volume}手 | 红绿灯={self.light or '无灯'} | "
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
        用于预热/中场/冷静期等每 tick 都触发的状态，避免刷屏。"""
        now = time.time()
        if tag != self._status_tag or (now - self._status_ts) >= interval:
            self.output(msg)
            self._status_ts = now
            self._status_tag = tag

    # ------------------------- 心跳（复用官方状态栏） -------------------------
    def _light_simple_text(self) -> str:
        """状态栏用的简短灯态（一个符号只表示一个意思，见灯系统原文）。"""
        if self.position != 0:
            if self.light_yellow:
                return f"黄{self.light_yellow}级"
            if self.light_blue:
                return f"蓝{self.light_blue}级"
        if self.light_red:
            return f"红{self.light_red}级"
        if self.light_green:
            return f"绿{self.light_green}级"
        return "-"

    def _run_mode_text(self) -> str:
        """当前处在什么模式的一句话状态（状态栏 / 日志共用，全策略就这一个口径）。"""
        try:
            if self._halted:
                return "已停止(仅兜底)"
            if os.path.exists(self.killfile):
                return "killfile(禁交易)"
            if getattr(self, "api_mgr", None) is not None and self.api_mgr.suspended():
                return f"冷静剩{self.api_mgr.seconds_left()}s"
            if self._fund_blocked:
                return "资金不足(禁开新仓)"
            return "正常"
        except Exception:
            return "未知"

    def _heartbeat(self, force: bool = False) -> None:
        """刷新官方状态栏 —— 一个"当前状态展示面板"，**不是**"程序还活着"的证明。

        说清楚边界（2026-09-18 用户纠正）：这个函数挂在 on_tick 里，靠行情驱动。
        一旦你在平台上按下暂停键，行情不再推送，它也就不会再跑，状态栏停在最后一帧。
        所以它做不到"程序死了它还在报" —— 别拿它当看门狗用。

        它能做的是：让你用眼睛看一眼策略现在处于哪个模式、持仓多少、浮亏多少、钱还够不够。
        官方 Demo 写的是 `if self.trading: update_status_bar()`（只在能交易时刷）；
        这里故意不套 trading 判断 —— 冷静期、熔断、资金不足时恰恰最需要看得见这些数字。
        """
        now = time.time()
        if not force and (now - self._heartbeat_ts) < HEARTBEAT_SEC:
            return
        self._heartbeat_ts = now
        # 资金不足经常是暂时的（出入金、保证金释放），到期放它再试一次；
        # 再被柜台拒，on_error 会重新把它标记上。
        if self._fund_blocked and self._fund_blocked_ts and (now - self._fund_blocked_ts) >= FUND_RETRY_SEC:
            self._fund_blocked = False
            self._fund_blocked_ts = 0.0
            self.output(f"[资金不足] 已过{FUND_RETRY_SEC:.0f}s观察期 → 恢复尝试开仓（再被拒会重新标记）")
        try:
            h = self._last_account_health or {}
            st = self.state_map
            st.mode = self._run_mode_text()
            st.light = self._light_simple_text()
            st.position = int(self.position)
            if self.position != 0 and self.entry_price and self.price_buf:
                px = self.price_buf[-1]
                st.pnl = f"{(px - self.entry_price) * self.position * POINT_VALUE:.0f}元"
            else:
                st.pnl = "-"
            st.available = float(h.get("available", 0) or 0)
            st.risk = f"{float(h.get('risk', 0) or 0):.2f}" if h else "-"
            st.today_pnl = float(getattr(self.accountant, "daily_realized", 0.0) or 0.0)
            # 显示最后行情的**时刻**，不要显示"多久之前"——后者会误导成在监视行情有没有断。
            st.last_tick = (time.strftime("%H:%M:%S", time.localtime(self._last_tick_ts))
                            if self._last_tick_ts else "-")
            self.update_status_bar()
        except Exception:
            pass   # 心跳自己绝不能把策略搞崩

    # ------------------------- 官方回调（照 SDK 自带 Demo 补回来） -------------------------
    # 这几个是 pythongo BaseStrategy 本来就有的钩子，原文件一个都没重写，
    # 结果是：「资金到底够不够」「单子被拒了没有」「停牌了没有」全靠猜，只能干等 60s 超时。
    def on_error(self, error) -> None:
        """收到报单错误推送 —— 资金不足 / 保证金不足 / 被拒单，官方统一从这里送回来。

        父类实现只做一件事：errCode=="0004"（错单流控）时把 self.trading 关掉，limit_time 秒后自动恢复。
        这里先 super() 保留官方那套流控，再把"钱不够"这类错误**单独认出来**并置位：
        否则界面上只能看到"平台未接受挂单"这么一句，不知道原因，下一轮还会傻乎乎重发。
        """
        super().on_error(error)
        try:
            code = str((error or {}).get("errCode", ""))
            msg = str((error or {}).get("errMsg", "") or error)
        except Exception:
            code, msg = "", str(error)
        try:
            if any(k in msg for k in ("资金", "保证金", "可用资金", "不足", "拒绝", "拒单")):
                if not self._fund_blocked:
                    self._fund_blocked = True
                    self._fund_blocked_ts = time.time()
                    self.output(f"[资金不足] 柜台返回「{msg}」→ 标记禁开新仓"
                                f"（{FUND_RETRY_SEC:.0f}s 后自动恢复尝试）；持仓/止损/心跳照常，不会自己停程序")
                return
            self.output(f"[报错回报] code={code} {msg}")
        except Exception:
            pass

    def on_order(self, order) -> None:
        """报单变化回调（官方 Note：发单成功也算报单变化）。

        父类会自动把「已撤销」分发给 on_order_cancel、「全部成交」分发给 on_order_trade，
        所以这里只补剩下的收尾：挂单号还在但柜台已判终局时，立刻清掉挂单状态。
        """
        super().on_order(order)
        try:
            st = getattr(order, "status", "")
            oid = getattr(order, "order_id", None)
            if self.pending_order_id is not None and oid == self.pending_order_id and st in ("已撤销", "部成部撤"):
                self.pending_order_id = None
                self.pending_order_ts = 0.0
                self._pending_kind = None
                self._notify("撤单", f"挂单 id={oid} 柜台状态={st}，挂单已清空可重新决策")
        except Exception:
            pass

    def on_order_cancel(self, order) -> None:
        """撤单回报 —— 官方 Demo 里就是在这一步把 order_id 清掉的。"""
        super().on_order_cancel(order)
        try:
            if self.pending_order_id is not None and getattr(order, "order_id", None) == self.pending_order_id:
                self.pending_order_id = None
                self.pending_order_ts = 0.0
                self._pending_kind = None
        except Exception:
            pass

    def on_contract_status(self, status) -> None:
        """合约状态变化（停牌 / 停板 / 集合竞价）—— 这类突发情况官方统一从这里送回来。"""
        super().on_contract_status(status)
        try:
            self.output(f"[合约状态] {status}")
        except Exception:
            pass

    def on_tick(self, tick) -> None:
        try:
            super().on_tick(tick)
            # 记录最后行情时刻（状态栏显示用；不是断流检测 —— tick 是交易所实时推的，不按暂停就不会停）
            self._last_tick_ts = time.time()
            # —— 停止 / 熔断 / killfile 怎么办 ——
            # 旧写法这里写的是 `if self._halted: return`，等于把整个 tick 停掉：
            # K线不再更新、持仓对账哨兵不跑、硬止损不跑 → **手里还拿着单子就没人管了**。
            # 现在改为照常往下跑，由 _drive 内部决定省掉什么：
            # _drive 会因为 _halted 跳过「AI 辩论 / 开新仓」（避免继续烧钱、继续下单），
            # 但它前面的「硬止损 / 止盈 / 挂单超时 / 持仓对账 / 账户体检」每 tick 照样跑。
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
            self._heartbeat()                              # 心跳：任何模式下都刷新状态栏
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
            _close = float(getattr(kline, "close_price", 0) or 0)
            self.kline_buf.append({
                "open":   float(getattr(kline, "open_price", 0) or 0),
                "high":   float(getattr(kline, "high_price", 0) or 0),
                "low":    float(getattr(kline, "low_price", 0) or 0),
                "close":  _close,
                "volume": float(getattr(kline, "volume", 0) or 0),
            })
            # 翻修v2：持仓中每根新K收盘计入黄/蓝灯分级序列
            if self.position != 0 and _close:
                self._post_entry_closes.append(_close)
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
                self._post_entry_closes = []    # 翻修v2：新仓→清空持仓后K收盘序列（黄/蓝灯分级用）
            self.position += sign * v
            self.pending_order_id = None        # 挂单已成交，清掉挂单号（防重复撤/开）
            self._pending_kind = None
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
            self._pending_kind = None
            self.pending_order_ts = 0.0
            _was_tp = self._tp_fired    # 记下是否为止盈平仓（复位前读取）
            _was_stop = self._hard_stop_fired
            if self.position == 0:
                self.entry_ts = 0.0     # 记录员：已空仓，持票时长清零
                self._post_entry_closes = []    # 翻修v2：已空仓→清空持仓后K收盘序列
                self._hard_stop_fired = False   # 硬止损哨兵复位（等下一笔持仓）
                self._tp_fired = False          # 止盈哨兵复位（等下一笔持仓）
            # 平仓后不额外进冷静期 —— 全文件只有「4级蓝」会触发冷静（见 _update_light）。
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

    # ------------------------- 红绿灯（翻修v2·灯系统重做） -------------------------
    def _update_light(self, tick) -> None:
        # 红绿灯 —— 严格按原始设计文档的四色五级重算（不再有我自创的那些东西）。
        #   🔴红/🟢绿  未持仓：连续累计涨/跌
        #   🟡黄/🔵蓝  持仓后：第 k 根收盘 - 持仓价，为正 k 级黄、为负 k 级蓝
        # 详情见文件末上方的 trend_levels / position_levels 两个纯函数。

        # ---- 未持仓：趋势灯（红/绿），依据主周期 K 线收盘 ----
        _closes = []
        try:
            _closes = [float(x["close"]) for x in self.kline_buf if x.get("close")]
        except Exception:
            _closes = []

        self.light_red, self.light_green = trend_levels(_closes, MAX_LEVEL)

        # ---- 持仓后：盈亏灯（黄/蓝）----
        # 【多空方向的修正】原文是多仓语意："收盘>持仓价=盈"。本系统多空都做，
        # 空单是跌破持仓价才赚，所以按持仓方向取符号，否则整套灯反过来。
        self.light_yellow, self.light_blue = position_levels(
            self.entry_price, self._post_entry_closes,
            is_short=(self.position < 0), max_level=MAX_LEVEL)

        # ---- 喂给 AI 的颜色组合（备注③：未持仓一个色，持仓两个色）----
        self.light_payload = build_light_payload(
            self.light_red, self.light_green, self.light_yellow, self.light_blue)

        try:
            _pfx = float(getattr(tick, "last_price", 0) or 0) if tick is not None else 0.0
        except Exception:
            _pfx = 0.0
        self._light_pnl = (_pfx - self.entry_price) * self.position * POINT_VALUE if self.position else 0.0

        # ---- 兼容旧字段（旧判定 / 状态栏还在读这些）----
        if self.light_red:
            self.light_trend, self.light_trend_level = Light.RED, self.light_red
        elif self.light_green:
            self.light_trend, self.light_trend_level = Light.GREEN, self.light_green
        else:
            self.light_trend, self.light_trend_level = None, 0

        if self.light_yellow:
            self.light_pos, self.light_pos_level = Light.YELLOW, self.light_yellow
        elif self.light_blue:
            self.light_pos, self.light_pos_level = Light.BLUE, self.light_blue
        else:
            self.light_pos, self.light_pos_level = None, 0

        # 旧字段 self.light 与新字段（light_pos / light_trend）口径必须统一：**没灯就是 None**。
        # 老写法是 `else Light.GREEN` / `else Light.BLUE`，等于把"还没数据"默认画成一个颜色 ——
        # 持仓后如果还没有持仓后收盘（或收盘价正好等于入场价），黄蓝都是 0 → 被判成蓝灯（亏损），
        # 而下游 1643 行正是拿这个字段决定能不能下单的。地图不能自己瞎补内容。
        if self.position == 0:
            self.light = Light.RED if self.light_red else (Light.GREEN if self.light_green else None)
        else:
            self.light = Light.YELLOW if self.light_yellow else (Light.BLUE if self.light_blue else None)

        # ---- 蓝灯分级告警 ----
        if self.light_blue:
            depth = abs(self._light_pnl)
            level = "L1" if depth < 20 else ("L3" if depth < 40 else "L5")
            self._status(f"blue_{level}",
                         f"[蓝灯·{level}] 持仓亏损 {depth:.1f}元（距硬止损约{HARD_STOP_YUAN - depth:.1f}元）",
                         interval=5.0)
            # 原文⑥：四级蓝 → 冷静 5 分钟，且**由 api 管理系统触发**
            # （计时器归 ApiManager 管，灯只负责发请求；BLUE4_COOLDOWN_SEC = 300 秒）
            if self.light_blue >= 4 and not self.api_mgr.suspended():
                self.api_mgr.suspend(reason=f"蓝灯{self.light_blue}级", sec=BLUE4_COOLDOWN_SEC)

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
            f"红绿灯={self.light or '无灯'} 时间={tick.update_time}"
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
        """预算闸：调用次数或估算成本任一达上限即停。

        唯一算法在 ApiManager.budget_exceeded —— 这里只转调。
        以前同一个判断写了两份实现，改了一处容易忘另一处。
        """
        return self.api_mgr.budget_exceeded()

    def _light_block(self, tick) -> str:
        """四色五级的展示串 —— 按原始设计文档。

          未持仓：[灯]🔴🔴🔴 3级红  最新收盘2503 21:30:00 [⏸冷静 剩287s]
          持仓后：[灯]🟢🟢 2级绿 + 🔵🔵🔵 3级蓝  浮亏30.0元  最新收盘2603

        冷静一律用 ⏸ 表示，**不再染成黄色** ——
        上一版用 🟡 同时表示"盈利"和"冷静"，一个符号两个意思。
        """
        t = getattr(tick, "update_time", "-") if tick is not None else "-"
        try:
            last_txt = f"{float(getattr(tick, 'last_price', 0) or 0):.0f}"
        except Exception:
            last_txt = "-"

        tail = ""
        if self.api_mgr.suspended():
            tail = f" [⏸冷静 剩{self.api_mgr.seconds_left()}s]"

        # ---- 未持仓 / 平仓后：只给一个色（🔴 或 🟢）----
        if self.position == 0:
            if self.light_red:
                return f"[灯]{'🔴' * self.light_red} {self.light_red}级红 最新收盘{last_txt} {t}{tail}"
            if self.light_green:
                return f"[灯]{'🟢' * self.light_green} {self.light_green}级绿 最新收盘{last_txt} {t}{tail}"
            return f"[灯]—— 数据不足 {t}{tail}"

        # ---- 持仓后：趋势色 + 盈亏色，共两个（备注③）----
        if self.light_red:
            trend_txt = f"{'🔴' * self.light_red} {self.light_red}级红 "
        elif self.light_green:
            trend_txt = f"{'🟢' * self.light_green} {self.light_green}级绿 "
        else:
            trend_txt = ""

        if self.light_yellow:
            return (f"[灯]{trend_txt}+ {'🟡' * self.light_yellow} {self.light_yellow}级黄(盈) "
                    f"浮盈 {self._light_pnl:.1f}元（≈{self._light_pnl / POINT_VALUE:.1f}跳） "
                    f"最新收盘{last_txt} {t}{tail}\n"
                    f"  {light_meaning(self.light_yellow, 0)}")

        if self.light_blue:
            depth = abs(self._light_pnl)
            return (f"[灯]{trend_txt}+ {'🔵' * self.light_blue} {self.light_blue}级蓝(亏) "
                    f"浮亏 {depth:.1f}元（≈{-self._light_pnl / POINT_VALUE:.1f}跳） "
                    f"最新收盘{last_txt} {t}{tail}\n"
                    f"  距硬止损(-{HARD_STOP_YUAN:.0f}元) 还差 {max(0.0, HARD_STOP_YUAN - depth):.1f}元\n"
                    f"  {light_meaning(0, self.light_blue)}")

        return f"[灯]{trend_txt}持仓数据不足 {t}{tail}"

    def _account_health_block(self) -> str:
        """翻修v2·检测系统：把最近一次账户体检快照接入奏折。"""
        h = self._last_account_health
        if not h:
            return ""
        return (f"[账户体检] 占用保证金={h.get('margin', 0):.0f} 冻结={h.get('frozen_margin', 0):.0f} "
                f"手续费={h.get('commission', 0):.0f} 风险度={h.get('risk', 0):.2f} "
                f"动态权益={h.get('dynamic_rights', 0):.0f} 可用={h.get('available', 0):.0f} "
                f"{'[⚠风险击穿]' if h.get('risk_breach') else ''}")

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
                f"{self._account_health_block()}"
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
            # 撤单失败也清，交给后面的市价单处理（注释原话：失败多半是"已成交/已撤"）。
            # 但如果失败原因是网络而不是已成交，柜台可能还有这张单 —— 为防万一，
            # 这里同时把身份标记清掉，并记一句日志，事后能从日志里查到这张单是哪家路数。
            self.pending_order_id = None
            self.pending_order_ts = 0.0
            self._pending_kind = None

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
            # 【重要】旧写法在这里 `return`，后患很大：
            # 熔断只发**一次**平仓单，之后每个 tick 都从这里退出 → 这单成没成交、要不要重试，全是黑洞；
            # 而且下面的硬止损 / 止盈 / 挂单超时 / 持仓对账 / 账户体检全都不跑，持仓等于裸奔。
            # 现在改为**继续往下跑**：_halted 已置位，_drive 走到下方「安全闸」那行会自然跳过
            # AI 辩论与开仓（不会继续烧钱、不会继续下单），但持仓兜底三项每 tick 照跑。
            self._status("breach", f"[导演·熔断] 已全停，仅保留持仓兜底与心跳（当前持仓={self.position}手）")

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
                self.output(f"[会计小兵→计票员] 浮亏 {_fl:.1f}元 触及 -{HARD_STOP_YUAN:.0f}元 硬止损线 → 立即强制平仓（市价）")
                self._cancel_pending_before_market_close("硬止损")   # ⚠️ 先撤预挂单再市价平，防双单冲突
                self._close(tick, _d, market=True)
                self._last_round_result = (f"会计小兵硬止损强制平仓（浮亏触及-{HARD_STOP_YUAN:.0f}元）"
                                           f"；净持仓=0手（空仓，可开新仓）")
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
                self.output(f"[会计小兵→计票员] 浮盈 {_pts:.1f}点 触及 +{TAKE_PROFIT_POINTS:.0f}点 止盈线 → 立即止盈平仓（市价）")
                self._cancel_pending_before_market_close("止盈")   # ⚠️ 先撤预挂单再市价平，防双单冲突
                self._close(tick, _d, market=True)
                self._last_round_result = (f"会计小兵止盈强制平仓（浮盈触及+{TAKE_PROFIT_POINTS:.0f}点）"
                                           f"；净持仓=0手（空仓，可开新仓）")
                return

        # ===== 挂单超时主动撤单（防挂单堆积；若系统硬处理会很伤）=====
        # 只撤**开仓**预挂单 —— 下面那句通知文案本来就写的是"开仓挂单超时"。
        # 平仓单不在这里撤：单子平不掉是危险状态，应该交给 AI 重新决策或由硬止损改市价，
        # 悄悄撤掉会让"以为平了其实没平"。
        if self.pending_order_id is not None and self._pending_kind == "open" and self.pending_order_ts > 0:
            if time.time() - self.pending_order_ts > PENDING_TIMEOUT:
                self.cancel_order(self.pending_order_id)
                self.pending_order_id = None
                self.pending_order_ts = 0.0
                self._pending_kind = None
                self._notify("撤单", f"开仓挂单超{PENDING_TIMEOUT:.0f}s未成交已主动撤，可基于新行情重新决策")
                return

        # —— 持仓对账哨兵（每 tick 必跑，前置到节流/冷静/预热之前；外部手动平/开仓不推 on_trade，只能主动问柜台）——
        # 修复：原先在节流 early-return 之后，节流窗口(≤30s)及冷静期(≤5min)内不跑 → 人工平仓严重滞后才发现。
        # 现前置到此，每个 tick 都对账；并兼容 net_position/position/long-short 多种字段取法，避免取到 0/None 漏检。
        try:
            _gp = getattr(self, "get_position", None)
            # 有自己在途的单（开仓单 or 平仓单）时不对账 —— 这是**故意**的：
            # 单子还在半路上，柜台快照可能还没反映这笔，抢先同步会把还没平掉的仓位压成 0，
            # 造成双重记账（v2 11:13 / 22:16 两次事故就是这么来的）。
            # 所以平仓期间对账会暂停一会儿，等成交回报或超时；这不是 bug，别"修"掉这个守卫。
            if (_gp is not None and self.pending_order_id is None
                    and getattr(self.params_map, "instrument_id", "")):
                _pos_obj = _gp(self.params_map.instrument_id)
                _real = None
                for _f in ("net_position", "position", "net_pos"):
                    _v = getattr(_pos_obj, _f, None)
                    if _v is not None:
                        _real = _v
                        break
                if _real is None and hasattr(_pos_obj, "long") and hasattr(_pos_obj, "short"):
                    _real = (getattr(_pos_obj, "long", 0) or 0) - (getattr(_pos_obj, "short", 0) or 0)
                _real = int(_real) if _real is not None else 0
                if _real != self.position:
                    _old = self.position
                    self.position = _real
                    self._pos_synced_by_reconcile = True
                    if _real == 0:
                        self.entry_ts = 0.0
                        self._hard_stop_fired = False
                        self._tp_fired = False
                    self._notify("持仓对账",
                                 f"柜台净持仓={_real}手 与策略记录={_old}手 不一致，已同步"
                                 f"（可能是外部手动平仓/开仓），请基于实际持仓重新评估")
                    self.output(f"[持仓对账] 柜台={_real} 策略={_old} → 已同步，事件已回灌AI")
        except Exception as _e:
            self.output(f"[持仓对账] 查询失败(忽略): {_e}")

        # —— 翻修v2·检测系统：账户体检（保证金/冻结/手续费/风险度/权益/可用）+ 风险击穿拦截 ——
        self.acct_health.check()

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

        # 冷静期唯一来源：4级蓝 → ApiManager（见 _update_light）。
        # 这里**不再 return**：冷静期只拦「开新仓」（由 LockSystem.can_open 拦），
        # 不能连平仓决策一起停 —— 手上有单子时 AI 该管出场照样得管。
        if self.api_mgr.suspended():
            self._status("cooldown",
                         f"[冷静期] 剩余{self.api_mgr.seconds_left()}s"
                         f"(累计第{self.api_mgr.suspend_count}次) 只不开新仓，平仓决策照常")

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

        # （持仓对账哨兵 + 账户体检已前置到节流/冷静/预热之前，见上方，确保每个 tick 都跑）

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

        # 能不能「开新仓」看的是**手上有没有货**，不看灯的颜色。
        # 灯是喂给 AI 看的地图信息（原始设计备注③），不该在本地直接硬控能不能下单。
        # 老写法 `if self.light in (RED, BLUE)` 有两处跑偏：
        #   ① 未持仓时若是红灯（上涨趋势），也会被当成"持仓中" → 上涨趋势下永远开不了仓；
        #   ② 持仓但黄灯（浮盈）时反而漏出这个分支 → 交给 _execute 去开新仓。
        if self.position != 0:
            if action == "close":
                self._close(tick, d, target_price=sig.get("target_price"))   # AI 共识平仓 → 预挂平仓单
            else:
                self.output(f"[持仓中·{self.light or '无灯'}] 计票员未达成平仓共识 → 仅持")
        else:
            # 空仓：交给 _execute（它内部还有在途单锁 / 资金不足 / 冷静期 / 逆反弹四道再看一遍）
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

        # 翻修v2·锁系统：在途单锁 + 冷静锁（全文件只有 4级蓝→300s 这一把冷静锁）
        # （本方向锁已真正删除；同方向加仓由下方「已是目标仓位」+ max_volume=1 兜底）
        if not self.locks.can_open(d):
            self.output(f"[开仓拦截] {self.locks.last_reason} → 本轮不开新仓")
            self._open_note = f"开仓未执行：{self.locks.last_reason}"
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
            self._pending_kind = "open"           # 这张是开仓单
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
            self._pending_kind = "close"          # 这张是平仓单（超时自动撤单不碰它）
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
                    f"volume={sig.get('order_volume')} 理由={sig.get('reasoning')} | 当前灯={self.light or '无灯'} "
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


# ===================== 翻修v2：四大子系统（灯/检测/API/锁/重启） =====================
MAX_LEVEL = 5


def trend_levels(closes, max_level: int = MAX_LEVEL):
    """
    红/绿强弱（原始设计文档 1）2））。纯函数，可直接单测。

        k 级红 🔴 = 收盘[n] - 收盘[n-k] > 0
        k 级绿 🟢 = 收盘[n] - 收盘[n-k] < 0

    （这等价于文档里 m1..mk 全加起来的累计值，telescoping 化简后不用逐步加）
    红绿互斥：涨就红、跌就绿，不会同时亮。最多 max_level 级（默认 5）。
    中间夹了平盘（差为 0）时跳过不计，不打断也不叠级数。
    """
    red_level = 0
    green_level = 0
    n = len(closes)
    if n >= 2:
        last = closes[-1]
        limit = min(n - 1, max_level)
        for k in range(1, limit + 1):
            d = last - closes[-1 - k]
            if d > 0:
                green_level, red_level = 0, k
            elif d < 0:
                red_level, green_level = 0, k
    return red_level, green_level


def position_levels(entry_price, post_entry_closes, is_short: bool = False,
                    max_level: int = MAX_LEVEL):
    """
    黄/蓝强弱（原始设计文档 3）4））。纯函数，可直接单测。

        第 k 根收盘(A+k) - 持仓价(A) > 0 → k 级黄 🟡
        第 k 根收盘(A+k) - 持仓价(A) < 0 → k 级蓝 🔵

    黄蓝互斥，最多 max_level 级。返回 (黄级, 蓝级)。

    【唯一一处必然的适应性改动】文档是暗含多仓写的；本系统多空都做，
    空单是**跌破持仓价才赚钱**，所以这里按持仓方向给差值取符号，
    否则空单赚钱会亮蓝灯（表示亏），整套灯是反的。
    """
    if not entry_price or not post_entry_closes:
        return 0, 0

    k = min(len(post_entry_closes), max_level)
    if k <= 0:
        return 0, 0

    diff = post_entry_closes[-1] - entry_price
    if is_short:
        diff = -diff

    if diff > 0:
        return k, 0
    if diff < 0:
        return 0, k
    return 0, 0


def light_meaning(yellow: int, blue: int) -> str:
    """
    把文档备注②翻译成人话（这条以前完全没落到任何判断上，现在补回来）：

        "3级黄基本可以确定扣完手续费是赚钱的，而1级蓝已经可以知道在亏损了"
    """
    if yellow >= 3:
        return f"黄{yellow}级：扣完手续费基本确定赚钱"
    if yellow >= 1:
        return f"黄{yellow}级：浮盈中，但还不够覆盖手续费"
    if blue >= 1:
        return f"蓝{blue}级：已在亏损"
    return ""


def build_light_payload(red: int, green: int, yellow: int, blue: int) -> dict:
    """
    喂给 AI 的颜色组合（文档备注③）：

        未开仓 / 平仓后 → 只传【一个】颜色：🔴 或 🟢
        开仓后           → 传【最多两个】：🔴/🟡、🔴/🔵、🟢/🟡、🟢/🔵
    """
    holding = (yellow > 0 or blue > 0)

    if red > 0:
        trend = Light.RED
    elif green > 0:
        trend = Light.GREEN
    else:
        trend = ""

    if holding:
        pos = Light.YELLOW if yellow > 0 else Light.BLUE
        colors = [c for c in (trend, pos) if c]
    else:
        colors = [trend] if trend else []

    return {
        "colors": colors,
        "red": red, "green": green, "yellow": yellow, "blue": blue,
        "held": holding,
        "meaning": light_meaning(yellow, blue),
    }


# 兼容旧名字（文件底部自带的自测还在用 _trend_levels）
_trend_levels = trend_levels


class ApiManager:
    """翻修v2·API管理系统：集中 API 调用计数/成本估算/预算闸门，并**独占**「交易暂停」这个状态。

    原始设计文档⑥：四级蓝 🔵🔵🔵🔵 → 冷静 5 分钟，且由 api 管理系统触发。
    所以冷静期的计时器归它管：suspended_until 是唯一权威，别处只读不写。
    （以前 s.cooldown_until 谁都能写，灯写一份、检测系统绕过 ApiManager 再写一份，两套冷静期互相打架。）
    """
    def __init__(self, s):
        self.s = s
        self.suspended_until = 0.0      # 冷静期截止时间戳（唯一权威）
        self.suspend_count = 0          # 累计触发次数

    def suspend(self, reason="4级蓝", sec=BLUE4_COOLDOWN_SEC):
        """暂停开新仓。全文件只有一处调用：灯看到 4 级蓝（见 _update_light）。"""
        self.suspended_until = max(self.suspended_until, time.time() + float(sec))
        self.suspend_count += 1
        self.s.output(f"[API管理·暂停] {reason} → 冷静{int(sec)}s（累计第{self.suspend_count}次）")

    def suspended(self):
        return time.time() < self.suspended_until

    def seconds_left(self):
        return max(0, int(self.suspended_until - time.time()))

    def budget_exceeded(self):
        """预算是否打满 —— 全文件唯一的算法（策略层的 _budget_exceeded 只是转调到这里）。"""
        return self.s._api_calls >= self.s.max_api_calls or self.s._est_cost >= self.s.max_cost_yuan


class LockSystem:
    """翻修v2·锁系统：集中所有「不允许做X」守卫（在途单锁 / 冷静锁）。

    冷静锁只有一把，就是「4级蓝 → 300s」，时间由 ApiManager 管，这里只负责读。
    封条·本方向锁 已于 2026-09-18 按用户意图 D 第 1 条删除。
    """
    def __init__(self, s):
        self.s = s
        self.last_reason = ""

    def can_open(self, direction):
        s = self.s
        if s.pending_order_id is not None:
            _what = {"open": "开仓", "close": "平仓"}.get(getattr(s, "_pending_kind", None), "")
            self.last_reason = f"已有{_what}挂单 id={s.pending_order_id} 未成交（在途单锁）"
            return False
        if s._fund_blocked:
            self.last_reason = f"资金不足（柜台已报错）→ 禁开新仓（{FUND_RETRY_SEC:.0f}s后自动再试）"
            return False
        if s.api_mgr.suspended():
            self.last_reason = f"4级蓝冷静期（还剩{s.api_mgr.seconds_left()}s，由API管理系统计时）"
            return False
        # 【封条·本方向锁 已于 2026-09-18 删除】—— 用户意图 D 第 1 条。
        # 这里原先留着一句"代码还在，没删干净"的检查注释，但那行代码（pos == target）后来已经删了，
        # 注释反而比代码旧、继续误导人。注释要跟着代码一起改，这是这次全文审计的教训。
        # 同方向加仓由 _execute 的「已是目标仓位，不动」+ max_volume=1 兜底。
        return True


class AccountHealth:
    """翻修v2·检测系统：账户体检（保证金/冻结/手续费/风险度/权益/可用），风险击穿拦截，回传奏折。"""
    RISK_LIMIT = 0.85   # 风险度阈值（占用保证金/动态权益），超则告警（2026-09-18 起不再自动暂停开仓）

    def __init__(self, s):
        self.s = s

    def check(self):
        s = self.s
        s._last_account_health = {}
        try:
            fn = getattr(s, "get_account_fund_data", None)
            if fn is None:
                return
            investor = getattr(s, "investor", "") or getattr(s.params_map, "investor", "")
            try:
                acc = fn(investor) if investor else fn()
            except TypeError:
                acc = fn()   # 无参兜底
            if acc is None:
                return
            margin = float(getattr(acc, "margin", 0) or 0)
            frozen = float(getattr(acc, "frozen_margin", 0) or 0)
            commission = float(getattr(acc, "commission", 0) or 0)
            dynamic = float(getattr(acc, "dynamic_rights", 0) or 0)
            available = float(getattr(acc, "available", 0) or 0)
            risk = (margin / dynamic) if dynamic > 0 else 0.0
            breach = risk > self.RISK_LIMIT
            s._last_account_health = {
                "margin": margin, "frozen_margin": frozen, "commission": commission,
                "risk": risk, "dynamic_rights": dynamic, "available": available,
                "risk_breach": breach,
            }
            if breach:
                # 2026-09-18 按用户指令：风险击穿只告警，**不再自动暂停**。
                # 旧代码在这里直接写 s.cooldown_until，绕过 ApiManager 抢写同一个状态，两套冷静期互相打架；
                # 而且"暂停 600s"对风控没有实际意义 —— 真到风险度 0.85 该是人工介入，不是程序自己歇 10 分钟。
                s.output(f"[检测系统·风险击穿] 风险度={risk:.2f} 超阈值{self.RISK_LIMIT} → 告警（不自动暂停，请人工介入）")
        except Exception as _e:
            s.output(f"[检测系统] 账户体检失败(忽略): {_e}")


class RestartSystem:
    """翻修v2·重启系统：内部状态重置（不杀交易平台进程），手动或检测致命不一致时触发。"""
    def __init__(self, s):
        self.s = s

    def reset_runtime_state(self, reason="手动重置"):
        s = self.s
        s.output(f"[重启系统] {reason} → 重置内部运行时状态（不杀交易平台）")
        s.api_mgr.suspended_until = 0.0
        s.api_mgr.suspend_count = 0
        s._hard_stop_fired = False
        s._tp_fired = False
        s._pos_synced_by_reconcile = False
        s.pending_order_id = None
        s.pending_order_ts = 0.0
        s._pending_kind = None
        s.pending_order_ts = 0.0
        s._post_entry_closes = []
        s.light_trend = None
        s.light_trend_level = 0
        s.light_pos = None
        s.light_pos_level = 0
        s._api_calls = 0
        s._est_cost = 0.0
        s._api_calls_by_provider = {}
        s._last_decision_time = 0.0
        s._cached_sig = {"direction": "hold", "offset_ticks": 0, "order_volume": 0, "reasoning": ""}
        s._last_round_result = ""
        s._open_note = None
        s._event_note = None
        # 重新从柜台同步真实持仓，避免重置后状态漂移
        try:
            gp = getattr(s, "get_position", None)
            if gp is not None and getattr(s.params_map, "instrument_id", ""):
                _pos = gp(s.params_map.instrument_id)
                s.position = _pos.net_position
                if s.position == 0:
                    s.entry_price = 0.0
                    s.entry_ts = 0.0
                else:
                    s.entry_price = getattr(_pos.long, "open_avg_price", 0.0) or \
                                    getattr(_pos.short, "open_avg_price", 0.0) or 0.0
        except Exception as _e:
            s.output(f"[重启系统] 持仓同步失败(忽略): {_e}")


def _self_test_light():
    """翻修v2·灯系统纯函数自测（无需 SDK / API，python 直接跑）。

    按原始设计文档：
      🔴红 / 🟢绿 = 未持仓：连续累计涨 / 跌
      🟡黄 / 🔵蓝 = 持仓后：第 k 根收盘 - 持仓价，为正 / 为负
    """
    # ---- 红 / 绿 ----
    # 例1：5 连涨 → 红5级；绿0级
    c1 = [2600, 2610, 2620, 2630, 2640, 2650]
    assert _trend_levels(c1) == (5, 0), _trend_levels(c1)
    # 例2：5 连跌 → 绿5级；红0级
    c2 = [2650, 2640, 2630, 2620, 2610, 2600]
    assert _trend_levels(c2) == (0, 5), _trend_levels(c2)
    # 例3：含 0 差值（持平）→ 该级不计入。收盘序列整体净涨 2 点，但中间有 0 步
    c3 = [2600, 2600, 2602, 2602, 2602]   # 末-首=+2 → 红4级；全程无跌 → 绿0
    r, g = _trend_levels(c3)
    assert r == 4 and g == 0, (r, g)
    # 例4：V 型（先跌后涨，末>首但中间跌）→ 红=最长净涨窗口
    c4 = [2600, 2590, 2580, 2610, 2620]   # 末2620-首2600=+20 → 红4级；绿：末-次? 2620-2610=+10>0 不计绿；末-2580=+40 不计；末-2590=+30；末-2600=+20 → 全红
    r, g = _trend_levels(c4)
    assert r == 4 and g == 0, (r, g)
    assert _trend_levels([100.0] * 6) == (0, 0)                    # 全平盘 → 红绿都0
    assert _trend_levels([2600]) == (0, 0)                         # 数据不足
    assert _trend_levels([2600 + i for i in range(50)])[0] == MAX_LEVEL   # 最多5级

    # ---- 黄 / 蓝 ----
    assert position_levels(2600.0, [2601, 2602, 2603]) == (3, 0)   # 多仓盈利3根 → 黄3级
    assert position_levels(2600.0, [2599, 2598]) == (0, 2)         # 多仓亏损2根 → 蓝2级
    assert position_levels(2600.0, []) == (0, 0)                   # 没收盘数据
    assert position_levels(2600.0, [2601] * 9) == (5, 0)           # 封顶5级
    # 空单方向：跌破持仓价才赚钱（文档是暗含多仓写的，这里按持仓方向取符号）
    assert position_levels(2600.0, [2599, 2598, 2597], is_short=True) == (3, 0)
    assert position_levels(2600.0, [2601, 2602], is_short=True) == (0, 2)
    assert position_levels(2600.0, [2598], is_short=False) == (0, 1)
    assert position_levels(2600.0, [2598], is_short=True) == (1, 0)   # 同一组数据，多空结论相反

    # ---- 备注②：3级黄≈扣完手续费赚钱 / 1级蓝=已在亏 ----
    assert "赚钱" in light_meaning(3, 0)
    assert "不够" in light_meaning(1, 0)
    assert "亏损" in light_meaning(0, 1)
    assert light_meaning(0, 0) == ""

    # ---- 备注③：未持仓给一个色，持仓给两个色 ----
    assert build_light_payload(3, 0, 0, 0)["colors"] == [Light.RED]
    assert build_light_payload(0, 2, 0, 0)["colors"] == [Light.GREEN]
    assert build_light_payload(3, 0, 2, 0)["colors"] == [Light.RED, Light.YELLOW]
    assert build_light_payload(0, 1, 0, 3)["colors"] == [Light.GREEN, Light.BLUE]
    assert build_light_payload(0, 0, 0, 0)["colors"] == []         # 没灯就不给色
    for args in ((1, 0, 1, 0), (1, 0, 0, 1), (0, 1, 1, 0), (0, 1, 0, 1)):
        assert len(build_light_payload(*args)["colors"]) == 2, args

    print("[自测·灯系统] 四色五级 全部通过（红绿/黄蓝/备注②/备注③）")


if __name__ == "__main__":
    # 翻修v2：先跑灯系统纯函数自测（无需 SDK/API）
    try:
        _self_test_light()
    except AssertionError as _ae:
        print("灯系统自测失败:", _ae)
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
