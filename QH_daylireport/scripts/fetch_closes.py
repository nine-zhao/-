#!/usr/bin/env python3
"""
OI / P / Y 期货主力合约收盘价抓取（新浪期货 hq 接口）

字段映射（实测确认）：
[0] 代码
[1] 时间 HHMMSS
[2] 今开
[3] 最高
[4] 最低
[5] 收盘价（盘后=最新价）
[6] 昨结（中国期货标准）
[7-8] 现价/现收（与 [5] 重复）
[9] 昨收
[10] 涨停价
[11] 跌停价
[12] 外盘
[13] 持仓量
[14] 成交量
[15] 成交额
"""
import json
import random
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path

# 主力合约月份（油脂期货）
MAIN_MONTHS = [1, 5, 9]


def generate_candidates(code_prefix: str) -> list[str]:
    """
    根据当前日期动态生成候选主力合约代码。

    逻辑：
      - 从去年最后一个主力月开始，往后取 5 个主力合约
      - 确保覆盖换月期（刚退出的、当前的、未来远月）
    示例：2026年6月 → [OI2601, OI2605, OI2609, OI2701, OI2705]
    """
    now = datetime.now()
    year = now.year % 100
    month = now.month

    # 生成完整的主力合约时间线（去年到明年）
    all_main = []
    for y in range(year - 1, year + 3):
        for m in MAIN_MONTHS:
            all_main.append((y, m))

    # 找到当前应为主力合约的索引（从当前月份之后的主力月份开始）
    current_idx = 0
    for i, (y, m) in enumerate(all_main):
        if y > year or (y == year and m >= month):
            current_idx = i
            break

    # 取前2个 + 当前 + 后2个 = 5个
    start = max(0, current_idx - 2)
    candidates = all_main[start:start + 5]

    return [f"{code_prefix}{y:02d}{m:02d}" for y, m in candidates]


PRODUCTS = [
    {
        "name": "菜油OI",
        "code": "OI",
        "candidates": generate_candidates("OI"),
    },
    {
        "name": "棕榈油P",
        "code": "P",
        "candidates": generate_candidates("P"),
    },
    {
        "name": "豆油Y",
        "code": "Y",
        "candidates": generate_candidates("Y"),
    },
]

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

# 交易所代码映射（东方财富 API 用）
EXCHANGE_MAP = {
    "OI": 115,  # 郑商所
    "P":  114,  # 大商所
    "Y":  114,  # 大商所
}


def sina_to_eastmoney(contract: str) -> str:
    """将新浪合约代码转为东方财富 API 格式"""
    code = contract.rstrip("0123456789")  # OI2609 → OI, P2609 → P
    if code == "OI":
        # OI2609 → OI609（郑商所用 3 位缩位，舍去年份首位数）
        return f"OI{contract[len(code) + 1:]}"
    # P2609 → p2609, Y2609 → y2609（大商所用小写 + 4 位）
    return contract.lower()


