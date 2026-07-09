# -*- coding: utf-8 -*-
"""
===================================
市场主线 & 涨停板深度扫描模块
===================================

功能：
1. 识别今日市场主线（领涨板块）及龙头股
2. 板块/个股资金流向排名
3. 涨停板深度分析（封板强度、连板潜力）
4. 缩量洗筹形态检测
5. LLM 综合研判，生成可操作建议

使用方式：
    python market_scanner.py                    # 运行分析并打印报告
    python market_scanner.py --send-email       # 分析并发送邮件
    python market_scanner.py --debug            # 调试模式
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# ---- Path setup: ensure we can import from the project root ----
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---- Logger ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("market_scanner")


# ============================================================
# 1. DATA FETCHING — AkShare wrappers with fallback & retry
# ============================================================

def _retry(func, max_retries: int = 3, label: str = ""):
    """Simple retry wrapper for flaky AkShare endpoints."""
    import time
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            logger.warning("[%s] Attempt %d/%d failed: %s", label, attempt, max_retries, e)
            if attempt == max_retries:
                raise
            time.sleep(2 * attempt)


def fetch_limit_up_pool(trade_date: Optional[str] = None) -> pd.DataFrame:
    """
    获取涨停板池。

    source: AkShare stock_zt_pool_em (东方财富)
    columns: 代码, 名称, 涨跌幅, 最新价, 涨停价, 成交额, 流通市值,
             总市值, 换手率, 封板资金, 首次封板时间, 最后封板时间,
             炸板次数, 涨停统计(连板数/天数), 所属行业
    """
    import akshare as ak

    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")

    def _fetch():
        df = ak.stock_zt_pool_em(date=trade_date)
        if df is None or df.empty:
            raise ValueError("AkShare returned empty limit-up pool")
        return df

    df = _retry(_fetch, max_retries=3, label=f"limit_up_pool({trade_date})")
    logger.info("涨停板池: %d 只涨停股", len(df))
    return df


def fetch_sector_fund_flow() -> pd.DataFrame:
    """
    获取行业板块资金流向排名。

    source: AkShare stock_sector_fund_flow_rank (东方财富)
    columns: 序号, 名称, 今日涨跌幅, 主力净流入-净额, 主力净流入-净占比,
             超大单净流入-净额, 超大单净流入-净占比, 大单净流入-净额,
             大单净流入-净占比, 中单净流入-净额, 中单净流入-净占比,
             小单净流入-净额, 小单净流入-净占比
    """
    import akshare as ak

    def _fetch():
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流向")
        if df is None or df.empty:
            raise ValueError("AkShare returned empty sector fund flow")
        return df

    df = _retry(_fetch, max_retries=3, label="sector_fund_flow")
    logger.info("板块资金流向: %d 个板块", len(df))
    return df


def fetch_concept_fund_flow() -> pd.DataFrame:
    """
    获取概念板块资金流向排名。
    """
    import akshare as ak

    def _fetch():
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流向")
        if df is None or df.empty:
            raise ValueError("AkShare returned empty concept fund flow")
        return df

    df = _retry(_fetch, max_retries=3, label="concept_fund_flow")
    logger.info("概念板块资金流向: %d 个板块", len(df))
    return df


def fetch_industry_spot() -> pd.DataFrame:
    """
    获取行业板块实时行情（涨跌幅排名）。

    source: AkShare stock_board_industry_name_em
    """
    import akshare as ak

    def _fetch():
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            raise ValueError("AkShare returned empty industry spot")
        return df

    df = _retry(_fetch, max_retries=3, label="industry_spot")
    logger.info("行业板块行情: %d 个板块", len(df))
    return df


def fetch_concept_spot() -> pd.DataFrame:
    """
    获取概念板块实时行情。

    source: AkShare stock_board_concept_name_em
    """
    import akshare as ak

    def _fetch():
        df = ak.stock_board_concept_name_em()
        if df is None or df.empty:
            raise ValueError("AkShare returned empty concept spot")
        return df

    df = _retry(_fetch, max_retries=3, label="concept_spot")
    logger.info("概念板块行情: %d 个板块", len(df))
    return df


def fetch_individual_fund_flow() -> pd.DataFrame:
    """
    获取个股资金流向排名（全市场）。

    source: AkShare stock_individual_fund_flow_rank
    """
    import akshare as ak

    def _fetch():
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
        if df is None or df.empty:
            raise ValueError("AkShare returned empty individual fund flow")
        return df

    df = _retry(_fetch, max_retries=3, label="individual_fund_flow")
    logger.info("个股资金流向: %d 只股票", len(df))
    return df


def fetch_stock_kline(symbol: str, days: int = 10) -> pd.DataFrame:
    """
    获取个股近期日K线数据，用于缩量洗筹检测。

    symbol: 纯数字代码如 '000063'
    """
    import akshare as ak

    # Determine market code
    code = symbol.strip()
    if code.startswith(("0", "3")):
        full_code = f"sz{code}" if code.startswith(("0", "3")) else f"sh{code}"
    else:
        full_code = f"sh{code}"

    def _fetch():
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days + 15)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start_date, end_date=end_date, adjust="qfq"
        )
        if df is None or df.empty:
            raise ValueError(f"No kline data for {code}")
        return df.tail(days)

    df = _retry(_fetch, max_retries=2, label=f"kline({code})")
    return df


# ============================================================
# 2. ANALYSIS ENGINE
# ============================================================

def _safe_float(val, default: float = 0.0) -> float:
    """Safely parse a value to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default: int = 0) -> int:
    """Safely parse a value to int."""
    if val is None:
        return default
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return default


