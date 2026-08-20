#!/usr/bin/env python3
"""A-share intraday snapshot for GitHub Pages + AI analysis.

Design goals:
- Index quotes and RSI must survive Eastmoney throttling on GitHub Actions.
- Critical index data therefore prefers Tencent Finance and only falls back to Eastmoney.
- Eastmoney remains the source for board fund-flow and limit-up/limit-down pools.
- 10jqka provides the hourly hot-stock list.

Outputs:
  docs/data/latest.json
  docs/data/history.json
  docs/data/analysis-input.md
"""
from __future__ import annotations

import argparse
import json
import math
import re
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
SESSION.headers.update({"User-Agent": UA, "Accept": "*/*"})

INDEXES = {
    "shanghai": {"name": "上证指数", "code": "000001", "secid": "1.000001", "tx": "sh000001"},
    "chinext": {"name": "创业板指", "code": "399006", "secid": "0.399006", "tx": "sz399006"},
    "star50": {"name": "科创50", "code": "000688", "secid": "1.000688", "tx": "sh000688"},
}
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"
ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"


def num(v: Any) -> float | None:
    try:
        if v is None or v == "-" or v == "":
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
    return round(100.0 - 100.0 / (1.0 + rs), 2)


# ------------------------- Tencent: critical index path -------------------------

def tencent_index_quotes() -> dict[str, dict[str, Any]]:
    """Return three index quotes from qt.gtimg.cn.

    Tencent quote rows use ~ separated fields. For index quotes the stable fields
    we need are name/code/current/prev-close/open/timestamp/change/pct/high/low.
    """
    symbols = [m["tx"] for m in INDEXES.values()]
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    last: Exception | None = None
    for scheme_url in (url, url.replace("https://", "http://")):
        try:
            r = SESSION.get(
                scheme_url,
                timeout=10,
                headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
            )
            r.raise_for_status()
            r.encoding = "gbk"
            text = r.text
            out: dict[str, dict[str, Any]] = {}
            for match in re.finditer(r'v_([a-z0-9]+)="([^"]*)"', text, re.I):
                symbol, payload = match.group(1), match.group(2)
                f = payload.split("~")
                if len(f) < 35:
                    continue
                ts = f[30] if len(f) > 30 else ""
                quote_date = None
                if len(ts) >= 8 and ts[:8].isdigit():
                    quote_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                out[symbol] = {
                    "name": f[1] or None,
                    "code": f[2] or None,
                    "price": num(f[3]),
                    "prev_close": num(f[4]),
                    "open": num(f[5]),
                    "quote_date": quote_date,
                    "change": num(f[31]) if len(f) > 31 else None,
                    "pct": num(f[32]) if len(f) > 32 else None,
                    "high": num(f[33]) if len(f) > 33 else None,
                    "low": num(f[34]) if len(f) > 34 else None,
                }
            if out:
                return out
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Tencent quote failed: {last}")


def tencent_kline(symbol: str, timeframe: str, count: int = 200) -> tuple[list[float], str | None]:
    if timeframe == "day":
        data = get_json(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            {"param": f"{symbol},day,,,{count},qfq"},
        )
        node = ((data.get("data") or {}).get(symbol) or {})
        rows = node.get("qfqday") or node.get("day") or []
    else:
        period = "m30"
        data = get_json(
            "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
            {"param": f"{symbol},{period},,{count}"},
        )
        node = ((data.get("data") or {}).get(symbol) or {})
        rows = node.get(period) or []

    closes: list[float] = []
    last_date = None
    for row in rows:
        if isinstance(row, list) and len(row) >= 3:
            close = num(row[2])
            if close is not None:
                closes.append(close)
                raw_dt = str(row[0])
                if len(raw_dt) >= 8 and raw_dt[:8].isdigit():
                    last_date = f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]}"
                else:
                    last_date = raw_dt[:10]
    if not closes:
        raise RuntimeError(f"Tencent {timeframe} kline empty for {symbol}")
    return closes, last_date


