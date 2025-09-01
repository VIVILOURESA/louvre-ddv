import streamlit as st
import requests
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import json

# ------------------ 設定 ------------------
API_ENDPOINT = "https://www.ticketlouvre.fr/louvre/b2c/RemotingService.cfc?method=doJson"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": "https://www.ticketlouvre.fr",
    "Referer": "https://www.ticketlouvre.fr/",
    "Connection": "keep-alive",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8,zh-TW;q=0.7",
}

DDV_CONFIG = {
    "eventCode": "GA",
    "performanceId": "720553",
    "priceTableId": "1",
    "performanceAk": "LVR.EVN21.PRF116669",
}

TARGET_WEEKDAYS = [0, 2, 4, 6]  # 週一/三/五/日

# ------------------ 工具 ------------------
def post_form(session: requests.Session, form: dict) -> dict:
    """統一送出 POST 請求，加上 headers"""
    r = session.post(API_ENDPOINT, data=form, headers=HEADERS, timeout=15)

    if r.status_code >= 400:
        downgraded = {k: v for k, v in HEADERS.items() if k not in ("Origin", "Referer")}
        r = session.post(API_ENDPOINT, data=form, headers=downgraded, timeout=15)

    if r.status_code >= 400:
        return {"__http_error__": True, "status": r.status_code, "text": r.text[:500]}

    try:
        return r.json()
    except Exception:
        return json.loads(r.text)


def fetch_date_list(session: requests.Session, cfg: dict, month: int, year: int):
    form = {
        "eventName": "date.list.nt",
        "eventCode": cfg["eventCode"],
        "eventAk": cfg["performanceAk"].split(".PRF")[0],  # eventAk 不要用 PRF 那段
        "month": month,
        "year": year,
    }
    return post_form(session, form).get("api", {}).get("result", {}).get("dateList", [])


def fetch_timeslots_with_retry(session, cfg, date: str, retry_seconds: int):
    form = {
        "eventName": "ticket.list",
        "dateFrom": date,
        "eventCode": cfg["eventCode"],
        "performanceId": cfg["performanceId"],
        "priceTableId": cfg["priceTableId"],
        "performanceAk": cfg["performanceAk"],
    }

    deadline = time.time() + retry_seconds
    while time.time() < deadline:
        data = post_form(session, form)
        if "__http_error__" not in data:
            try:
                products = data["api"]["result"]["product.list"]
                if products and products[0]["available"] > 0:
                    return [f"{products[0]['available']} places"]
                else:
                    return []
            except Exception:
                return []
        time.sleep(0.5)
    return []


def scan_month(cfg: dict, month: int, year: int, max_workers: int, retry_seconds: int):
    session = requests.Session()
    all_dates = fetch_date_list(session, cfg, month, year)

    date_strs = [
        d["date"] for d in all_dates
        if datetime.strptime(d["date"], "%Y-%m-%d").weekday() in TARGET_WEEKDAYS
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(fetch_timeslots_with_retry, session, cfg, ds, retry_seconds) for ds in date_strs]
        for fut in as_completed(futs):
            idx = futs.index(fut)
            d = date_strs[idx]
            results[d] = fut.result()
    return results


def render_table(data: dict):
    rows = []
    for d, slots in sorted(data.items()):
        wk = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
        rows.append({
            "日期": f"{d} (週{wk})",
            "狀態": "✅ 有" if slots else "❌ 無",
            "時段": " / ".join(slots) if slots else "-"
        })

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("這個月份 (週一/三/五/日) 沒有找到可售日期或時段。")


# ------------------ UI ------------------
st.title("🎫 Louvre – Droit de visite (DDV) (週一/三/五/日)")

now = datetime.now()
month = st.selectbox("選擇月份 / Month", [now.month, now.month + 1, now.month + 2, now.month + 3])
concurrency = st.slider("並行數 / Concurrency", 4, 20, 10)
retry_window = st.selectbox("重試時間 (秒)", [60, 120, 180], index=1)

if st.button("開始掃描 / Scan", type="primary"):
    st.info("查詢中：並行請求 + 自動重試…")
    data = scan_month(DDV_CONFIG, month, now.year, concurrency, retry_window)
    render_table(data)
    st.success("完成。")