def _parse_seal_time(value) -> str:
    """Normalize封板时间 to a readable string."""
    if value is None or pd.isna(value):
        return "未知"
    s = str(value).strip()
    if s in ("", "-", "--"):
        return "未知"
    return s


def _parse_zt_streak(raw) -> str:
    """
    Parse the 涨停统计 column to get consecutive board count.
    常见格式: "2/2" (2天2板), "1/1" (首板)
    """
    if raw is None or pd.isna(raw):
        return "首板"
    s = str(raw).strip()
    if "/" in s:
        parts = s.split("/")
        return f"{parts[0]}天{parts[1]}板" if len(parts) == 2 else s
    return s


def analyze_limit_up_pool(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    深度分析涨停板池，提取关键指标。
    """
    results = []
    for _, row in df.iterrows():
        seal_amt = _safe_float(row.get("封板资金", 0))  # 封板资金（万元）
        turnover = _safe_float(row.get("换手率", 0))     # 换手率 %
        amount = _safe_float(row.get("成交额", 0))        # 成交额
        first_seal = _parse_seal_time(row.get("首次封板时间"))
        last_seal = _parse_seal_time(row.get("最后封板时间"))
        break_count = _safe_int(row.get("炸板次数", 0))
        zt_streak = _parse_zt_streak(row.get("涨停统计"))
        change_pct = _safe_float(row.get("涨跌幅", 0))
        float_mv = _safe_float(row.get("流通市值", 0))

        # ---- Scoring logic ----
        score = 0
        flags = []

        # 1. 封板强度 (封板资金 / 成交额)
        if amount > 0 and seal_amt > 0:
            seal_ratio = (seal_amt * 10000) / (amount * 10000)  # both in万元 → ratio
            if seal_ratio > 0.5:
                score += 20
                flags.append("🔥封板极强")
            elif seal_ratio > 0.3:
                score += 12
                flags.append("✅封板较强")
            elif seal_ratio > 0.1:
                score += 6
                flags.append("⚡封板一般")
            else:
                flags.append("⚠️封单薄弱")

        # 2. 封板时间越早越好
        if first_seal != "未知":
            try:
                h, m, s = map(int, first_seal.replace(":", ":").split(":")[:2])
                seal_minutes = h * 60 + m
                if seal_minutes <= 31:  # 9:30-10:01 秒板
                    score += 25
                    flags.append("🚀秒板")
                elif seal_minutes <= 61:  # 10:01-11:01
                    score += 18
                    flags.append("⏰早盘封板")
                elif seal_minutes <= 121:  # 11:01-13:01
                    score += 10
                    flags.append("🕐午间封板")
                else:
                    score += 3
                    flags.append("🌙尾盘封板")
            except (ValueError, AttributeError):
                pass

        # 3. 炸板扣分
        if break_count > 2:
            score -= 15
            flags.append(f"💣炸板{break_count}次")
        elif break_count > 0:
            score -= 5 * break_count
            flags.append(f"🔧炸板{break_count}次")

        # 4. 换手率合理性 (太低=无量一字板难参与, 太高=分歧大)
        if 3 <= turnover <= 15:
            score += 10
            flags.append("📊换手健康")
        elif 15 < turnover <= 25:
            score += 5
            flags.append("⚡换手偏高")
        elif turnover > 25:
            score -= 3
            flags.append("⚠️换手过高")
        else:
            score += 3
            flags.append("🔒无量一字")

        # 5. 流通市值 (小盘更容易连板)
        if float_mv < 50:
            score += 8
            flags.append("💰小盘")
        elif float_mv < 100:
            score += 5
            flags.append("💰中盘")
        elif float_mv > 500:
            score -= 3
            flags.append("🏢大盘股")

        # ---------- 连板潜力评级 ----------
        if score >= 55:
            grade = "⭐A级-高连板潜力"
        elif score >= 40:
            grade = "⭐B级-中等潜力"
        elif score >= 25:
            grade = "⭐C级-关注观察"
        else:
            grade = "⭐D级-谨慎参与"

        results.append({
            "code": str(row.get("代码", "")).strip(),
            "name": str(row.get("名称", "")).strip(),
            "change_pct": change_pct,
            "price": _safe_float(row.get("最新价")),
            "limit_price": _safe_float(row.get("涨停价")),
            "amount": amount,
            "float_mv": float_mv,
            "turnover": turnover,
            "seal_amount": seal_amt,
            "first_seal": first_seal,
            "last_seal": last_seal,
            "break_count": break_count,
            "streak": zt_streak,
            "industry": str(row.get("所属行业", "")).strip(),
            "score": score,
            "grade": grade,
            "flags": flags,
        })

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def detect_volume_washout(
    limit_up_stocks: List[Dict[str, Any]],
    prev_limit_up_codes: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    检测「昨天涨停 + 今天缩量调整」的洗筹形态。

    策略：
    1. 找出昨天涨停（或前天涨停）的股票
    2. 检查今天是否缩量（成交量 < 昨日 60%）
    3. 检查今天K线是否收小阴/小阳/十字星（振幅 < 5%）
    """
    washout_candidates = []
    today = datetime.now().strftime("%Y-%m-%d")

    for stock in limit_up_stocks:
        code = stock["code"]
        try:
            df = fetch_stock_kline(code, days=5)
            if len(df) < 3:
                continue

            # Latest 3 rows
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            prev2 = df.iloc[-3]

            today_vol = _safe_float(latest.get("成交量", 0))
            prev_vol = _safe_float(prev.get("成交量", 0))
            prev2_vol = _safe_float(prev2.get("成交量", 0))

            today_pct = _safe_float(latest.get("涨跌幅", 0))
            prev_pct = _safe_float(prev.get("涨跌幅", 0))
            prev2_pct = _safe_float(prev2.get("涨跌幅", 0))

            today_open = _safe_float(latest.get("开盘", 0))
            today_close = _safe_float(latest.get("收盘", 0))
            today_high = _safe_float(latest.get("最高", 0))
            today_low = _safe_float(latest.get("最低", 0))

            # Amplitude
            amplitude = (today_high - today_low) / today_open * 100 if today_open > 0 else 0

            # Condition 1: Yesterday or day before was limit-up (>= 9.5%)
            prev_was_zt = prev_pct >= 9.5 or prev2_pct >= 9.5

            # Condition 2: Today volume contracted significantly vs yesterday
            if prev_vol > 0 and today_vol > 0:
                vol_ratio = today_vol / prev_vol
                is_shrinking = vol_ratio < 0.65
            else:
                vol_ratio = 1.0
                is_shrinking = False

            # Condition 3: Today is small range day
            is_small_range = abs(today_pct) < 5.0 and amplitude < 6.0

            if prev_was_zt and is_shrinking and is_small_range:
                washout_type = (
                    "缩量洗筹" if vol_ratio < 0.4 else
                    "缩量调整" if vol_ratio < 0.55 else
                    "缩量整理"
                )
                washout_candidates.append({
                    **stock,
                    "vol_ratio": round(vol_ratio, 2),
                    "today_pct": round(today_pct, 2),
                    "prev_pct": round(prev_pct, 2),
                    "amplitude": round(amplitude, 2),
                    "washout_type": washout_type,
                    "washout_score": (
                        30 if vol_ratio < 0.35 else
                        20 if vol_ratio < 0.5 else 10
                    ),
                })

        except Exception as e:
            logger.debug("Washout check failed for %s: %s", code, e)
            continue

    washout_candidates.sort(key=lambda x: x.get("washout_score", 0), reverse=True)
    return washout_candidates


# ============================================================
# 3. REPORT GENERATION
# ============================================================

# Updated with Anspire API key for LLM calls
_ANSPIRE_BASE = "https://open-gateway.anspire.cn/v6"

def _llm_analyze(prompt: str, api_key: str, model: str = "Doubao-Seed-2.0-lite") -> str:
    """Call Anspire LLM for analysis."""
    import requests

    resp = requests.post(
        f"{_ANSPIRE_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是A股量化分析专家，回答简洁专业，用Markdown格式。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 3000,
        },
        timeout=120,
    )

    if resp.status_code != 200:
        logger.error("LLM call failed: %s", resp.text[:200])
        return f"[LLM调用失败: {resp.status_code}]"

    data = resp.json()
    return data["choices"][0]["message"]["content"]