# -------------------------- Eastmoney: fallback + flows -------------------------

def eastmoney_kline(secid: str, timeframe: str, count: int = 200) -> tuple[list[float], str | None]:
    klt = 101 if timeframe == "day" else 30
    hosts = [
        "https://7.push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://91.push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    ]
    last: Exception | None = None
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
                if len(p) >= 3:
                    close = num(p[2])
                    if close is not None:
                        closes.append(close)
                        last_date = p[0][:10]
            if closes:
                return closes, last_date
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Eastmoney {timeframe} kline failed: {last}")


def get_kline(meta: dict[str, str], timeframe: str) -> tuple[list[float], str | None, str]:
    try:
        closes, dt = tencent_kline(meta["tx"], timeframe)
        return closes, dt, "tencent"
    except Exception as tx_exc:
        try:
            closes, dt = eastmoney_kline(meta["secid"], timeframe)
            return closes, dt, "eastmoney"
        except Exception as em_exc:
            raise RuntimeError(f"Tencent={tx_exc}; Eastmoney={em_exc}")


def eastmoney_index_quotes() -> dict[str, dict[str, Any]]:
    secids = ",".join(m["secid"] for m in INDEXES.values())
    data = get_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        {
            "secids": secids, "fltt": 2, "invt": 2, "ut": EM_UT,
            "fields": "f12,f14,f2,f3,f4,f17,f18,f15,f16,f124",
        },
    )
    rows = ((data.get("data") or {}).get("diff") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    out: dict[str, dict[str, Any]] = {}
    for x in rows:
        if not isinstance(x, dict):
            continue
        code = str(x.get("f12"))
        symbol = next((m["tx"] for m in INDEXES.values() if m["code"] == code), None)
        if not symbol:
            continue
        quote_date = None
        try:
            if x.get("f124"):
                quote_date = datetime.fromtimestamp(int(x["f124"]), CN_TZ).strftime("%Y-%m-%d")
        except Exception:
            pass
        out[symbol] = {
            "name": x.get("f14"), "code": code, "price": num(x.get("f2")),
            "pct": num(x.get("f3")), "change": num(x.get("f4")),
            "open": num(x.get("f17")), "prev_close": num(x.get("f18")),
            "high": num(x.get("f15")), "low": num(x.get("f16")),
            "quote_date": quote_date,
        }
    if not out:
        raise RuntimeError("Eastmoney index quotes empty")
    return out


def index_snapshot() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    health: dict[str, str] = {}
    quotes: dict[str, dict[str, Any]] = {}
    quote_source = "none"
    try:
        quotes = tencent_index_quotes()
        quote_source = "tencent"
    except Exception as tx_exc:
        try:
            quotes = eastmoney_index_quotes()
            quote_source = "eastmoney"
        except Exception as em_exc:
            health["quotes"] = f"error: Tencent={tx_exc}; Eastmoney={em_exc}"
    if "quotes" not in health:
        health["quotes"] = f"ok:{quote_source}"

    out: dict[str, dict[str, Any]] = {}
    rsi_complete = True
    for key, meta in INDEXES.items():
        q = quotes.get(meta["tx"], {})
        item: dict[str, Any] = {
            "name": meta["name"], "code": meta["code"],
            "price": q.get("price"), "pct": q.get("pct"), "change": q.get("change"),
            "open": q.get("open"), "prev_close": q.get("prev_close"),
            "high": q.get("high"), "low": q.get("low"), "quote_date": q.get("quote_date"),
        }
        sources: dict[str, str] = {}
        errors: dict[str, str] = {}
        latest_kline_date = None
        for timeframe, field in (("day", "rsi14_daily"), ("m30", "rsi14_30m")):
            try:
                closes, dt, src = get_kline(meta, timeframe)
                item[field] = rsi_wilder(closes)
                sources[timeframe] = src
                if timeframe == "day":
                    latest_kline_date = dt
            except Exception as exc:
                item[field] = None
                errors[timeframe] = str(exc)
                sources[timeframe] = "failed"
                rsi_complete = False
        item["latest_kline_date"] = latest_kline_date
        item["rsi_sources"] = sources
        if errors:
            item["rsi_errors"] = errors
        alerts = []
        for label, field in (("日线", "rsi14_daily"), ("30分钟", "rsi14_30m")):
            value = item.get(field)
            if value is not None and value < 20:
                alerts.append(f"{label}RSI超卖")
            elif value is not None and value > 80:
                alerts.append(f"{label}RSI超买")
        item["rsi_alerts"] = alerts
        out[key] = item

    health["rsi"] = "ok" if rsi_complete else "partial"
    return out, health


def market_breadth() -> dict[str, Any]:
    """Exchange advance/decline counts from Eastmoney index quote fields.

    f104/f105/f106 correspond to rise/fall/flat counts. This source is useful but
    non-critical; callers keep the rest of the snapshot if Eastmoney is blocked.
    """
    data = get_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        {
            "secids": "1.000001,0.399106", "fltt": 2, "invt": 2, "ut": EM_UT,
            "fields": "f12,f14,f104,f105,f106",
        },
    )
    rows = ((data.get("data") or {}).get("diff") or [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    up = down = flat = 0
    exchanges = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        u = int(num(x.get("f104")) or 0)
        d = int(num(x.get("f105")) or 0)
        f = int(num(x.get("f106")) or 0)
        up += u; down += d; flat += f
        exchanges.append({"name": x.get("f14"), "up": u, "down": d, "flat": f})
    total = up + down + flat
    if not total:
        raise RuntimeError("breadth empty")
    return {
        "up": up, "down": down, "flat": flat, "total": total,
        "up_ratio": round(up / total * 100, 1), "exchanges": exchanges,
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
            "code": x.get("f12"), "name": x.get("f14"), "pct": num(x.get("f3")),
            "main_net": num(x.get("f62")), "main_ratio": num(x.get("f184")),
            "super_net": num(x.get("f66")), "large_net": num(x.get("f72")),
            "mid_net": num(x.get("f78")), "small_net": num(x.get("f84")),
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
        streak = int(num(x.get("lbc")) or 0)
        max_streak = max(max_streak, streak)
        if streak >= 2:
            leaders.append({
                "code": x.get("c"), "name": x.get("n"), "pct": num(x.get("zdp")),
                "streak": streak, "industry": x.get("hybk"),
                "seal_fund": num(x.get("fund")), "open_count": x.get("zbc"),
            })
    leaders.sort(key=lambda x: (x.get("streak") or 0, x.get("seal_fund") or 0), reverse=True)
    denom = len(zt) + len(zb)
    return {
        "limit_up": len(zt), "broken": len(zb), "limit_down": len(dt),
        "seal_success_rate": round(len(zt) / denom * 100, 1) if denom else None,
        "max_streak": max_streak, "streak_leaders": leaders[:10],
    }


def flatten_concepts(it: dict[str, Any]) -> list[str]:
    values = [it.get("concept_tag"), it.get("concepts"), it.get("concept_list")]
    tag = it.get("tag")
    if isinstance(tag, dict):
        values.extend([tag.get("concept_tag"), tag.get("concept"), tag.get("name")])
    out: list[str] = []
    for value in values:
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
                "rank": x.get("order") or x.get("rank"),
                "code": x.get("code"), "name": x.get("name"),
                "pct": num(x.get("rise_and_fall")),
                "heat": num(x.get("rate")) if num(x.get("rate")) is not None else x.get("rate"),
                "rank_change": x.get("hot_rank_chg"),
                "concepts": flatten_concepts(x),
            })
        return out
    except Exception:
        return []


def emotion_score(breadth: dict[str, Any], limits: dict[str, Any], indexes: dict[str, Any]) -> dict[str, Any]:
    score = 50.0
    if breadth.get("up_ratio") is not None:
        score += (breadth["up_ratio"] - 50) * 0.45
    score += min(limits.get("limit_up", 0), 100) * 0.10
    score -= min(limits.get("limit_down", 0), 100) * 0.18
    if limits.get("seal_success_rate") is not None:
        score += (limits["seal_success_rate"] - 65) * 0.16
    pcts = [x.get("pct") for x in indexes.values() if x.get("pct") is not None]
    if pcts:
        score += (sum(pcts) / len(pcts)) * 2.0
    score = round(max(0, min(100, score)), 1)
    label = "高热" if score >= 75 else "偏强" if score >= 60 else "震荡" if score >= 45 else "偏弱" if score >= 30 else "低迷"
    return {"score": score, "label": label, "note": "盘面遥测分，不替代AI综合判断"}


def market_phase(now: datetime, trade_day: bool) -> str:
    if not trade_day:
        return "非交易日/行情日期异常"
    t = now.hour * 100 + now.minute
    if t < 930:
        return "盘前"
    if t <= 1130:
        return "上午交易"
    if t < 1300:
        return "午间休市"
    if t <= 1500:
        return "下午交易"
    return "收盘后"


def build_snapshot() -> dict[str, Any]:
    now = datetime.now(CN_TZ)
    health: dict[str, str] = {}

    try:
        indexes, index_health = index_snapshot()
        health.update(index_health)
    except Exception as exc:
        indexes = {k: {"name": v["name"], "code": v["code"], "error": str(exc)} for k, v in INDEXES.items()}
        health["quotes"] = f"error:{exc}"
        health["rsi"] = "error"

    candidate_dates: list[str] = []
    for x in indexes.values():
        for field in ("quote_date", "latest_kline_date"):
            if x.get(field):
                candidate_dates.append(str(x[field]))
    trade_date = max(candidate_dates) if candidate_dates else None
    trade_day = trade_date == now.strftime("%Y-%m-%d")

    try:
        breadth = market_breadth()
        health["breadth"] = "ok"
    except Exception as exc:
        breadth = {"up": None, "down": None, "flat": None, "total": None, "up_ratio": None, "error": str(exc)}
        health["breadth"] = f"error:{exc}"

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
        health["board_flow"] = f"error:{exc}"

    limits = limit_emotion(now)
    health["limit_pool"] = "ok" if any(limits.get(k) for k in ("limit_up", "broken", "limit_down")) else "empty"
    hot = hot_list()
    health["hot_list"] = "ok" if hot else "empty"
    telemetry = emotion_score(breadth, limits, indexes)

    return {
        "schema_version": 3,
        "generated_at": now.isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "is_trading_day": trade_day,
        "market_phase": market_phase(now, trade_day),
        "indices": indexes,
        "breadth": breadth,
        "limit_emotion": limits,
        "emotion_telemetry": telemetry,
        "fund_flow": fund_flow,
        "hot_stocks": hot,
        "source_health": health,
    }


def pick_prev(history: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any] | None:
    if not current.get("trade_date"):
        return None
    now = datetime.fromisoformat(current["generated_at"])
    candidates = []
    for item in history:
        try:
            if item.get("trade_date") != current.get("trade_date"):
                continue
            dt = datetime.fromisoformat(item["generated_at"])
            candidates.append((abs((now - dt).total_seconds() - 3600), item))
        except Exception:
            pass
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def add_hour_delta(current: dict[str, Any], history: list[dict[str, Any]]) -> None:
    prev = pick_prev(history, current)
    if not prev:
        current["vs_1h"] = None
        return
    out: dict[str, Any] = {"reference_at": prev.get("generated_at"), "indices": {}}
    for key in INDEXES:
        a = current.get("indices", {}).get(key, {})
        b = prev.get("indices", {}).get(key, {})
        out["indices"][key] = {
            "pct_change_points": round(a["pct"] - b["pct"], 2) if a.get("pct") is not None and b.get("pct") is not None else None,
            "rsi30_change": round(a["rsi14_30m"] - b["rsi14_30m"], 2) if a.get("rsi14_30m") is not None and b.get("rsi14_30m") is not None else None,
        }
    a = current.get("breadth", {}).get("up_ratio")
    b = prev.get("breadth", {}).get("up_ratio")
    out["up_ratio"] = round(a - b, 1) if a is not None and b is not None else None
    a = current.get("emotion_telemetry", {}).get("score")
    b = prev.get("emotion_telemetry", {}).get("score")
    out["emotion_score"] = round(a - b, 1) if a is not None and b is not None else None
    current["vs_1h"] = out


def yi(value: float | None) -> str:
    return "--" if value is None else f"{value / 1e8:+.2f}亿"


def markdown_feed(s: dict[str, Any]) -> str:
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
        pct = "--" if x.get("pct") is None else f"{x['pct']:+.2f}%"
        alerts = " / ".join(x.get("rsi_alerts") or []) or "-"
        lines.append(f"| {x.get('name','--')} | {pct} | {x.get('rsi14_daily','--')} | {x.get('rsi14_30m','--')} | {alerts} |")

    b = s.get("breadth", {})
    l = s.get("limit_emotion", {})
    e = s.get("emotion_telemetry", {})
    lines += [
        "", "## 2. 市场情绪", "",
        f"- 上涨/下跌/平盘：**{b.get('up','--')} / {b.get('down','--')} / {b.get('flat','--')}**，上涨占比 **{b.get('up_ratio','--')}%**",
        f"- 涨停 **{l.get('limit_up','--')}**，炸板 **{l.get('broken','--')}**，跌停 **{l.get('limit_down','--')}**，封板率 **{l.get('seal_success_rate','--')}%**，最高 **{l.get('max_streak','--')}板**",
        f"- 情绪遥测：**{e.get('score','--')} / 100（{e.get('label','--')}）**",
        "", "## 3. 板块主力资金", "",
    ]
    for title, key in (
        ("行业净流入", "industry_top_in"), ("行业净流出", "industry_top_out"),
        ("概念净流入", "concept_top_in"), ("概念净流出", "concept_top_out"),
    ):
        lines.append(f"### {title}")
        for x in s.get("fund_flow", {}).get(key, [])[:6]:
            lines.append(f"- {x.get('name')}: {yi(x.get('main_net'))}，涨跌 {x.get('pct') if x.get('pct') is not None else '--'}%，领涨 {x.get('leader_name') or '--'}")
        lines.append("")

    lines += ["## 4. 小时热门股", ""]
    for x in s.get("hot_stocks", [])[:12]:
        p = "--" if x.get("pct") is None else f"{x['pct']:+.2f}%"
        lines.append(f"- #{x.get('rank','--')} {x.get('name','--')} ({x.get('code','--')}) {p}；热度 {x.get('heat','--')}；{' / '.join(x.get('concepts') or [])}")

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
        "", "## 7. 数据源状态", "",
        *[f"- {k}: {v}" for k, v in (s.get("source_health") or {}).items()],
        "", "## 8. AI分析要求", "",
        "不要机械复述数据。结合当时公开财经/产业新闻，重点判断指数强弱是否一致、资金集中还是扩散、热点是否持续、情绪升温还是退潮、RSI是否超买超卖，并给出未来1小时最值得观察的方向和风险信号。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/data/latest.json")
    parser.add_argument("--history", default="docs/data/history.json")
    parser.add_argument("--feed", default="docs/data/analysis-input.md")
    args = parser.parse_args()

    out = Path(args.output)
    history_path = Path(args.history)
    feed_path = Path(args.feed)
    out.parent.mkdir(parents=True, exist_ok=True)

    history: list[dict[str, Any]] = []
    if history_path.exists():
        try:
            loaded = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = loaded
        except Exception:
            pass

    snapshot = build_snapshot()
    add_hour_delta(snapshot, history)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    feed_path.write_text(markdown_feed(snapshot), encoding="utf-8")
    history.append(snapshot)
    history_path.write_text(json.dumps(history[-240:], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out}, {feed_path}, {history_path}")


if __name__ == "__main__":
    main()
