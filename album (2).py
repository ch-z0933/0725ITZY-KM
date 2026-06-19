import streamlit as st
import requests
import pandas as pd
import time
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials

# =========================
# 1. Google Sheets 連線
# =========================
def init_connection():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
    return client.open("ITZY_MOTTO_Sales")

try:
    gc = init_connection()
except Exception as e:
    st.error(f"雲端連線失敗: {e}")
    gc = None

# =========================
# 2. 頁面設定
# =========================
st.set_page_config(page_title="ITZY MOTTO 簽售監控", layout="wide")
st.title("💿 ITZY MOTTO 簽名會 in Taipei")

TW_API = "https://www.kmonstar.com.tw/products/%E6%87%89%E5%8B%9F-260725-itzy-motto-%E5%B0%88%E8%BC%AF%E7%99%BC%E8%A1%8C%E7%B4%80%E5%BF%B5%E7%B0%BD%E5%90%8D%E6%9C%83-in-taipei.json"

ITEM_NAME = "ITZY 團體簽售"
LOG_COLUMNS = ['時間', '張數', '來源', '總銷售量']

# =========================
# 3. 初始化 session_state
# =========================
if 'log_df' not in st.session_state:
    st.session_state.log_df = pd.DataFrame(columns=LOG_COLUMNS)

if 'last_total' not in st.session_state:
    st.session_state.last_total = None

if 'bootstrapped' not in st.session_state:
    st.session_state.bootstrapped = False

# =========================
# 4. Google Sheet
# =========================
def ensure_worksheet():
    if not gc:
        return None

    try:
        return gc.worksheet(ITEM_NAME)
    except:
        try:
            wks = gc.add_worksheet(title=ITEM_NAME, rows=2000, cols=10)
            wks.append_row(LOG_COLUMNS)
            return wks
        except Exception as e:
            st.sidebar.error(f"建立工作表失敗: {e}")
            return None

def sync_from_cloud():
    if not gc:
        return pd.DataFrame(columns=LOG_COLUMNS)

    try:
        wks = ensure_worksheet()
        values = wks.get_all_values()

        if not values:
            wks.append_row(LOG_COLUMNS)
            return pd.DataFrame(columns=LOG_COLUMNS)

        if values[0] != LOG_COLUMNS:
            wks.clear()
            wks.append_row(LOG_COLUMNS)
            return pd.DataFrame(columns=LOG_COLUMNS)

        if len(values) == 1:
            return pd.DataFrame(columns=LOG_COLUMNS)

        df = pd.DataFrame(values[1:], columns=values[0])
        df['張數'] = pd.to_numeric(df['張數'], errors='coerce').fillna(0).astype(int)
        df['總銷售量'] = pd.to_numeric(df['總銷售量'], errors='coerce').fillna(0).astype(int)
        df = df.iloc[::-1].reset_index(drop=True)
        return df

    except Exception as e:
        st.sidebar.error(f"同步雲端資料失敗: {e}")
        return pd.DataFrame(columns=LOG_COLUMNS)

def append_sale_log(now_str, diff, source, total_now):
    if not gc:
        return False

    try:
        wks = ensure_worksheet()
        wks.append_row([now_str, int(diff), source, int(total_now)])
        return True
    except Exception as e:
        st.sidebar.error(f"寫入失敗: {e}")
        return False

# =========================
# 5. API 抓取
# =========================
INTL_API = "https://kmonstar.com/api/v1/event/detail/72fc931d-ebc0-4208-9a7c-e6e8d8b8643e"
INTL_INITIAL_STOCK = 10000


def get_tw_sales(session):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.kmonstar.com.tw/"
    }

    try:
        res = session.get(
            f"{TW_API}?t={int(time.time())}",
            headers=headers,
            timeout=10
        )
        res.raise_for_status()
        data = res.json()

        total_sold = 0

        for v in data.get("variants", []):
            inventory_qty = v.get("inventory_quantity", 0)
            total_sold += abs(int(inventory_qty))

        return total_sold

    except Exception as e:
        st.sidebar.error(f"台灣 API 抓取失敗: {e}")
        return 0


def get_intl_sales(session):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://kmonstar.com/zh/eventproductdetail/72fc931d-ebc0-4208-9a7c-e6e8d8b8643e",
        "Origin": "https://kmonstar.com",
    }

    try:
        res = session.get(
            f"{INTL_API}?t={int(time.time())}",
            headers=headers,
            timeout=10
        )

        if res.status_code != 200:
            return 0

        data = res.json()
        options = data.get("data", {}).get("optionList", [])

        total_sold = 0

        for o in options:
            stock_ko = o.get("stockKo", {}).get("quantity")

            if stock_ko is not None:
                total_sold += INTL_INITIAL_STOCK - int(stock_ko)

        return total_sold

    except Exception as e:
        st.sidebar.error(f"國際 API 抓取失敗: {e}")
        return 0


