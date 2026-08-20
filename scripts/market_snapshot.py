#!/usr/bin/env python3
"""Generate a compact A-share intraday snapshot for GitHub Pages and AI analysis.

The collector intentionally depends only on requests + Python stdlib so it can run
reliably in GitHub Actions. It reuses the same public data families documented by
this repository (Eastmoney push2/push2ex and 10jqka hot list) and emits both JSON
and an AI-friendly Markdown feed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

CN_TZ = ZoneInfo("Asia/Shanghai")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})

INDEXES = {
    "shanghai": {"name": "上证指数", "code": "000001", "secid": "1.000001"},
    "chinext": {"name": "创业板指", "code": "399006", "secid": "0.399006"},
    "star50": {"name": "科创50", "code": "000688", "secid": "1.000688"},
}


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "-":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def get_json(url: str, *, params: dict[str, Any] | None = None, timeout: int = 12) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception as exc:  # network sources occasionally wobble
            last = exc
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}: {last}")


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        delta = cur - prev
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def kline(secid: str, klt: int, limit: int = 180) -> tuple[list[float], str | None]:
    data = get_json(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": secid,
            "klt": klt,
            "fqt": 0,
            "beg": 0,
            "end": 20500101,
            "lmt": limit,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
    )
    rows = ((data.get("data") or {}).get("klines") or [])
    closes: list[float] = []
    last_date = None
    for row in rows:
        parts = str(row).split(",")
        if len(parts) >= 3:
            close = _num(parts[2])
            if close is not None:
                closes.append(close)
                last_date = parts[0][:10]
    return closes, last_date


def index_quotes() -> dict[str, dict[str, Any]]:
    secids = ",".join(v["secid"] for v in INDEXES.values())
    data = get_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        params={
            "secids": secids,
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f14,f2,f3,f4,f5,f6,f17,f18,f15,f16",
        },
    )
    rows = ((data.get("data") or {}).get("diff") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    by_code = {str(x.get("f12")): x for x in rows if isinstance(x, dict)}

    out: dict[str, dict[str, Any]] = {}
    for key, meta in INDEXES.items():
        row = by_code.get(meta["code"], {})
        daily, daily_date = kline(meta["secid"], 101)
        m30, _ = kline(meta["secid"], 30)
        daily_rsi = rsi_wilder(daily)
        m30_rsi = rsi_wilder(m30)
        alerts = []
        for label, value in (("日线", daily_rsi), ("30分钟", m30_rsi)):
            if value is not None and value < 20:
                alerts.append(f"{label}RSI超卖")
            elif value is not None and value > 80:
                alerts.append(f"{label}RSI超买")
        out[key] = {
            "name": meta["name"],
            "code": meta["code"],
            "price": _num(row.get("f2")),
            "pct": _num(row.get("f3")),
            "change": _num(row.get("f4")),
            "volume": _num(row.get("f5")),
            "amount": _num(row.get("f6")),
            "open": _num(row.get("f17")),
            "prev_close": _num(row.get("f18")),
            "high": _num(row.get("f15")),
            "low": _num(row.get("f16")),
            "rsi14_daily": daily_rsi,
            "rsi14_30m": m30_rsi,
            "rsi_alerts": alerts,
            "latest_kline_date": daily_date,
        }
    return out


def board_fund_flow(board_type: str, limit: int = 12) -> list[dict[str, Any]]:
    fs_map = {
        "industry": "m:90+t:2+f:!50",
        "concept": "m:90+t:3+f:!50",
    }
    data = get_json(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": 1,
            "pz": max(limit * 3, 40),
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f62",
            "fs": fs_map[board_type],
            "fields": "f12,f14,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f206",
        },
    )
    rows = ((data.get("data") or {}).get("diff") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    out = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        out.append({
            "code": x.get("f12"),
            "name": x.get("f14"),
            "pct": _num(x.get("f3")),
            "main_net": _num(x.get("f62")),
            "main_ratio": _num(x.get("f184")),
            "super_net": _num(x.get("f66")),
            "large_net": _num(x.get("f72")),
            "mid_net": _num(x.get("f78")),
            "small_net": _num(x.get("f84")),
            "leader_code": x.get("f204"),
            "leader_name": x.get("f205"),
            "leader_pct": _num(x.get("f206")),
        })
    return out[:limit]


def market_breadth() -> dict[str, Any]:
    data = get_json(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": 1,
            "pz": 6000,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f3",
        },
        timeout=20,
    )
    rows = ((data.get("data") or {}).get("diff") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    up = down = flat = strong = weak = 0
    for x in rows:
        pct = _num((x or {}).get("f3")) if isinstance(x, dict) else None
        if pct is None:
            continue
        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct >= 5:
            strong += 1
        if pct <= -5:
            weak += 1
    total = up + down + flat
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "total": total,
        "up_ratio": round(up / total * 100, 1) if total else None,
        "strong_ge_5pct": strong,
        "weak_le_minus5pct": weak,
    }


def eastmoney_pool(kind: str, date: str) -> list[dict[str, Any]]:
    endpoint = {
        "limit_up": "getTopicZTPool",
        "broken": "getTopicZBPool",
        "limit_down": "getTopicDTPool",
    }[kind]
    try:
        data = get_json(
            f"https://push2ex.eastmoney.com/{endpoint}",
            params={
                "ut": EM_UT,
                "dpt": "wz.ztzt",
                "Pageindex": 0,
                "pagesize": 300,
                "sort": "fbt:asc",
                "date": date,
            },
        )
        pool = ((data.get("data") or {}).get("pool") or [])
        return pool if isinstance(pool, list) else []
    except Exception:
        return []


def limit_emotion(now: datetime) -> dict[str, Any]:
    date = now.strftime("%Y%m%d")
    zt = eastmoney_pool("limit_up", date)
    zb = eastmoney_pool("broken", date)
    dt = eastmoney_pool("limit_down", date)
    max_streak = 0
    leaders = []
    for x in zt:
        streak = int(_num(x.get("lbc")) or 0)
        max_streak = max(max_streak, streak)
        if streak >= 2:
            leaders.append({
                "code": x.get("c"),
                "name": x.get("n"),
                "pct": _num(x.get("zdp")),
                "streak": streak,
                "industry": x.get("hybk"),
                "seal_fund": _num(x.get("fund")),
                "open_count": x.get("zbc"),
            })
    leaders.sort(key=lambda x: (x.get("streak") or 0, x.get("seal_fund") or 0), reverse=True)
    denom = len(zt) + len(zb)
    return {
        "limit_up": len(zt),
        "broken": len(zb),
        "limit_down": len(dt),
        "seal_success_rate": round(len(zt) / denom * 100, 1) if denom else None,
        "max_streak": max_streak,
        "streak_leaders": leaders[:10],
    }


def _flatten_concepts(it: dict[str, Any]) -> list[str]:
    candidates = [it.get("concept_tag"), it.get("concepts"), it.get("concept_list")]
    tag = it.get("tag")
    if isinstance(tag, dict):
        candidates.extend([tag.get("concept_tag"), tag.get("concept"), tag.get("name")])
    out: list[str] = []
    for value in candidates:
        if isinstance(value, str):
            out.extend([x.strip() for x in value.replace("|", ",").split(",") if x.strip()])
        elif isinstance(value, list):
            for x in value:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    name = x.get("name") or x.get("concept_name") or x.get("tag_name")
                    if name:
                        out.append(str(name))
    return list(dict.fromkeys(out))[:6]


def ths_hot_list(limit: int = 20) -> list[dict[str, Any]]:
    try:
        data = get_json(
            "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
            params={"stock_type": "a", "type": "hour", "list_type": "normal"},
        )
        rows = ((data.get("data") or {}).get("stock_list") or [])
        out = []
        for it in rows[:limit]:
            if not isinstance(it, dict):
                continue
            out.append({
                "rank": it.get("order") or it.get("rank"),
                "code": it.get("code"),
                "name": it.get("name"),
                "pct": _num(it.get("rate") or it.get("pct") or it.get("change_rate")),
                "heat": it.get("hot") or it.get("heat") or it.get("hot_score"),
                "rank_change": it.get("rank_change") or it.get("rank_chg") or it.get("change"),
                "concepts": _flatten_concepts(it),
            })
        return out
    except Exception:
        return []


def emotion_score(breadth: dict[str, Any], limits: dict[str, Any], indexes: dict[str, Any]) -> dict[str, Any]:
    """A transparent 0-100 telemetry score; not an investment recommendation."""
    score = 50.0
    up_ratio = breadth.get("up_ratio")
    if up_ratio is not None:
        score += (up_ratio - 50) * 0.45
    score += min(limits.get("limit_up", 0), 100) * 0.10
    score -= min(limits.get("limit_down", 0), 100) * 0.18
    seal = limits.get("seal_success_rate")
    if seal is not None:
        score += (seal - 65) * 0.16
    pct_values = [v.get("pct") for v in indexes.values() if v.get("pct") is not None]
    if pct_values:
        score += (sum(pct_values) / len(pct_values)) * 2.0
    score = round(max(0, min(100, score)), 1)
    if score >= 75:
        label = "高热"
    elif score >= 60:
        label = "偏强"
    elif score >= 45:
        label = "震荡"
    elif score >= 30:
        label = "偏弱"
    else:
        label = "低迷"
    return {"score": score, "label": label, "note": "仅为盘面遥测分，不替代人工分析"}


def format_yi(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value / 1e8:+.2f}亿"


def market_phase(now: datetime, trade_day: bool) -> str:
    if not trade_day:
        return "非交易日/数据未进入当日"
    hhmm = now.hour * 100 + now.minute
    if hhmm < 930:
        return "盘前"
    if 930 <= hhmm <= 1130:
        return "上午交易"
    if 1130 < hhmm < 1300:
        return "午间休市"
    if 1300 <= hhmm <= 1500:
        return "下午交易"
    return "收盘后"


def build_snapshot() -> dict[str, Any]:
    now = datetime.now(CN_TZ)
    health: dict[str, str] = {}

    try:
        indexes = index_quotes()
        health["indexes_rsi"] = "ok"
    except Exception as exc:
        indexes = {k: {"name": v["name"], "code": v["code"], "error": str(exc)} for k, v in INDEXES.items()}
        health["indexes_rsi"] = f"error: {exc}"

    latest_date = (indexes.get("shanghai") or {}).get("latest_kline_date")
    trade_day = latest_date == now.strftime("%Y-%m-%d")

    try:
        breadth = market_breadth()
        health["breadth"] = "ok"
    except Exception as exc:
        breadth = {"error": str(exc)}
        health["breadth"] = f"error: {exc}"

    try:
        industry = board_fund_flow("industry")
        concept = board_fund_flow("concept")
        health["board_flow"] = "ok"
    except Exception as exc:
        industry, concept = [], []
        health["board_flow"] = f"error: {exc}"

    limits = limit_emotion(now)
    health["limit_pool"] = "ok" if any(limits.get(k) for k in ("limit_up", "broken", "limit_down")) else "empty"

    hot = ths_hot_list()
    health["hot_list"] = "ok" if hot else "empty"

    industry_out = sorted(industry, key=lambda x: x.get("main_net") if x.get("main_net") is not None else 0)[:8]
    concept_out = sorted(concept, key=lambda x: x.get("main_net") if x.get("main_net") is not None else 0)[:8]

    score = emotion_score(breadth, limits, indexes) if "error" not in breadth else {"score": None, "label": "数据不足"}

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": latest_date,
        "is_trading_day": trade_day,
        "market_phase": market_phase(now, trade_day),
        "indices": indexes,
        "breadth": breadth,
        "limit_emotion": limits,
        "emotion_telemetry": score,
        "fund_flow": {
            "industry_top_in": industry[:8],
            "industry_top_out": industry_out,
            "concept_top_in": concept[:8],
            "concept_top_out": concept_out,
        },
        "hot_stocks": hot,
        "source_health": health,
    }


def previous_snapshot(history: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any] | None:
    if not history:
        return None
    now = datetime.fromisoformat(current["generated_at"])
    candidates = []
    for item in history:
        try:
            dt = datetime.fromisoformat(item["generated_at"])
            delta = abs((now - dt).total_seconds() - 3600)
            candidates.append((delta, item))
        except Exception:
            pass
    return min(candidates, key=lambda x: x[0])[1] if candidates else history[-1]


def add_deltas(snapshot: dict[str, Any], history: list[dict[str, Any]]) -> None:
    prev = previous_snapshot(history, snapshot)
    if not prev:
        snapshot["vs_1h"] = None
        return
    delta: dict[str, Any] = {"reference_at": prev.get("generated_at"), "indices": {}}
    for key in INDEXES:
        cur = ((snapshot.get("indices") or {}).get(key) or {})
        old = ((prev.get("indices") or {}).get(key) or {})
        cur_pct, old_pct = cur.get("pct"), old.get("pct")
        delta["indices"][key] = {
            "pct_change_points": round(cur_pct - old_pct, 2) if cur_pct is not None and old_pct is not None else None,
            "rsi30_change": round(cur.get("rsi14_30m") - old.get("rsi14_30m"), 2)
            if cur.get("rsi14_30m") is not None and old.get("rsi14_30m") is not None else None,
        }
    for name, path in (("up_ratio", ("breadth", "up_ratio")), ("emotion_score", ("emotion_telemetry", "score"))):
        cur = snapshot.get(path[0], {}).get(path[1])
        old = prev.get(path[0], {}).get(path[1])
        delta[name] = round(cur - old, 1) if cur is not None and old is not None else None
    snapshot["vs_1h"] = delta


def markdown_feed(s: dict[str, Any]) -> str:
    lines = [
        "# A股盘中雷达 · AI分析输入",
        "",
        f"- 生成时间（北京时间）：**{s['generated_at']}**",
        f"- 交易日期：**{s.get('trade_date') or '--'}**",
        f"- 市场阶段：**{s.get('market_phase')}**",
        f"- 是否当日交易数据：**{'是' if s.get('is_trading_day') else '否'}**",
        "",
        "## 1. 指数与 RSI",
        "",
        "| 指数 | 涨跌幅 | 日线RSI14 | 30分钟RSI14 | 预警 |",
        "|---|---:|---:|---:|---|",
    ]
    for key in ("shanghai", "chinext", "star50"):
        x = (s.get("indices") or {}).get(key, {})
        alerts = " / ".join(x.get("rsi_alerts") or []) or "-"
        pct = x.get("pct")
        lines.append(
            f"| {x.get('name','--')} | {pct:+.2f}%" if pct is not None else f"| {x.get('name','--')} | --"
        )
        # complete the partially built row to keep formatting readable
        lines[-1] += f" | {x.get('rsi14_daily') if x.get('rsi14_daily') is not None else '--'} | {x.get('rsi14_30m') if x.get('rsi14_30m') is not None else '--'} | {alerts} |"

    b = s.get("breadth") or {}
    l = s.get("limit_emotion") or {}
    e = s.get("emotion_telemetry") or {}
    lines += [
        "",
        "## 2. 市场情绪遥测",
        "",
        f"- 上涨/下跌/平盘：**{b.get('up','--')} / {b.get('down','--')} / {b.get('flat','--')}**，上涨占比 **{b.get('up_ratio','--')}%**",
        f"- 涨停 **{l.get('limit_up','--')}**，炸板 **{l.get('broken','--')}**，跌停 **{l.get('limit_down','--')}**，封板成功率 **{l.get('seal_success_rate','--')}%**",
        f"- 最高连板：**{l.get('max_streak','--')}**",
        f"- 情绪遥测分：**{e.get('score','--')} / 100（{e.get('label','--')}）**；这只是数据底座，最终结论由 AI 结合资金、热点和新闻判断。",
        "",
        "## 3. 板块主力资金",
        "",
        "### 行业净流入 TOP",
    ]
    for x in ((s.get("fund_flow") or {}).get("industry_top_in") or [])[:6]:
        lines.append(f"- {x.get('name')}: {format_yi(x.get('main_net'))}，涨跌 {x.get('pct') if x.get('pct') is not None else '--'}%")
    lines += ["", "### 行业净流出 TOP"]
    for x in ((s.get("fund_flow") or {}).get("industry_top_out") or [])[:6]:
        lines.append(f"- {x.get('name')}: {format_yi(x.get('main_net'))}，涨跌 {x.get('pct') if x.get('pct') is not None else '--'}%")
    lines += ["", "### 概念净流入 TOP"]
    for x in ((s.get("fund_flow") or {}).get("concept_top_in") or [])[:6]:
        lines.append(f"- {x.get('name')}: {format_yi(x.get('main_net'))}，涨跌 {x.get('pct') if x.get('pct') is not None else '--'}%")
    lines += ["", "### 概念净流出 TOP"]
    for x in ((s.get("fund_flow") or {}).get("concept_top_out") or [])[:6]:
        lines.append(f"- {x.get('name')}: {format_yi(x.get('main_net'))}，涨跌 {x.get('pct') if x.get('pct') is not None else '--'}%")

    lines += ["", "## 4. 小时热榜", ""]
    for x in (s.get("hot_stocks") or [])[:12]:
        concepts = " / ".join(x.get("concepts") or [])
        lines.append(f"- #{x.get('rank','--')} {x.get('name','--')} ({x.get('code','--')})  {concepts}")

    lines += ["", "## 5. 连板梯队", ""]
    for x in (l.get("streak_leaders") or [])[:8]:
        lines.append(f"- {x.get('name')} ({x.get('code')})：{x.get('streak')}板，行业 {x.get('industry') or '--'}")

    if s.get("vs_1h"):
        d = s["vs_1h"]
        lines += ["", "## 6. 相比约1小时前", "", f"参考快照：{d.get('reference_at')}"]
        for key in ("shanghai", "chinext", "star50"):
            x = d.get("indices", {}).get(key, {})
            lines.append(f"- {INDEXES[key]['name']}：涨跌幅变化 {x.get('pct_change_points','--')}pct，30分钟RSI变化 {x.get('rsi30_change','--')}")
        lines.append(f"- 上涨占比变化：{d.get('up_ratio','--')}pct；情绪遥测分变化：{d.get('emotion_score','--')}")

    lines += [
        "",
        "## 7. AI分析要求",
        "",
        "请不要机械复述数据。重点判断：指数强弱是否一致、资金是否集中/扩散、热点是否有持续性、情绪是在升温还是退潮、RSI是否触发超买超卖，并结合当时重大财经/产业新闻给出未来1小时风险与观察点。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/data/latest.json")
    parser.add_argument("--history", default="docs/data/history.json")
    parser.add_argument("--feed", default="docs/data/analysis-input.md")
    args = parser.parse_args()

    output = Path(args.output)
    history_path = Path(args.history)
    feed_path = Path(args.feed)
    output.parent.mkdir(parents=True, exist_ok=True)

    history: list[dict[str, Any]] = []
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = loaded
        except Exception:
            history = []

    snapshot = build_snapshot()
    add_deltas(snapshot, history)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    feed_path.write_text(markdown_feed(snapshot), encoding="utf-8")

    history.append(snapshot)
    # Keep a rolling window; enough for several weeks of intraday comparisons.
    history = history[-240:]
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {output}, {feed_path}, {history_path}")


if __name__ == "__main__":
    main()
