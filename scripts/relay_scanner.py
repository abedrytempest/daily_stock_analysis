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
    """梳理连板梯队：身位龙/板块龙/补涨龙"""
    ladder = snapshot.get("ladder", {})
    themes = snapshot.get("hot_themes", [])
    data_date = snapshot.get("date", "")

    boards_data = ladder.get("boards", {})
    top_streak = ladder.get("top_streak", {})

    # Parse board levels from ladder
    levels = {}
    for key, stocks in boards_data.items():
        if key.startswith("board_") and isinstance(stocks, list):
            level = key.replace("board_", "")
            levels[level] = stocks

    # Theme → top stocks mapping
    theme_leaders = {}
    for t in themes:
        name = t.get("name", "")
        tops = t.get("top_stocks", [])
        if tops:
            theme_leaders[name] = tops[0].get("name", "")

    # Identify roles
    shenwei_dragon = top_streak  # 市场最高板 = 身位龙
    sector_dragons = []  # 板块龙
    for theme_name, leader_name in theme_leaders.items():
        if leader_name != shenwei_dragon.get("name", ""):
            sector_dragons.append({"theme": theme_name, "leader": leader_name})

    return {
        "data_date": data_date,
        "max_streak": ladder.get("max_streak", 0),
        "total_zt": ladder.get("total_limit_up", 0),
        "shenwei_dragon": shenwei_dragon,
        "sector_dragons": sector_dragons,
        "levels": levels,
        "theme_leaders": theme_leaders,
        "theme_stats": [
            {"name": t["name"], "zt_count": t["limitup_count"], "net_yi": t.get("net_yi", 0)}
            for t in themes[:10]
        ],
    }


# ====================================================================
# 3. PROMOTION PREDICTION ENGINE
# ====================================================================

