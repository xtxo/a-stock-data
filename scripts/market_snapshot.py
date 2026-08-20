#!/usr/bin/env python3
"""Generate an AI-friendly A-share intraday market snapshot.

Sources are deliberately redundant:
- Eastmoney push2 / push2ex: quotes, breadth, board fund flow, limit pools
- Tencent Finance: preferred K-line source for RSI (more reliable on GitHub Actions)
- Eastmoney push2his: K-line fallback
- 10jqka: hourly hot list

Outputs:
- docs/data/latest.json
- docs/data/history.json
- docs/data/analysis-input.md
"""
from __future__ import annotations

import argparse
import json
import math
import time
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
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://quote.eastmoney.com/",
})

INDEXES = {
    "shanghai": {"name": "上证指数", "code": "000001", "secid": "1.000001", "tx": "sh000001"},
    "chinext": {"name": "创业板指", "code": "399006", "secid": "0.399006", "tx": "sz399006"},
    "star50": {"name": "科创50", "code": "000688", "secid": "1.000688", "tx": "sh000688"},
}
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"


def n(v: Any) -> float | None:
    try:
        if v is None or v == "-":
            return None
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def get_json(url: str, params: dict[str, Any] | None = None, timeout: int = 15) -> dict:
    last: Exception | None = None
    for i in range(3):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            text = r.text.strip()
            # Tencent sometimes wraps JSON as: var_name={...}
            if text and not text.startswith("{") and "=" in text:
                text = text.split("=", 1)[1].rstrip(";\n ")
                return json.loads(text)
            data = r.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url}: {last}")


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def tx_kline(symbol: str, period: str, count: int = 180) -> tuple[list[float], str | None]:
    if period == "day":
        data = get_json(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            {"param": f"{symbol},day,,,{count},qfq"},
        )
        node = ((data.get("data") or {}).get(symbol) or {})
        rows = node.get("qfqday") or node.get("day") or []
    else:
        data = get_json(
            "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
            {"param": f"{symbol},{period},,{count}"},
        )
        node = ((data.get("data") or {}).get(symbol) or {})
        rows = node.get(period) or []
    closes, last_date = [], None
    for row in rows:
        if isinstance(row, list) and len(row) >= 3:
            close = n(row[2])
            if close is not None:
                closes.append(close)
                last_date = str(row[0])[:10]
    return closes, last_date


def em_kline(secid: str, klt: int, count: int = 180) -> tuple[list[float], str | None]:
    hosts = [
        "https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    ]
    last_exc: Exception | None = None
    for url in hosts:
        try:
            data = get_json(url, {
                "secid": secid, "klt": klt, "fqt": 0, "beg": 0,
                "end": 20500101, "lmt": count,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            })
            rows = ((data.get("data") or {}).get("klines") or [])
            closes, last_date = [], None
            for row in rows:
                p = str(row).split(",")
                if len(p) >= 3 and n(p[2]) is not None:
                    closes.append(float(p[2]))
                    last_date = p[0][:10]
            if closes:
                return closes, last_date
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"Eastmoney kline failed: {last_exc}")


def get_kline(meta: dict[str, str], timeframe: str) -> tuple[list[float], str | None, str]:
    tx_period = "day" if timeframe == "day" else "m30"
    try:
        closes, dt = tx_kline(meta["tx"], tx_period)
        if closes:
            return closes, dt, "tencent"
    except Exception:
        pass
    klt = 101 if timeframe == "day" else 30
    closes, dt = em_kline(meta["secid"], klt)
    return closes, dt, "eastmoney"


def quote_rows(secids: str, fields: str) -> list[dict[str, Any]]:
    data = get_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        {"secids": secids, "fltt": 2, "invt": 2, "ut": EM_UT, "fields": fields},
    )
    rows = ((data.get("data") or {}).get("diff") or [])
    return list(rows.values()) if isinstance(rows, dict) else rows


