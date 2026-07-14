# -*- coding: utf-8 -*-
"""
===================================
A股接力环境 & 连板晋级分析系统 v3
===================================

功能：
1. 连板梯队梳理（身位龙/板块龙/补涨龙 + 辨识度 + 涨停时间）
2. 情绪周期判定 + 炸板率/晋级率 → 仓位安全等级
3. 晋级概率筛选（板块支撑/封板质量/筹码/龙虎榜）
4. 每只标的：概率% + 竞价标准 + 介入方式 + 止损位
5. 次日接力仓位建议 + 操作纪律

数据源：hhxg.top API (免费) + AkShare + Anspire LLM
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("relay_scanner")

HHXG_BASE = "https://hhxg.top/api"
ANSPIRE_BASE = "https://open-gateway.anspire.cn/v6"


# ====================================================================
# 0. DATA LAYER
# ====================================================================

def fetch_hhxg_snapshot() -> dict:
    """获取恢恢量化日报快照"""
    r = requests.get(f"{HHXG_BASE}/snapshot", timeout=30)
    r.raise_for_status()
    d = r.json()
    if not d.get("success"):
        raise RuntimeError(f"hhxg API: {d}")
    return d["data"]


def fetch_ak_zt_pool(date_str: str = None) -> list:
    """AkShare涨停板池（获取封板时间/封单等详细数据）"""
    try:
        import akshare as ak
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")
        df = ak.stock_zt_pool_em(date=date_str)
        if df is None or df.empty:
            return []
        return df.to_dict(orient="records")
    except Exception as e:
        logger.warning("AkShare涨停池失败: %s", e)
        return []


def parse_float(v, default=0.0):
    if v is None or (isinstance(v, float) and pd.isna(v)) if 'pd' in dir() else False:
        return default
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return default


# ====================================================================
# 1. SENTIMENT CYCLE ENGINE
# ====================================================================

def calc_sentiment_cycle(snapshot: dict, zt_pool: list) -> dict:
    """计算情绪周期阶段 + 仓位安全等级"""
    mkt = snapshot.get("market", {})
    ladder = snapshot.get("ladder", {})

    sentiment = mkt.get("sentiment_index", 50)
    limit_up = mkt.get("limit_up", 0)
    fried = mkt.get("fried", 0)
    limit_down = mkt.get("limit_down", 0)
    promo_rate_str = mkt.get("promotion_rate", "0%")
    promo_rate = float(promo_rate_str.replace("%", ""))

    # 炸板率
    total_attempts = limit_up + fried
    fry_rate = fried / total_attempts * 100 if total_attempts > 0 else 0

    # 连板数据
    max_streak = ladder.get("max_streak", 1)

    # ---- 情绪周期判定 ----
    if sentiment >= 75 and fry_rate < 25 and max_streak >= 5:
        cycle = "🔥 高潮期 — 接力黄金窗口"
        cycle_detail = "赚钱效应极强，高标持续拓展空间，适合积极接力"
    elif sentiment >= 60 and fry_rate < 35:
        cycle = "🌤️ 发酵期 — 主线明朗，可积极试错"
        cycle_detail = "赚钱效应良好，新题材开始发酵，首板/二板性价比高"
    elif sentiment >= 40:
        cycle = "🌥️ 分歧期 — 轮动加快，精选个股"
        cycle_detail = "赚钱效应一般，板块轮动快，仅参与主线核心标的"
    elif sentiment >= 20:
        cycle = "🌧️ 退潮期 — 亏钱效应扩散，谨慎接力"
        cycle_detail = "高标炸板率高，连板晋级率下滑，建议轻仓或空仓"
    else:
        cycle = "⛈️ 冰点期 — 全面退潮，空仓等待"
        cycle_detail = "市场情绪极度低迷，暂停所有接力操作"

    # ---- 仓位安全等级 ----
    safety_score = 0
    safety_score += min(sentiment * 0.3, 30)          # 赚钱效应 0-30
    safety_score += max(0, (30 - fry_rate) * 0.3)     # 炸板率低 → 高 0-30
    safety_score += min(promo_rate * 1.5, 20)          # 晋级率 0-20
    safety_score += min(max_streak * 4, 20)            # 最高连板 0-20

    if safety_score >= 75:
        safety = "🟢 A级 — 建议仓位 6-8成"
        position = "60-80%"
    elif safety_score >= 55:
        safety = "🟡 B级 — 建议仓位 4-6成"
        position = "40-60%"
    elif safety_score >= 35:
        safety = "🟠 C级 — 建议仓位 2-3成"
        position = "20-30%"
    else:
        safety = "🔴 D级 — 建议空仓或试错仓(<10%)"
        position = "0-10%"

    return {
        "cycle": cycle,
        "cycle_detail": cycle_detail,
        "sentiment": sentiment,
        "fry_rate": round(fry_rate, 1),
        "promo_rate": promo_rate,
        "max_streak": max_streak,
        "limit_up": limit_up,
        "fried": fried,
        "limit_down": limit_down,
        "safety": safety,
        "position": position,
        "safety_score": round(safety_score, 1),
    }


# ====================================================================
# 2. LADDER ANALYSIS (梯队梳理)
# ====================================================================

def analyze_ladder(snapshot: dict) -> dict:
    """梳理连板梯队：身位龙/板块龙/补涨龙 — 多源交叉验证"""
    ladder = snapshot.get("ladder", {})
    ladder_detail = snapshot.get("ladder_detail", {})
    themes = snapshot.get("hot_themes", [])
    hotmoney = snapshot.get("hotmoney", {})
    data_date = snapshot.get("date", "")

    # ---- Ladder from ladder_detail.levels (primary source) ----
    raw_levels = ladder_detail.get("levels", [])
    lb_rates = ladder_detail.get("lb_rates_map", {})
    concept_counts = ladder_detail.get("concept_counts", {})

    # Build levels dict: board_count → {success: [...], failed: [...]}
    levels_data = {}
    for lv in raw_levels:
        board_n = lv.get("boards", 0)
        success_stocks = [s for s in lv.get("stocks", []) if s.get("is_success")]
        fail_stocks = [s for s in lv.get("stocks", []) if not s.get("is_success")]
        levels_data[str(board_n)] = {
            "stocks": lv.get("stocks", []),
            "success": success_stocks,
            "failed": fail_stocks,
            "total": lv.get("count", 0) + lv.get("fail_count", 0),
            "success_count": lv.get("count", 0),
            "fail_count": lv.get("fail_count", 0),
        }

    # ---- Shenwei dragon (market top) ----
    top_streak = ladder.get("top_streak", {})
    shenwei_dragon = {
        "name": top_streak.get("name", ""),
        "boards": top_streak.get("boards", 0),
        "industry": top_streak.get("industry", ""),
    }

    # ---- Sector dragons & theme mapping ----
    # Build stock→themes mapping from hot_themes[].stocks[]
    stock_themes_map = {}  # clean_code → [theme_names]
    theme_stock_codes = {}  # theme_name → {clean_codes}
    for t in themes:
        t_name = t["name"]
        theme_stock_codes[t_name] = set()
        for s in t.get("stocks", []):
            raw_code = s.get("code", "")
            clean_code = raw_code.replace(".SZ", "").replace(".SH", "").replace(".BJ", "").replace(".N", "")
            if clean_code:
                theme_stock_codes[t_name].add(clean_code)
                if clean_code not in stock_themes_map:
                    stock_themes_map[clean_code] = []
                stock_themes_map[clean_code].append(t_name)

    # Sector dragons = top_stocks from each theme (if different from shenwei dragon)
    sector_dragons = []
    for t in themes:
        tops = t.get("top_stocks", [])
        if tops:
            leader_name = tops[0].get("name", "")
            if leader_name != shenwei_dragon.get("name", ""):
                sector_dragons.append({"theme": t["name"], "leader": leader_name})

    # ---- Hot money / Dragon-Tiger Board ----
    hotmoney_top_buy = hotmoney.get("top_net_buy", [])
    hotmoney_seats = hotmoney.get("seats", [])

    # Build seat → stocks mapping
    seat_map = {}
    for seat in hotmoney_seats:
        seat_name = seat.get("name", "")
        seat_stocks = [s.get("name", "") for s in seat.get("stocks", [])]
        if seat_name:
            seat_map[seat_name] = seat_stocks

    return {
        "data_date": data_date,
        "max_streak": ladder.get("max_streak", 0),
        "total_zt": ladder.get("total_limit_up", 0),
        "shenwei_dragon": shenwei_dragon,
        "sector_dragons": sector_dragons,
        "levels_data": levels_data,
        "lb_rates": lb_rates,
        "concept_counts": concept_counts,
        "stock_themes_map": stock_themes_map,
        "theme_stock_codes": theme_stock_codes,
        "hotmoney_top_buy": hotmoney_top_buy,
        "seat_map": seat_map,
        "theme_stats": [
            {"name": t["name"], "zt_count": t["limitup_count"],
             "net_yi": t.get("net_yi", 0),
             "top_stocks": t.get("top_stocks", []),
             "stocks": t.get("stocks", [])}
            for t in themes[:10]
        ],
    }


# ====================================================================
# 3. PROMOTION PREDICTION ENGINE
# ====================================================================

def predict_promotion(zt_pool: list, snapshot: dict, ladder_data: dict) -> List[Dict]:
    """从涨停板中筛选晋级概率最高的标的 — 多源交叉验证"""
    themes = snapshot.get("hot_themes", [])
    hotmoney = snapshot.get("hotmoney", {})
    mkt = snapshot.get("market", {})

    # Theme matching maps (already cleaned in analyze_ladder)
    stock_themes_map = ladder_data.get("stock_themes_map", {})

    # Hot money data
    hotmoney_buy_names = {s.get("name", "") for s in hotmoney.get("top_net_buy", [])}
    seat_stock_names = set()
    for seat in hotmoney.get("seats", []):
        for s in seat.get("stocks", []):
            seat_stock_names.add(s.get("name", ""))

    # All theme stocks with codes for matching
    theme_code_map = {}  # clean_code → theme_name
    for t in themes:
        for s in t.get("stocks", []):
            raw = s.get("code", "")
            clean = raw.replace(".SZ","").replace(".SH","").replace(".BJ","")
            if clean and clean not in theme_code_map:
                theme_code_map[clean] = t["name"]

    candidates = []
    for row in zt_pool:
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not code or not name:
            continue

        # Basic data
        change = parse_float(row.get("涨跌幅", 0))
        turnover = parse_float(row.get("换手率", 0))
        amount = parse_float(row.get("成交额", 0))      # 元
        seal_amt = parse_float(row.get("封板资金", 0))   # 元
        float_mv = parse_float(row.get("流通市值", 0))   # 元
        first_seal = str(row.get("首次封板时间", "")).strip()
        break_n = int(parse_float(row.get("炸板次数", 0)))
        streak_raw = str(row.get("涨停统计", "")).strip()

        # Market cap in 亿
        float_mv_yi = float_mv / 1e8
        # Amount in 亿
        amount_yi = amount / 1e8
        # Seal amount in 亿
        seal_yi = seal_amt / 1e8

        # Streak count
        streak_n = 1
        if "/" in streak_raw:
            try:
                streak_n = int(streak_raw.split("/")[0])
            except Exception:
                streak_n = 1

        # Only analyze consecutive board stocks (连板股)
        if streak_n < 2:
            continue

        # ---- Matched themes (verified cross-source) ----
        matched_themes = stock_themes_map.get(code, [])
        # Also try matching by name from theme stock lists
        if not matched_themes:
            for t in themes:
                for s in t.get("stocks", []):
                    if s.get("name", "") == name:
                        matched_themes.append(t["name"])
        matched_themes = list(dict.fromkeys(matched_themes))  # dedup

        # ---- Score dimensions ----
        scores = {}

        # A. 板块支撑 (0-25) - verified against hhxg theme data
        theme_score = 0
        if matched_themes:
            # Find max limitup count among matched themes
            max_zt = max([
                t.get("limitup_count", 0) for t in themes
                if t["name"] in matched_themes
            ] or [0])
            theme_score = 5 + min(max_zt * 0.6, 20)
        scores["板块支撑"] = round(min(theme_score, 25), 1)

        # B. 封板质量 (0-25)
        seal_score = 0
        if first_seal and ":" in first_seal:
            try:
                parts = first_seal.replace("：", ":").split(":")
                mins = int(parts[0]) * 60 + int(parts[1])
                if mins <= 31:       seal_score += 12
                elif mins <= 61:     seal_score += 9
                elif mins <= 121:    seal_score += 6
                else:                seal_score += 3
            except Exception:
                pass
        if amount > 0 and seal_amt > 0:
            seal_ratio = seal_amt / amount
            if seal_ratio > 0.5:   seal_score += 8
            elif seal_ratio > 0.3: seal_score += 5
            elif seal_ratio > 0.1: seal_score += 3
        if break_n == 0:           seal_score += 5
        elif break_n <= 2:         seal_score += 2
        else:                      seal_score -= 5
        scores["封板质量"] = round(max(0, min(seal_score, 25)), 1)

        # C. 筹码健康度 (0-20)
        chip_score = 10
        if 3 <= turnover <= 15:    chip_score += 6
        elif 15 < turnover <= 25:  chip_score += 3
        elif turnover > 25:        chip_score -= 3
        if float_mv_yi < 50:       chip_score += 4
        elif float_mv_yi < 100:    chip_score += 2
        scores["筹码健康度"] = round(max(0, min(chip_score, 20)), 1)

        # D. 辨识度/梯队 (0-15)
        id_score = 0
        if streak_n >= 3:          id_score += 8
        elif streak_n >= 2:        id_score += 4
        if matched_themes:         id_score += 4
        # Check if sector dragon
        for t in themes:
            tops = t.get("top_stocks", [])
            if tops and tops[0].get("name", "") == name:
                id_score += 3
                break
        scores["辨识度/梯队"] = round(min(id_score, 15), 1)

        # E. 资金/龙虎榜 (0-15) - verified against hhxg hotmoney data
        fund_score = 5
        if name in hotmoney_buy_names:
            fund_score += 5  # 机构/游资净买入TOP
        if name in seat_stock_names:
            fund_score += 3  # 知名游资席位参与
        for t in themes:
            if t.get("name", "") in matched_themes:
                net_yi = t.get("net_yi", 0)
                if net_yi > 10:      fund_score += 5
                elif net_yi > 0:     fund_score += 2
                break
        scores["资金/龙虎榜"] = round(max(0, min(fund_score, 15)), 1)

        # ---- Total ----
        total = sum(scores.values())

        # ---- Intervention method ----
        if streak_n >= 3 and scores["封板质量"] >= 20:
            method = "打板(确认封板后扫板)"
        elif scores["封板质量"] >= 15 and turnover < 20:
            method = "半路(5%-7%追入)"
        else:
            method = "低吸(开盘分歧-3%~-5%低吸)"

        # ---- Stop loss ----
        if streak_n >= 3:
            stop_loss = "-5%或破5日线"
        else:
            stop_loss = "-3%无条件止损"

        # ---- Auction standard ----
        if streak_n >= 3:
            auction = f"竞价高开+3%~+7%, 竞价量>昨日10%"
        else:
            auction = f"竞价高开+2%~+5%, 竞价量>昨日8%"

        # ---- Is sector dragon or shenwei ----
        is_sector_dragon = any(
            t.get("top_stocks", [{}])[0].get("name", "") == name
            for t in themes
        )
        is_shenwei = name == ladder_data.get("shenwei_dragon", {}).get("name", "")

        candidates.append({
            "code": code, "name": name,
            "streak": streak_n,
            "first_seal": first_seal,
            "turnover": round(turnover, 1),
            "amount_yi": round(amount_yi, 2),
            "seal_yi": round(seal_yi, 2),
            "float_mv_yi": round(float_mv_yi, 0),
            "break_n": break_n,
            "themes": matched_themes,
            "scores": scores,
            "total_score": round(total, 1),
            "promo_prob": round(total, 1),
            "method": method,
            "stop_loss": stop_loss,
            "auction": auction,
            "is_sector_dragon": is_sector_dragon,
            "is_shenwei": is_shenwei,
        })

    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    return candidates


# ====================================================================
# 4. LLM ANALYSIS
# ====================================================================

def llm_deep_analysis(
    sentiment: dict, ladder_data: dict, candidates: List[Dict], api_key: str
) -> str:
    """LLM深度分析"""
    if not api_key or not candidates:
        return None

    top_n = candidates[:15]
    cand_lines = []
    for i, c in enumerate(top_n):
        role = ""
        if c["is_shenwei"]:
            role = "【身位龙】"
        elif c["is_sector_dragon"]:
            role = "【板块龙】"
        elif c["streak"] >= 2:
            role = "【补涨龙】" if c["streak"] == 2 else f"【{c['streak']}板高标】"

        themes_str = ", ".join(c["themes"][:3]) if c["themes"] else "无主线"
        scores_str = " | ".join(f"{k}:{v}" for k, v in c["scores"].items())
        cand_lines.append(
            f"{i+1}. {role} {c['name']}({c['code']}) | "
            f"{c['streak']}连板 | 封{c['first_seal']} | 换手{c['turnover']:.1f}% | "
            f"封单{c['seal_yi']:.2f}亿 | 板块:{themes_str} | "
            f"晋级概率:{c['promo_prob']:.0f}% | 介入:{c['method']} | {scores_str}"
        )

    theme_lines = []
    for t in ladder_data.get("theme_stats", [])[:8]:
        theme_lines.append(f"  {t['name']}: {t['zt_count']}只涨停, 净流入{t['net_yi']:.1f}亿")

    prompt = f"""你是A股超短线接力交易专家。基于以下今日盘面数据，给出明日接力策略。