def generate_llm_analysis(
    top_sectors: pd.DataFrame,
    top_concepts: pd.DataFrame,
    limit_up_analysis: List[Dict],
    top_inflow_stocks: pd.DataFrame,
    washout_stocks: List[Dict],
    api_key: str,
) -> Dict[str, str]:
    """
    Use LLM to generate comprehensive market analysis.
    Returns a dict with: main_theme, dragon_heads, limit_up_forecast, washout_analysis, summary
    """
    # ---- Build context for LLM ----

    # Top 10 sectors
    sector_text = "\n".join([
        f"  {i+1}. {row.get('名称', '')} 涨跌幅{row.get('涨跌幅', 0):+.2f}% "
        f"主力净流入{_safe_float(row.get('主力净流入-净额', 0))/1e8:.2f}亿"
        for i, (_, row) in enumerate(top_sectors.head(10).iterrows())
    ])

    concept_text = "\n".join([
        f"  {i+1}. {row.get('名称', '')} 涨跌幅{_safe_float(row.get('涨跌幅', 0)):+.2f}% "
        f"主力净流入{_safe_float(row.get('主力净流入-净额', 0))/1e8:.2f}亿"
        for i, (_, row) in enumerate(top_concepts.head(15).iterrows())
    ])

    zt_text = "\n".join([
        f"  {i+1}. {s['name']}({s['code']}) | {s['streak']} | "
        f"封板{s['first_seal']} | 换手{s['turnover']:.1f}% | "
        f"{s['grade']} | {' '.join(s['flags'][:3])}"
        for i, s in enumerate(limit_up_analysis[:30])
    ])

    inflow_text = "\n".join([
        f"  {i+1}. {row.get('名称', row.get('代码', ''))} "
        f"主力净流入{_safe_float(row.get('主力净流入-净额', 0))/1e8:.2f}亿 "
        f"涨跌幅{_safe_float(row.get('涨跌幅', 0)):+.2f}%"
        for i, (_, row) in enumerate(top_inflow_stocks.head(15).iterrows())
    ])

    washout_text = "\n".join([
        f"  {i+1}. {s['name']}({s['code']}) | "
        f"{s['washout_type']} | 量比{s['vol_ratio']:.2f} | "
        f"今日{s['today_pct']:+.2f}% | 昨涨停{s['prev_pct']:+.1f}%"
        for i, s in enumerate(washout_stocks[:10])
    ]) or "今日未检测到明显缩量洗筹形态"

    # ---- Individual LLM prompts ----
    prompts = {
        "main_theme": f"""
你是一位顶级A股量化策略分析师。基于以下今日市场数据，请分析：

【领涨行业板块TOP10】
{sector_text}

【领涨概念板块TOP15】
{concept_text}

请回答（用Markdown格式，简洁专业）：
1. **今日市场主线**：今天资金主要围绕哪1-3条主线操作？分析每条主线的核心逻辑和催化因素。
2. **主线持续性与明日展望**：这几条主线明天继续走强的可能性分析，给出你的判断依据。
3. **主线龙头股**：每条主线列出最核心的2-3只龙头标的（名称+代码），并简述选择理由。
""",
        "limit_up_forecast": f"""
你是一位A股涨停板战法专家。基于以下今日涨停板深度数据，请分析：

【涨停板重点标的TOP30】
{zt_text}

请分析（Markdown格式）：
1. **明日连板潜力股**：从以上标的中筛选出5-8只明天最有可能继续连板的股票（代码+名称），给出每条推荐理由（从封板强度、板块共振、市值、股性等角度分析）。
2. **风险提示**：哪些涨停板明天大概率会炸板或高开低走？至少列举3只并说明原因。
3. **参与策略**：明天开盘对涨停板的操作策略建议（竞价观察要点、买点信号等）。
""",
        "washout_analysis": f"""
你是一位擅长K线形态分析的A股技术分析师。以下是今日检测到的缩量洗筹候选标的：

{wasout_text}

请分析（Markdown格式）：
1. **缩量洗筹标的研判**：逐个点评最有潜力的3-5只标的，分析洗筹质量（缩量程度、调整幅度、K线形态）。
2. **明日操作策略**：对这些缩量洗筹标的，明日的买入信号和止损位如何设定？
3. **陷阱警告**：哪些标的外观像洗筹但更可能是出货？至少指出2处疑点。
""",
    }

    results = {}
    for key, prompt in prompts.items():
        logger.info("Calling LLM for: %s", key)
        try:
            results[key] = _llm_analyze(prompt, api_key)
        except Exception as e:
            logger.error("LLM failed for %s: %s", key, e)
            results[key] = f"[分析失败: {e}]"

    return results


