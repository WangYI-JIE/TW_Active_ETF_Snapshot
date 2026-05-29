#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股主動式ETF 每日持股 + 收盤價抓取器 (GitHub Actions 版)
v12:
 - 🆕 名稱補回: 當 MoneyDJ 沒有股票代號時，用股票名稱比對
   當日其他 ETF 持股 + 前日快照，自動補回代號與持股
   (修復: MoneyDJ 補上代號後不會誤判成買超)
 - 出清股票補抓: 把「前日有持股、今日已出清」的股票也加進收盤價抓取清單
 - 失敗 ETF 單獨重試 (最多 2 輪, 每輪間隔 15 秒)
 - 用 MoneyDJ 的 holdings_date 眾數當檔名日期, 避免 cron 延遲跨日
 - 防呆: holdings_date 全 None / 未開盤日 / ETF 數量異常
 - regex 寬鬆匹配全形冒號
"""

import re
import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup


ETFS = [
    "00980A", "00981A", "00982A", "00984A", "00985A",
    "00987A", "00991A", "00992A", "00993A", "00994A",
    "00995A", "00996A", "00400A", "00401A", "00999A",
    "00403A",
]

MONEYDJ_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0007B.xdjhtm?etfid={code}.TW"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# =============================================================
# MoneyDJ 持股抓取
# =============================================================
def fetch_etf_holdings(etf_code, session, retries=3):
    url = MONEYDJ_URL.format(code=etf_code)
    for attempt in range(retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            if r.apparent_encoding:
                r.encoding = r.apparent_encoding
            break
        except Exception as e:
            if attempt < retries:
                time.sleep(3 + attempt * 2)
            else:
                raise RuntimeError(f"HTTP 失敗: {e}")

    soup = BeautifulSoup(r.text, "html.parser")
    text_all = soup.get_text(" ", strip=True)

    etf_name = etf_code
    if soup.title:
        title_text = soup.title.get_text()
        m = re.match(r"^(.+?)-" + re.escape(etf_code) + r"\.TW", title_text)
        if m:
            etf_name = m.group(1).strip()
    if etf_name == etf_code:
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]+?)[〈<\(]" + re.escape(etf_code) + r"\.TW[〉>\)]", text_all)
        if m:
            etf_name = m.group(1).strip()
    if etf_name == etf_code:
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9\-]+?)\(" + re.escape(etf_code) + r"\.TW\)\s*-\s*全部持股", text_all)
        if m:
            etf_name = m.group(1).strip()

    m = re.search(r"資料日期[^\d]{0,5}(\d{4}/\d{1,2}/\d{1,2})", text_all)
    holdings_date = m.group(1) if m else None

    if holdings_date is None:
        print(f"\n      [DATE DEBUG {etf_code}] 抓不到 holdings_date")
        print(f"      [DATE DEBUG {etf_code}] 頁面長度={len(r.text)}, 狀態={r.status_code}")
        print(f"      [DATE DEBUG {etf_code}] 含 '資料日期': {'資料日期' in text_all}")
        date_matches = re.findall(r"\d{4}/\d{1,2}/\d{1,2}", text_all[:5000])
        print(f"      [DATE DEBUG {etf_code}] 前 5000 字內找到的日期樣式: {date_matches[:5]}")
        idx = text_all.find("資料日期")
        if idx >= 0:
            print(f"      [DATE DEBUG {etf_code}] '資料日期' 附近 100 字: ...{text_all[idx:idx+100]}...")
        else:
            print(f"      [DATE DEBUG {etf_code}] 找不到 '資料日期' 四個字 (可能頁面改版或不完整)")

    target_table = None
    for table in soup.find_all("table"):
        headers_text = " ".join(th.get_text(strip=True) for th in table.find_all("th"))
        if "個股名稱" in headers_text and "持有股數" in headers_text:
            target_table = table
            break

    if target_table is None:
        print(f"      [DEBUG {etf_code}] HTTP 狀態={r.status_code}, 回應長度={len(r.text)}")
        print(f"      [DEBUG {etf_code}] 含 '持有股數': {'持有股數' in r.text}")
        print(f"      [DEBUG {etf_code}] 含 '個股名稱': {'個股名稱' in r.text}")
        print(f"      [DEBUG {etf_code}] 含 'Cloudflare': {('Cloudflare' in r.text) or ('cloudflare' in r.text)}")
        print(f"      [DEBUG {etf_code}] 含 'cf-ray': {'cf-ray' in r.text.lower()}")
        print(f"      [DEBUG {etf_code}] 前 300 字元: {r.text[:300]}")
        raise ValueError("找不到持股表格")

    holdings = []
    skipped_rows = []

    for tr in target_table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue

        first_cell = tds[0]
        stock_cell_text = first_cell.get_text(strip=True)

        if "個股名稱" in stock_cell_text or not stock_cell_text:
            continue

        m2 = re.match(r"(.+?)\(([0-9A-Z]+)\.TW\)", stock_cell_text)

        if not m2:
            link = first_cell.find("a")
            if link and link.get("href"):
                href = link["href"]
                href_match = re.search(r"etfid=([0-9A-Z]+)\.TW", href)
                if href_match:
                    stock_code = href_match.group(1)
                    stock_name = stock_cell_text.rstrip("*").strip() or "未知"
                    m2 = True
                    try:
                        weight = float(tds[1].get_text(strip=True))
                        shares = int(tds[2].get_text(strip=True).replace(",", "").replace(" ", ""))
                    except (ValueError, AttributeError):
                        skipped_rows.append({
                            "raw_text": stock_cell_text,
                            "weight_raw": tds[1].get_text(strip=True) if len(tds) > 1 else "",
                            "shares_raw": tds[2].get_text(strip=True) if len(tds) > 2 else "",
                            "reason": "權重或股數無法解析"
                        })
                        continue
                    lots = shares // 1000
                    if lots < 1:
                        continue
                    holdings.append({
                        "code": stock_code,
                        "name": stock_name,
                        "lots": lots,
                        "weight": round(weight, 2),
                    })
                    continue

        if not m2:
            try:
                weight_raw = tds[1].get_text(strip=True)
                shares_raw = tds[2].get_text(strip=True)
            except Exception:
                weight_raw = ""
                shares_raw = ""
            skipped_rows.append({
                "raw_text": stock_cell_text or "(空白)",
                "weight_raw": weight_raw,
                "shares_raw": shares_raw,
                "reason": "無代號格式"
            })
            continue

        if m2 is not True:
            stock_name = m2.group(1).strip().rstrip("*").strip()
            stock_code = m2.group(2)
            try:
                weight = float(tds[1].get_text(strip=True))
                shares = int(tds[2].get_text(strip=True).replace(",", "").replace(" ", ""))
            except (ValueError, AttributeError):
                skipped_rows.append({
                    "raw_text": stock_cell_text,
                    "weight_raw": tds[1].get_text(strip=True),
                    "shares_raw": tds[2].get_text(strip=True),
                    "reason": "權重或股數無法解析"
                })
                continue
            lots = shares // 1000
            if lots < 1:
                continue
            holdings.append({
                "code": stock_code,
                "name": stock_name,
                "lots": lots,
                "weight": round(weight, 2),
            })

    return {
        "name": etf_name,
        "holdings_date": holdings_date,
        "holdings": holdings,
        "skipped_rows": skipped_rows,
    }


# =============================================================
# 🆕 v12: 名稱補回
# 用當日其他 ETF 持股 + 前日快照建立 名稱→代號 對照表
# 對「無代號格式」的 skipped rows 嘗試補回
# =============================================================
def build_name_code_map(all_etf_data, prev_snapshot):
    """建立 名稱 → 股票代號 對照表"""
    name_map = {}

    # 1. 前日快照（較低優先級）
    if prev_snapshot:
        for etf_data in (prev_snapshot.get("today") or {}).values():
            for h in (etf_data.get("holdings") or []):
                name = h.get("name", "").strip().rstrip("*").strip()
                code = h.get("code", "")
                if name and code:
                    name_map[name] = code

    # 2. 當日已成功解析的持股（較高優先級，覆蓋前日）
    for etf_data in all_etf_data.values():
        for h in etf_data.get("holdings", []):
            name = h.get("name", "").strip().rstrip("*").strip()
            code = h.get("code", "")
            if name and code:
                name_map[name] = code

    return name_map


def recover_skipped_by_name(all_etf_data, name_code_map, all_stock_codes):
    """
    對所有「無代號格式」的 skipped rows，嘗試用名稱查代號補回持股。
    查到 + 能解析張數 → 補回 holdings，從 skipped_rows 移除。
    查不到 → 保留在 skipped_rows（接受一天沒資料）。
    """
    total_recovered = 0
    recovered_detail = []

    for etf_code, etf_data in all_etf_data.items():
        new_skipped = []
        for row in etf_data.get("skipped_rows", []):
            if row["reason"] != "無代號格式":
                new_skipped.append(row)
                continue

            name = row["raw_text"].strip().rstrip("*").strip()
            code = name_code_map.get(name)

            if not code:
                # 查不到，保留 skipped
                new_skipped.append(row)
                continue

            # 嘗試解析張數
            try:
                weight = float(row["weight_raw"])
                shares = int(row["shares_raw"].replace(",", "").replace(" ", ""))
                lots = shares // 1000
                if lots < 1:
                    new_skipped.append(row)
                    continue
            except (ValueError, AttributeError):
                new_skipped.append(row)
                continue

            # 補回持股
            etf_data["holdings"].append({
                "code": code,
                "name": name,
                "lots": lots,
                "weight": round(weight, 2),
            })
            all_stock_codes.add(code)
            total_recovered += 1
            recovered_detail.append(f"{etf_code} '{name}' → {code} ({lots} 張)")
            # 不加回 new_skipped

        etf_data["skipped_rows"] = new_skipped

    if total_recovered > 0:
        print(f"\n  🔧 名稱補回: 成功補回 {total_recovered} 筆")
        for d in recovered_detail:
            print(f"     ✓ {d}")

    return total_recovered


# =============================================================
# yfinance (.TW -> .TWO)
# =============================================================
def fetch_prices_bulk_yfinance(codes):
    prices = {}
    try:
        import yfinance as yf
    except ImportError:
        print("  [yfinance] 未安裝, 跳過")
        return prices

    def _batch(suffix, code_list):
        got = {}
        if not code_list:
            return got
        tickers = [f"{c}{suffix}" for c in code_list]
        try:
            df = yf.download(
                " ".join(tickers),
                period="5d", interval="1d",
                progress=False, threads=True, auto_adjust=False,
            )
        except Exception as e:
            print(f"  [yfinance{suffix}] 批次下載失敗: {e}")
            return got

        if df is None or df.empty:
            return got

        try:
            if len(code_list) == 1:
                if "Close" in df.columns:
                    v = df["Close"].dropna()
                    if not v.empty:
                        got[code_list[0]] = round(float(v.iloc[-1]), 2)
            else:
                close = df["Close"]
                for code in code_list:
                    t = f"{code}{suffix}"
                    if t in close.columns:
                        v = close[t].dropna()
                        if not v.empty:
                            got[code] = round(float(v.iloc[-1]), 2)
        except Exception as e:
            print(f"  [yfinance{suffix}] 解析失敗: {e}")
        return got

    print(f"  [yfinance.TW ] 嘗試 {len(codes)} 檔...", end="", flush=True)
    tw_got = _batch(".TW", codes)
    prices.update(tw_got)
    print(f" 取得 {len(tw_got)} 檔")

    missing = [c for c in codes if c not in prices]
    if missing:
        print(f"  [yfinance.TWO] 嘗試 {len(missing)} 檔 (上櫃)...", end="", flush=True)
        two_got = _batch(".TWO", missing)
        prices.update(two_got)
        print(f" 取得 {len(two_got)} 檔")

    return prices


# =============================================================
# TPEx openapi
# =============================================================
_TPEX_CACHE = None
def fetch_all_tpex_prices(session, headers):
    global _TPEX_CACHE
    if _TPEX_CACHE is not None:
        return _TPEX_CACHE
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    try:
        r = session.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            print(f"  [TPEx openapi] HTTP {r.status_code}")
            _TPEX_CACHE = {}
            return _TPEX_CACHE
        arr = r.json()
        result = {}
        for row in arr:
            code = row.get("SecuritiesCompanyCode", "").strip()
            close = row.get("Close", "").strip()
            if code and close:
                try:
                    result[code] = round(float(close.replace(",", "")), 2)
                except ValueError:
                    continue
        _TPEX_CACHE = result
        print(f"  [TPEx openapi] 建 cache: {len(result)} 檔上櫃股")
        return result
    except Exception as e:
        print(f"  [TPEx openapi] 失敗: {e}")
        _TPEX_CACHE = {}
        return _TPEX_CACHE


_TWSE_CACHE = None
def fetch_all_twse_prices(session, headers):
    global _TWSE_CACHE
    if _TWSE_CACHE is not None:
        return _TWSE_CACHE
    for back in range(7):
        d = datetime.now() - timedelta(days=back)
        date_str = d.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
        try:
            r = session.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            if data.get("stat") != "OK":
                continue
            tables = data.get("tables", [])
            if not tables:
                rows = data.get("data9") or data.get("data8") or []
                fields = data.get("fields9") or data.get("fields8") or []
            else:
                target = None
                for t in tables:
                    if "收盤價" in t.get("fields", []) and "證券代號" in t.get("fields", []):
                        target = t
                        break
                if not target:
                    continue
                rows = target.get("data", [])
                fields = target.get("fields", [])
            if not rows or not fields:
                continue
            try:
                idx_code = fields.index("證券代號")
                idx_close = fields.index("收盤價")
            except ValueError:
                continue
            result = {}
            for row in rows:
                try:
                    code = row[idx_code].strip()
                    close = row[idx_close].replace(",", "").strip()
                    if close in ("--", "", "---"):
                        continue
                    result[code] = round(float(close), 2)
                except (ValueError, IndexError, AttributeError):
                    continue
            if result:
                _TWSE_CACHE = result
                print(f"  [TWSE MI_INDEX] {date_str} 建 cache: {len(result)} 檔上市股")
                return result
        except Exception:
            continue
    print(f"  [TWSE MI_INDEX] 7 天內皆失敗")
    _TWSE_CACHE = {}
    return _TWSE_CACHE


# =============================================================
# Yahoo HTML 單檔
# =============================================================
def fetch_price_yahoo_html(code, session, headers):
    for suffix in (".TW", ".TWO"):
        url = f"https://tw.stock.yahoo.com/quote/{code}{suffix}"
        try:
            r = session.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            m = re.search(r'"regularMarketPrice"\s*:\s*\{[^}]*?"raw"\s*:\s*([\d.]+)', r.text)
            if m:
                return round(float(m.group(1)), 2), suffix
            soup = BeautifulSoup(r.text, "html.parser")
            span = soup.find("span", class_=re.compile(r"Fz\(32px\)"))
            if span:
                txt = span.get_text(strip=True).replace(",", "")
                try:
                    return round(float(txt), 2), suffix
                except ValueError:
                    pass
        except Exception:
            continue
    return None, None


# =============================================================
# 主 orchestrator
# =============================================================
def fetch_all_prices(all_stock_codes, session, headers):
    codes = sorted(all_stock_codes)
    print(f"\n[價格抓取] 目標 {len(codes)} 檔")
    prices = fetch_prices_bulk_yfinance(codes)
    missing = [c for c in codes if c not in prices]
    print(f"  小計: {len(prices)}/{len(codes)}  缺 {len(missing)} 檔")
    if not missing:
        return prices
    print(f"\n  [官方 API 批次] 建 cache...")
    twse_map = fetch_all_twse_prices(session, headers)
    tpex_map = fetch_all_tpex_prices(session, headers)
    hit_twse, hit_tpex = 0, 0
    for c in list(missing):
        if c in twse_map:
            prices[c] = twse_map[c]; hit_twse += 1
        elif c in tpex_map:
            prices[c] = tpex_map[c]; hit_tpex += 1
    print(f"  TWSE API 補: {hit_twse} 檔, TPEx API 補: {hit_tpex} 檔")
    missing = [c for c in codes if c not in prices]
    print(f"  小計: {len(prices)}/{len(codes)}  缺 {len(missing)} 檔")
    if not missing:
        return prices
    print(f"\n  [Yahoo HTML] 單檔 fallback ({len(missing)} 檔)")
    for c in missing:
        p, suffix = fetch_price_yahoo_html(c, session, headers)
        if p:
            prices[c] = p
            print(f"    {c}{suffix} -> {p}")
        time.sleep(0.3)
    final_missing = [c for c in codes if c not in prices]
    print(f"\n  === 最終: {len(prices)}/{len(codes)} ===")
    if final_missing:
        print(f"  ❌ 仍缺: {', '.join(final_missing)}")
    return prices


# =============================================================
# 前一日快照
# =============================================================
def find_prev_snapshot(out_dir, today_date):
    candidates = []
    for f in out_dir.glob("snapshot-*.json"):
        m = re.match(r"snapshot-(\d{4}-\d{2}-\d{2})\.json$", f.name)
        if m and m.group(1) < today_date:
            candidates.append((m.group(1), f))
    if not candidates:
        return None, None
    candidates.sort(reverse=True)
    date_str, path = candidates[0]
    try:
        return date_str, json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None


# =============================================================
# main
# =============================================================
def main():
    out_dir = Path("snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)

    today_date = datetime.now().strftime("%Y-%m-%d")
    print(f"\n=== 台股主動式ETF 追蹤器 v12 (系統時間: {today_date}) ===\n", flush=True)

    prev_date, prev_snapshot = find_prev_snapshot(out_dir, today_date)
    if prev_snapshot:
        print(f"前一日快照: {prev_date}")
    else:
        print("前一日快照: 無 (首次執行)")

    print(f"\n[1/3] 抓取 MoneyDJ 持股 ({len(ETFS)} 檔)", flush=True)
    session = requests.Session()
    all_etf_data = {}
    all_stock_codes = set()
    failed = []
    all_skipped = []

    for i, code in enumerate(ETFS, 1):
        print(f"  [{i:2d}/{len(ETFS)}] {code}  ", end="", flush=True)
        try:
            data = fetch_etf_holdings(code, session)
            all_etf_data[code] = data
            for h in data["holdings"]:
                all_stock_codes.add(h["code"])
            skipped_count = len(data.get("skipped_rows", []))
            warn_mark = f"  ⚠️ 跳過 {skipped_count} 列" if skipped_count > 0 else ""
            print(f"OK  {data['name'][:20]:20s}  ({data['holdings_date']})  {len(data['holdings']):3d} 檔{warn_mark}", flush=True)
            if skipped_count > 0:
                for row in data["skipped_rows"]:
                    print(f"      └─ 異常列: '{row['raw_text']}' | 權重={row['weight_raw']} | 股數={row['shares_raw']} | 原因={row['reason']}")
        except Exception as e:
            print(f"FAIL  {e}", flush=True)
            failed.append(code)
        if i < len(ETFS):
            time.sleep(2)

    # 失敗重試
    if failed:
        print(f"\n  ⚠️ 主迴圈完成, 有 {len(failed)} 檔失敗: {', '.join(failed)}", flush=True)
        for retry_round in range(1, 3):
            still_failed = list(failed)
            if not still_failed:
                break
            print(f"\n  ⟳ 重試第 {retry_round}/2 輪 ({len(still_failed)} 檔): {', '.join(still_failed)}", flush=True)
            print(f"     等候 15 秒讓 MoneyDJ 喘息...", flush=True)
            time.sleep(15)
            newly_succeeded = []
            for code in still_failed:
                print(f"     重試 {code}  ", end="", flush=True)
                try:
                    data = fetch_etf_holdings(code, session)
                    all_etf_data[code] = data
                    for h in data["holdings"]:
                        all_stock_codes.add(h["code"])
                    skipped_count = len(data.get("skipped_rows", []))
                    warn_mark = f"  ⚠️ 跳過 {skipped_count} 列" if skipped_count > 0 else ""
                    print(f"OK  {data['name'][:20]:20s}  ({data['holdings_date']})  {len(data['holdings']):3d} 檔{warn_mark}", flush=True)
                    newly_succeeded.append(code)
                except Exception as e:
                    print(f"FAIL  {e}", flush=True)
                time.sleep(3)
            for code in newly_succeeded:
                if code in failed:
                    failed.remove(code)
            if not failed:
                print(f"\n  ✅ 重試成功, 所有 ETF 都抓到了", flush=True)
                break
        if failed:
            print(f"\n  ❌ 重試後仍失敗: {', '.join(failed)}", flush=True)

    # ========================================================
    # 🆕 v12: 名稱補回（在 holdings_date 計算之前執行）
    # ========================================================
    print(f"\n  [名稱補回] 建立 名稱→代號 對照表...", flush=True)
    name_code_map = build_name_code_map(all_etf_data, prev_snapshot)
    print(f"  [名稱補回] 對照表共 {len(name_code_map)} 筆", flush=True)
    recover_skipped_by_name(all_etf_data, name_code_map, all_stock_codes)

    # 補回後重新統計 all_skipped
    for code, data in all_etf_data.items():
        skipped_count = len(data.get("skipped_rows", []))
        if skipped_count > 0:
            all_skipped.append({
                "etf": code,
                "etf_name": data["name"],
                "rows": data["skipped_rows"],
            })

    # holdings_date 眾數
    today_holdings_dates = [d["holdings_date"] for d in all_etf_data.values() if d.get("holdings_date")]
    most_common_today_hd = max(set(today_holdings_dates), key=today_holdings_dates.count) if today_holdings_dates else None

    prev_holdings_dates = []
    if prev_snapshot:
        for etf_data in (prev_snapshot.get("today") or {}).values():
            hd = etf_data.get("holdings_date")
            if hd:
                prev_holdings_dates.append(hd)
    most_common_prev_hd = max(set(prev_holdings_dates), key=prev_holdings_dates.count) if prev_holdings_dates else None

    print(f"\n  本次 holdings_date 眾數: {most_common_today_hd}")
    print(f"  上次 holdings_date 眾數: {most_common_prev_hd}")

    # 防呆 1
    if most_common_today_hd is None:
        print(f"\n{'='*60}\n🛑 holdings_date 全部抓不到\n{'='*60}\n  -> 為避免寫出無日期的快照, 本次不寫檔\n{'='*60}\n")
        return

    # 用 holdings_date 當檔名日期
    original_today_date = today_date
    today_date = most_common_today_hd.replace("/", "-")
    if original_today_date != today_date:
        print(f"\n  ℹ️ 系統時間是 {original_today_date}, 但 MoneyDJ 資料日期是 {today_date}")
        prev_date, prev_snapshot = find_prev_snapshot(out_dir, today_date)
        if prev_snapshot:
            print(f"  ℹ️ 重新載入前一日快照: {prev_date}")
        prev_holdings_dates = []
        if prev_snapshot:
            for etf_data in (prev_snapshot.get("today") or {}).values():
                hd = etf_data.get("holdings_date")
                if hd:
                    prev_holdings_dates.append(hd)
        most_common_prev_hd = max(set(prev_holdings_dates), key=prev_holdings_dates.count) if prev_holdings_dates else None

    # 防呆 2: 未開盤日
    has_any_update = False
    update_detail = []
    if prev_snapshot:
        prev_today = prev_snapshot.get("today", {}) or {}
        for etf_code, today_data in all_etf_data.items():
            today_hd = today_data.get("holdings_date")
            prev_hd = (prev_today.get(etf_code) or {}).get("holdings_date")
            if today_hd and (not prev_hd or today_hd > prev_hd):
                has_any_update = True
                update_detail.append(f"{etf_code}: {prev_hd or '(無)'} -> {today_hd}")
    else:
        has_any_update = True
        update_detail.append("(首次執行, 無上次快照)")

    if not has_any_update:
        print(f"\n{'='*60}\n🛑 台股未開盤日偵測 (無任何 ETF 有新資料)\n{'='*60}\n  -> 本次不寫檔\n{'='*60}\n")
        return

    print(f"\n  有新資料的 ETF ({len(update_detail)} 檔):")
    for d in update_detail[:5]:
        print(f"    {d}")
    if len(update_detail) > 5:
        print(f"    ...(還有 {len(update_detail)-5} 檔)")

    # 防呆 3: ETF 數量異常
    today_etf_count = len(all_etf_data)
    prev_etf_count = len((prev_snapshot or {}).get("today") or {})
    if prev_etf_count > 0 and today_etf_count < prev_etf_count:
        today_keys = set(all_etf_data.keys())
        prev_keys = set((prev_snapshot or {}).get("today", {}).keys())
        missing_etfs = prev_keys - today_keys
        print(f"\n{'='*60}\n🛑 ETF 數量異常偵測\n{'='*60}")
        print(f"  本次抓到: {today_etf_count} 檔, 上次快照: {prev_etf_count} 檔")
        print(f"  失聯 ETF: {', '.join(sorted(missing_etfs))}")
        print(f"  -> 為避免污染 latest.json, 本次不寫檔\n{'='*60}\n")
        return

    # 出清股票補抓
    today_only_count = len(all_stock_codes)
    if prev_snapshot:
        prev_today = prev_snapshot.get("today", {}) or {}
        prev_stock_codes = set()
        for etf_code, etf_data in prev_today.items():
            for h in (etf_data.get("holdings") or []):
                prev_stock_codes.add(h["code"])
        added_for_clearance = prev_stock_codes - all_stock_codes
        all_stock_codes.update(prev_stock_codes)
        if added_for_clearance:
            print(f"\n  📦 出清股票補抓: 加入前日有持股、今日已全部出清的 {len(added_for_clearance)} 檔")
            print(f"     (今日持股 {today_only_count} 檔 + 補抓 {len(added_for_clearance)} 檔 = 共 {len(all_stock_codes)} 檔需抓收盤價)")

    # 抓收盤價
    print(f"\n[2/3] 抓取收盤價", flush=True)
    prices = {}
    if all_stock_codes:
        prices = fetch_all_prices(all_stock_codes, session, HEADERS)

    # 組合快照並儲存
    print(f"\n[3/3] 組合快照並儲存", flush=True)
    snapshot = {
        "today_date": today_date,
        "prev_date": prev_date,
        "prices": prices,
        "today": {
            code: {
                "name": data["name"],
                "holdings_date": data["holdings_date"],
                "holdings": data["holdings"],
            }
            for code, data in all_etf_data.items()
        },
        "prev": (prev_snapshot or {}).get("today", {}),
    }

    out_file = out_dir / f"snapshot-{today_date}.json"
    latest_file = Path("latest.json")
    out_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"執行結果")
    print(f"{'='*60}")
    print(f"  檔名日期:   snapshot-{today_date}.json")
    print(f"  ETF 成功:   {len(snapshot['today'])}/{len(ETFS)}")
    if failed:
        print(f"  ETF 失敗:   {', '.join(failed)}")
    print(f"  股票數:     {len(all_stock_codes)}")
    print(f"  價格數:     {len(prices)}/{len(all_stock_codes)}")
    print(f"  異常列總數: {sum(len(x['rows']) for x in all_skipped)}")

    if all_skipped:
        print(f"\n{'='*60}")
        print(f"⚠️  資料完整性警告 ({len(all_skipped)} 檔 ETF 有異常列，名稱補回後仍無法解析)")
        print(f"{'='*60}")
        for item in all_skipped:
            print(f"\n📌 {item['etf']} ({item['etf_name']}):")
            for row in item["rows"]:
                print(f"   - '{row['raw_text']}' | 權重 {row['weight_raw']}% | 股數 {row['shares_raw']} | {row['reason']}")
        print(f"\n建議: 到 MoneyDJ 網頁核對這些 ETF 後，手動補資料或通知我修正爬蟲\n")
    else:
        print(f"\n✅ 資料完整性:  無異常列\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[!] 執行失敗: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
