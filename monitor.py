import requests
import pandas as pd
import time
from datetime import datetime
import pytz
import gspread
from google.oauth2.service_account import Credentials
import json
import os

# =========================
# 1. Google Sheets 連線
# =========================
def init_connection():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    # 本機跑：放 service_account.json
    creds = Credentials.from_service_account_file(
        "service_account.json",
        scopes=scope
    )

    client = gspread.authorize(creds)
    return client.open("ITZY_MOTTO_Sales")

gc = init_connection()

# =========================
# 2. 基本設定
# =========================
TW_API = "https://www.kmonstar.com.tw/products/%E6%87%89%E5%8B%9F-260725-itzy-motto-%E5%B0%88%E8%BC%AF%E7%99%BC%E8%A1%8C%E7%B4%80%E5%BF%B5%E7%B0%BD%E5%90%8D%E6%9C%83-in-taipei.json"
INTL_API = "https://kmonstar.com/api/v1/event/detail/72fc931d-ebc0-4208-9a7c-e6e8d8b8643e"

INTL_INITIAL_STOCK = 10000
ITEM_NAME = "ITZY 團體簽售"
LOG_COLUMNS = ['時間', '張數', '來源', '總銷售量']

CHECK_SECONDS = 15
MAX_REASONABLE_DROP = 100

# =========================
# 3. Google Sheet
# =========================
def ensure_worksheet():
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
            raise e

def get_latest_total():
    wks = ensure_worksheet()
    values = wks.get_all_values()

    if not values:
        wks.append_row(LOG_COLUMNS)
        return 0

    if values[0] != LOG_COLUMNS:
        raise Exception("Google Sheet 欄位名稱不一致，請確認第一列是：時間、張數、來源、總銷售量")

    if len(values) == 1:
        return 0

    try:
        return int(float(values[-1][3]))
    except:
        return 0

def append_sale_log(now_str, diff, source, total_now):
    wks = ensure_worksheet()

    values = wks.get_all_values()
    if len(values) > 1:
        latest_total = int(float(values[-1][3]))
        if latest_total == int(total_now):
            print(f"[略過] 已經寫過總量 {total_now}")
            return False

    wks.append_row([now_str, int(diff), source, int(total_now)])
    return True

# =========================
# 4. API
# =========================
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

    except Exception as e:
        print(f"[錯誤] 台灣 API 抓取失敗: {e}")
        return None

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
            print(f"[錯誤] 國際 API 狀態碼: {res.status_code}")
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
        print(f"[錯誤] 國際 API 抓取失敗: {e}")
        return 0

def get_total_sales(session):
    tw = get_tw_sales(session)
    intl = get_intl_sales(session)

    if tw is None:
        return None, intl, intl

    return tw, intl, tw + intl

# =========================
# 5. 主監控
# =========================
def run_monitor():
    session = requests.Session()
    tz = pytz.timezone('Asia/Taipei')

    last_tw = None
    last_intl = None

    print("ITZY 監控啟動，每 15 秒偵測一次。")

    while True:
        try:
            now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

            tw_now, intl_now, total_now = get_total_sales(session)

            if tw_now is None:
                print(f"[{now}] 台灣 API 異常，本輪跳過")
                time.sleep(CHECK_SECONDS)
                continue

            last_total_in_sheet = get_latest_total()
            diff = total_now - last_total_in_sheet

            if diff < -MAX_REASONABLE_DROP:
                print(f"[{now}] 偵測到異常下降 {diff}，跳過不寫入。API總量={total_now} Sheet總量={last_total_in_sheet}")
                time.sleep(CHECK_SECONDS)
                continue

            if last_tw is None:
                last_tw = tw_now
                last_intl = intl_now

            if diff != 0:
                tw_delta = tw_now - last_tw
                intl_delta = intl_now - last_intl

                source_parts = []

                if tw_delta > 0:
                    source_parts.append(f"TW+{tw_delta}")
                elif tw_delta < 0:
                    source_parts.append(f"TW退{abs(tw_delta)}")

                if intl_delta > 0:
                    source_parts.append(f"INTL+{intl_delta}")
                elif intl_delta < 0:
                    source_parts.append(f"INTL退{abs(intl_delta)}")

                source = " / ".join(source_parts) if source_parts else ("合計變動" if diff > 0 else "合計退單")

                ok = append_sale_log(now, diff, source, total_now)

                if ok:
                    print(f"[新增] {now} | +{diff if diff > 0 else diff} | {source} | 累:{total_now}")
            else:
                print(f"[檢查] {now} | 無變動 | TW:{tw_now} INTL:{intl_now} 總:{total_now}")

            last_tw = tw_now
            last_intl = intl_now

        except Exception as e:
            print(f"[錯誤] {e}")

        time.sleep(CHECK_SECONDS)

if __name__ == "__main__":
    run_monitor()
