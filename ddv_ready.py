# ddv_ready.py — Louvre DDV 快速掃描（週一/三/五/日 + 自動重試）
import asyncio, random
from datetime import datetime
import httpx, pandas as pd, streamlit as st
import nest_asyncio  # <-- 讓已存在的事件迴圈也可再跑協程

API_ENDPOINT = "https://www.ticketlouvre.fr/louvre/api/RemotingService.cfc?method=doJson"

MAX_CONCURRENCY = 10
RETRY_UNTIL_SEC = 120
TARGET_WEEKDAYS = {0, 2, 4, 6}  # 只看週一/三/五/日
AUTO_REFRESH_SEC = 0

# 你抓到的 DDV 參數
DDV_CONFIG = {
    "eventCode": "GA",
    "performanceId": "720553",
    "performanceAk": "LVR.EVN21.PRF116669",
    "priceTableId": "1",
}

# --------- UI ----------
st.set_page_config(page_title="Louvre DDV Tickets", layout="wide")
st.title("🎟️ Louvre – Droit de visite (DDV)（週一／三／五／日）")

ak = DDV_CONFIG.get("performanceAk", "")
ak_mask = ak[:6] + "..." + ak[-4:] if len(ak) > 10 else ak
st.caption(
    f"eventCode={DDV_CONFIG['eventCode']} • performanceId={DDV_CONFIG['performanceId']} "
    f"• priceTableId={DDV_CONFIG['priceTableId']} • performanceAk={ak_mask}"
)

now = datetime.now()
m_list = [now.month, now.month + 1, now.month + 2, now.month + 3]
if now.day >= 15:
    m_list.insert(0, now.month + 4)
months = sorted({(m - 1) % 12 + 1 for m in m_list})

c1, c2, c3 = st.columns(3)
with c1:
    month = st.selectbox("選擇月份 / Month", months, index=0)
with c2:
    concurrency = st.slider("並行數 / Concurrency", 5, 40, MAX_CONCURRENCY, 1)
with c3:
    retry_window = st.selectbox("重試時間（秒）", [60, 120, 180, 300], index=[60,120,180,300].index(RETRY_UNTIL_SEC))

# --------- HTTP ----------
def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_ENDPOINT,
        http2=True,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.ticketlouvre.fr",
            "Referer": "https://www.ticketlouvre.fr/",
        },
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
        timeout=httpx.Timeout(15.0),
    )

async def api_post(client: httpx.AsyncClient, form: dict):
    r = await client.post("", data=form)  # 官方是用 form-data
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        import json as _json
        return _json.loads(r.text)

# --------- 業務邏輯 ----------
async def fetch_date_list(client: httpx.AsyncClient, cfg: dict, for_month: int, year: int):
    date_from = datetime(year, for_month, 1).strftime("%Y-%m-%d")
    form = {"eventName": "date.list.nt", "dateFrom": date_from, **cfg}
    data = await api_post(client, form)
    return data.get("api", {}).get("result", {}).get("date", []) or []

async def fetch_timeslots_with_retry(client: httpx.AsyncClient, cfg: dict, date_str: str, sem: asyncio.Semaphore, retry_seconds: int):
    form = {"eventName": "ticket.list", "dateFrom": date_str, **cfg}
    deadline = datetime.now().timestamp() + retry_seconds
    last_err = None
    while datetime.now().timestamp() < deadline:
        async with sem:
            try:
                data = await api_post(client, form)
                products = data.get("api", {}).get("result", {}).get("product", []) or []
                times = []
                for p in products:
                    t = p.get("time") or p.get("startTime") or p.get("start_time")
                    if t:
                        times.append(str(t))
                return date_str, sorted(times)
            except Exception as e:
                last_err = str(e)
                await asyncio.sleep(random.uniform(0.5, 1.0))
    # 超過重試視為無
    return date_str, []

async def scan_month(cfg: dict, month: int, year: int, concurrency: int, retry_seconds: int):
    async with make_client() as client:
        # 月份所有可售日期
        dates = await fetch_date_list(client, cfg, month, year)
        # 過濾週一三五日
        date_strs = []
        for d in dates:
            ds = d.get("date")
            if not ds:
                continue
            wk = datetime.strptime(ds, "%Y-%m-%d").weekday()  # 週一=0 … 週日=6
            if wk in TARGET_WEEKDAYS:
                date_strs.append(ds)

        sem = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(
            *[fetch_timeslots_with_retry(client, cfg, ds, sem, retry_seconds) for ds in date_strs]
        )
        return {d: ts for d, ts in results}

def render_table(data: dict):
    rows = []
    for d, slots in sorted(data.items()):
        wk = "一二三四五六日"[datetime.strptime(d, "%Y-%m-%d").weekday()]
        rows.append({"日期": f"{d} (週{wk})", "狀態": "✅ 有" if slots else "❌ 無", "時段": "、".join(slots) or "-"})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("這個月份（僅週一／三／五／日）目前查不到可售日期或時段。")

# --------- 在 Streamlit/雲端安全執行協程 ----------
def run_async(awaitable):
    """在現有事件迴圈或無事件迴圈時，都能安全執行"""
    try:
        loop = asyncio.get_running_loop()
        # 已有 loop（例如 Streamlit/Tornado）→ 用 nest_asyncio 補丁後執行
        nest_asyncio.apply(loop)
        return loop.run_until_complete(awaitable)
    except RuntimeError:
        # 沒有 loop → 正常用 asyncio.run
        return asyncio.run(awaitable)

year = now.year if month >= now.month else (now.year + 1)

if st.button("開始掃描 / Scan", type="primary"):
    st.info("查詢中：並行請求 + 失敗自動重試…")
    data = run_async(scan_month(DDV_CONFIG, month, year, concurrency, retry_window))
    render_table(data)
    st.success("完成。")

if AUTO_REFRESH_SEC:
    import time
    st.caption(f"自動刷新：每 {AUTO_REFRESH_SEC} 秒")
    time.sleep(AUTO_REFRESH_SEC)
    st.rerun()
