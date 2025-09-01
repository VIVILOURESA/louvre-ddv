# ddv_ready.py — Louvre DDV (GA) 掃描器
# - 只掃團體 DDV (GA)
# - 週一/三/五/日
# - 失敗自動 0.5–1 秒退避，持續到指定秒數
# - 同步 + 多執行緒；無 while 1

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time, random, json
import requests, pandas as pd, streamlit as st

# ---------------- API 與常數 ----------------
API_ENDPOINT = "https://www.ticketlouvre.fr/louvre/b2c/RemotingService.cfc?method=doJson"

# 模擬真實瀏覽器，降低被擋機率
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://www.ticketlouvre.fr",
    "Referer": "https://www.ticketlouvre.fr/",
    "Connection": "keep-alive",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8,zh-TW;q=0.7",
}

# 固定為 DDV（團體）
DDV = {
    "eventCode": "GA",
    "performanceId": "720553",
    "priceTableId": "1",
    "performanceAk": "LVR.EVN21.PRF116669",  # 你抓到的值
}

# 只看：週一(0)／三(2)／五(4)／日(6)
TARGET_WEEKDAYS = {0, 2, 4, 6}

# ---------------- HTTP helpers ----------------
def post_form(session: requests.Session, form: dict) -> dict:
    """帶 headers 送出；若 4xx 再降級重試一次；失敗時回傳簡短錯誤資訊給 UI。"""
    r = session.post(API_ENDPOINT, data=form, headers=HEADERS, timeout=15)
    if r.status_code >= 400:
        downgraded = {k: v for k, v in HEADERS.items() if k not in ("Origin", "Referer", "Accept-Language")}
        r = session.post(API_ENDPOINT, data=form, headers=downgraded, timeout=15)

    if r.status_code >= 400:
        return {"__http_error__": True, "status": r.status_code, "text": r.text[:500]}

    try:
        return r.json()
    except Exception:
        return json.loads(r.text)

# ---------------- 業務邏輯 ----------------
def fetch_date_list(session: requests.Session, month: int, year: int):
    """date.list.nt：b2c 端點有時回 dateList、有時回 date；兩者皆支援。"""
    form = {
        "eventName": "date.list.nt",
        "eventCode": "GA",
        # b2c 這支 API 要用活動層級的 eventAk（去掉 PRF 尾巴）
        "eventAk": DDV["performanceAk"].split(".PRF")[0],  # -> LVR.EVN21
        "month": month,
        "year": year,
    }
    data = post_form(session, form)
    if isinstance(data, dict) and data.get("__http_error__"):
        return data
    res = data.get("api", {}).get("result", {})
    dates = res.get("dateList") or res.get("date") or []
    # 正規化成 [{'date': 'YYYY-MM-DD'}, ...]
    if dates and isinstance(dates[0], str):
        dates = [{"date": d} for d in dates]
    return dates

def fetch_timeslots_with_retry(session: requests.Session, date_str: str, retry_seconds: int):
    """ticket.list：掃全部 products；抓可售 (>0) 的時段；失敗退避重試直到截止。"""
    form = {
        "eventName": "ticket.list",
        "dateFrom": date_str,
        "eventCode": DDV["eventCode"],
        "performanceId": DDV["performanceId"],
        "priceTableId": DDV["priceTableId"],
        "performanceAk": DDV["performanceAk"],
    }
    deadline = time.time() + retry_seconds
    while time.time() < deadline:
        data = post_form(session, form)
        # 被擋或 4xx：退避重試
        if isinstance(data, dict) and data.get("__http_error__"):
            time.sleep(random.uniform(0.5, 1.0))
            continue

        res = data.get("api", {}).get("result", {})
        products = res.get("product") or res.get("product.list") or []
        available_slots = []
        for p in products:
            # 可能的時間欄位
            t = p.get("time") or p.get("startTime") or p.get("start_time") or p.get("perfTime")
            # 可售數量
            avail = p.get("available", 0)
            try:
                avail = int(avail)
            except Exception:
                avail = 0
            if t and avail > 0:
                available_slots.append(str(t))

        # 回傳當天所有「真的可訂」的時段（可能多個）
        return date_str, sorted(available_slots)

    # 超過重試視窗：視為暫無
    return date_str, []

def scan_month(month: int, year: int, max_workers: int, retry_seconds: int):
    session = requests.Session()
    # 取得該月可售日期
    all_dates = fetch_date_list(session, month, year)
    if isinstance(all_dates, dict) and all_dates.get("__http_error__"):
        retu