def index_snapshot() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    health: dict[str, str] = {}
    rows = quote_rows(
        ",".join(x["secid"] for x in INDEXES.values()),
        "f12,f14,f2,f3,f4,f5,f6,f17,f18,f15,f16,f124",
    )
    by_code = {str(x.get("f12")): x for x in rows if isinstance(x, dict)}
    out: dict[str, dict[str, Any]] = {}

    for key, meta in INDEXES.items():
        row = by_code.get(meta["code"], {})
        daily_rsi = m30_rsi = None
        daily_date = None
        src_day = src_30 = "failed"
        day_err = m30_err = None
        try:
            closes, daily_date, src_day = get_kline(meta, "day")
            daily_rsi = rsi_wilder(closes)
        except Exception as exc:
            day_err = str(exc)
        try:
            closes, _, src_30 = get_kline(meta, "m30")
            m30_rsi = rsi_wilder(closes)
        except Exception as exc:
            m30_err = str(exc)

        alerts = []
        for label, value in (("日线", daily_rsi), ("30分钟", m30_rsi)):
            if value is not None and value < 20:
                alerts.append(f"{label}RSI超卖")
            if value is not None and value > 80:
                alerts.append(f"{label}RSI超买")

        quote_ts = row.get("f124")
        quote_date = None
        try:
            if quote_ts:
                quote_date = datetime.fromtimestamp(int(quote_ts), CN_TZ).strftime("%Y-%m-%d")
        except Exception:
            pass
        out[key] = {
            "name": meta["name"], "code": meta["code"],
            "price": n(row.get("f2")), "pct": n(row.get("f3")), "change": n(row.get("f4")),
            "volume": n(row.get("f5")), "amount": n(row.get("f6")),
            "open": n(row.get("f17")), "prev_close": n(row.get("f18")),
            "high": n(row.get("f15")), "low": n(row.get("f16")),
            "quote_date": quote_date, "latest_kline_date": daily_date,
            "rsi14_daily": daily_rsi, "rsi14_30m": m30_rsi, "rsi_alerts": alerts,
            "rsi_sources": {"daily": src_day, "m30": src_30},
        }
        if day_err or m30_err:
            out[key]["rsi_errors"] = {"daily": day_err, "m30": m30_err}

    health["quotes"] = "ok" if any(x.get("price") is not None for x in out.values()) else "empty"
    health["rsi"] = "ok" if all(x.get("rsi14_daily") is not None and x.get("rsi14_30m") is not None for x in out.values()) else "partial"
    return out, health


def market_breadth() -> dict[str, Any]:
    # Shanghai Composite + Shenzhen Composite provide exchange-wide advance/decline counts.
    rows = quote_rows("1.000001,0.399106", "f12,f14,f104,f105,f106")
    up = down = flat = 0
    parts = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        u = int(n(x.get("f104")) or 0)
        d = int(n(x.get("f105")) or 0)
        f = int(n(x.get("f106")) or 0)
        up += u; down += d; flat += f
        parts.append({"name": x.get("f14"), "up": u, "down": d, "flat": f})
    total = up + down + flat
    return {
        "up": up, "down": down, "flat": flat, "total": total,
        "up_ratio": round(up / total * 100, 1) if total else None,
        "exchanges": parts,
    }


def board_flow(board_type: str, direction: str, limit: int = 8) -> list[dict[str, Any]]:
    fs = "m:90+t:2+f:!50" if board_type == "industry" else "m:90+t:3+f:!50"
    data = get_json(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {
            "pn": 1, "pz": limit, "po": 1 if direction == "in" else 0,
            "np": 1, "fltt": 2, "invt": 2, "ut": EM_UT,
            "fid": "f62", "fs": fs,
            "fields": "f12,f14,f3,f62,f184,f66,f72,f78,f84,f204,f205",
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
            "code": x.get("f12"), "name": x.get("f14"), "pct": n(x.get("f3")),
            "main_net": n(x.get("f62")), "main_ratio": n(x.get("f184")),
            "super_net": n(x.get("f66")), "large_net": n(x.get("f72")),
            "mid_net": n(x.get("f78")), "small_net": n(x.get("f84")),
            # Live verification on push2 shows f204=name, f205=code for this endpoint.
            "leader_name": x.get("f204"), "leader_code": x.get("f205"),
        })
    return out[:limit]


