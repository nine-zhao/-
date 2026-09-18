#!/usr/bin/env python3
"""
OI/P/Y 收盘价监控 - 写入 + 推送
- 抓数据 (fetch_closes.py 同样的逻辑)
- upsert 到多维表格 (主键 = "{主力合约}_{日期}")
- 推送到 LMSH 群 + 私聊

调用方式：
  python3 record_and_push.py morning   # 08:30 写"夜盘收"到 T-1 行
  python3 record_and_push.py noon      # 12:00 写"午盘收"到 T 行
  python3 record_and_push.py day       # 16:00 写"日盘收"到 T 行
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

# === 配置 ===
# 飞书 CLI 路径（npm 全局安装的 lark-cli）
import os, subprocess as _sp
_NODE = os.path.expandvars(r"%APPDATA%\npm\node_modules\@larksuite\cli\scripts\run.js")
LARK_CLI_CMD = [_sp.check_output("where node", shell=True, text=True).strip(), _NODE] if os.path.exists(_NODE) else ["lark-cli"]
BASE_TOKEN = "Tm2FbyJD0azcvOsjE87ceae6nkd"
TABLE_ID = "tblviLic6xHxBt7a"
LMSH_CHAT_ID = "oc_afe83337de7edcb0b030f5d03ba06b65"
USER_OPEN_ID = "ou_77d981b552a63a4dbe84db527e23ba07"

PRODUCTS_TEMPLATE = [
    {"name": "菜油OI",   "code": "OI", "select_value": "菜油OI"},
    {"name": "棕榈油P", "code": "P",  "select_value": "棕榈油P"},
    {"name": "豆油Y",   "code": "Y",  "select_value": "豆油Y"},
]

# 主力合约月份（油脂期货）
MAIN_MONTHS = [1, 5, 9]


def generate_candidates(code_prefix: str) -> list[str]:
    """根据当前日期动态生成候选主力合约代码"""
    now = datetime.now()
    year = now.year % 100
    month = now.month

    all_main = []
    for y in range(year - 1, year + 3):
        for m in MAIN_MONTHS:
            all_main.append((y, m))

    current_idx = 0
    for i, (y, m) in enumerate(all_main):
        if y > year or (y == year and m >= month):
            current_idx = i
            break

    start = max(0, current_idx - 2)
    candidates = all_main[start:start + 5]
    return [f"{code_prefix}{y:02d}{m:02d}" for y, m in candidates]


def build_products() -> list[dict]:
    """构建完整产品列表（注入动态候选合约）"""
    products = []
    for p in PRODUCTS_TEMPLATE:
        item = dict(p)
        item["candidates"] = generate_candidates(p["code"])
        products.append(item)
    return products


PRODUCTS = build_products()

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "main.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ===== 数据抓取 =====
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0.0.0",
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
        return f"OI{contract[len(code) + 1:]}"  # OI2609 → OI609
    return contract.lower()  # P2609 → p2609


def get_code_prefix(contract: str) -> str:
    """提取合约代码中的品种字母前缀：OI2609 → OI, P2609 → P"""
    return contract.rstrip("0123456789")

def http_get(url: str, retries: int = 3, timeout: int = 15) -> str:
    for attempt in range(1, retries + 1):
        try:
            ua = random.choice(UA_LIST)
            req = urllib.request.Request(url, headers={
                "User-Agent": ua, "Accept": "*/*", "Referer": "https://finance.sina.com.cn/"
            })
            time.sleep(random.uniform(1, 3))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try: return raw.decode("gbk")
                except UnicodeDecodeError: return raw.decode("utf-8", errors="ignore")
        except Exception as e:
            log(f"  HTTP {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt + random.uniform(0, 1))
    return ""


def fetch_quote(contract: str) -> dict | None:
    url = f"https://hq.sinajs.cn/list=nf_{contract}"
    raw = http_get(url)
    if not raw: return None
    for line in raw.strip().split("\n"):
        if "=" not in line or "hq_str_" not in line: continue
        idx = line.find('="')
        if idx < 0: continue
        inner = line[idx + 2:].rstrip('";\n ')
        if not inner: continue
        parts = inner.split(",")
        if len(parts) < 15: continue
        try:
            return {
                "contract": contract,
                "open": float(parts[2]) if parts[2] else None,
                "high": float(parts[3]) if parts[3] else None,
                "low": float(parts[4]) if parts[4] else None,
                "close": float(parts[5]) if parts[5] else None,
                "prev_settle": float(parts[6]) if parts[6] else None,
                "open_interest": int(float(parts[13])) if parts[13] else 0,
                "volume": int(float(parts[14])) if parts[14] else 0,
            }
        except (ValueError, IndexError):
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
    quotes = []
    for c in product["candidates"]:
        q = fetch_quote(c)
        if q and q.get("close"):
            quotes.append(q)
    if not quotes:
        return {"name": product["name"], "code": product["code"], "error": "fetch_failed"}

    # 如果新浪持仓不可靠（全部为 0 或最高持仓过低），用东方财富补拉
    max_oi = max(q.get("open_interest", 0) for q in quotes)
    if max_oi == 0 or max_oi < 50000:
        log(f"  {product['name']} Sina持仓不可靠(max_oi={max_oi})，尝试东方财富补拉...")
        em_quotes = []
        for c in product["candidates"]:
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
        log(f"  {product['name']} 全部候选持仓为0，回退到全部候选")
        valid_quotes = quotes

    main = max(valid_quotes, key=lambda x: x["open_interest"])
    close = main["close"]
    prev_settle = main.get("prev_settle") or 0
    if prev_settle:
        change_amt = round(close - prev_settle, 2)
        change_pct = round((close - prev_settle) / prev_settle * 100, 2)
    else:
        change_amt, change_pct = 0, 0
    return {
        "name": product["name"], "code": product["code"],
        "main_contract": main["contract"], "close": close,
        "open": main.get("open"), "high": main.get("high"), "low": main.get("low"),
        "prev_settle": prev_settle, "change_amt": change_amt, "change_pct": change_pct,
        "open_interest": main["open_interest"], "volume": main["volume"],
    }


# ===== 表格 upsert =====
def run_cli(args: list[str]) -> dict:
    """运行 lark-cli 命令"""
    r = subprocess.run(LARK_CLI_CMD + args, capture_output=True, timeout=60)
    stdout = r.stdout.decode("utf-8", errors="replace")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        log(f"CLI not JSON: {stdout[:300]} | stderr: {r.stderr[:200]}")
        return {"ok": False, "error": "non_json", "raw": stdout}


def search_record_by_key(contract: str, target_date: str) -> str | None:
    """按合约代码 + 日期查找现有记录"""
    r = run_cli([
        "base", "+record-search",
        "--as", "user",
        "--base-token", BASE_TOKEN,
        "--table-id", TABLE_ID,
        "--search-field", "主力合约代码",
        "--keyword", contract,
        "--format", "json",
    ])
    if not r.get("ok"):
        return None
    data = r.get("data", {})
    fields = data.get("fields", [])
    records = data.get("data", [])
    record_ids = data.get("record_id_list", [])
    try:
        code_idx = fields.index("主力合约代码")
        date_idx = fields.index("日期")
    except ValueError:
        return None
    # 日期在 API 返回中为 "2026-06-24 00:00:00" 格式
    date_str = f"{target_date} 00:00:00"
    for i, row in enumerate(records):
        if i < len(record_ids) and row and len(row) > max(code_idx, date_idx):
            row_code = str(row[code_idx] or "")
            # code 匹配：兼容旧数据（含日期后缀）和新数据（纯合约号）
            if row_code.split("_")[0] == contract and str(row[date_idx] or "") == date_str:
                return record_ids[i]
    return None


def upsert_record(session: str, data: dict) -> tuple[bool, str]:
    """根据 session (morning/noon/day) 写入对应字段"""
    name = data["name"]
    code = data["code"]
    contract = data["main_contract"]
    close = data["close"]
    change_amt = data["change_amt"]
    change_pct = data["change_pct"]

    # 计算日期归属
    today = datetime.now()
    if session == "morning":
        # 夜盘收 → 写 T-1
        target_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        # 午盘/日盘收 → 写 T
        target_date = today.strftime("%Y-%m-%d")

    # 字段映射
    fields = {
        "主力合约代码": contract,  # 仅合约号，便于数据聚合
        "日期": int(datetime.strptime(target_date, "%Y-%m-%d").timestamp() * 1000),  # datetime 用毫秒
        "品种": data["select_value"],
        "抓取时间": int(today.timestamp() * 1000),
    }

    if session == "morning":
        fields["夜盘收"] = close
        fields["夜盘涨跌"] = change_pct
    elif session == "noon":
        fields["午盘收"] = close
        fields["午盘涨跌"] = change_pct
    elif session == "day":
        fields["日盘收"] = close
        fields["日盘涨跌"] = change_pct

    # 查找现有记录
    record_id = search_record_by_key(contract, target_date)

    if record_id:
        # update
        log(f"  更新 {name} {contract} ({target_date}) -> {session} 收 {close}")
        r = run_cli([
            "base", "+record-upsert",
            "--as", "user",
            "--base-token", BASE_TOKEN,
            "--table-id", TABLE_ID,
            "--record-id", record_id,
            "--json", json.dumps(fields, ensure_ascii=False),
        ])
        if r.get("ok"):
            return True, "updated"
        log(f"  更新失败: {r}")
        return False, "update_failed"
    else:
        # create
        log(f"  创建 {name} {contract} ({target_date}) -> {session} 收 {close}")
        r = run_cli([
            "base", "+record-upsert",
            "--as", "user",
            "--base-token", BASE_TOKEN,
            "--table-id", TABLE_ID,
            "--json", json.dumps(fields, ensure_ascii=False),
        ])
        if r.get("ok"):
            return True, "created"
        log(f"  创建失败: {r}")
        return False, "create_failed"


# ===== 飞书推送 =====
def push_to_feishu(session: str, results: list[dict]) -> None:
    """推送到 LMSH 群和私聊"""
    title_map = {
        "morning": "🌙 昨日夜盘收盘价（早报）",
        "noon": "☀️ 今日午盘收盘价",
        "day": "🌆 今日日盘收盘价",
    }
    title = title_map[session]
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [f"**{title}**  ·  {today}", ""]
    for r in results:
        if r.get("error"):
            lines.append(f"- **{r.get('name', '?')}**：拉取失败 ({r.get('error')})")
            continue
        sign = "📈" if r["change_pct"] >= 0 else "📉"
        lines.append(
            f"- **{r['name']}** `{r['main_contract']}` · **{r['close']:.0f}** "
            f"{sign} {r['change_amt']:+.0f} ({r['change_pct']:+.2f}%) · 持仓 {r['open_interest']:,}"
        )

    lines.append("")
    lines.append(f"[打开表格](https://kcnerdao2typ.feishu.cn/base/{BASE_TOKEN})")

    text = "\n".join(lines)

    for target_kind, target_id in [("chat", LMSH_CHAT_ID), ("user", USER_OPEN_ID)]:
        args = LARK_CLI_CMD + [
            "im", "+messages-send",
            "--as", "bot",
            "--markdown", text,
        ]
        if target_kind == "chat":
            args.extend(["--chat-id", target_id])
        else:
            args.extend(["--user-id", target_id])
        log(f"  推送{target_kind}开始...")
        r = subprocess.run(args, capture_output=True, timeout=30)
        stdout = r.stdout.decode("utf-8", errors="replace")
        try:
            j = json.loads(stdout)
            if j.get("ok"):
                log(f"  推送{target_kind}成功")
            else:
                log(f"  推送{target_kind}失败: {j}")
        except json.JSONDecodeError:
            log(f"  推送{target_kind}异常: stdout=「{stdout[:300]}」 stderr=「{r.stderr[:200]}」")


# ===== 主流程 =====
def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("morning", "noon", "day"):
        log("用法: record_and_push.py morning|noon|day")
        sys.exit(1)
    session = sys.argv[1]
    log("=" * 50)
    log(f"开始执行 [{session}]")

    # 1. 抓取
    results = []
    for p in PRODUCTS:
        try:
            r = fetch_for_product(p)
            results.append(r)
        except Exception as e:
            log(f"  {p['name']} 异常: {e}")
            results.append({"name": p["name"], "code": p["code"], "error": str(e)})

    # 加 select_value 字段
    for p, r in zip(PRODUCTS, results):
        r["select_value"] = p["select_value"]

    log(f"  抓取完成: {len([r for r in results if not r.get('error')])}/{len(results)} 成功")

    # 2. 写入表格
    if session in ("morning", "noon", "day"):
        log("  写入表格...")
        for r in results:
            if r.get("error"):
                continue
            ok, status = upsert_record(session, r)
            r["upsert_status"] = status

    # 3. 推送
    log("  推送...")
    push_to_feishu(session, results)

    log("=" * 50)
    log(f"完成 [{session}]")


if __name__ == "__main__":
    main()
