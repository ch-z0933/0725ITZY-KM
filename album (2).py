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
# 2. 基本設定
# =========================
st.set_page_config(page_title="ITZY MOTTO 簽售監控", layout="wide")
st.title("💿 ITZY MOTTO 簽名會 in Taipei")

TW_API = "https://www.kmonstar.com.tw/products/%E6%87%89%E5%8B%9F-260725-itzy-motto-%E5%B0%88%E8%BC%AF%E7%99%BC%E8%A1%8C%E7%B4%80%E5%BF%B5%E7%B0%BD%E5%90%8D%E6%9C%83-in-taipei.json"
INTL_API = "https://kmonstar.com/api/v1/event/detail/72fc931d-ebc0-4208-9a7c-e6e8d8b8643e"

ITEM_NAME = "ITZY 團體簽售"
INTL_INITIAL_STOCK = 10000

LOG_COLUMNS = ['時間', '張數', '來源', '總銷售量', '台灣版總銷量', '國際版總銷量']

MAX_REASONABLE_DROP = 100
CHECK_SECONDS = 20

# =========================
# 3. 初始化
# =========================
if 'log_df' not in st.session_state:
    st.session_state.log_df = pd.DataFrame(columns=LOG_COLUMNS)

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
            wks = gc.add_worksheet(title=ITEM_NAME, rows=5000, cols=10)
            wks.append_row(LOG_COLUMNS)
            return wks
        except Exception as e:
            if "already exists" in str(e):
                return gc.worksheet(ITEM_NAME)
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

        # 舊版只有 4 欄時，自動補成新版 6 欄，不清空舊資料
        headers = values[0]
        if headers[:4] != ['時間', '張數', '來源', '總銷售量']:
            st.sidebar.error("Google Sheet 第一列欄位錯誤，請確認是：時間、張數、來源、總銷售量")
            return pd.DataFrame(columns=LOG_COLUMNS)

        if headers != LOG_COLUMNS:
            wks.update('A1:F1', [LOG_COLUMNS])
            headers = LOG_COLUMNS

        if len(values) == 1:
            return pd.DataFrame(columns=LOG_COLUMNS)

        rows = values[1:]
        fixed_rows = []
        for row in rows:
            row = row + [""] * (len(LOG_COLUMNS) - len(row))
            fixed_rows.append(row[:len(LOG_COLUMNS)])

        df = pd.DataFrame(fixed_rows, columns=LOG_COLUMNS)
        df['張數'] = pd.to_numeric(df['張數'], errors='coerce').fillna(0).astype(int)
        df['總銷售量'] = pd.to_numeric(df['總銷售量'], errors='coerce').fillna(0).astype(int)
        df['台灣版總銷量'] = pd.to_numeric(df['台灣版總銷量'], errors='coerce').fillna(-1).astype(int)
        df['國際版總銷量'] = pd.to_numeric(df['國際版總銷量'], errors='coerce').fillna(-1).astype(int)

        df = df.iloc[::-1].reset_index(drop=True)
        return df

    except Exception as e:
        st.sidebar.error(f"同步雲端資料失敗: {e}")
        return pd.DataFrame(columns=LOG_COLUMNS)

def append_sale_log(now_str, diff, source, total_now, tw_now, intl_now):
    if not gc:
        return False

    try:
        wks = ensure_worksheet()
        values = wks.get_all_values()

        if len(values) > 1:
            latest = values[-1]
            latest_total = int(float(latest[3]))
            if latest_total == int(total_now):
                return False

        wks.append_row([now_str, int(diff), source, int(total_now), int(tw_now), int(intl_now)])
        return True

    except Exception as e:
        st.sidebar.error(f"寫入失敗: {e}")
        return False

# =========================
# 5. API 抓取
# =========================
def get_tw_sales_once(session):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.kmonstar.com.tw/"
    }

    res = session.get(f"{TW_API}?t={int(time.time())}", headers=headers, timeout=10)
    res.raise_for_status()
    data = res.json()

    variants = data.get("variants", [])
    if not variants:
        return None

    total_sold = 0
    for v in variants:
        inventory_qty = v.get("inventory_quantity", None)
        if inventory_qty is None:
            continue
        total_sold += abs(int(inventory_qty))

    return total_sold

def get_tw_sales(session):
    try:
        first = get_tw_sales_once(session)

        if first is None:
            return None

        # 如果台灣站偶發回傳異常低值，第二次確認
        time.sleep(1)
        second = get_tw_sales_once(session)

        if second is None:
            return first

        # 兩次差太多時，取較大的那個，避免短暫掉到 75
        if abs(second - first) > MAX_REASONABLE_DROP:
            return max(first, second)

        return second

    except Exception as e:
        st.sidebar.error(f"台灣 API 抓取失敗: {e}")
        return None

