# ddv_ready.py  — Louvre DDV 快速掃描（週一/三/五/日 + 自動重試）
import asyncio, random
from datetime import datetime
import httpx, pandas as pd, streamlit as st

API_ENDPOINT = "https://www.ticketlouvre.fr/louvre/api/RemotingService.cfc?method=doJson"

MAX_CONCURRENCY = 10
RETRY_UNTIL_SEC = 120
TARGET_WEEKDAYS = {0, 2, 4, 6}  # 週一=0, 週三=2, 週五=4, 週日=6
AUTO_REFRESH_SEC = 0  

DDV_CONFIG = {
    "eventCode": "GA",
    "performanceId": "720553",
    "performanceAk": "LVR.EVN21.PRF116669",
    "priceTableId": "1",
}

st.set_page_config(page_title="Louvre DDV Tickets", layout="wide")
st.title("🎟️ Louvre – Droit de visite (DDV)（週一/三/五/日）")

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
    try:
        r = await client.post("", data=form)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            import json as _json
            return _json.loads(r.text)
    except Exception as e:
        return {"__error__": str(e), "__form__": form}

async def fetch_date_list(client: httpx.AsyncClient, cfg: dict, for_month: int, year: int):
    date_from = datetime(year, for_month, 1).strftime("%Y-%m-%d")
    form = {"eventName": "date.list.nt", "dateFrom": date_from, **cfg}
    data = await api_post(client, form)
    return data.get("api", {}).get("result", {}).get("date", []) or [], data

async def fetch_timeslots_with_retry(client: httpx.AsyncClient, cfg: dict, date_str: str, sem: asyncio.Semaphore, retry_seconds: int):
    form = {"eventName": "ticket.list", "dateFrom": date_str, **cfg}
    deadline = datetime.now().timestamp() + retry_seconds
    last_error = None
    while datetime.now().timestamp() < deadline:
        async with sem:
            try:
                data = await api_post(client, form)
                if "__error__" in data:
                    last_error = data["__error__"]
                    raise RuntimeError(last_error)
                products = data.get("api", {}).get("result", {}).get("product", []) or []
                times = []
                for p in products:
                    t = p.get("time") or p.get("startTime") or p.get("start_time")
                    if t:
                        times.append(str(t))
                return date_str, sorted(times), None
            except Exception as e:
                last_error = str(e)
                await asyncio.sleep(random.uniform(0.5, 1.0))
    return date_str, [], last_error or "timeout"

async def scan_month(cfg: dict, month: int, year: int, concurrency: int, retry_seconds: int):
    debug_slot = st.expander("顯示偵錯資訊（需要時展開）", expanded=False)
    async with make_client() as client:
        dates, raw_first = await fetch_date_list(client, cfg, month, year)
        with debug_slot:
            st.write("date.list.nt 原始回應（截斷）：", str(raw_first)[:1000])
        date_strs = []
        for d in dates:
            ds = d.get("date")
            if not ds:
                continue
            wk = datetime.strptime(ds, "%Y-%m-%d").weekday()
            if wk in TARGET_WEEKDAYS:
                date_strs.append(ds)

        sem = asyncio.Semaphore(concurrency)
        tasks = [fetch_timeslots_with_retry(client, cfg, ds, sem, retry_seconds) for ds in date_strs]
        results = await asyncio.gather(*tasks)
        data = {d: ts for d, ts, _err in results}
        errors = {d: err for d, _ts, err in results if err}
        if errors:
            with debug_slot:
                st.warning("部分日期查詢錯誤（將自動忽略）：")
                st.write(errors)
        return data

def render_table(data: dict):
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
        st.info("這個月份（僅週一/三/五/日）目前查不到可售日期或時段。")

year = now.year if month >= now.month else (now.year + 1)

if st.button("開始掃描 / Scan", type="primary"):
    st.info("查詢中：並行請求 + 失敗自動重試…")
    data = asyncio.run(scan_month(DDV_CONFIG, month, year, concurrency, retry_window))
    render_table(data)
    st.success("完成。")

if AUTO_REFRESH_SEC:
    import time
    st.caption(f"自動刷新：每 {AUTO_REFRESH_SEC} 秒")
    time.sleep(AUTO_REFRESH_SEC)
    st.rerun()

