# -*- coding: utf-8 -*-
"""
===================================
市场主线 & 涨停板深度扫描模块 v2
===================================

功能：
1. 今日涨停板深度分析（封板强度、连板潜力）
2. 行业板块涨跌排名
3. 缩量洗筹形态检测（yfinance数据源）
4. LLM 综合研判

集成方式：追加到主程序日报后面
    python scripts/market_scanner.py --output reports/market_scan.md

使用方式：
    python scripts/market_scanner.py                    # 打印报告到stdout
    python scripts/market_scanner.py --output FILE.md   # 保存到文件
    python scripts/market_scanner.py --skip-llm         # 跳过AI分析
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("market_scanner")


# ============================================================
# 0. UTILITY
# ============================================================

def _sf(val, default=0.0):
    """safe float"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return float(str(val).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return default


def _si(val, default=0):
    """safe int"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return int(float(str(val).replace(",", "")))
    except (ValueError, TypeError):
        return default


def _retry(func, max_retries=3, label=""):
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            logger.warning("[%s] %d/%d: %s", label, attempt, max_retries, str(e)[:80])
            if attempt == max_retries:
                raise
            time.sleep(2 * attempt)


# ============================================================
# 1. DATA FETCHING
# ============================================================

def fetch_limit_up_pool(trade_date: str = None) -> pd.DataFrame:
    """获取涨停板池 (AkShare)"""
    import akshare as ak
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y%m%d")
    df = _retry(lambda: ak.stock_zt_pool_em(date=trade_date), 3, "涨停板池")
    logger.info("涨停板: %d 只", len(df) if df is not None else 0)
    return df if df is not None else pd.DataFrame()


def fetch_industry_board() -> pd.DataFrame:
    """获取行业板块涨跌幅排名 (AkShare)"""
    import akshare as ak
    try:
        df = _retry(lambda: ak.stock_board_industry_name_em(), 2, "行业板块")
        logger.info("行业板块: %d 个", len(df) if df is not None else 0)
        return df if df is not None else pd.DataFrame()
    except Exception:
        logger.warning("行业板块获取失败，尝试备用接口")
        try:
            df = _retry(lambda: ak.stock_board_industry_summary_em(), 2, "行业板块v2")
            return df if df is not None else pd.DataFrame()
        except Exception:
            return pd.DataFrame()


def fetch_concept_board() -> pd.DataFrame:
    """获取概念板块涨跌幅排名 (AkShare)"""
    import akshare as ak
    try:
        df = _retry(lambda: ak.stock_board_concept_name_em(), 2, "概念板块")
        logger.info("概念板块: %d 个", len(df) if df is not None else 0)
        return df if df is not None else pd.DataFrame()
    except Exception:
        logger.warning("概念板块获取失败")
        return pd.DataFrame()


def fetch_stock_kline(symbol: str, days: int = 5) -> pd.DataFrame:
    """获取个股日K线 (yfinance，GitHub Actions稳定)"""
    try:
        import yfinance as yf
    except ImportError:
        logger.debug("yfinance 未安装")
        raise RuntimeError("yfinance not available")

    code = symbol.strip()
    if code.startswith(("6", "9")):
        ticker = f"{code}.SS"
    elif code.startswith(("0", "3", "4")):
        ticker = f"{code}.SZ"
    else:
        ticker = f"{code}.SS"

    for attempt in range(2):
        try:
            t = yf.Ticker(ticker)
            df = t.history(period=f"{days + 10}d")
            if df is not None and not df.empty and len(df) >= 3:
                df = df.reset_index()
                df = df.rename(columns={
                    "Date": "日期", "Open": "开盘", "High": "最高",
                    "Low": "最低", "Close": "收盘", "Volume": "成交量",
                })
                df["涨跌幅"] = df["收盘"].pct_change() * 100
                logger.debug("K线 %s OK via yfinance", code)
                return df.tail(days)
        except Exception:
            if attempt < 1:
                time.sleep(1)

    # Fallback to akshare
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days + 15)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
        if df is not None and not df.empty:
            logger.debug("K线 %s OK via akshare", code)
            return df.tail(days)
    except Exception:
        pass

    raise RuntimeError(f"Kline failed for {code}")


# ============================================================
# 2. ANALYSIS
# ============================================================

def _parse_seal_time(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "未知"
    s = str(val).strip()
    return s if s and s != "-" and s != "--" else "未知"


def analyze_limit_up_pool(df: pd.DataFrame) -> List[Dict]:
    """深度分析涨停板"""
    results = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        name = str(row.get("名称", "")).strip()
        change_pct = _sf(row.get("涨跌幅"))
        turnover = _sf(row.get("换手率"))
        amount = _sf(row.get("成交额"))       # 万元
        seal_amt = _sf(row.get("封板资金"))   # 万元
        float_mv = _sf(row.get("流通市值"))   # 亿元
        first_seal = _parse_seal_time(row.get("首次封板时间"))
        break_count = _si(row.get("炸板次数"))
        zt_streak_raw = row.get("涨停统计", "")
        industry = str(row.get("所属行业", "")).strip()

        # Parse streak
        streak_str = str(zt_streak_raw).strip() if zt_streak_raw is not None else "首板"
        if "/" in streak_str:
            parts = streak_str.split("/")
            streak_str = f"{parts[0]}天{parts[1]}板"

        # ---- Score ----
        score = 0
        tags = []

        # Seal strength
        if amount > 0 and seal_amt > 0:
            ratio = seal_amt / amount
            if ratio > 0.5:   score += 20; tags.append("🔥封板极强")
            elif ratio > 0.3: score += 12; tags.append("✅封板较强")
            elif ratio > 0.1: score += 6;  tags.append("⚡封板一般")
            else:                          tags.append("⚠️封单弱")

        # Seal time
        if ":" in first_seal:
            try:
                parts = first_seal.replace("：", ":").split(":")
                h, m = int(parts[0]), int(parts[1])
                mins = h * 60 + m
                if mins <= 31:       score += 25; tags.append("🚀秒板")
                elif mins <= 61:     score += 18; tags.append("⏰早盘封")
                elif mins <= 121:    score += 10; tags.append("午间封板")
                else:                score += 3;  tags.append("尾盘封板")
            except Exception:
                pass

        # Break count
        if break_count > 2:   score -= 15; tags.append(f"💣炸{break_count}次")
        elif break_count > 0: score -= 5 * break_count; tags.append(f"炸{break_count}次")

        # Turnover
        if 3 <= turnover <= 15:    score += 10; tags.append("换手佳")
        elif 15 < turnover <= 25:  score += 5;  tags.append("换手偏高")
        elif turnover > 25:        score -= 3;  tags.append("换手过高")
        else:                      score += 3;  tags.append("无量一字")

        # Market cap
        if float_mv < 50:        score += 8;  tags.append("小盘")
        elif float_mv < 100:     score += 5;  tags.append("中盘")
        elif float_mv > 500:     score -= 3;  tags.append("大盘")

        # Grade
        if score >= 55:      grade = "A级"
        elif score >= 40:    grade = "B级"
        elif score >= 25:    grade = "C级"
        else:                grade = "D级"

        results.append({
            "code": code, "name": name, "change": change_pct,
            "turnover": turnover, "amount": amount, "seal_amt": seal_amt,
            "float_mv": float_mv, "first_seal": first_seal,
            "breaks": break_count, "streak": streak_str,
            "industry": industry, "score": score, "grade": grade, "tags": tags,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def detect_washout(zt_list: List[Dict]) -> List[Dict]:
    """检测缩量洗筹形态"""
    candidates = []
    for s in zt_list:
        code = s["code"]
        try:
            df = fetch_stock_kline(code, 5)
            if len(df) < 3:
                continue

            tday = df.iloc[-1];   yday = df.iloc[-2];   dby = df.iloc[-3]

            t_vol = _sf(tday.get("成交量")); y_vol = _sf(yday.get("成交量"))
            t_pct = _sf(tday.get("涨跌幅")); y_pct = _sf(yday.get("涨跌幅"))
            t_open = _sf(tday.get("开盘"));  t_high = _sf(tday.get("最高"))
            t_low = _sf(tday.get("最低"));   t_close = _sf(tday.get("收盘"))

            if t_open <= 0:
                continue

            ampl = (t_high - t_low) / t_open * 100
            y_was_zt = y_pct >= 9.5

            if y_vol > 0 and t_vol > 0 and y_was_zt and abs(t_pct) < 5 and ampl < 6:
                ratio = t_vol / y_vol
                if ratio < 0.65:
                    wtype = "强缩量洗筹" if ratio < 0.4 else ("缩量洗筹" if ratio < 0.55 else "缩量整理")
                    candidates.append({
                        **s, "vol_ratio": round(ratio, 2),
                        "t_pct": round(t_pct, 2), "y_pct": round(y_pct, 2),
                        "ampl": round(ampl, 2), "washout_type": wtype,
                        "w_score": 30 if ratio < 0.35 else (20 if ratio < 0.5 else 10),
                    })
        except Exception:
            continue

    candidates.sort(key=lambda x: x.get("w_score", 0), reverse=True)
    logger.info("缩量洗筹候选: %d 只", len(candidates))
    return candidates


# ============================================================
# 3. LLM ANALYSIS
# ============================================================

def llm_analyze(zt_data: List[Dict], industry_df: pd.DataFrame,
                concept_df: pd.DataFrame, api_key: str) -> Optional[str]:
    """LLM综合研判"""
    if not api_key or not zt_data:
        return None

    zt_lines = []
    for i, s in enumerate(zt_data[:25]):
        tags = " ".join(s["tags"][:3])
        zt_lines.append(
            f"{i+1}. {s['name']}({s['code']}) | {s['streak']} | "
            f"封{s['first_seal']} | 换手{s['turnover']:.1f}% | "
            f"封单{(s['seal_amt']/1e4):.2f}亿 | {s['grade']} | {tags}"
        )

    ind_lines = []
    if not industry_df.empty:
        for _, r in industry_df.head(10).iterrows():
            nm = str(r.get("板块名称", r.get("名称", "")))
            ch = _sf(r.get("板块涨跌幅", r.get("涨跌幅", 0)))
            ind_lines.append(f"  {nm}: {ch:+.2f}%")

    con_lines = []
    if not concept_df.empty:
        for _, r in concept_df.head(10).iterrows():
            nm = str(r.get("板块名称", r.get("名称", "")))
            ch = _sf(r.get("板块涨跌幅", r.get("涨跌幅", 0)))
            con_lines.append(f"  {nm}: {ch:+.2f}%")

    prompt = f"""你是A股短线交易专家。基于今日市场数据给出简洁分析。