def predict_promotion(zt_pool: list, snapshot: dict) -> List[Dict]:
    """从涨停板中筛选晋级概率最高的标的"""
    ladder = snapshot.get("ladder", {})
    themes = snapshot.get("hot_themes", [])
    mkt = snapshot.get("market", {})

    boards_map = ladder.get("boards", {})
    # Flatten all ladder stocks for quick lookup
    ladder_stocks = {}
    for k, v in boards_map.items():
        if isinstance(v, list):
            for s in v:
                code = str(s.get("code", "")).strip()
                if code:
                    ladder_stocks[code] = {**s, "board_level": k}

    # Theme stock sets
    theme_stock_sets = {}
    for t in themes:
        t_name = t["name"]
        stocks = set()
        if "stocks" in t:
            for s in t["stocks"]:
                stocks.add(str(s.get("code", "")).strip())
        theme_stock_sets[t_name] = {"count": t.get("limitup_count", 0), "stocks": stocks}

    candidates = []
    for row in zt_pool:
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        if not code:
            continue

        # Basic data
        change = parse_float(row.get("涨跌幅", 0))
        turnover = parse_float(row.get("换手率", 0))
        amount = parse_float(row.get("成交额", 0))
        seal_amt = parse_float(row.get("封板资金", 0))
        float_mv = parse_float(row.get("流通市值", 0))
        first_seal = str(row.get("首次封板时间", "")).strip()
        break_n = int(parse_float(row.get("炸板次数", 0)))
        streak_raw = str(row.get("涨停统计", "")).strip()

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

        # ---- Score dimensions ----
        scores = {}

        # A. 板块支撑 (0-25)
        theme_score = 0
        matched_themes = []
        for t_name, t_data in theme_stock_sets.items():
            if code in t_data["stocks"] or name in str(t_data.get("stocks", "")):
                matched_themes.append(t_name)
                theme_score = max(theme_score, min(t_data["count"] * 2, 20) + 5)
        if not matched_themes:
            theme_score = 5  # 无板块支撑
        scores["板块支撑"] = min(theme_score, 25)

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
        scores["封板质量"] = max(0, min(seal_score, 25))

        # C. 筹码健康度 (0-20)
        chip_score = 10  # base
        if 3 <= turnover <= 15:    chip_score += 6
        elif 15 < turnover <= 25:  chip_score += 3
        elif turnover > 25:        chip_score -= 3
        if float_mv < 50:          chip_score += 4
        elif float_mv < 100:       chip_score += 2
        scores["筹码健康度"] = max(0, min(chip_score, 20))

        # D. 辨识度/梯队地位 (0-15)
        id_score = 0
        if streak_n >= 3:          id_score += 8
        elif streak_n >= 2:        id_score += 4
        if matched_themes:         id_score += 4
        # Check if it's sector dragon
        for t in themes:
            tops = t.get("top_stocks", [])
            if tops and tops[0].get("name", "") == name:
                id_score += 3
                break
        scores["辨识度/梯队"] = min(id_score, 15)

        # E. 资金/龙虎榜 (0-15)
        fund_score = 5  # base
        ls = ladder_stocks.get(code, {})
        hotmoney = ls.get("hotmoney", "")
        if "机构" in str(hotmoney):
            fund_score += 5
        elif "知名" in str(hotmoney) or "游资" in str(hotmoney):
            fund_score += 3
        # Check theme net inflow
        for t in themes:
            if t.get("name", "") in matched_themes:
                net_yi = t.get("net_yi", 0)
                if net_yi > 5:      fund_score += 5
                elif net_yi > 0:    fund_score += 2
                break
        scores["资金/龙虎榜"] = max(0, min(fund_score, 15))

        # ---- Total ----
        total = sum(scores.values())  # max = 100

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

        candidates.append({
            "code": code, "name": name,
            "streak": streak_n,
            "first_seal": first_seal,
            "turnover": turnover,
            "amount": amount,
            "seal_amt": seal_amt,
            "float_mv": float_mv,
            "break_n": break_n,
            "themes": matched_themes,
            "scores": scores,
            "total_score": round(total, 1),
            "promo_prob": round(total, 1),  # 0-100
            "method": method,
            "stop_loss": stop_loss,
            "auction": auction,
            "is_sector_dragon": any(
                t.get("top_stocks", [{}])[0].get("name", "") == name
                for t in themes
            ),
            "is_shenwei": name == ladder.get("top_streak", {}).get("name", ""),
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
            f"封单{(c['seal_amt']/1e4):.2f}亿 | 板块:{themes_str} | "
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
    """生成完整接力分析报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sep = "\n\n---\n\n"

    lines = [
        "# 🎯 短线接力环境 & 连板晋级分析",
        f"**{ladder_data.get('data_date', '')} 盘后复盘** | 生成: {now}",
        "",
    ]

    # ===== 1. 情绪环境 =====
    lines += [
        sep,
        "## 📊 一、情绪环境评估",
        "",
        f"| 指标 | 数值 | 状态 |",
        f"|------|------|------|",
        f"| 赚钱效应 | {sentiment['sentiment']:.0f}/100 | {sentiment['cycle']} |",
        f"| 涨跌结构 | {sentiment.get('limit_up',0)}涨停 / {sentiment.get('limit_down',0)}跌停 | — |",
        f"| 炸板率 | {sentiment['fry_rate']:.1f}% | {'⚠️偏高' if sentiment['fry_rate']>30 else '✅正常'} |",
        f"| 连板晋级率 | {sentiment['promo_rate']:.0f}% | {'✅健康' if sentiment['promo_rate']>25 else '⚠️偏低'} |",
        f"| 最高连板 | {sentiment['max_streak']}板 | {'🔥空间打开' if sentiment['max_streak']>=5 else '📈空间有限'} |",
        f"| **仓位建议** | **{sentiment['position']}** | **{sentiment['safety']}** |",
        "",
        f"> {sentiment['cycle_detail']}",
    ]

    # ===== 2. 连板梯队 =====
    lines += [
        sep,
        "## 🪜 二、连板梯队梳理",
        "",
    ]

    # 身位龙
    sd = ladder_data.get("shenwei_dragon", {})
    if sd and sd.get("name"):
        lines += [
            f"### 👑 身位龙（市场总龙头）",
            f"**{sd.get('name', '')}** — {sd.get('boards', '?')}连板 | "
            f"板块: {sd.get('industry', '')} | "
            f"辨识度: ⭐⭐⭐⭐⭐",
            "",
            "> 全市场最高连板，情绪风向标，不一定要买但必须盯。",
            "",
        ]

    # 板块龙
    sdragons = ladder_data.get("sector_dragons", [])
    if sdragons:
        lines += ["### 🏆 板块龙头", ""]
        lines += ["| 主线题材 | 板块龙 | 涨停家数 | 龙头定位 |", "|------|------|:---:|------|"]
        for ts in ladder_data.get("theme_stats", [])[:8]:
            name = ts["name"]
            sd_match = [d for d in sdragons if d["theme"] == name]
            leader = sd_match[0]["leader"] if sd_match else "—"
            zt_n = ts["zt_count"]
            role = "身位龙" if leader == sd.get("name", "") else ("板块龙头" if leader != "—" else "—")
            lines.append(f"| {name} | {leader} | {zt_n} | {role} |")
        lines.append("")

    # 梯队完整度
    levels = ladder_data.get("levels", {})
    if levels:
        lines += ["### 📋 涨停板梯队", ""]
        sorted_keys = sorted(levels.keys(), key=lambda x: int(x) if x.isdigit() else 99)
        for lv in sorted_keys:
            stocks = levels[lv]
            stock_strs = []
            for s in stocks[:12]:
                sn = s.get("name", "")
                sc = s.get("code", "")
                si = s.get("industry", "")
                stock_strs.append(f"{sn}({sc})")
            stock_line = " → ".join(stock_strs) if stock_strs else "—"
            if lv == "1":
                lines.append(f"**首板** ({len(stocks)}只): {stock_line}")
            else:
                lines.append(f"**{lv}板** ({len(stocks)}只): {stock_line}")
            lines.append("")

    # ===== 3. 晋级筛选 =====
    lines += [
        sep,
        "## 🎯 三、连板晋级候选（按概率排序）",
        "",
    ]

    high_prob = [c for c in candidates if c["promo_prob"] >= 55]
    mid_prob = [c for c in candidates if 40 <= c["promo_prob"] < 55]
    low_prob = [c for c in candidates if c["promo_prob"] < 40]

    if high_prob:
        lines += ["### 🔥 高概率晋级 (≥55%)", ""]
        lines += [
            "| # | 股票 | 连板 | 封板 | 换手 | 板块支撑 | 封板质量 | 筹码 | 辨识度 | 资金 | 概率 | 介入 | 止损 |",
            "|---|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|------|------|",
        ]
        for i, c in enumerate(high_prob[:15]):
            s = c["scores"]
            lines.append(
                f"| {i+1} | **{c['name']}**({c['code']}) | {c['streak']}板 | "
                f"{c['first_seal']} | {c['turnover']:.1f}% | "
                f"{s['板块支撑']} | {s['封板质量']} | {s['筹码健康度']} | "
                f"{s['辨识度/梯队']} | {s['资金/龙虎榜']} | "
                f"**{c['promo_prob']:.0f}%** | {c['method']} | {c['stop_loss']} |"
            )

    if mid_prob:
        lines += ["", "### 🟡 中等概率晋级 (40-55%)", ""]
        lines += [
            "| # | 股票 | 连板 | 封板 | 换手 | 概率 | 竞价标准 | 介入 | 止损 |",
            "|---|------|:---:|:---:|:---:|:---:|------|------|------|",
        ]
        for i, c in enumerate(mid_prob[:15]):
            lines.append(
                f"| {i+1} | {c['name']}({c['code']}) | {c['streak']}板 | "
                f"{c['first_seal']} | {c['turnover']:.1f}% | "
                f"{c['promo_prob']:.0f}% | {c['auction']} | {c['method']} | {c['stop_loss']} |"
            )

    if not high_prob and not mid_prob:
        lines.append("> 今日连板股较少，无高概率晋级标的。建议等待下一个情绪周期。")

    # ===== 4. 每日精选 =====
    lines += [
        sep,
        "## ⭐ 四、每日精选（Top 5 详细分析）",
        "",
    ]
    for i, c in enumerate(candidates[:5]):
        role_tags = []
        if c["is_shenwei"]:
            role_tags.append("👑身位龙")
        if c["is_sector_dragon"]:
            role_tags.append("🏆板块龙")
        if c["streak"] >= 3:
            role_tags.append(f"📈{c['streak']}板高标")
        if c["streak"] == 2:
            role_tags.append("🔄补涨龙")
        role_str = " ".join(role_tags) if role_tags else "连板股"

        s = c["scores"]
        themes_str = "/".join(c["themes"][:3]) if c["themes"] else "无主线"

        lines += [
            f"### {i+1}. {role_str} — **{c['name']}**({c['code']})",
            "",
            f"| 维度 | 详情 |",
            f"|------|------|",
            f"| 连板/封板 | {c['streak']}连板，首封{c['first_seal']}，{'未开板' if c['break_n']==0 else '开板' + str(c['break_n']) + '次'} |",
            f"| 换手/量能 | 换手{c['turnover']:.1f}%，{'✅健康' if 3<=c['turnover']<=15 else '⚠️关注'} |",
            f"| **板块支撑** | {themes_str} — 得分{s['板块支撑']}/25 |",
            f"| **封板质量** | 得分{s['封板质量']}/25 —— {'封单强劲，早盘秒封' if s['封板质量']>=20 else '封板质量中等' if s['封板质量']>=12 else '封板偏弱，谨慎'} |",
            f"| **筹码健康度** | 得分{s['筹码健康度']}/20 —— 流通市值{c['float_mv']:.0f}亿 |",
            f"| **辨识度/梯队** | 得分{s['辨识度/梯队']}/15 —— {role_str} |",
            f"| **资金/龙虎榜** | 得分{s['资金/龙虎榜']}/15 |",
            f"| **晋级概率** | **{c['promo_prob']:.0f}%** |",
            f"| **竞价标准** | {c['auction']} |",
            f"| **介入方式** | {c['method']} |",
            f"| **止损位** | {c['stop_loss']} |",
            f"| **核心逻辑** | {themes_str}主线共振 + {'身位优势' if c['is_shenwei'] else '板块龙头' if c['is_sector_dragon'] else '补涨潜力'} + {'封板质量优秀' if s['封板质量']>=20 else '封板质量尚可'} |",
            f"| **风险点** | {'明日面临' + str(c['streak']+1) + '板考验，需确认竞价强度' if c['streak']>=3 else '板块能否持续发酵是核心变量'} |",
            "",
        ]

    # ===== 5. LLM分析 =====
    if llm_text:
        lines += [
            sep,
            "## 🧠 五、AI综合研判",
            "",
            llm_text,
        ]

    # ===== 6. 操作纪律 =====
    lines += [
        sep,
        "## 📋 六、明日操作纪律",
        "",
        "### 竞价观察清单",
        "- ✅ 高概率标的竞价是否高开在标准区间内",
        "- ✅ 竞价量能是否达标（>昨日成交量10%）",
        "- ✅ 板块其他涨停股竞价表现（判断板块持续性）",
        "- ✅ 市场整体竞价情绪（涨跌比、炸板率预期）",
        "",
        "### 放弃信号（任一触发即放弃接力）",
        "- ❌ 竞价大幅低开（>3%）或高开后快速跳水",
        "- ❌ 竞价量能严重萎缩（<昨日5%）",
        "- ❌ 开盘5分钟内未封板或开板超过2次",
        "- ❌ 板块龙头集体走弱",
        "- ❌ 大盘低开超过1%且无反弹迹象",
        "",
        "### 仓位纪律",
        f"- 📊 当前仓位建议: **{sentiment['position']}**",
        "- 🚫 单票上限不超过总仓位20%",
        "- 🛑 日内止损无条件执行，不扛单",
        "- 📝 盘后复盘每笔交易，记录买入理由与实际走势偏差",
    ]

    # ===== 免责 =====
    lines += [
        sep,
        "## ⚠️ 免责声明",
        "",
        "> 本报告基于公开市场数据和AI量化模型自动生成，",
        "> **所有分析结论仅供参考，不构成任何投资建议。**",
        "> 短线接力风险极高，请结合自身风险承受能力独立判断。",
        "> 历史表现不代表未来收益，市场有风险，投资需谨慎。",
        "",
        f"*Generated by DSA Relay Analyzer v3 @ {now}*",
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
    print(f"最高{ladder_data['max_streak']}板, {len(ladder_data.get('levels',{}))}级梯队")

    # 4. Promotion prediction
    print("[3/4] 晋级预测...", end=" ")
    candidates = predict_promotion(zt_pool, snapshot)
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