def build_report(
    top_sectors: pd.DataFrame,
    top_concepts: pd.DataFrame,
    limit_up_analysis: List[Dict],
    top_inflow_stocks: pd.DataFrame,
    top_inflow_sectors: pd.DataFrame,
    washout_stocks: List[Dict],
    llm_results: Optional[Dict[str, str]] = None,
) -> str:
    """Build the final Markdown report."""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    hline = "─" * 60

    lines = [
        f"# 📊 A股市场深度扫描日报",
        f"**生成时间**: {now_str}",
        "",
        "---",
        "",
    ]

    # ===== LLM Analysis Sections =====
    if llm_results and llm_results.get("main_theme"):
        lines += [
            "## 🎯 市场主线分析（AI研判）",
            "",
            llm_results["main_theme"],
            "",
            "---",
            "",
        ]

    # ===== Sector Fund Flow =====
    lines += [
        "## 💰 行业板块资金流向 TOP15",
        "",
        "| 排名 | 板块 | 涨跌幅 | 主力净流入(亿) | 净占比 |",
        "|:---:|------|------:|------:|------:|",
    ]

    for i, (_, row) in enumerate(top_inflow_sectors.head(15).iterrows()):
        name = str(row.get("名称", ""))
        change = _safe_float(row.get("涨跌幅", 0))
        net_inflow = _safe_float(row.get("主力净流入-净额", 0)) / 1e8
        net_ratio = _safe_float(row.get("主力净流入-净占比", 0))
        icon = "🟢" if change > 0 else "🔴"
        lines.append(
            f"| {i+1} | {icon} {name} | {change:+.2f}% | "
            f"{net_inflow:+.2f} | {net_ratio:+.2f}% |"
        )

    lines += ["", "---", ""]

    # ===== Concept sector performance =====
    lines += [
        "## 🔥 概念板块涨幅 TOP15",
        "",
        "| 排名 | 板块 | 涨跌幅 | 主力净流入(亿) |",
        "|:---:|------|------:|------:|",
    ]
    for i, (_, row) in enumerate(top_concepts.head(15).iterrows()):
        name = str(row.get("名称", ""))
        change = _safe_float(row.get("涨跌幅", 0))
        net_inflow = _safe_float(row.get("主力净流入-净额", 0)) / 1e8
        icon = "🟢" if change > 0 else "🔴"
        lines.append(f"| {i+1} | {icon} {name} | {change:+.2f}% | {net_inflow:+.2f} |")

    lines += ["", "---", ""]

    # ===== Individual Stock Fund Flow =====
    lines += [
        "## 📈 个股资金流入 TOP15",
        "",
        "| 排名 | 股票 | 涨跌幅 | 主力净流入(亿) |",
        "|:---:|------|------:|------:|",
    ]
    for i, (_, row) in enumerate(top_inflow_stocks.head(15).iterrows()):
        name = str(row.get("名称", row.get("代码", "")))
        change = _safe_float(row.get("涨跌幅", 0))
        net_inflow = _safe_float(row.get("主力净流入-净额", 0)) / 1e8
        icon = "🟢" if change > 0 else "🔴"
        lines.append(f"| {i+1} | {icon} {name} | {change:+.2f}% | {net_inflow:+.2f} |")

    lines += ["", "---", ""]

    # ===== Limit-Up Deep Analysis =====
    lines += [
        "## 📋 涨停板深度分析",
        "",
        f"**今日涨停: {len(limit_up_analysis)} 只**（仅展示A级+B级高潜力标的）",
        "",
        "| 排名 | 股票 | 连板 | 封板时间 | 换手 | 封单(亿) | 评级 | 关键信号 |",
        "|:---:|------|:---:|:---:|:---:|------:|:---:|------|",
    ]

    shown = 0
    for i, s in enumerate(limit_up_analysis):
        if s["grade"] not in ("⭐A级-高连板潜力", "⭐B级-中等潜力"):
            continue
        shown += 1
        if shown > 30:
            break
        seal_amt = s["seal_amount"] / 1e4 if s["seal_amount"] > 0 else 0  # 万元→亿
        flags_str = " ".join(s["flags"][:3])
        lines.append(
            f"| {i+1} | {s['name']}({s['code']}) | {s['streak']} | "
            f"{s['first_seal']} | {s['turnover']:.1f}% | "
            f"{seal_amt:.2f} | {s['grade'][:6]} | {flags_str} |"
        )

    if shown == 0:
        lines.append("| - | 今日无A/B级连板潜力标的 | - | - | - | - | - | - |")

    lines += ["", "---", ""]

    # ===== LLM: Limit-up forecast =====
    if llm_results and llm_results.get("limit_up_forecast"):
        lines += [
            "## 🔮 明日连板预测（AI研判）",
            "",
            llm_results["limit_up_forecast"],
            "",
            "---",
            "",
        ]

    # ===== Volume Washout Detection =====
    lines += [
        "## 🔍 缩量洗筹检测",
        "",
    ]
    if washout_stocks:
        lines += [
            "| 排名 | 股票 | 类型 | 量比 | 今日涨跌 | 振幅 | 昨涨停 |",
            "|:---:|------|------|:---:|------:|:---:|:---:|",
        ]
        for i, s in enumerate(washout_stocks[:15]):
            lines.append(
                f"| {i+1} | {s['name']}({s['code']}) | {s['washout_type']} | "
                f"{s['vol_ratio']:.2f} | {s['today_pct']:+.2f}% | "
                f"{s['amplitude']:.1f}% | {s['prev_pct']:+.1f}% |"
            )
    else:
        lines.append("> 今日未检测到明显的缩量洗筹形态。")

    lines += ["", "---", ""]

    # ===== LLM: Washout Analysis =====
    if llm_results and llm_results.get("washout_analysis"):
        lines += [
            "## 🧠 缩量洗筹研判（AI分析）",
            "",
            llm_results["washout_analysis"],
            "",
            "---",
            "",
        ]

    # ===== Disclaimer =====
    lines += [
        "## ⚠️ 免责声明",
        "",
        "> 本报告由AI模型自动生成，基于公开市场数据和量化分析模型，",
        "> **不构成任何投资建议**。股市有风险，投资需谨慎。",
        "> 所有分析结论仅供参考，请结合自身风险承受能力独立判断。",
        "",
        f"*Generated by DSA Market Scanner @ {now_str}*",
    ]

    return "\n".join(lines)