def get_intl_sales(session):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://kmonstar.com/zh/eventproductdetail/72fc931d-ebc0-4208-9a7c-e6e8d8b8643e",
        "Origin": "https://kmonstar.com",
    }

    try:
        res = session.get(f"{INTL_API}?t={int(time.time())}", headers=headers, timeout=10)

        if res.status_code != 200:
            st.sidebar.warning(f"國際 API 狀態碼：{res.status_code}")
            return None

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
        return None

def get_total_sales(session):
    tw = get_tw_sales(session)
    intl = get_intl_sales(session)

    if tw is None and intl is None:
        return None, None, None

    if tw is None:
        tw = 0

    if intl is None:
        intl = 0

    return tw, intl, tw + intl

# =========================
# 6. 排行沖銷退單
# =========================
def build_rank_df(log_df):
    if log_df.empty:
        return pd.DataFrame(columns=['張數'])

    rank_df = log_df.copy()
    rank_df = rank_df[~rank_df['來源'].isin(['INIT', '補歷史'])]
    rank_df['張數'] = pd.to_numeric(rank_df['張數'], errors='coerce').fillna(0).astype(int)

    positives = rank_df[rank_df['張數'] > 0].copy().reset_index(drop=True)
    negatives = rank_df[rank_df['張數'] < 0].copy().reset_index(drop=True)

    kept_rows = positives.to_dict('records')

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
        return pd.DataFrame(columns=['張數'])

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

skip_write = False

if total_now is None:
    st.sidebar.warning("本輪 API 抓取失敗，已跳過寫入。")
    tw_now, intl_now, total_now = 0, 0, 0
    skip_write = True

last_total_in_sheet = 0
last_tw_in_sheet = None
last_intl_in_sheet = None

if not log_df.empty:
    last_total_in_sheet = int(log_df.iloc[0]['總銷售量'])

    if int(log_df.iloc[0]['台灣版總銷量']) >= 0:
        last_tw_in_sheet = int(log_df.iloc[0]['台灣版總銷量'])

    if int(log_df.iloc[0]['國際版總銷量']) >= 0:
        last_intl_in_sheet = int(log_df.iloc[0]['國際版總銷量'])

if last_tw_in_sheet is None:
    last_tw_in_sheet = tw_now

if last_intl_in_sheet is None:
    last_intl_in_sheet = intl_now

diff = total_now - last_total_in_sheet

# 避免台灣 API 短暫掉到 75 造成假退單
if diff < -MAX_REASONABLE_DROP:
    st.sidebar.warning(f"偵測到異常大幅下降：{diff}，本輪不寫入。")
    total_now = last_total_in_sheet
    tw_now = last_tw_in_sheet
    intl_now = last_intl_in_sheet
    skip_write = True

# 第一次啟動且 Sheet 是空的，不補舊單
if not st.session_state.bootstrapped and last_total_in_sheet == 0:
    st.session_state.bootstrapped = True

else:
    if diff != 0 and not skip_write:
        tw_delta = tw_now - last_tw_in_sheet
        intl_delta = intl_now - last_intl_in_sheet

        new_entries = []

        # 台灣版變動，獨立寫一列
        if tw_delta != 0:
            tw_source = f"TW+{tw_delta}" if tw_delta > 0 else f"TW退{abs(tw_delta)}"

            ok = append_sale_log(
                now,
                tw_delta,
                tw_source,
                total_now,
                tw_now,
                intl_now
            )

            if ok:
                new_entries.append({
                    '時間': now,
                    '張數': int(tw_delta),
                    '來源': tw_source,
                    '總銷售量': int(total_now),
                    '台灣版總銷量': int(tw_now),
                    '國際版總銷量': int(intl_now)
                })

        # 國際版變動，獨立寫一列
        if intl_delta != 0:
            intl_source = f"INTL+{intl_delta}" if intl_delta > 0 else f"INTL退{abs(intl_delta)}"

            ok = append_sale_log(
                now,
                intl_delta,
                intl_source,
                total_now,
                tw_now,
                intl_now
            )

            if ok:
                new_entries.append({
                    '時間': now,
                    '張數': int(intl_delta),
                    '來源': intl_source,
                    '總銷售量': int(total_now),
                    '台灣版總銷量': int(tw_now),
                    '國際版總銷量': int(intl_now)
                })

        if new_entries:
            new_entry_df = pd.DataFrame(new_entries)
            st.session_state.log_df = pd.concat(
                [new_entry_df, log_df],
                ignore_index=True
            )
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
                log_df[['時間', '張數', '來源']].head(100),
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
            })
            st.table(rank_display)
        else:
            st.info("目前沒有有效排行資料")

st.caption(f"最後更新時間：{now}")

time.sleep(CHECK_SECONDS)
st.rerun()
