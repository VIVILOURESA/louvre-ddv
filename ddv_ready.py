# ddv_ready.py — Louvre DDV 快速掃描（週一/三/五/日 + 自動重試）— 同步多執行緒版
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import random, time
import requests, pandas as pd, streamlit as st

API_ENDPOINT = "https://www.ticketlouvre.fr/louvre/api/RemotingService.cfc?method=doJson"

# 預設參數（頁面可調整）
DEFAULT_MAX_WORKERS = 10            # 同時請求數
DEFAULT_RETRY_SECONDS = 120         # 每一天最多重試秒數（建議 120）
TARGET_WEEKDAYS = {0, 2, 4, 6}      # 只看：一(0)/三(2)/五(4)/日(6)

# 你的 DDV 產品參數（已填好）
DDV_CONFIG = {
    "eventCode": "GA",
    "performanceId": "720553",
    "performanceAk": "LVR.EVN21.PRF116669",
    "priceTableId": "1",
}

# ---------------- UI ----------------
st.set_page_config(page_title="Louvre DDV Tickets", layout="wide")
st.title("🎟️ Louvre – Droit de visite (DDV)（週一／三／五／日）")

ak = DDV_CONFIG["performanceAk"]
ak_mask = (ak[:6] + "..." + ak[-4:]) if len(ak) > 10 else ak
st.caption(
    f"eventCode={DDV_CONFIG['eventCode']} • performanceId={DDV_CONFIG['performanceId']} "
    f"• priceTableId={DDV_CONFIG['priceTableId']} • performanceAk={ak_mask}"
)

now = datetime.now()
m_list = [now.month, (now.month % 12) + 1, ((now.month + 1) % 12) + 1, ((now.month + 2) % 12) + 1]
months = sorted(set(m_list))

c1, c2, c3 = st.columns(3)
with c1:
    month = st.selectbox("選擇月份 / Month", months, index=0)
with c2:
    max_workers = st.slider("並行數 / Concurrency", 5, 40, DEFAULT_MAX_WORKERS, 1)
with c3:
    retry_window = st.selectbox("重試時間（秒）", [60, 120, 180, 300], index=[60,120,180,300].index(DEFAULT_RETRY_SECONDS))

# ---------------- HTTP helpers ----------------
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.ticketlouvre.fr",
        "Referer": "https://www.ticketlouvre.fr/",
    })
    s.timeout = 15
    return s

def post_form(session: requests.Session, form: dict) -> dict:
    r = session.post(API_ENDPOINT, data=form, timeout=15)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        import json as _json
        return _json.loads(r.text)

# ---------------- Domain logic ----------------
def fetch_date_list(session: requests.Session, cfg: dict, for_month: int, year: int):
    date_from = datetime(year, for_month, 1).strftime("%Y-%m-%d")
    form = {"eventName": "date.list.nt", "dateFrom": date_from, **cfg}
    data = post_form(session, form)
    return data.get("api", {}).get("result", {}).get("date", []) or []

def fetch_timeslots_with_retry(session: requests.Session, cfg: dict, date_str: str, retry_seconds: int):
    """查單一天的時段；失敗則 0.5–1 秒間隔重試直到 retry_seconds"""
    form = {"eventName": "ticket.list", "dateFrom": date_str, **cfg}
    deadline = time.time() + retry_seconds
    last_err = None
    while time.time() < deadline:
        try:
            data = post_form(session, form)
            products = data.get("api", {}).get("result", {}).get("product", []) or []
            times = []
            for p in products:
                t = p.get("time") or p.get("startTime") or p.get("start_time")
                if t:
                    times.append(str(t))
            return date_str, sorted(times)
        except Exception as e:
            last_err = e
            time.sleep(random.uniform(0.5, 1.0))
    # 超過重試時間就視為無
    return date_str, []

def scan_month(cfg: dict, month: int, year: int, max_workers: int, retry_seconds: int):
    session = new_session()
    # 取得該月所有可售日期
    all_dates = fetch_date_list(session, cfg, month, year)
    date_strs = []
    for d in all_dates:
        ds = d.get("date")
        if not ds:
            continue
        if datetime.strptime(ds, "%Y-%m-%d").weekday() in TARGET_WEEKDAYS:
            date_strs.append(ds)

    results = {}
    if not date_strs:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(fetch_timeslots_with_retry, session, cfg, ds, retry_seconds) for ds in date_strs]
        for fut in as_completed(futs):
            d, slots = fut.result()
            results[d] = slots
    return results

def render_table(data: dict):
    rows = []
    for d, slots in sorted(data.items()):
        wk = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
        rows.append({"日期": f"{d} (週{wk})", "狀態": "✅ 有" if slots else "❌ 無", "時段": "、".join(slots) or "-"})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("這個月份（僅週一／三／五／日）目前查不到可售日期或時段。")

# ---------------- Run ----------------
year = now.year if month >= now.month else (now.year + 1)

if st.button("開始掃描 / Scan", type="primary"):
    st.info("查詢中：並行請求 + 失敗自動重試…（0.5–1 秒退避）")
    data = scan_month(DDV_CONFIG, month, year, max_workers, retry_window)
    render_table(data)
    st.success("完成。")