def topic_pool(kind: str, date: str) -> list[dict[str, Any]]:
    endpoint = {"up": "getTopicZTPool", "broken": "getTopicZBPool", "down": "getTopicDTPool"}[kind]
    try:
        data = get_json(
            f"https://push2ex.eastmoney.com/{endpoint}",
            {"ut": ZT_UT, "dpt": "wz.ztzt", "Pageindex": 0, "pagesize": 300, "sort": "fbt:asc", "date": date},
        )
        rows = ((data.get("data") or {}).get("pool") or [])
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def limit_emotion(now: datetime) -> dict[str, Any]:
    date = now.strftime("%Y%m%d")
    zt, zb, dt = topic_pool("up", date), topic_pool("broken", date), topic_pool("down", date)
    leaders, max_streak = [], 0
    for x in zt:
        streak = int(n(x.get("lbc")) or 0)
        max_streak = max(max_streak, streak)
        if streak >= 2:
            leaders.append({
                "code": x.get("c"), "name": x.get("n"), "pct": n(x.get("zdp")),
                "streak": streak, "industry": x.get("hybk"),
                "seal_fund": n(x.get("fund")), "open_count": x.get("zbc"),
            })
    leaders.sort(key=lambda x: (x.get("streak") or 0, x.get("seal_fund") or 0), reverse=True)
    denom = len(zt) + len(zb)
    return {
        "limit_up": len(zt), "broken": len(zb), "limit_down": len(dt),
        "seal_success_rate": round(len(zt) / denom * 100, 1) if denom else None,
        "max_streak": max_streak, "streak_leaders": leaders[:10],
    }


def flatten_concepts(it: dict[str, Any]) -> list[str]:
    vals = [it.get("concept_tag"), it.get("concepts"), it.get("concept_list")]
    tag = it.get("tag")
    if isinstance(tag, dict):
        vals += [tag.get("concept_tag"), tag.get("concept"), tag.get("name")]
    out: list[str] = []
    for val in vals:
        if isinstance(val, str):
            out += [x.strip() for x in val.replace("|", ",").split(",") if x.strip()]
        elif isinstance(val, list):
            for x in val:
                if isinstance(x, str):
                    out.append(x)
                elif isinstance(x, dict):
                    name = x.get("name") or x.get("concept_name") or x.get("tag_name")
                    if name:
                        out.append(str(name))
    return list(dict.fromkeys(out))[:6]


def hot_list(limit: int = 20) -> list[dict[str, Any]]:
    try:
        data = get_json(
            "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
            {"stock_type": "a", "type": "hour", "list_type": "normal"},
        )
        rows = ((data.get("data") or {}).get("stock_list") or [])
        out = []
        for x in rows[:limit]:
            if not isinstance(x, dict):
                continue
            out.append({
                "rank": x.get("order") or x.get("rank"), "code": x.get("code"), "name": x.get("name"),
                "pct": n(x.get("rate") or x.get("pct") or x.get("change_rate")),
                "heat": x.get("hot") or x.get("heat") or x.get("hot_score"),
                "rank_change": x.get("rank_change") or x.get("rank_chg") or x.get("change"),
                "concepts": flatten_concepts(x),
            })
        return out
    except Exception:
        return []


def emotion_score(b: dict[str, Any], l: dict[str, Any], idx: dict[str, Any]) -> dict[str, Any]:
    score = 50.0
    if b.get("up_ratio") is not None:
        score += (b["up_ratio"] - 50) * 0.45
    score += min(l.get("limit_up", 0), 100) * 0.10
    score -= min(l.get("limit_down", 0), 100) * 0.18
    if l.get("seal_success_rate") is not None:
        score += (l["seal_success_rate"] - 65) * 0.16
    pcts = [x.get("pct") for x in idx.values() if x.get("pct") is not None]
    if pcts:
        score += sum(pcts) / len(pcts) * 2
    score = round(max(0, min(100, score)), 1)
    label = "高热" if score >= 75 else "偏强" if score >= 60 else "震荡" if score >= 45 else "偏弱" if score >= 30 else "低迷"
    return {"score": score, "label": label, "note": "盘面遥测分，不替代人工分析"}


def phase(now: datetime, trade_day: bool) -> str:
    if not trade_day:
        return "非交易日/行情日期异常"
    t = now.hour * 100 + now.minute
    if t < 930: return "盘前"
    if t <= 1130: return "上午交易"
    if t < 1300: return "午间休市"
    if t <= 1500: return "下午交易"
    return "收盘后"


