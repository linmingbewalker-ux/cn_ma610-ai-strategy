# accountant.py
# ===========================================================================
# 「会计小兵」—— 幕后记账水手。
# 职责：记录每一笔成交（开/平、方向、价格、手数、时刻），用 FIFO 核算
#       已实现盈亏 + 浮动盈亏 + 手续费，并 output 为台账 CSV / 汇总文本。
# 原则：只记账、不亲自下单；但会识别硬止损（浮亏达线）报告给计票员，由策略强制平仓。
# 依赖：纯标准库（csv / datetime / collections），无 pythongo、无第三方包。
# ===========================================================================
import csv
from collections import deque
from datetime import datetime


class TradeAccountant:
    """逐笔成交账本 + 盈亏统计。"""

    def __init__(self, point_value=10.0, fee_per_leg=5.0, instrument="", log_fn=print,
                 max_daily_loss=100.0):
        self.pv = float(point_value)          # 每点价值（元/手）
        self.fee = float(fee_per_leg)         # 每笔成交手续费（元/手·边）
        self.instrument = instrument
        self._log = log_fn
        self.max_daily_loss = float(max_daily_loss)  # 亏损硬闸阈值(元)·导演第⑤条

        self.open_lots = deque()              # FIFO 未平仓：{direction, price, volume, ts}
        self.position = 0                     # 净持仓：+多 / -空
        self.fills = []                       # 全部成交流水（开+平）
        self.votes = []                       # 投票流水（每轮辩论一笔，记录员账本）
        self.realized = 0.0                   # 已实现盈亏（元，毛，未扣费）
        self.fees_paid = 0.0                  # 累计手续费（元）
        self.n_fills = 0
        self.breached = False                 # 亏损熔断标志（达阈值置位，不自动复位）

    # ------------------------- 核心：记录一笔成交 -------------------------
    def record_fill(self, kind, direction, volume, price, ts=None):
        """kind: 'open' / 'close'；direction: 'buy' / 'sell'。返回该笔平仓盈亏(元)或 None。"""
        direction = str(direction).lower()
        volume = int(volume)
        price = float(price)
        self.n_fills += 1
        sign = 1 if direction == "buy" else -1
        pnl_this = None

        # 每笔成交都计手续费（开+平各算一边）
        self.fees_paid += self.fee * volume

        if kind == "open":
            self.open_lots.append({"direction": direction, "price": price,
                                   "volume": volume, "ts": ts})
            self.position += sign * volume
        else:  # close —— FIFO 平掉 open_lots
            remain = volume
            while remain > 0 and self.open_lots:
                lot = self.open_lots[0]
                m = min(remain, lot["volume"])
                if lot["direction"] == "buy":        # 平多
                    pnl_this = (price - lot["price"]) * m * self.pv
                else:                                 # 平空
                    pnl_this = (lot["price"] - price) * m * self.pv
                self.realized += pnl_this
                lot["volume"] -= m
                remain -= m
                if lot["volume"] <= 0:
                    self.open_lots.popleft()
            self.position += sign * volume

        self.fills.append({
            "ts": ts, "kind": kind, "direction": direction,
            "volume": volume, "price": price,
            "pnl_this": pnl_this, "position": self.position,
            "realized_cum": self.realized,
        })

        # 幕后输出：每笔成交打一行账
        if self._log:
            tag = "开" if kind == "open" else "平"
            # 开仓：方向即建仓方向；平仓：平掉的是反向头寸（sell平多 / buy平空）
            side = "多" if (direction == "buy") == (kind == "open") else "空"
            pnl_txt = "" if pnl_this is None else " 笔盈亏≈%.1f元" % pnl_this
            self._log("[会计] %s%s %d手 @ %.1f%s → 净持仓=%+d 累计已实现=%+.1f元"
                      % (tag, side, volume, price, pnl_txt, self.position, self.realized))

        # 亏损熔断监测（导演第⑤条）：当日已实现亏损达阈值 → 置位 breached
        if (not self.breached) and self.max_daily_loss > 0 and self.realized <= -self.max_daily_loss:
            self.breached = True
            if self._log:
                self._log("[会计·熔断] 当日已实现亏损 ¥%.1f ≥ 阈值¥%.1f → 触发导演硬闸"
                          % (-self.realized, self.max_daily_loss))
        return pnl_this

    # ------------------------- 浮动盈亏 -------------------------
    def floating_pnl(self, mark_price):
        mp = float(mark_price)
        f = 0.0
        for lot in self.open_lots:
            if lot["direction"] == "buy":
                f += (mp - lot["price"]) * lot["volume"] * self.pv
            else:
                f += (lot["price"] - mp) * lot["volume"] * self.pv
        return f

    # ------------------------- 硬止损识别（会计小兵 → 计票员） -------------------------
    def check_hard_stop(self, mark_price, stop_yuan):
        """识别硬止损：存在净持仓且浮动亏损达到 stop_yuan（元）时返回 True。
        策略据此立即通知计票员强制平仓并进入黄灯冷静期。只报告、不亲自下单。"""
        if self.position == 0:
            return False
        return self.floating_pnl(mark_price) <= -float(stop_yuan)

    # ------------------------- 止盈识别（会计小兵 → 计票员） -------------------------
    def check_take_profit(self, mark_price, take_profit_points):
        """识别止盈：存在净持仓且浮盈达到 take_profit_points 个点（价格单位）时返回 True。
        策略据此立即通知计票员强制止盈平仓并进入黄灯冷静期。只报告、不亲自下单。"""
        if self.position == 0:
            return False
        profit_points = self.floating_pnl(mark_price) / self.pv
        return profit_points >= float(take_profit_points)

    # ------------------------- 汇总 -------------------------
    def summary(self, mark_price=None):
        floating = self.floating_pnl(mark_price) if mark_price is not None else None
        net = self.realized - self.fees_paid
        if floating is not None:
            net += floating
        return {
            "instrument": self.instrument,
            "position": self.position,
            "n_fills": self.n_fills,
            "open_lots": len(self.open_lots),
            "realized": self.realized,
            "fees_paid": self.fees_paid,
            "floating": floating,
            "net": net,
        }

    def summary_text(self, mark_price=None):
        s = self.summary(mark_price)
        fl = "—" if s["floating"] is None else "%+.1f" % s["floating"]
        brk = " [熔断!]" if self.breached else ""
        return ("会计汇总 | 品种=%s 净持仓=%+d 笔数=%d | 已实现=%+.1f元 "
                "手续费=-%.1f元 浮动=%s元 | 净(估)=%+.1f元%s"
                % (s["instrument"] or "?", s["position"], s["n_fills"],
                   s["realized"], s["fees_paid"], fl, s["net"], brk))

    # ------------------------- output：台账 CSV -------------------------
    def export_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["idx", "time", "action", "side", "volume",
                        "price", "pnl_this(元)", "position_after",
                        "realized_cum(元)", "fees_cum(元)"])
            for i, r in enumerate(self.fills, 1):
                ts = r["ts"]
                if isinstance(ts, (int, float)):
                    tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    tstr = "" if ts is None else str(ts)
                act = "开仓" if r["kind"] == "open" else "平仓"
                side = "多" if r["direction"] == "buy" else "空"
                pnl = "" if r["pnl_this"] is None else "%+.1f" % r["pnl_this"]
                w.writerow([i, tstr, act, side, r["volume"], "%.1f" % r["price"],
                            pnl, r["position"], "%+.1f" % r["realized_cum"],
                            "%.1f" % self.fees_paid])
        return path

    # ------------------------- 投票账本（记录员坐在计票员旁） -------------------------
    def record_vote(self, round_no, ts, price, position, hold_seconds, votes, verdict, mode="open"):
        """记录员：每轮辩论落一笔（票数/价格/多空/持票时长）。
        votes   = [{"role","direction","conv","weight","reasoning"}, ...] 四家各自票型
        verdict = 计票员结论 dict（action/direction/weight/voters/support_rate/...）
        """
        self.votes.append({
            "round_no": round_no, "ts": ts, "price": price,
            "position": position, "hold_seconds": hold_seconds,
            "votes": votes, "verdict": verdict, "mode": mode,
        })
        if self._log:
            side = "多" if position > 0 else ("空" if position < 0 else "空仓")
            vtxt = " ".join("%s:%s%s" % (v["role"][:2], v.get("direction", "?")[:1], v.get("conv", ""))
                            for v in votes)
            self._log("[记录员] 第%d轮 价%.1f 仓%d(%s) 持%d秒 | %s → %s %s"
                      % (round_no, price, position, side, hold_seconds, vtxt,
                         verdict.get("action"), verdict.get("direction", "-")))

    def export_vote_csv(self, path):
        """导出投票账本为 vote_log.csv（与成交台账同目录，机器可读、复盘可追溯）。"""
        cols = ["idx", "time", "round", "price", "position", "side", "hold_sec",
                "技术分析师_方向", "技术分析师_意愿", "民间高手_方向", "民间高手_意愿",
                "交易员_方向", "交易员_意愿", "组合经理_方向", "组合经理_意愿",
                "action", "direction", "weight", "voters", "support_rate", "note"]
        roles = ["技术分析师", "民间高手", "交易员", "组合经理"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for i, r in enumerate(self.votes, 1):
                ts = r["ts"]
                if isinstance(ts, (int, float)):
                    tstr = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                else:
                    tstr = "" if ts is None else str(ts)
                pos = r["position"]
                side = "多" if pos > 0 else ("空" if pos < 0 else "空仓")
                vt = {v["role"]: v for v in r["votes"]}
                def _dc(role):
                    v = vt.get(role)
                    return (v.get("direction", ""), v.get("conv", "")) if v else ("", "")
                dvs = {role: _dc(role) for role in roles}
                vt_ = r["verdict"]
                note = ""
                if vt_.get("double_insurance"):
                    note = "双保险"
                elif vt_.get("conflict"):
                    note = "方向冲突"
                sr = vt_.get("support_rate")
                w.writerow([i, tstr, r["round_no"], "%.1f" % r["price"], pos, side, r["hold_seconds"],
                           dvs["技术分析师"][0], dvs["技术分析师"][1],
                           dvs["民间高手"][0], dvs["民间高手"][1],
                           dvs["交易员"][0], dvs["交易员"][1],
                           dvs["组合经理"][0], dvs["组合经理"][1],
                           vt_.get("action", ""), vt_.get("direction", ""),
                           vt_.get("weight", ""), vt_.get("voters", ""),
                           "" if sr is None else "%.2f" % sr, note])
        return path