def get_total_sales(session):
    tw = get_tw_sales(session)
    intl = get_intl_sales(session)

    return tw, intl, tw + intl

# =========================
# 6. 排行沖銷退單
# =========================
def build_rank_df(log_df):
    if log_df.empty:
        return pd.DataFrame(columns=['張數', '來源'])

    rank_df = log_df.copy()
    rank_df = rank_df[~rank_df['來源'].isin(['INIT', '補歷史'])]
    rank_df['張數'] = pd.to_numeric(rank_df['張數'], errors='coerce').fillna(0).astype(int)

    positives = rank_df[rank_df['張數'] > 0].copy().reset_index(drop=True)
    negatives = rank_df[rank_df['張數'] < 0].copy().reset_index(drop=True)

    if positives.empty:
        return pd.DataFrame(columns=['張數', '來源'])

    kept_rows = positives.to_dict('records')

    # 退單：刪掉一筆相同張數的正單
    for _, row in negatives.iterrows():
        cancel_qty = abs(int(row['張數']))

        match_idx = None
        for i, pos in enumerate(kept_rows):
            if int(pos['張數']) == cancel_qty:
                match_idx = i
                break

        if match_idx is not None:
            kept_rows.pop(match_idx)

    if not kept_rows:
        return pd.DataFrame(columns=['張數', '來源'])

    final_rank_df = pd.DataFrame(kept_rows)
    final_rank_df['張數'] = pd.to_numeric(final_rank_df['張數'], errors='coerce').fillna(0).astype(int)
    final_rank_df = final_rank_df.sort_values("張數", ascending=False).reset_index(drop=True)

    return final_rank_df

# =========================
# 7. 主流程
# =========================
status_placeholder = st.empty()
session = requests.Session()

log_df = sync_from_cloud()
st.session_state.log_df = log_df

tz = pytz.timezone('Asia/Taipei')
now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

tw_now, intl_now, total_now = get_total_sales(session)

last_total_in_sheet = 0
if not log_df.empty and '總銷售量' in log_df.columns:
    last_total_in_sheet = int(pd.to_numeric(
        pd.Series([log_df.iloc[0]['總銷售量']]),
        errors='coerce'
    ).fillna(0).iloc[0])

diff = total_now - last_total_in_sheet

# 第一次啟動且 Sheet 是空的，不補舊單
if not st.session_state.bootstrapped and last_total_in_sheet == 0:
    st.session_state.last_total = total_now
else:
    if diff != 0:
        if diff > 0:
            source = f"TW+{diff}"
        else:
            source = f"TW退{abs(diff)}"

        ok = append_sale_log(now, diff, source, total_now)

        if ok:
            new_entry = pd.DataFrame([{
                '時間': now,
                '張數': int(diff),
                '來源': source,
                '總銷售量': int(total_now)
            }])

            st.session_state.log_df = pd.concat(
                [new_entry, log_df],
                ignore_index=True
            )

st.session_state.last_total = total_now
st.session_state.bootstrapped = True

# =========================
# 8. 畫面顯示
# =========================
with status_placeholder.container():
    st.write("### 📊 ITZY 團體簽售總銷量")

    summary_df = pd.DataFrame([{
    "項目": ITEM_NAME,
    "台灣版": tw_now,
    "國際版": intl_now,
    "總計": total_now
}])
    st.table(summary_df)

    st.divider()

    log_df = st.session_state.log_df

    cl, cr = st.columns(2)

    with cl:
        st.write("🕒 **銷售時間紀錄**")
        if not log_df.empty:
            st.dataframe(
                log_df[['時間', '張數', '來源']],
                width='stretch',
                hide_index=True
            )
        else:
            st.info("目前沒有紀錄")

    with cr:
        st.write("🏆 **單筆排行**")
        final_rank_df = build_rank_df(log_df)

        if not final_rank_df.empty:
            final_rank_df = final_rank_df.reset_index(drop=True)
            final_rank_df.index = final_rank_df.index + 1

            rank_display = pd.DataFrame({
                "排名": [f"第 {idx} 名" for idx in final_rank_df.index],
                "單筆張數": final_rank_df['張數'].values,
                "來源": final_rank_df['來源'].values
            })
            st.table(rank_display)
        else:
            st.info("目前沒有有效排行資料")

st.caption(f"最後更新時間：{now}")
time.sleep(15)
st.rerun()