def get_code_prefix(contract: str) -> str:
    """提取合约代码中的品种字母前缀：OI2609 → OI, P2609 → P"""
    return contract.rstrip("0123456789")

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "fetch.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_get(url: str, retries: int = 3, timeout: int = 15) -> str:
    for attempt in range(1, retries + 1):
        try:
            ua = random.choice(UA_LIST)
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://finance.sina.com.cn/",
            })
            time.sleep(random.uniform(1, 3))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    return raw.decode("gbk")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="ignore")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            log(f"  HTTP attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt + random.uniform(0, 1))
    return ""


def fetch_quote(contract: str) -> dict | None:
    """拉单个合约行情"""
    url = f"https://hq.sinajs.cn/list=nf_{contract}"
    raw = http_get(url)
    if not raw:
        return None
    for line in raw.strip().split("\n"):
        if "=" not in line or "hq_str_" not in line:
            continue
        idx = line.find('="')
        if idx < 0:
            continue
        inner = line[idx + 2 :].rstrip('";\n ')
        if not inner:
            continue
        parts = inner.split(",")
        if len(parts) < 15:
            continue
        try:
            return {
                "contract": contract,
                "trade_time": parts[1],
                "open": float(parts[2]) if parts[2] else None,
                "high": float(parts[3]) if parts[3] else None,
                "low": float(parts[4]) if parts[4] else None,
                "close": float(parts[5]) if parts[5] else None,    # 收盘价
                "prev_settle": float(parts[6]) if parts[6] else None,  # 昨结
                "prev_close": float(parts[9]) if parts[9] else None,  # 昨收
                "open_interest": int(float(parts[13])) if parts[13] else 0,  # 持仓
                "volume": int(float(parts[14])) if parts[14] else 0,  # 成交
            }
        except (ValueError, IndexError) as e:
            log(f"  parse {contract}: {e}")
            return None
    return None


def fetch_quote_eastmoney(contract: str) -> dict | None:
    """从东方财富拉取单个合约行情（新浪数据不可用时作为互补源）"""
    code = get_code_prefix(contract)
    market_code = EXCHANGE_MAP.get(code)
    if not market_code:
        return None

    em_contract = sina_to_eastmoney(contract)

    params = {
        "callback": "a", "orderBy": "zdf", "sort": "desc",
        "pageSize": "500", "pageIndex": "0", "callbackName": "a",
        "token": "58b2fa8f54638b60b87d69b31969089c",
        "field": "dm,p,zsjd,o,h,l,vol,ccl,name",
        "blockName": "a",
    }
    url = f"https://futsseapi.eastmoney.com/list/{market_code}?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://futsseapi.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            text = raw[raw.index("(") + 1:raw.rindex(")")]
            data = json.loads(text)
            for item in data.get("list", []):
                dm = item.get("dm", "").lower()
                if dm == em_contract.lower():
                    return {
                        "contract": contract,
                        "close": float(item["p"]) if item.get("p") else None,
                        "prev_settle": float(item["zsjd"]) if item.get("zsjd") else None,
                        "open": float(item["o"]) if item.get("o") else None,
                        "high": float(item["h"]) if item.get("h") else None,
                        "low": float(item["l"]) if item.get("l") else None,
                        "open_interest": int(float(item["ccl"])) if item.get("ccl") else 0,
                        "volume": int(float(item["vol"])) if item.get("vol") else 0,
                    }
    except Exception as e:
        log(f"  EM {contract}: {e}")
    return None


def fetch_for_product(product: dict) -> dict:
    name = product["name"]
    code = product["code"]
    candidates = product["candidates"]
    log(f"=== {name} ({code}) ===")

    quotes = []
    for c in candidates:
        q = fetch_quote(c)
        if q and q.get("close"):
            quotes.append(q)

    if not quotes:
        log(f"  X 全部候选拉取失败")
        return {"name": name, "code": code, "error": "fetch_failed"}

    # 如果新浪持仓不可靠（全部为 0 或最高持仓过低），用东方财富补拉
    max_oi = max(q.get("open_interest", 0) for q in quotes)
    if max_oi == 0 or max_oi < 50000:
        log(f"  Sina持仓不可靠(max_oi={max_oi})，尝试东方财富补拉...")
        em_quotes = []
        for c in candidates:
            q = fetch_quote_eastmoney(c)
            if q and q.get("close"):
                em_quotes.append(q)
        if em_quotes:
            log(f"  东财补拉成功 {len(em_quotes)} 条")
            quotes = em_quotes
        else:
            log(f"  东财补拉也失败")

    # 排除持仓量为 0 的合约（未上市或已退市）
    valid_quotes = [q for q in quotes if q.get("open_interest", 0) > 0]
    if not valid_quotes:
        # 全部持仓为 0 时回退到全部候选（容错）
        log(f"  全部候选持仓为0，回退到全部候选")
        valid_quotes = quotes

    # 主力 = 持仓量最大
    main = max(valid_quotes, key=lambda x: x["open_interest"])
    log(f"  主力={main['contract']} 持仓={main['open_interest']} 收={main['close']}")

    close = main["close"]
    prev_settle = main.get("prev_settle") or 0
    if prev_settle:
        change_amt = round(close - prev_settle, 2)
        change_pct = round((close - prev_settle) / prev_settle * 100, 2)
    else:
        change_amt = 0
        change_pct = 0

    # 推断交易日期
    trade_date = datetime.now().strftime("%Y-%m-%d")

    log(f"  OK 日期={trade_date} 主力={main['contract']} 收={close} 涨跌={change_amt:+.0f}({change_pct:+.2f}%)")
    return {
        "name": name,
        "code": code,
        "main_contract": main["contract"],
        "trade_date": trade_date,
        "close": close,
        "open": main.get("open"),
        "high": main.get("high"),
        "low": main.get("low"),
        "prev_settle": prev_settle,
        "prev_close": main.get("prev_close"),
        "change_amt": change_amt,
        "change_pct": change_pct,
        "open_interest": main["open_interest"],
        "volume": main["volume"],
    }


def main():
    log("=" * 50)
    log("开始抓取 OI/P/Y 主力合约收盘价")
    results = []
    for p in PRODUCTS:
        try:
            r = fetch_for_product(p)
            results.append(r)
        except Exception as e:
            log(f"  X 异常: {e}")
            results.append({"name": p["name"], "code": p["code"], "error": str(e)})
    log("=" * 50)
    log(f"完成,共 {len(results)} 项")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