【涨停板TOP25】
{chr(10).join(zt_lines)}

【行业板块TOP10】
{chr(10).join(ind_lines) if ind_lines else "数据缺失"}

【概念板块TOP10】
{chr(10).join(con_lines) if con_lines else "数据缺失"}

请用Markdown格式简要分析（每项200字以内）：
1. **今日主线题材**：资金围绕哪2-3个方向？列出核心龙头。
2. **明日连板预测**：从涨停板中挑3-5只最可能连板的（代码+名称+理由），再挑2只风险最高的。
3. **操作建议**：明天早盘竞价关注要点。"""

    import requests
    try:
        resp = requests.post(
            "https://open-gateway.anspire.cn/v6/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "Doubao-Seed-2.0-lite", "messages": [
                {"role": "system", "content": "你是A股短线分析专家。回答简洁专业，Markdown格式。"},
                {"role": "user", "content": prompt},
            ], "temperature": 0.3, "max_tokens": 2500},
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        logger.error("LLM返回 %d", resp.status_code)
        return None
    except Exception as e:
        logger.error("LLM调用失败: %s", e)
        return None


# ============================================================
# 4. REPORT BUILDING
# ============================================================

def build_report(zt_list: List[Dict], industry_df: pd.DataFrame,
                 concept_df: pd.DataFrame, washout_list: List[Dict],
                 llm_text: Optional[str] = None) -> str:
    """生成Markdown报告"""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sep = "\n\n---\n\n"

    lines = [
        "## 📊 市场深度扫描",
        f"*扫描时间: {now}*",
        "",
    ]

    # ---- LLM analysis ----
    if llm_text:
        lines += [sep, "### 🧠 AI综合研判", "", llm_text]

    # ---- Industry sectors ----
    if not industry_df.empty:
        lines += [sep, f"### 🏭 行业板块涨跌 TOP12", ""]
        lines += ["| # | 板块 | 涨跌幅 | 领涨股 |", "|---|------|:---:|------|"]
        for i, (_, r) in enumerate(industry_df.head(12).iterrows()):
            nm = str(r.get("板块名称", r.get("名称", "")))
            ch = _sf(r.get("板块涨跌幅", r.get("涨跌幅", 0)))
            lead = str(r.get("领涨股票名称", r.get("领涨股", ""))) if "领涨股票名称" in r.index or "领涨股" in r.index else ""
            icon = "🔴" if ch > 0 else "🟢"
            lines.append(f"| {i+1} | {icon} {nm} | {ch:+.2f}% | {lead} |")

    # ---- Concept sectors ----
    if not concept_df.empty:
        lines += [sep, f"### 💡 概念板块涨跌 TOP12", ""]
        lines += ["| # | 板块 | 涨跌幅 |", "|---|------|:---:|"]
        for i, (_, r) in enumerate(concept_df.head(12).iterrows()):
            nm = str(r.get("板块名称", r.get("名称", "")))
            ch = _sf(r.get("板块涨跌幅", r.get("涨跌幅", 0)))
            icon = "🔴" if ch > 0 else "🟢"
            lines.append(f"| {i+1} | {icon} {nm} | {ch:+.2f}% |")

    # ---- Limit-up deep analysis ----
    lines += [sep, f"### 📈 涨停板深度分析", ""]
    lines += [f"**涨停总数: {len(zt_list)} 只**（按连板潜力排序）", ""]

    # A & B grade table
    high = [s for s in zt_list if s["grade"] in ("A级", "B级")]
    if high:
        lines += ["#### ⭐ A/B级 — 高潜力连板标的", ""]
        lines += [
            "| # | 股票 | 连板 | 封板 | 换手 | 封单(亿) | 市值 | 评级 | 信号 |",
            "|---|------|:---:|:---:|:---:|:---:|:---:|:---:|------|",
        ]
        for i, s in enumerate(high[:20]):
            seal_e = s["seal_amt"] / 1e4  # 万元→亿
            lines.append(
                f"| {i+1} | {s['name']}({s['code']}) | {s['streak']} | "
                f"{s['first_seal']} | {s['turnover']:.1f}% | "
                f"{seal_e:.2f} | {s['float_mv']:.0f}亿 | {s['grade']} | "
                f"{' '.join(s['tags'][:3])} |"
            )
    else:
        lines.append("> 今日无A/B级标的（无涨停或全部中低评级）")

    # C & D grade (collapsed summary)
    low = [s for s in zt_list if s["grade"] in ("C级", "D级")]
    if low:
        lines += ["", "#### C/D级 — 其余涨停板", ""]
        lines += [
            "| # | 股票 | 连板 | 封板 | 换手 | 评级 |",
            "|---|------|:---:|:---:|:---:|:---:|",
        ]
        for i, s in enumerate(low[:30]):
            lines.append(
                f"| {i+1} | {s['name']}({s['code']}) | {s['streak']} | "
                f"{s['first_seal']} | {s['turnover']:.1f}% | {s['grade']} |"
            )
        if len(low) > 30:
            lines.append(f"| ... | 还有 {len(low)-30} 只 | ... | ... | ... | ... |")

    # ---- Washout detection ----
    lines += [sep, "### 🔍 缩量洗筹检测", ""]
    if washout_list:
        lines += [
            "| # | 股票 | 类型 | 量比 | 今涨跌 | 昨涨停 | 振幅 |",
            "|---|------|------|:---:|:---:|:---:|:---:|",
        ]
        for i, w in enumerate(washout_list[:12]):
            lines.append(
                f"| {i+1} | {w['name']}({w['code']}) | {w['washout_type']} | "
                f"{w['vol_ratio']:.2f} | {w['t_pct']:+.2f}% | "
                f"{w['y_pct']:+.1f}% | {w['ampl']:.1f}% |"
            )
    else:
        lines.append("> 今日未检测到明显的缩量洗筹形态（需昨涨停+今日缩量<65%+小振幅<6%）")

    # ---- Disclaimer ----
    lines += [sep, "### ⚠️ 免责声明", "",
              "> 以上分析基于公开数据和AI模型，**不构成投资建议**。",
              "> 涨停板战法风险极高，请结合自身情况独立判断。",
              "", f"*Generated by DSA Market Scanner v2 @ {now}*", ""]

    return "\n".join(lines)


# ============================================================
# 5. MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="市场主线 & 涨停板扫描 v2")
    parser.add_argument("--output", "-o", type=str, default=None, help="报告输出路径")
    parser.add_argument("--skip-llm", action="store_true", help="跳过AI分析")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD")
    args = parser.parse_args()

    trade_date = args.date or datetime.now().strftime("%Y%m%d")
    today = datetime.now().strftime("%Y-%m-%d")

    # Load env
    from dotenv import load_dotenv
    load_dotenv()
    anspire_key = os.getenv("ANSPIRE_API_KEYS", "").strip()

    print(f"\n{'='*50}")
    print(f"  📊 市场深度扫描 v2  |  {today}")
    print(f"{'='*50}\n")

    # Step 1: Data
    errors = []

    print("[1/5] 涨停板...", end=" ")
    zt_df = pd.DataFrame()
    try:
        zt_df = fetch_limit_up_pool(trade_date)
        print(f"OK ({len(zt_df)}只)")
    except Exception as e:
        errors.append(f"涨停板: {e}")
        print(f"FAIL: {e}")

    print("[2/5] 行业板块...", end=" ")
    ind_df = pd.DataFrame()
    try:
        ind_df = fetch_industry_board()
        print(f"OK ({len(ind_df)}个)")
    except Exception as e:
        errors.append(f"行业: {e}")
        print(f"FAIL: {e}")

    print("[3/5] 概念板块...", end=" ")
    con_df = pd.DataFrame()
    try:
        con_df = fetch_concept_board()
        print(f"OK ({len(con_df)}个)")
    except Exception as e:
        errors.append(f"概念: {e}")
        print(f"SKIP: {e}")

    # Step 2: Analysis
    print("[4/5] 分析...", end=" ")
    zt_list = analyze_limit_up_pool(zt_df) if not zt_df.empty else []

    a_n = sum(1 for s in zt_list if s["grade"] == "A级")
    b_n = sum(1 for s in zt_list if s["grade"] == "B级")
    print(f"涨停{len(zt_list)}只 A:{a_n} B:{b_n}")

    print("      缩量洗筹...", end=" ")
    washout = detect_washout(zt_list)
    print(f"{len(washout)}只候选")

    # Step 3: LLM
    llm_text = None
    if not args.skip_llm and anspire_key and zt_list:
        print("[5/5] AI研判...", end=" ")
        llm_text = llm_analyze(zt_list, ind_df, con_df, anspire_key)
        print("OK" if llm_text else "SKIP")
    else:
        print("[5/5] AI研判: SKIP")
        if not anspire_key:
            errors.append("ANSPIRE_API_KEYS 未设置")

    # Step 4: Build report
    report = build_report(zt_list, ind_df, con_df, washout, llm_text)

    if errors:
        report += f"\n\n> ⚠️ 数据获取异常: {'; '.join(errors)}\n"

    # Step 5: Output
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"\n✅ 报告: {out_path} ({len(report)} chars)")
    else:
        print(report)

    # Summary
    print(f"\n{'='*50}")
    print(f"  涨停:{len(zt_list)} | A:{a_n} B:{b_n} | 洗筹:{len(washout)}")
    if errors:
        print(f"  ⚠️ {len(errors)} 个错误")
    print(f"{'='*50}\n")

    return report


if __name__ == "__main__":
    main()
