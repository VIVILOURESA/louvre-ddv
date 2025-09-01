# ddv_ready.py — Louvre DDV 快速掃描
# - 只查團體 DDV (GA)
# - 只顯示週一/三/五/日
# - 每一天失敗會 0.5–1 秒退避重試，直到 retry_window 秒
# - 按「開始掃描」才會跑（不會 while 1 卡死）

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time, random, json
import requests, pandas as pd, streamlit as st

# ---------------- API ----------------
API_ENDPOINT = "https://www.ticketlouvre.fr/louvre/b2c/RemotingService.cfc?method=doJson"

# DDV (GA) 參數（固定）
DDV_ONLY = {
    "eventCode": "GA",
    "performanceId": "720553",
    "performanceAk": "LVR.EVN21.PRF116669",
    "priceTableId": "1",
}

# 只查：週一(0)／三(2)／五(4)／日(6)
TARGET_WEEKDAYS = {0, 2, 4, 6}

# ---------------- UI ----------------
st.set_page_config(page_title="Louvre DDV Tickets", layout="wide")
st.title("🎟️ Louvre – Droit de visite (DDV)")

ak = DDV_ONLY["performanceAk"]
ak_mask = (ak[:6] + "..." + ak[-4:]) if len(ak) > 10 else ak
st.caption(
    f"eventCode={DDV_ONLY['eventCode']} • performanceId={DDV_ONLY['performanceId']} "
    f"• priceTableId={DDV_ONLY['priceTableId']} • performanceAk={ak_mask}"
)

now = datetime.now()
m_list = [now.month, (now.month % 12) + 1, ((now.month + 1) % 12) + 1, ((now.month + 2) % 12) + 1]
months = sorted(set(m_list))

c1, c2, c3 = st.columns(3)
with c1:
    month = st.selectbox("選擇月份 / Month", months, index=0)
with c2:
    max_workers = st.slider("並行數 / Concurrency", 5, 40, 10, 1)
with c3:
    retry_window = st.selectbox("重試時間（秒）", [60, 120, 180, 300], index=1)  # 預設 120

# ---------------- HTTP ----------------
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.ticketlouvre.fr",
        "Referer": "https://www.ticketlouvre.fr/",
        "Connection": "keep-alive",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8,zh-TW;q=0.7",
    })
    return s

def post_form(session: requests.Session, form: dict) -> dict:
    r = session.post(API_ENDPOINT, data=form, timeout=15)
    if r.status_code >= 400:
        # 降級重試一次
        h = session.headers.copy()
        h.pop("Origin", None); h.pop("Referer", None); h.pop("Accept-Language", None)
        r = session.post(API_ENDPOINT, data=form, headers=h, timeout=15)

    if r.status_code >= 400:
        return {"__http_error__": True, "status": r.status_code, "text": r.text[:500]}

    try:
        return r.json()
    except Exception:
        return json.loads(r.text)

# ---------------- 邏輯 ----------------
def fetch_date_list(session: requests.Session, year: int, month: int):
    form = {
        "eventName": "date.list.nt",
        "year": year,
        "month": month,
        "eventCode": "GA",
        "eventAk": "LVR.EVN21",
    }
    data = post_form(session, form)
    if isinstance(data, dict) and data.get("__http_error__"):
        return data
    return data.get("api", {}).get("result", {}).get("dateList", []) or []

def fetch_timeslots_with_retry(session: requests.Session, date_str: str, retry_seconds: int):
    form = {"eventName": "ticket.list", "dateFrom": date_str, **DDV_ONLY}
    deadline = time.time() + retry_seconds
    while time.time() < deadline:
        data = post_form(session, form)
        if isinstance(data, dict) and data.get("__http_error__"):
            time.sleep(random.uniform(0.5, 1.0))
            continue

        products = data.get("api", {}).get("result", {}).get("product", []) \
                   or data.get("api", {}).get("result", {}).get("product.list", [])
        slots = []
        for p in products:
            t = p.get("time") or p.get("startTime") or p.get("start_time")
            if t:
                slots.append(str(t))
        return date_str, sorted(slots)

    return date_str, []  # 超時

def scan_month(month: int, year: int, max_workers: int, retry_seconds: int):
    session = new_session()
    all_dates = fetch_date_list(session, year, month)
    if isinstance(all_dates, dict) and all_dates.get("__http_error__"):
        return all_dates

    # 過濾週一/三/五/日
    date_strs = [
        d["date"] for d in all_dates
        if datetime.strptime(d["date"], "%Y-%m-%d").weekday() in TARGET_WEEKDAYS
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(fetch_timeslots_with_retry, session, ds, retry_seconds) for ds in date_strs]
        for fut in as_completed(futs):
            d, slots = fut.result()
            results[d] = slots
    return results

def render_table(data: dict):
    if isinstance(data, dict) and data.get("__http_error__"):
        st.error(f"HTTP {data['status']} – {data['text']}")
        return

    rows = []
    for d, slots in sorted(data.items()):
        wk = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
        rows.append({
            "日期": f"{d} (週{wk})",
            "狀態": "✅ 有" if slots else "❌ 無",
            "時段": "、".join(slots) or "-"
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("這個月份（僅週一／三／五／日）目前查不到可售日期或時段。")

# ---------------- RUN ----------------
year = now.year if month >= now.month else (now.year + 1)

if st.button("開始掃描 / Scan", type="primary"):
    st.info("查詢中：並行請求 + 自動重試…")
    data = scan_month(month, year, max_workers, retry_window)
    render_table(data)
    st.success("完成。")