def build_snapshot() -> dict[str, Any]:
    now = datetime.now(CN_TZ)
    health: dict[str, str] = {}

    try:
        indexes, idx_health = index_snapshot()
        health.update(idx_health)
    except Exception as exc:
        indexes = {k: {"name": v["name"], "code": v["code"], "error": str(exc)} for k, v in INDEXES.items()}
        health["quotes"] = f"error: {exc}"
        health["rsi"] = "error"

    candidate_dates = []
    for x in indexes.values():
        for field in ("quote_date", "latest_kline_date"):
            if x.get(field): candidate_dates.append(x[field])
    trade_date = max(candidate_dates) if candidate_dates else None
    trade_day = trade_date == now.strftime("%Y-%m-%d")

    try:
        breadth = market_breadth(); health["breadth"] = "ok" if breadth.get("total") else "empty"
    except Exception as exc:
        breadth = {"error": str(exc)}; health["breadth"] = f"error: {exc}"

    try:
        fund_flow = {
            "industry_top_in": board_flow("industry", "in"),
            "industry_top_out": board_flow("industry", "out"),
            "concept_top_in": board_flow("concept", "in"),
            "concept_top_out": board_flow("concept", "out"),
        }
        health["board_flow"] = "ok"
    except Exception as exc:
        fund_flow = {"industry_top_in": [], "industry_top_out": [], "concept_top_in": [], "concept_top_out": []}
        health["board_flow"] = f"error: {exc}"

    limits = limit_emotion(now)
    health["limit_pool"] = "ok" if (limits.get("limit_up") or limits.get("broken") or limits.get("limit_down")) else "empty"
    hot = hot_list(); health["hot_list"] = "ok" if hot else "empty"
    telemetry = emotion_score(breadth, limits, indexes) if breadth.get("total") else {"score": None, "label": "数据不足"}

    return {
        "schema_version": 2, "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": trade_date, "is_trading_day": trade_day, "market_phase": phase(now, trade_day),
        "indices": indexes, "breadth": breadth, "limit_emotion": limits,
        "emotion_telemetry": telemetry, "fund_flow": fund_flow, "hot_stocks": hot,
        "source_health": health,
    }


def pick_prev(history: list[dict[str, Any]], cur: dict[str, Any]) -> dict[str, Any] | None:
    now = datetime.fromisoformat(cur["generated_at"])
    valid = []
    for item in history:
        try:
            if item.get("trade_date") != cur.get("trade_date") or not item.get("trade_date"):
                continue
            dt = datetime.fromisoformat(item["generated_at"])
            valid.append((abs((now - dt).total_seconds() - 3600), item))
        except Exception:
            pass
    return min(valid, key=lambda x: x[0])[1] if valid else None


def add_hour_delta(cur: dict[str, Any], history: list[dict[str, Any]]) -> None:
    prev = pick_prev(history, cur)
    if not prev:
        cur["vs_1h"] = None; return
    out: dict[str, Any] = {"reference_at": prev.get("generated_at"), "indices": {}}
    for key in INDEXES:
        a = cur.get("indices", {}).get(key, {}); b = prev.get("indices", {}).get(key, {})
        out["indices"][key] = {
            "pct_change_points": round(a["pct"] - b["pct"], 2) if a.get("pct") is not None and b.get("pct") is not None else None,
            "rsi30_change": round(a["rsi14_30m"] - b["rsi14_30m"], 2) if a.get("rsi14_30m") is not None and b.get("rsi14_30m") is not None else None,
        }
    a = cur.get("breadth", {}).get("up_ratio"); b = prev.get("breadth", {}).get("up_ratio")
    out["up_ratio"] = round(a - b, 1) if a is not None and b is not None else None
    a = cur.get("emotion_telemetry", {}).get("score"); b = prev.get("emotion_telemetry", {}).get("score")
    out["emotion_score"] = round(a - b, 1) if a is not None and b is not None else None
    cur["vs_1h"] = out


def yi(v: float | None) -> str:
    return "--" if v is None else f"{v / 1e8:+.2f}亿"