【情绪环境】
- 情绪指数: {sentiment['sentiment']:.0f}/100 ({sentiment['cycle']})
- 炸板率: {sentiment['fry_rate']:.1f}%
- 连板晋级率: {sentiment['promo_rate']:.0f}%
- 最高连板: {sentiment['max_streak']}板
- 仓位建议: {sentiment['safety']}

【主线题材】
{chr(10).join(theme_lines)}

【连板晋级候选TOP15】
{chr(10).join(cand_lines)}

请用Markdown格式分析（每项200字内）：
1. **情绪环境评估**：当前周期适合接力吗？给出你的独立判断。
2. **主线持续性**：哪些题材明天最可能继续？龙头是谁？
3. **晋级精选**：从候选池中挑出你最看好的3-5只（代码+名称+核心逻辑）。
4. **风险警示**：哪2-3只虽然评分高但暗藏风险？
5. **明日操作纪律**：竞价关注什么信号？什么情况下该放弃操作？"""

    try:
        resp = requests.post(
            f"{ANSPIRE_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "Doubao-Seed-2.0-lite",
                "messages": [
                    {"role": "system", "content": "你是A股超短线接力专家。回答专业、简洁、结构化，Markdown格式。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 3000,
            },
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        logger.error("LLM返回%d", resp.status_code)
    except Exception as e:
        logger.error("LLM失败: %s", e)
    return None


# ====================================================================
# 5. REPORT BUILDING
# ====================================================================

def build_report(
    sentiment: dict, ladder_data: dict, candidates: List[Dict], llm_text: str = None
) -> str:
    """生成接力分析报告 — 纯文本格式，对齐原项目决策仪表盘风格"""

    trade_date = ladder_data.get('data_date', datetime.now().strftime("%Y-%m-%d"))
    now = datetime.now().strftime("%H:%M")

    lines = []
    parts = [f"🎯 {trade_date} 短线接力决策仪表盘"]

    # ---- Summary header ----
    high = [c for c in candidates if c["promo_prob"] >= 55]
    mid = [c for c in candidates if 40 <= c["promo_prob"] < 55]
    low = [c for c in candidates if c["promo_prob"] < 40]
    buy = len(high)
    watch = len(mid)
    sell = len(low)

    lines = [
        f"🎯 {trade_date} 短线接力决策仪表盘",
        f"共{len(candidates)}只连板候选 | 🔥高概率:{buy} 🟡中等:{watch} 🔴低概率:{sell}",
        "",
        "📊 接力环境评估",
    ]

    # ---- Environment ----
    lines += [
        f"💭 情绪周期: {sentiment['cycle']}",
        f"📈 赚钱效应: {sentiment['sentiment']:.0f}/100 | 涨停{sentiment.get('limit_up',0)}家 | 跌停{sentiment.get('limit_down',0)}家",
        f"💥 炸板率: {sentiment['fry_rate']:.1f}% | 连板晋级率: {sentiment['promo_rate']:.0f}%",
        f"🏔️ 最高连板: {sentiment['max_streak']}板",
        f"🛡️ 仓位安全: {sentiment['safety']}",
        f"",
        f"💬 环境解读: {sentiment['cycle_detail']}",
        "",
    ]

    # ---- Ladder ----
    sd = ladder_data.get("shenwei_dragon", {})
    levels_data = ladder_data.get("levels_data", {})

    lines.append("🪜 连板梯队梳理")
    if sd and sd.get("name"):
        lines.append(f"👑 身位龙: {sd.get('name','')} ({sd.get('boards',0)}连板 | {sd.get('industry','')})")

    # Theme leaders with accurate data
    for ts in ladder_data.get("theme_stats", [])[:8]:
        lines.append(f"🏆 {ts['name']}: {ts['zt_count']}只涨停 | 净流入{ts['net_yi']:+.1f}亿 | 龙一: {ts['top_stocks'][0]['name'] if ts.get('top_stocks') else '—'}")

    # Ladder levels with success/fail rates
    if levels_data:
        lines.append(f"")
        for lv_key in sorted(levels_data.keys(), key=lambda x: int(x)):
            lv = levels_data[lv_key]
            suc = lv["success_count"]
            fail = lv["fail_count"]
            total_attempts = suc + fail
            # Compute晋级率 from actual data (verifiable)
            next_lv_key = str(int(lv_key) + 1)
            next_lv = levels_data.get(next_lv_key, {})
            from_next = next_lv.get("total", 0)
            # 晋级率 = 该梯队成功晋级到下一梯队的数量 / 该梯队总尝试数
            if total_attempts > 0:
                calc_rate = f"{suc/total_attempts*100:.0f}%"
            else:
                calc_rate = "—"
            label = f"{lv_key}板" if lv_key != "1" else "首板"

            # Show success stocks
            success_names = [f"{s.get('name','')}({s.get('code','').replace('.SZ','').replace('.SH','')})" for s in lv["success"][:8]]
            success_str = ", ".join(success_names) if success_names else "无"
            lines.append(f"📋 {label}: {suc}成功/{fail}失败 (晋级率{calc_rate}) | 晋级: {success_str}")

    lines.append("")

    # ---- Hot money reference ----
    hotmoney_buy = ladder_data.get("hotmoney_top_buy", [])
    if hotmoney_buy:
        names = [f"{s.get('name','')}({s.get('net_yi',0):+.1f}亿)" for s in hotmoney_buy[:5]]
        lines.append(f"💰 龙虎榜净买TOP5: {', '.join(names)}")
        lines.append("")

    # ---- LLM Analysis ----
    if llm_text:
        # Clean up LLM output (remove ## headers, make it plain text)
        cleaned = llm_text
        cleaned = cleaned.replace("## ", "").replace("### ", "")
        # Remove bold markers and horizontal rules
        cleaned = cleaned.replace("**", "")
        cleaned = cleaned.replace("---", "")
        lines.append("🧠 AI综合研判")
        lines.append(cleaned)
        lines.append("")

    # ---- Top Picks (card format, matching original style) ----
    top_picks = candidates[:8]
    if top_picks:
        lines.append("🎯 晋级精选标的")

        for i, c in enumerate(top_picks):
            prob = c["promo_prob"]
            prob_icon = "🟢" if prob >= 55 else ("🟡" if prob >= 40 else "🔴")
            s = c["scores"]
            themes_str = "/".join(c["themes"][:2]) if c["themes"] else "无主线"

            # Role
            role = ""
            if c["is_shenwei"]:
                role = "👑身位龙 "
            elif c["is_sector_dragon"]:
                role = "🏆板块龙 "
            elif c["streak"] >= 3:
                role = f"📈{c['streak']}板高标 "

            lines.append("")
            lines.append(f"{prob_icon} {role}{c['name']} ({c['code']})")
            lines.append(f"📊 晋级概率: {prob:.0f}% | 综合评分: {c['total_score']:.0f}/100")
            lines.append(f"📰 {c['streak']}连板 | 首封{c['first_seal']} | {'未开板' if c['break_n']==0 else '开板'+str(c['break_n'])+'次'} | 换手{c['turnover']:.1f}%")
            lines.append(f"💭 板块支撑: {themes_str} | 得分{s['板块支撑']}/25")
            lines.append(f"💭 封板质量: 得分{s['封板质量']}/25 | 筹码: {s['筹码健康度']}/20 | 辨识度: {s['辨识度/梯队']}/15")
            lines.append(f"")
            lines.append(f"✨ 核心逻辑: {themes_str}主线共振 + {'身位优势' if c['is_shenwei'] else '板块龙头' if c['is_sector_dragon'] else '补涨潜力'} + {'封板质量优秀' if s['封板质量']>=20 else '封板质量良好' if s['封板质量']>=12 else '封板质量待观察'}")
            lines.append(f"")
            lines.append(f"🚨 风险点: {'面临' + str(c['streak']+1) + '板分歧考验，需确认竞价强度' if c['streak']>=3 else '板块持续性需观察'} | {s['资金/龙虎榜']}/15资金分")
            lines.append(f"")
            lines.append(f"📋 操盘预案:")
            lines.append(f"   竞价标准: {c['auction']}")
            lines.append(f"   介入方式: {c['method']}")
            lines.append(f"   止损位: {c['stop_loss']}")

    # ---- Remaining candidates (compact) ----
    rest = [c for c in candidates if c not in top_picks]
    if rest:
        lines.append("")
        lines.append(f"📋 其余连板候选 ({len(rest)}只)")
        for c in rest[:15]:
            role = "👑" if c["is_shenwei"] else ("🏆" if c["is_sector_dragon"] else "⚪")
            lines.append(f"  {role} {c['name']}({c['code']}): {c['streak']}连板 | 概率{c['promo_prob']:.0f}% | {c['method']} | 止损{c['stop_loss']}")

    # ---- Discipline ----
    lines += [
        "",
        "📋 明日操作纪律",
        f"💰 仓位建议: {sentiment['position']} | 单票上限: 总仓位20%",
        "",
        "✅ 竞价关注清单:",
        "  1. 精选标的竞价高开是否在标准区间",
        "  2. 竞价量能达标 (>昨日成交量10%)",
        "  3. 板块其他涨停股竞价表现",
        "  4. 市场整体竞价情绪 (涨跌比)",
        "",
        "❌ 放弃信号 (任一触发即空仓):",
        "  1. 竞价大幅低开 (>3%) 或高开后快速跳水",
        "  2. 竞价量能严重萎缩 (<昨日5%)",
        "  3. 开盘5分钟未封板或开板超2次",
        "  4. 板块龙头集体走弱",
        "  5. 大盘低开超1%且无反弹迹象",
        "",
        "🛑 铁律: 日内止损无条件执行，不扛单，不侥幸",
        "📝 盘后复盘每笔交易，记录买入理由与实际走势偏差",
        "",
        "---",
        "⚠️ 免责声明: 基于公开数据和AI模型生成，仅供研究参考，不构成任何投资建议。",
        "短线接力风险极高，盈亏自负。",
        f"生成时间: {now}",
    ]

    return "\n".join(lines)


# ====================================================================
# 6. MAIN
# ====================================================================

def main():
    parser = argparse.ArgumentParser(description="A股接力环境 & 连板晋级分析 v3")
    parser.add_argument("--output", "-o", default=None, help="报告输出路径")
    parser.add_argument("--skip-llm", action="store_true", help="跳过AI分析")
    parser.add_argument("--date", default=None, help="日期 YYYYMMDD")
    args = parser.parse_args()

    trade_date = args.date or datetime.now().strftime("%Y%m%d")
    today = datetime.now().strftime("%Y-%m-%d")

    # Load env
    from dotenv import load_dotenv
    load_dotenv()
    anspire_key = os.getenv("ANSPIRE_API_KEYS", "").strip()

    print(f"\n{'='*55}")
    print(f"  🎯 短线接力环境分析 v3  |  {today}")
    print(f"{'='*55}\n")

    # 1. Fetch data
    print("[1/4] 获取数据...", end=" ")
    try:
        snapshot = fetch_hhxg_snapshot()
        print(f"OK (hhxg.top, {snapshot.get('date','')})")
    except Exception as e:
        print(f"FAIL: {e}")
        print("⚠️ hhxg.top API不可用，尝试备用数据源...")
        snapshot = None

    print("      涨停板详情...", end=" ")
    try:
        zt_pool = fetch_ak_zt_pool(trade_date)
        print(f"OK ({len(zt_pool)}只)")
    except Exception as e:
        print(f"FAIL: {e}")
        zt_pool = []

    if snapshot is None:
        print("❌ 无数据源可用，退出")
        sys.exit(1)

    # 2. Sentiment
    print("[2/4] 情绪周期分析...", end=" ")
    sentiment = calc_sentiment_cycle(snapshot, zt_pool)
    print(f"{sentiment['cycle']} | 仓位:{sentiment['position']}")

    # 3. Ladder
    print("      梯队梳理...", end=" ")
    ladder_data = analyze_ladder(snapshot)
    lv_count = len(ladder_data.get('levels_data', {}))
    print(f"最高{ladder_data['max_streak']}板, {lv_count}级梯队")

    # 4. Promotion prediction
    print("[3/4] 晋级预测...", end=" ")
    candidates = predict_promotion(zt_pool, snapshot, ladder_data)
    high_n = sum(1 for c in candidates if c["promo_prob"] >= 55)
    mid_n = sum(1 for c in candidates if 40 <= c["promo_prob"] < 55)
    print(f"{len(candidates)}只连板股 | 高概率:{high_n} 中等:{mid_n}")

    # 5. LLM
    llm_text = None
    if not args.skip_llm and anspire_key and candidates:
        print("[4/4] AI深度研判...", end=" ")
        llm_text = llm_deep_analysis(sentiment, ladder_data, candidates, anspire_key)
        print("OK" if llm_text else "SKIP")
    else:
        print("[4/4] AI研判: SKIP")

    # 6. Report
    report = build_report(sentiment, ladder_data, candidates, llm_text)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\n✅ 报告: {out_path} ({len(report)} chars)")
    else:
        out_path = Path(f"reports/relay_analysis_{trade_date}.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\n✅ 报告: {out_path} ({len(report)} chars)")

    # Summary
    print(f"\n{'='*55}")
    print(f"  📊 {sentiment['cycle']}")
    print(f"  🛡️ 仓位: {sentiment['safety']}")
    print(f"  🎯 连板候选: {len(candidates)}只")
    print(f"     🔥高概率: {high_n} | 🟡中等: {mid_n}")
    print(f"{'='*55}\n")

    return report


if __name__ == "__main__":
    import pandas as pd  # for parse_float's pd.isna
    main()