# ============================================================
# 4. MAIN ENTRY POINT
# ============================================================

def send_email_report(report_md: str, subject: str = None) -> bool:
    """
    Send report via the project's email sender.
    Falls back to standalone SMTP if project import fails.
    """
    if subject is None:
        subject = f"📊 A股市场深度扫描日报 - {datetime.now().strftime('%Y-%m-%d')}"

    # --- Try project's email sender first ---
    try:
        from src.config import get_config
        from src.notification_sender.email_sender import EmailSender

        config = get_config()
        if config.email_sender and config.email_password:
            sender = EmailSender(config)
            # Use the email sender's internal method
            from email.mime.text import MIMEText
            import smtplib

            msg = MIMEText(report_md, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = config.email_sender
            msg["To"] = config.email_receivers or config.email_sender

            # Determine SMTP server
            domain = config.email_sender.split("@")[-1].lower()
            smtp_configs = {
                "qq.com": ("smtp.qq.com", 465, True),
                "163.com": ("smtp.163.com", 465, True),
                "gmail.com": ("smtp.gmail.com", 587, False),
            }
            smtp_server, smtp_port, use_ssl = smtp_configs.get(
                domain, ("smtp.qq.com", 465, True)
            )

            if use_ssl:
                server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
                server.starttls()

            server.login(config.email_sender, config.email_password)
            server.sendmail(config.email_sender, [config.email_receivers or config.email_sender], msg.as_string())
            server.quit()
            logger.info("✅ 邮件发送成功 → %s", config.email_receivers or config.email_sender)
            return True
        else:
            logger.warning("邮件配置不完整，跳过发送")
            return False
    except Exception as e:
        logger.error("邮件发送失败: %s", e)
        return False


def main():
    parser = argparse.ArgumentParser(description="A股市场主线与涨停板深度扫描")
    parser.add_argument("--send-email", action="store_true", help="发送邮件报告")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    parser.add_argument("--output", type=str, default=None, help="报告输出路径")
    parser.add_argument("--skip-llm", action="store_true", help="跳过LLM分析（仅数据）")
    parser.add_argument("--date", type=str, default=None, help="分析日期 YYYYMMDD")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    trade_date = args.date or datetime.now().strftime("%Y%m%d")

    # ---- Load API key ----
    from dotenv import load_dotenv
    load_dotenv()

    anspire_key = os.getenv("ANSPIRE_API_KEYS", "")
    if not anspire_key and not args.skip_llm:
        logger.warning("ANSPIRE_API_KEYS 未设置，将跳过LLM分析")
        args.skip_llm = True

    print("=" * 60)
    print("  📊 A股市场主线 & 涨停板深度扫描")
    print(f"  日期: {trade_date}")
    print("=" * 60)
    print()

    # ========== Step 1: Fetch Data ==========
    print("[1/7] 获取涨停板池...")
    try:
        zt_df = fetch_limit_up_pool(trade_date)
    except Exception as e:
        logger.error("涨停板数据获取失败: %s", e)
        zt_df = pd.DataFrame()

    print("[2/7] 获取行业板块资金流向...")
    try:
        sector_flow = fetch_sector_fund_flow()
    except Exception as e:
        logger.error("行业板块资金流向获取失败: %s", e)
        sector_flow = pd.DataFrame()

    print("[3/7] 获取概念板块资金流向...")
    try:
        concept_flow = fetch_concept_fund_flow()
    except Exception as e:
        logger.error("概念板块资金流向获取失败: %s", e)
        concept_flow = pd.DataFrame()

    print("[4/7] 获取概念板块行情...")
    try:
        concept_spot = fetch_concept_spot()
    except Exception as e:
        logger.error("概念板块行情获取失败: %s", e)
        concept_spot = pd.DataFrame()

    print("[5/7] 获取个股资金流向...")
    try:
        individual_flow = fetch_individual_fund_flow()
    except Exception as e:
        logger.error("个股资金流向获取失败: %s", e)
        individual_flow = pd.DataFrame()

    # ========== Step 2: Analysis ==========
    print("[6/7] 分析涨停板...")
    limit_up_results = []
    if not zt_df.empty:
        limit_up_results = analyze_limit_up_pool(zt_df)
        print(f"  → 涨停板分析完成: {len(limit_up_results)} 只")
        a_count = sum(1 for s in limit_up_results if "A级" in s["grade"])
        b_count = sum(1 for s in limit_up_results if "B级" in s["grade"])
        print(f"  → A级(高连板潜力): {a_count} | B级(中等潜力): {b_count}")

    print("  检测缩量洗筹形态...")
    washout_stocks = detect_volume_washout(limit_up_results)
    print(f"  → 检测到 {len(washout_stocks)} 只缩量洗筹候选")

    # ========== Step 3: LLM Analysis ==========
    llm_results = None
    if not args.skip_llm and anspire_key and not zt_df.empty and not sector_flow.empty:
        print("[7/7] LLM智能分析中...")
        try:
            # Build compact dataframes for LLM context
            top_sectors = sector_flow.head(10)
            top_concepts = concept_spot.head(15) if not concept_spot.empty else concept_flow.head(15)
            top_inflow = individual_flow.head(15) if not individual_flow.empty else pd.DataFrame()

            llm_results = generate_llm_analysis(
                top_sectors=top_sectors,
                top_concepts=top_concepts,
                limit_up_analysis=limit_up_results,
                top_inflow_stocks=top_inflow,
                washout_stocks=washout_stocks,
                api_key=anspire_key,
            )
            print("  → LLM分析完成: 主线/连板预测/洗筹研判")
        except Exception as e:
            logger.error("LLM分析失败: %s", e)
    else:
        print("[7/7] 跳过LLM分析")

    # ========== Step 4: Build Report ==========
    top_inflow_sectors_ranked = sector_flow.sort_values(
        by=lambda c: pd.to_numeric(sector_flow.get("主力净流入-净额", c), errors="coerce"),
        ascending=False
    ) if not sector_flow.empty else sector_flow

    top_inflow_sectors_ranked = sector_flow.head(15) if not sector_flow.empty else pd.DataFrame()
    top_concepts_filtered = concept_spot.head(15) if not concept_spot.empty else (
        concept_flow.head(15) if not concept_flow.empty else pd.DataFrame()
    )
    top_individual = individual_flow.head(15) if not individual_flow.empty else pd.DataFrame()

    report = build_report(
        top_sectors=top_concepts_filtered,  # concept top for 主线 display
        top_concepts=top_concepts_filtered,
        limit_up_analysis=limit_up_results,
        top_inflow_stocks=top_individual,
        top_inflow_sectors=top_inflow_sectors_ranked,
        washout_stocks=washout_stocks,
        llm_results=llm_results,
    )

    # ========== Step 5: Output ==========
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"\n✅ 报告已保存: {output_path}")
    else:
        output_path = Path(f"reports/market_scan_{trade_date}.md")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"\n✅ 报告已保存: {output_path}")

    if args.send_email and anspire_key:
        print("\n📧 发送邮件报告...")
        send_email_report(report)

    # Print summary to console
    print("\n" + "=" * 60)
    print("📊 扫描完成摘要")
    print("=" * 60)
    print(f"  涨停板: {len(limit_up_results)} 只")
    if limit_up_results:
        a_count = sum(1 for s in limit_up_results if "A级" in s["grade"])
        b_count = sum(1 for s in limit_up_results if "B级" in s["grade"])
        print(f"    A级: {a_count} | B级: {b_count}")
    print(f"  缩量洗筹候选: {len(washout_stocks)} 只")
    print(f"  报告: {output_path}")
    print(f"  邮件: {'已发送' if args.send_email else '未发送(加--send-email)'}")

    return report


if __name__ == "__main__":
    main()