def feed(s: dict[str, Any]) -> str:
    lines = [
        "# A股盘中雷达 · AI分析输入", "",
        f"- 生成时间（北京时间）：**{s['generated_at']}**",
        f"- 交易日期：**{s.get('trade_date') or '--'}**",
        f"- 市场阶段：**{s.get('market_phase')}**",
        f"- 是否当日交易数据：**{'是' if s.get('is_trading_day') else '否'}**",
        "", "## 1. 指数与 RSI", "",
        "| 指数 | 涨跌幅 | 日线RSI14 | 30分钟RSI14 | 预警 |",
        "|---|---:|---:|---:|---|",
    ]
    for key in ("shanghai", "chinext", "star50"):
        x = s.get("indices", {}).get(key, {})
        p = "--" if x.get("pct") is None else f"{x['pct']:+.2f}%"
        alerts = " / ".join(x.get("rsi_alerts") or []) or "-"
        lines.append(f"| {x.get('name','--')} | {p} | {x.get('rsi14_daily','--')} | {x.get('rsi14_30m','--')} | {alerts} |")

    b, l, e = s.get("breadth", {}), s.get("limit_emotion", {}), s.get("emotion_telemetry", {})
    lines += [
        "", "## 2. 市场情绪", "",
        f"- 上涨/下跌/平盘：**{b.get('up','--')} / {b.get('down','--')} / {b.get('flat','--')}**，上涨占比 **{b.get('up_ratio','--')}%**",
        f"- 涨停 **{l.get('limit_up','--')}**，炸板 **{l.get('broken','--')}**，跌停 **{l.get('limit_down','--')}**，封板率 **{l.get('seal_success_rate','--')}%**，最高 **{l.get('max_streak','--')}板**",
        f"- 情绪遥测：**{e.get('score','--')} / 100（{e.get('label','--')}）**",
        "", "## 3. 板块主力资金", "",
    ]
    for title, key in (("行业净流入", "industry_top_in"), ("行业净流出", "industry_top_out"), ("概念净流入", "concept_top_in"), ("概念净流出", "concept_top_out")):
        lines += [f"### {title}"]
        for x in s.get("fund_flow", {}).get(key, [])[:6]:
            lines.append(f"- {x.get('name')}: {yi(x.get('main_net'))}，涨跌 {x.get('pct') if x.get('pct') is not None else '--'}%，领涨 {x.get('leader_name') or '--'}")
        lines.append("")

    lines += ["## 4. 小时热门股", ""]
    for x in s.get("hot_stocks", [])[:12]:
        lines.append(f"- #{x.get('rank','--')} {x.get('name','--')} ({x.get('code','--')}) {' / '.join(x.get('concepts') or [])}")
    lines += ["", "## 5. 连板梯队", ""]
    for x in l.get("streak_leaders", [])[:8]:
        lines.append(f"- {x.get('name')} ({x.get('code')})：{x.get('streak')}板，{x.get('industry') or '--'}")

    if s.get("vs_1h"):
        d = s["vs_1h"]
        lines += ["", "## 6. 相比约1小时前", "", f"参考：{d.get('reference_at')}"]
        for key in ("shanghai", "chinext", "star50"):
            x = d.get("indices", {}).get(key, {})
            lines.append(f"- {INDEXES[key]['name']}：涨跌幅变化 {x.get('pct_change_points','--')}pct；30分钟RSI变化 {x.get('rsi30_change','--')}")
        lines.append(f"- 上涨占比变化 {d.get('up_ratio','--')}pct；情绪分变化 {d.get('emotion_score','--')}")

    lines += [
        "", "## 7. AI分析要求", "",
        "不要机械复述数据。结合当时公开财经/产业新闻，重点判断指数强弱是否一致、资金集中还是扩散、热点是否持续、情绪升温还是退潮、RSI是否超买超卖，并给出未来1小时最值得观察的方向和风险信号。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="docs/data/latest.json")
    p.add_argument("--history", default="docs/data/history.json")
    p.add_argument("--feed", default="docs/data/analysis-input.md")
    a = p.parse_args()
    out, hp, fp = Path(a.output), Path(a.history), Path(a.feed)
    out.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    if hp.exists():
        try:
            x = json.loads(hp.read_text(encoding="utf-8")); history = x if isinstance(x, list) else []
        except Exception:
            pass
    snap = build_snapshot(); add_hour_delta(snap, history)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    fp.write_text(feed(snap), encoding="utf-8")
    history.append(snap)
    hp.write_text(json.dumps(history[-240:], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}, {fp}, {hp}")


if __name__ == "__main__":
    main()
