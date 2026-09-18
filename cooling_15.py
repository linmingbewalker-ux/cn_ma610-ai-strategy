"""cooling_15.py — 冷静期15问 自适应生成器
================================================

放在与各个 AI_* 策略同目录，被它们 import 使用：
    from cooling_15 import build_cooling_questions, SYSTEM_PROMPT_COOLING

设计（满足"15问跟随策略而改变，大致方向都一样"）：
- 板块结构固定：6 大板块、15 题，题号与数量不变 —— 这就是"大致方向都一样"。
- 具体措辞由 strategy_profile 填充（因子/短线/信号含糊 三种分支）。
- 真实参数值（净持仓上限、品种代码等）由策略在调用时通过 params 注入，
  避免 AI 在自检里"背"出训练记忆里的默认/幻觉数值（曾出现把 max_position 说成 5）。
- 配置了任何品种都追加【品种深挖】题（不框死 ao/MA/FG），AI 按 Q4 注入的真实
  品种代码作答对应商品，切品种无需改代码。
- 与 DeepSeekClient.chat 配合：冷静期内只问不交易（system 已声明不输出交易JSON）。
"""

SYSTEM_PROMPT_COOLING = (
    "你是期货交易AI，现在处于启动自检（冷静期）环节。请逐题回答用户的15道自检题，"
    "想到什么说什么，畅所欲言，展示你的真实认知。本环节不要输出交易指令JSON，"
    "只需逐题回答自检题。"
)

# 品种深挖专项自检题：配置任何品种都追加（不框死 ao/MA/FG），题目通用，
# AI 按 Q4 注入的真实品种代码作答对应商品。
COMMODITY_COOLING_EXTRA = """\
【品种深挖】请基于你实际交易的品种（见 Q4 代码），深入说明：
17. 该品种的成本结构与定价逻辑主要来自哪些环节（原料、能源、产能与政策）？你打算怎么跟踪它的供需平衡？
18. 该品种与上下游/替代品之间存在怎样的比价或利润分配关系？你在交易时如何利用或规避这种关系？
19. 该品种的投产、检修、库存或季节性规律如何？你打算怎么跟踪节奏，避免逆势而动？
"""


def build_cooling_questions(profile: dict, snapshot: str, params=None) -> str:
    # 复制一份，按真实参数值覆盖，避免 AI 自检里幻觉默认数值
    p = dict(profile)
    if params is not None:
        try:
            p["max_position"] = (
                f"净持仓上限={int(params.max_position)}手（代码强制，AI 改不了）"
            )
        except Exception:
            pass
        inst = getattr(params, "instrument_id", "") or ""
        if inst:
            p["instrument_hint"] = str(inst)
            p["extra_section"] = COMMODITY_COOLING_EXTRA

    sig = p.get("signal_label", "你的核心信号")
    sig_explain = p.get("signal_explain", "你使用的信号/指标")
    pos_q = p.get(
        "position_question",
        f"你当前跟踪的【{sig}】是多少？当前价处于什么位置？",
    )
    key_level = p.get("key_level", "关键阻力/支撑位")
    market_hint = p.get("market_hint", "趋势 / 震荡 / 单边")
    ob_hint = p.get("orderbook_hint", "盘口买卖量")
    max_pos = p.get("max_position", "净持仓上限")
    instrument_hint = p.get("instrument_hint", "见下方行情数据")

    if p.get("signal_ambiguous"):
        q5 = (
            f"5. 你的信号名是「{sig_explain}」，但这个名字不够清楚——"
            f"它具体由哪些字段/指标、用什么逻辑算出来的？请把它解释清楚，"
            f"避免我们理解偏差，并说清它现在指向什么方向。"
        )
    else:
        q5 = f"5. 用你的话解释：{sig_explain} 是什么？它怎么算出信号/方向？"

    body = (
        "策略刚启动，正处冷静期观察（只看不做）。请逐题回答下面15道自检题，"
        "想到什么说什么，没有标准答案：\n\n"
        "【数据感知】\n"
        "1. 把你收到的行情里，最新价、买一、卖一、价差，原样报出来。\n"
        f"2. {pos_q}\n"
        "3. 你看到的最近几根K线，分别涨还是跌？什么形态？\n\n"
        "【品种与概念】\n"
        f"4. 你正在交易的品种代码是【{instrument_hint}】。说说你对它的了解（波动特性、主力月份、关键价位）。\n"
        f"{q5}\n\n"
        "【市场理解】\n"
        f"6. 用几句话概括当前市场状态（{market_hint}）。\n"
        "7. 当前价格处于什么位置？这个位置意味着什么？\n"
        f"8. 盘口买方强还是卖方强？你怎么看出来的（参考：{ob_hint}）？\n\n"
        "【推测能力】\n"
        "9. 根据当前数据，你推测接下来价格可能往哪走？为什么？\n"
        f"10. 如果价格突然放量突破【{key_level}】，你会怎么做？\n\n"
        "【架构与自我认知】\n"
        "11. 你知道你的决策怎么被执行吗？你输出 direction/offset_ticks/order_volume，"
        "实际会发生什么（结合三道硬兜底：净持仓上限/涨跌停/最小变动价位）？\n"
        "12. 现在给你的信息，你觉得最缺什么？缺了会怎样？\n"
        '13. 对"AI判断+程序执行（你全权，但代码有硬兜底）"这个架构，你有什么想法或建议？\n\n'
        "【风控意识】\n"
        f"14. 你最多能开几手？为什么有这个限制（{max_pos}）？\n"
        "15. 假设你已持多1手、浮亏到-50元，你该做什么？为什么？\n\n"
        "【盈利认知】\n"
        "16. 请阐述你对「盈利」的理解（这关系到你的交易逻辑是否健康）：\n"
        "(1) 一笔多单从开仓到平仓，盈利具体由哪些量决定？请给出计算式（含合约乘数，并说明手续费/滑点如何侵蚀真实盈利）。\n"
        "(2) 浮盈（持仓未平）算不算「已经赚到」？为什么？\n"
        "(3) 假设你的方向判断对了，但你选择「没到止损就一直扛着」，最后只小赚或持平平仓——这算不算健康的盈利方式？为什么？\n"
        "(4) 你如何区分「靠信号逻辑赚钱」和「靠运气赚钱」？你觉得你现在的逻辑能稳定盈利吗？\n\n"
        "（下面是当前行情数据，仅供你回答自检题参考。本环节不需要输出交易JSON，只需逐题回答）\n"
        f"{snapshot}"
    )
    extra = p.get("extra_section", "")
    if extra:
        body += "\n\n" + extra
    return body
