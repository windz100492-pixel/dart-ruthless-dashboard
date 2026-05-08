import os
import re
import zipfile
import asyncio
import aiohttp
import pandas as pd
import numpy as np
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import xml.etree.ElementTree as ET
from datetime import datetime
import streamlit as st
import sqlite3
import hashlib
import json
import time
from typing import Dict, Tuple, Any, Optional
import requests

# ==========================================
# [HARNESS CORE] 0. Hardcore Local Caching Engine
# ==========================================
class HardcoreSQLiteCache:
    def __init__(self, db_path: str = "dart_ruthless_cache.db", ttl_sec: int = 604800):
        self.db_path = db_path
        self.ttl_sec = ttl_sec
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    response_payload TEXT,
                    timestamp REAL
                )
            """)
            conn.execute("DELETE FROM api_cache WHERE timestamp < ?", (time.time() - self.ttl_sec,))

    def _generate_key(self, url: str, params: dict) -> str:
        safe_params = {k: v for k, v in params.items() if k != 'crtfc_key'}
        param_str = json.dumps(safe_params, sort_keys=True)
        return hashlib.sha256(f"{url}|{param_str}".encode('utf-8')).hexdigest()

    def get(self, url: str, params: dict) -> Optional[dict]:
        key = self._generate_key(url, params)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT response_payload, timestamp FROM api_cache WHERE cache_key = ?", (key,))
            row = cursor.fetchone()
            if row:
                if time.time() - row[1] < self.ttl_sec:
                    return json.loads(row[0])
                else:
                    cursor.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
            return None

    def set(self, url: str, params: dict, response_data: dict):
        key = self._generate_key(url, params)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO api_cache (cache_key, response_payload, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(response_data), time.time())
            )

# ==========================================
# [HARNESS CORE] 1. DART Data Access Object
# ==========================================
class DartCoreClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.summary_url = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
        self.full_url = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
        self.corp_code_url = "https://opendart.fss.or.kr/api/corpCode.xml"
        self.reprt_codes = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}
        self.cache_dir = "dart_cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        self.acc_nm_pattern = re.compile(r'[\sⅠⅡI1제기]')
        self.cache_engine = HardcoreSQLiteCache()

    async def _request(self, session: aiohttp.ClientSession, url: str, params: dict) -> dict:
        cached_payload = self.cache_engine.get(url, params)
        if cached_payload: return cached_payload
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as res:
                if res.status != 200: return {}
                data = await res.json()
                if data and data.get('status') == '000':
                    self.cache_engine.set(url, params, data)
                return data
        except Exception:
            return {}

    async def get_corp_code(self, session: aiohttp.ClientSession, user_input: str) -> Tuple[str, str, str]:
        corp_zip_path = os.path.join(self.cache_dir, "CORPCODE.zip")
        if not os.path.exists(corp_zip_path):
            async with session.get(self.corp_code_url, params={'crtfc_key': self.api_key}) as res:
                if res.status != 200: raise ConnectionError("DART API 다운로드 실패")
                with open(corp_zip_path, 'wb') as f:
                    f.write(await res.read())

        is_digit = user_input.isdigit()
        search_target = user_input.zfill(6) if is_digit else user_input.replace(" ", "").lower()
        
        with zipfile.ZipFile(corp_zip_path) as z:
            with z.open('CORPCODE.xml') as f:
                context = ET.iterparse(f, events=('end',))
                for event, elem in context:
                    if elem.tag == 'list':
                        s_code = elem.findtext('stock_code')
                        if s_code and s_code.strip():
                            s_code = s_code.strip()
                            c_name = elem.findtext('corp_name').strip()
                            c_code = elem.findtext('corp_code')
                            if (is_digit and s_code == search_target) or (not is_digit and c_name.replace(" ", "").lower() == search_target):
                                elem.clear()
                                return c_code, s_code, c_name
                        elem.clear()
        raise ValueError("상장사를 찾을 수 없다.")

    def _clean_val(self, v: Any) -> float:
        if not v or v == '-': return 0.0
        try: return float(str(v).replace(',', '').strip())
        except ValueError: return 0.0

    async def fetch_quarter(self, session: aiohttp.ClientSession, corp_code: str, year: int, reprt_code: str, q_num: int):
        params = {'crtfc_key': self.api_key, 'corp_code': corp_code, 'bsns_year': str(year), 'reprt_code': reprt_code}
        data = await self._request(session, self.summary_url, params)
        if not data or data.get('status') != '000': 
            return year, q_num, np.nan, np.nan, np.nan, np.nan, np.nan
        
        df_list = data.get('list', [])
        def _extract(t_div):
            r, o, ni, a, e = np.nan, np.nan, np.nan, np.nan, np.nan
            for i in df_list:
                if i.get('fs_div') != t_div: continue
                acc = self.acc_nm_pattern.sub('', i.get('account_nm', ''))
                amt = self._clean_val(i.get('thstrm_amount'))
                add_amt = self._clean_val(i.get('thstrm_add_amount'))
                
                is_is = ('매출' in acc or '영업' in acc or '순이익' in acc) and '총' not in acc and '외' not in acc and '포괄' not in acc and '지배' not in acc
                val = amt
                if is_is:
                    if q_num == 3 and add_amt != 0: val = add_amt
                    elif q_num == 2 and add_amt != 0 and abs(add_amt) > abs(amt): val = add_amt
                
                if ('매출' in acc or '영업수익' in acc) and pd.isna(r): r = val
                elif '영업' in acc and ('이익' in acc or '손익' in acc or '손실' in acc) and pd.isna(o): o = val
                elif '순이익' in acc and pd.isna(ni): ni = val
                elif '자산총계' in acc and pd.isna(a): a = val
                elif '자본총계' in acc and pd.isna(e): e = val
            return r, o, ni, a, e

        rev, op, ni, ast, eqt = _extract('CFS')
        if pd.isna(rev) or pd.isna(op): 
            rev, op, ni, ast, eqt = _extract('OFS')
        return year, q_num, rev, op, ni, ast, eqt

    async def fetch_all_data(self, user_input: str, years: int):
        connector = aiohttp.TCPConnector(limit=20, limit_per_host=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            corp_code, stock_code, corp_name = await self.get_corp_code(session, user_input)
            
            curr_year = datetime.now().year
            start_year = curr_year - years
            
            tasks = [
                self.fetch_quarter(session, corp_code, y, r_code, q)
                for y in range(start_year, curr_year + 1)
                for r_code, q in self.reprt_codes.items()
            ]
            
            results = await asyncio.gather(*tasks)
            df = pd.DataFrame(results, columns=['Year', 'Quarter', 'Rev_Cum', 'Op_Cum', 'NI_Cum', 'Assets', 'Equity'])
            
            df.set_index(['Year', 'Quarter'], inplace=True)
            idx = pd.MultiIndex.from_product([range(start_year, curr_year + 1), [1, 2, 3, 4]], names=['Year', 'Quarter'])
            df = df.reindex(idx)
            
            for y in range(start_year, curr_year + 1):
                mask = df.index.get_level_values('Year') == y
                df.loc[mask, ['Rev_Cum', 'Op_Cum', 'NI_Cum']] = df.loc[mask, ['Rev_Cum', 'Op_Cum', 'NI_Cum']].interpolate(limit_direction='both')
                df.loc[mask, ['Assets', 'Equity']] = df.loc[mask, ['Assets', 'Equity']].ffill().bfill()
                    
            df = df.reset_index().dropna(subset=['Rev_Cum'])
            if df.empty: raise ValueError("DART 재무 데이터가 없다.")
            
            df['Date'] = pd.to_datetime(df['Year'].astype(str) + '-' + (df['Quarter'] * 3).astype(str) + '-01') + pd.offsets.MonthEnd(0)
            df.set_index('Date', inplace=True)
            
            df['Rev'] = df.groupby('Year')['Rev_Cum'].diff().fillna(df['Rev_Cum'])
            df['Op'] = df.groupby('Year')['Op_Cum'].diff().fillna(df['Op_Cum'])
            df['NI'] = df.groupby('Year')['NI_Cum'].diff().fillna(df['NI_Cum'])
            
            df = df[df['Rev'] > 0].copy()
            df['OPM'] = (df['Op'] / df['Rev']) * 100
            df['YoY'] = df['Rev'].pct_change(periods=4) * 100 
            df['Rev_QoQ'] = df['Rev'].pct_change(periods=1) * 100
            
            df['Rev_TTM'] = df['Rev'].rolling(window=4, min_periods=1).sum()
            df['NI_TTM'] = df['NI'].rolling(window=4, min_periods=1).sum()
            
            df['NI_Margin'] = np.where(df['Rev_TTM'] > 0, (df['NI_TTM'] / df['Rev_TTM']) * 100, 0) 
            df['Asset_Turnover'] = np.where(df['Assets'] > 0, df['Rev_TTM'] / df['Assets'], 0) 
            df['Leverage'] = np.where(df['Equity'] > 0, df['Assets'] / df['Equity'], 0) 
            df['ROE'] = np.where(df['Equity'] > 0, (df['NI_TTM'] / df['Equity']) * 100, 0) 
            
            display_start_year = start_year + 1
            stock = yf.download(f"{stock_code}.KS", start=f"{display_start_year}-01-01", progress=False)
            if stock.empty: stock = yf.download(f"{stock_code}.KQ", start=f"{display_start_year}-01-01", progress=False)
            if isinstance(stock.columns, pd.MultiIndex): stock.columns = stock.columns.get_level_values(0)
            
            return df[df['Year'] >= display_start_year], stock, corp_name, stock_code, corp_code

# ==========================================
# [HARNESS CORE] 2. Deterministic Inference Engine
# ==========================================
class RuthlessInferenceEngine:
    @staticmethod
    def analyze_dupont(df: pd.DataFrame) -> Dict[str, Any]:
        if len(df) < 4:
            return {"status": "error", "message": "데이터 부족. 추론 불가."}
            
        latest = df.iloc[-1]
        prev_year = df.iloc[-5] if len(df) >= 5 else df.iloc[0]
        
        roe = latest['ROE']
        margin = latest['NI_Margin']
        turnover = latest['Asset_Turnover']
        leverage = latest['Leverage']
        
        margin_trend = margin - prev_year['NI_Margin']
        turnover_trend = turnover - prev_year['Asset_Turnover']
        leverage_trend = leverage - prev_year['Leverage']

        synthesis = {}
        if roe > 15 and leverage < 2.0 and margin > 10:
            synthesis = {"level": "success", "title": f"[초우량 등급] 종합 ROE {roe:.1f}%", "desc": "부채(레버리지)에 의존하지 않고, 압도적인 마진과 자산 효율성으로 만들어낸 진짜 수익이다."}
        elif roe > 10 and leverage > 2.5:
            synthesis = {"level": "warning", "title": f"[주의 요망] 종합 ROE {roe:.1f}%", "desc": "겉보기엔 준수해 보이나, 속 빈 강정이다. 이익률이나 회전율의 결함을 빚(부채)으로 가리고 있다."}
        elif roe < 5 or margin < 0:
            synthesis = {"level": "error", "title": f"[투자 부적격] 종합 ROE {roe:.1f}%", "desc": "자본 비용조차 못 건지는 상태다. 기업의 아키텍처가 무너졌다."}
        else:
            synthesis = {"level": "info", "title": f"[무난/관망] 종합 ROE {roe:.1f}%", "desc": "치명적인 누수는 없으나, 시장을 압도할 만한 퍼포먼스도 보이지 않는 평범한 상태다."}

        details = []
        if margin > 15: details.append(f"🟢 **압도적 마진율**: 순이익률 `{margin:.1f}%`. 강력한 해자.")
        elif margin_trend < -3: details.append(f"🔴 **수익성 훼손**: 순이익률 전년비 `{margin_trend:+.1f}%p` 급감.")
        else: details.append(f"⚪ **마진율 평이**: 순이익률 `{margin:.1f}%`.")

        if turnover_trend < -0.1 and turnover < 0.5: details.append(f"🔴 **자산 비효율 경고**: 자산회전율 `{turnover:.2f}배` 우하향.")
        elif turnover > 1.0: details.append(f"🟢 **극한의 인프라 효율**: 자산회전율 `{turnover:.2f}배`.")
        else: details.append(f"⚪ **자산 효율성 평이**: 자산회전율 `{turnover:.2f}배`.")

        if leverage > 2.5: details.append(f"🔴 **부채 영끌 주의보**: 재무레버리지 `{leverage:.1f}배` 위험 수위.")
        elif leverage_trend > 0.5 and roe > 10: details.append(f"🟡 **레버리지 주도 성장**: 부채 비중 최근 급증.")
        else: details.append(f"🟢 **재무 건전성 방어**: 재무레버리지 `{leverage:.1f}배`.")

        return {"status": "success", "synthesis": synthesis, "details": details}


# ==========================================
# [DATA LAYER] 네이버 금융 컨센서스 자동 스크래핑 엔진 (V11 - Precision Strike)
# ==========================================
import requests
from bs4 import BeautifulSoup

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_naver_consensus(stock_code):
    """
    네이버 wisereport 'Financial Summary' 분기 탭에서 분기 추정치를 모두 추출.
    - 데이터 소스: navercomp.wisereport.co.kr (Financial Summary)
    - finance.naver.com 메인의 cop_analysis 표는 분기 (E) 1개뿐이라 부적합.
    반환: ([(period, rev_억, op_억), ...], 상태메시지)
    """
    url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Referer': f'https://finance.naver.com/item/coinfo.naver?code={stock_code}'
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, 'html.parser')

        # ============================================================
        # 분기 테이블 식별: 헤더에 'YYYY/MM' 형식 컬럼이 5개 이상인 table
        # (wisereport 페이지에는 연간/분기 두 테이블이 함께 있음)
        # ============================================================
        quarterly_pattern = re.compile(r'(\d{4})[/.](\d{2})')
        target_table = None

        for table in soup.find_all('table'):
            ths = table.find_all('th')
            quarterly_cells = [th for th in ths if quarterly_pattern.search(th.get_text())]
            if len(quarterly_cells) >= 5:
                target_table = table
                break

        if target_table is None:
            return [], "[실패] wisereport 분기 테이블 미발견 (마크업 변경 의심)"

        # 헤더에서 분기 컬럼만 순서대로 수집 (rowspan 빈셀 제외)
        thead = target_table.find('thead')
        if not thead:
            return [], "[실패] thead 없음"

        quarter_headers = []
        for th in thead.find_all('th'):
            text = th.get_text(strip=True)
            if quarterly_pattern.search(text):
                quarter_headers.append(text)

        # 분기 헤더 중 (E)/(P) 마킹된 컬럼 인덱스 추출
        est_indices = [(i, t) for i, t in enumerate(quarter_headers)
                       if '(E)' in t or '(P)' in t]
        if not est_indices:
            return [], f"[실패] 분기 (E)/(P) 없음 (헤더={quarter_headers})"

        # ============================================================
        # tbody에서 매출액 / 영업이익(발표기준 우선) 행 찾기
        # wisereport는 영업이익이 두 종류 (일반 / 발표기준), (E)는 발표기준에만 채워짐
        # ============================================================
        tbody = target_table.find('tbody')
        if not tbody:
            return [], "[실패] tbody 없음"

        rev_tds = None
        op_tds = None
        op_announce_tds = None

        for row in tbody.find_all('tr'):
            th = row.find('th')
            if not th:
                continue
            row_name = th.get_text(strip=True).replace(' ', '').replace('\xa0', '')

            if row_name == '매출액' and rev_tds is None:
                rev_tds = row.find_all('td')
            elif row_name == '영업이익(발표기준)' and op_announce_tds is None:
                op_announce_tds = row.find_all('td')
            elif row_name == '영업이익' and op_tds is None:
                op_tds = row.find_all('td')

        # 발표기준 우선, 없으면 일반 영업이익
        op_final = op_announce_tds if op_announce_tds is not None else op_tds

        if rev_tds is None or op_final is None:
            return [], "[실패] 매출/영업이익 행 매칭 실패"

        # ============================================================
        # 각 (E) 컬럼에서 값 추출
        # 주의: tbody의 td 인덱스가 thead의 분기 헤더 인덱스와 1:1 매핑되어야 함
        # (wisereport는 보통 한 테이블에 분기만 있어 이 가정이 성립)
        # ============================================================
        results = []
        for idx, period in est_indices:
            if idx >= len(rev_tds) or idx >= len(op_final):
                continue
            rv = rev_tds[idx].get_text(strip=True).replace(',', '')
            ov = op_final[idx].get_text(strip=True).replace(',', '')
            try:
                rv_f = float(rv) if rv and rv != '-' else 0.0
                ov_f = float(ov) if ov and ov != '-' else 0.0
                if rv_f > 0:
                    results.append((period, rv_f, ov_f))
            except ValueError:
                continue

        if not results:
            return [], "[실패] 추정치 파싱 결과 0건"
        return results, f"성공 ({len(results)}개 분기 자동 로드)"

    except Exception as e:
        return [], f"[에러] {str(e)}"

def fetch_naver_consensus_v3(stock_code):
    """[v3] cop_analysis 기반 - 분기 (E) 추출 (list 반환)"""
    url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }
    debug = []

    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, 'html.parser')

        analysis_div = soup.find('div', class_='cop_analysis')
        if not analysis_div:
            return [], "[실패] cop_analysis 없음", debug

        thead = analysis_div.find('thead')
        if not thead:
            return [], "[실패] thead 없음", debug

        header_rows = thead.find_all('tr')
        if len(header_rows) < 2:
            return [], "[실패] 헤더 비정상", debug

        # 분기 영역 시작 (colspan 누적)
        top_ths = header_rows[0].find_all('th')
        quarterly_start = -1
        cum = 0
        for th in top_ths:
            if th.get('rowspan'):
                continue
            text = th.get_text(strip=True)
            colspan = int(th.get('colspan', '1'))
            if '분기' in text:
                quarterly_start = cum
                break
            cum += colspan

        date_headers = header_rows[1].find_all('th')
        if quarterly_start < 0:
            quarterly_start = max(0, len(date_headers) - 4)

        debug.append(f"📅 분기 시작: idx={quarterly_start}")
        all_headers = [th.get_text(strip=True) for th in date_headers]
        debug.append(f"📅 전체 헤더: {all_headers}")

        # 분기 영역에서 (E)/(P) 모두 수집
        est_columns = []
        for i in range(quarterly_start, len(date_headers)):
            text = date_headers[i].get_text(strip=True)
            if '(E)' in text or '(P)' in text:
                est_columns.append((i, text))

        debug.append(f"🎯 (E) 컬럼: {est_columns}")

        if not est_columns:
            return [], f"[실패] 분기 (E)/(P) 없음", debug

        tbody = analysis_div.find('tbody')
        if not tbody:
            return [], "[실패] tbody 없음", debug

        rev_tds, op_tds = None, None
        for row in tbody.find_all('tr'):
            th = row.find('th')
            if not th: continue
            rn = th.get_text(strip=True).replace(' ', '').replace('\xa0', '')
            if rn == '매출액' and rev_tds is None:
                rev_tds = row.find_all('td')
            elif rn == '영업이익' and op_tds is None:
                op_tds = row.find_all('td')

        if not rev_tds or not op_tds:
            return [], "[실패] 행 매칭 실패", debug

        results = []
        for idx, period in est_columns:
            if idx >= len(rev_tds) or idx >= len(op_tds):
                continue
            rv = rev_tds[idx].get_text(strip=True).replace(',', '')
            ov = op_tds[idx].get_text(strip=True).replace(',', '')
            try:
                rv_f = float(rv) if rv and rv != '-' else 0.0
                ov_f = float(ov) if ov and ov != '-' else 0.0
                if rv_f > 0:
                    results.append((period, rv_f, ov_f))
            except ValueError:
                continue

        debug.append(f"✅ 결과: {len(results)}개 → {[(p, r, o) for p, r, o in results]}")

        if not results:
            return [], "[실패] 파싱 결과 0건", debug
        return results, f"성공 ({len(results)}개 분기 자동)", debug

    except Exception as e:
        debug.append(f"❌ {e}")
        return [], f"[에러] {str(e)}", debug

# ==========================================
# [PRESENTATION] Streamlit UI Layer
# ==========================================
st.set_page_config(page_title="DART Financial Dashboard", layout="wide")

def run_async_safe(coroutine):
    try: loop = asyncio.get_running_loop()
    except RuntimeError: loop = None
    if loop and loop.is_running(): return asyncio.run_coroutine_threadsafe(coroutine, loop).result()
    else: return asyncio.run(coroutine)

@st.cache_data(ttl=86400, show_spinner=False)
def load_data(api_key, query, years):
    client = DartCoreClient(api_key)
    return run_async_safe(client.fetch_all_data(query, years))

def render_dashboard():
    st.title("📊 DART 분석 대시보드 (Optimized Architecture)")
    
    default_api_key = ""
    try:
        if hasattr(st, "secrets") and "DART_API_KEY" in st.secrets:
            default_api_key = st.secrets["DART_API_KEY"]
    except Exception:
        pass

    # ==========================================
    # 1. 사이드바 (상단): 검색 폼 렌더링
    # ==========================================
    with st.sidebar:
        st.header("설정")
        with st.form(key='search_form'):
            api_key = st.text_input("DART API Key", value=default_api_key, type="password")
            query = st.text_input("종목명 또는 코드", "삼성전자")
            years = st.slider("조회 기간(년)", 3, 20, 10)
            fetch_btn = st.form_submit_button("데이터 조회", use_container_width=True)

    # ==========================================
    # 2. 메인 데이터 패치 계층 (폼 제출 시 즉시 실행)
    # ==========================================
    if fetch_btn:
        if not api_key:
            st.error("API Key 누락. 인증 없는 요청은 네트워크 I/O 낭비다.")
            return
        load_data.clear()
        with st.spinner("데이터 패치 및 연산 중..."):
            try:
                # DART 데이터 로드
                df, stock, corp_name, stock_code, corp_code = load_data(api_key, query, years)
                st.session_state.update({'df': df, 'stock': stock, 'corp_name': corp_name, 'stock_code': stock_code})

                # v3 함수 호출 (ajax 엔드포인트 직접 호출)
                naver_ests, msg, debug = fetch_naver_consensus_v3(stock_code)
                st.session_state['naver_estimates'] = naver_ests
                st.session_state['consensus_msg'] = msg
                st.session_state['naver_debug'] = debug

                for i, (_, rev, op) in enumerate(naver_ests):
                    st.session_state[f'e_rev_{i}'] = float(rev)
                    st.session_state[f'e_op_{i}'] = float(op)

            except Exception as e:
                st.error(f"런타임 에러: {e}")
                return

    # ==========================================
    # 3. 사이드바 (하단): 컨센서스 UI 렌더링 (최신화된 세션 데이터 반영)
    # ==========================================
    with st.sidebar:
        st.markdown("---")
        st.subheader("🔮 다음 분기 컨센서스 (E)")

        # 디버그 정보 노출 (스크래핑 진단용)
        debug_lines = st.session_state.get('naver_debug', [])
        if debug_lines:
            with st.expander("🔍 스크래핑 디버그"):
                for line in debug_lines:
                    st.caption(line)

        naver_ests = st.session_state.get('naver_estimates', [])
        msg = st.session_state.get('consensus_msg', '')

        if naver_ests:
            periods_str = ' / '.join([e[0] for e in naver_ests])
            st.caption(f"✅ 네이버 자동 로드: **{periods_str}**")
        else:
            st.caption(f"⚠️ {msg or '컨센서스 없음. 수동 입력하세요.'}")

        n_est = st.slider("표시할 추정 분기 수", 1, 4, 3)

        estimates = []
        for i in range(n_est):
            auto_label = f" *(자동: {naver_ests[i][0]})*" if i < len(naver_ests) else " *(수동)*"
            st.markdown(f"**Q+{i+1}**{auto_label}")

            # 첫 렌더 디폴트 (session_state에 키가 없을 때만 박는다)
            if f'e_rev_{i}' not in st.session_state:
                st.session_state[f'e_rev_{i}'] = float(naver_ests[i][1]) if i < len(naver_ests) else 0.0
            if f'e_op_{i}' not in st.session_state:
                st.session_state[f'e_op_{i}'] = float(naver_ests[i][2]) if i < len(naver_ests) else 0.0

            c1, c2 = st.columns(2)
            with c1:
                r = st.number_input("매출(억)", step=100.0,
                                    key=f"e_rev_{i}", label_visibility="collapsed")
            with c2:
                o = st.number_input("영업익(억)", step=10.0,
                                    key=f"e_op_{i}", label_visibility="collapsed")
            estimates.append((r, o))

    # ==========================================
    # 4. 메인 뷰 (차트 렌더링)
    # ==========================================
    if 'df' in st.session_state:
        df = st.session_state['df'].copy() 
        stock = st.session_state['stock']
        corp_name = st.session_state['corp_name']
        stock_code = st.session_state['stock_code']
        
        st.subheader(f"{corp_name} ({stock_code}) - {years}년 재무 및 주가 추이")
        
        # ============================================================
        # 다중 추정 분기 행 추가 (datetime 인덱스로 안전하게)
        # ============================================================
        estimates_active = [(r, o) for r, o in estimates if r > 0 and o > 0]
        n_estimated = len(estimates_active)

        if n_estimated > 0:
            n_orig = len(df)
            last_date = pd.Timestamp(df.index[-1])

            for i, (e_rev, e_op) in enumerate(estimates_active):
                e_rev_won = e_rev * 1e8
                e_op_won = e_op * 1e8

                # QoQ: 직전 분기 (i==0이면 실제 마지막, 아니면 직전 추정치)
                prev_rev_won = df['Rev'].iloc[-1] if i == 0 else estimates_active[i-1][0] * 1e8

                # YoY: 4분기 전 (원본 df 기준 위치)
                yr_idx = n_orig - 4 + i
                prev_yr_rev_won = df['Rev'].iloc[yr_idx] if 0 <= yr_idx < n_orig else np.nan

                e_yoy = ((e_rev_won - prev_yr_rev_won) / prev_yr_rev_won) * 100 \
                        if pd.notna(prev_yr_rev_won) and prev_yr_rev_won != 0 else 0
                e_qoq = ((e_rev_won - prev_rev_won) / prev_rev_won) * 100 if prev_rev_won != 0 else 0
                e_opm = (e_op_won / e_rev_won) * 100 if e_rev_won != 0 else 0

                next_date = (last_date + pd.DateOffset(months=3*(i+1))) + pd.offsets.MonthEnd(0)

                new_row = pd.DataFrame({
                    'Rev': [e_rev_won], 'Op': [e_op_won], 'YoY': [e_yoy],
                    'Rev_QoQ': [e_qoq], 'OPM': [e_opm],
                    'ROE': [df['ROE'].iloc[-1]], 'NI_Margin': [df['NI_Margin'].iloc[-1]],
                    'Asset_Turnover': [df['Asset_Turnover'].iloc[-1]],
                    'Leverage': [df['Leverage'].iloc[-1]]
                }, index=[next_date])
                df = pd.concat([df, new_row])

            df.index = pd.to_datetime(df.index)

        # 색상 배열 - 추정 분기들만 진하게
        rev_colors = ["rgba(231, 76, 60, 0.35)"] * len(df)
        yoy_colors = ["rgba(52, 73, 94, 0.6)"] * len(df)
        qoq_colors = ["rgba(142, 68, 173, 0.5)"] * len(df)

        for k in range(1, n_estimated + 1):
            rev_colors[-k] = "rgba(231, 76, 60, 1.0)"
            yoy_colors[-k] = "rgba(52, 73, 94, 1.0)"
            qoq_colors[-k] = "rgba(142, 68, 173, 1.0)"

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, 
                            row_heights=[0.5, 0.25, 0.25], 
                            specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{"secondary_y": True}]])

        fig.add_trace(go.Scatter(x=stock.index, y=stock['Close'], name="주가", line=dict(color="#2c3e50", width=1.5)), row=1, col=1, secondary_y=False)
        fig.add_trace(go.Bar(x=df.index, y=df['Rev']/1e8, name="분기 매출(억)", marker_color=rev_colors, marker_line_width=0), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(x=df.index, y=df['Op']/1e8, name="분기 영업이익(억)", mode="lines+markers", marker=dict(symbol='square', size=6), line=dict(color="#f39c12", width=2)), row=1, col=1, secondary_y=True)

        fig.add_trace(go.Bar(x=df.index, y=df['YoY'], name="매출 YoY(%)", marker_color=yoy_colors, marker_line_width=0), row=2, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=df.index, y=df['OPM'], name="영업이익률(%)", mode="lines+markers", marker=dict(size=6), line=dict(color="#27ae60", width=2)), row=2, col=1, secondary_y=True)

        fig.add_trace(go.Bar(x=df.index, y=df['Rev_QoQ'], name="매출 QoQ(%)", marker_color=qoq_colors, marker_line_width=0), row=3, col=1, secondary_y=False)
        fig.add_trace(go.Scatter(x=df.index, y=df['OPM'], name="영업이익률(%)", mode="lines+markers", marker=dict(size=6), line=dict(color="#27ae60", width=2), showlegend=False), row=3, col=1, secondary_y=True)

        fig.update_layout(
            height=750, hovermode="x unified", template="plotly_white", font=dict(color="#2c3e50"), 
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(color="#2c3e50")),
            margin=dict(l=40, r=40, t=60, b=40)
        )
        
        fig.update_yaxes(title_text="주가 (원)", tickformat=",", secondary_y=False, row=1, col=1, showgrid=False, rangemode="tozero")
        fig.update_yaxes(title_text="분기 실적 (억원)", tickformat=",", secondary_y=True, row=1, col=1, showgrid=True, gridcolor='rgba(0,0,0,0.1)', rangemode="tozero")
        
        fig.update_yaxes(title_text="매출 YoY (%)", secondary_y=False, row=2, col=1, showgrid=True, gridcolor='rgba(0,0,0,0.1)', zeroline=True, zerolinecolor='black')
        fig.update_yaxes(title_text="영업이익률 (%)", secondary_y=True, row=2, col=1, showgrid=False)
        
        fig.update_yaxes(title_text="매출 QoQ (%)", secondary_y=False, row=3, col=1, showgrid=True, gridcolor='rgba(0,0,0,0.1)', zeroline=True, zerolinecolor='black')
        fig.update_yaxes(title_text="영업이익률 (%)", secondary_y=True, row=3, col=1, showgrid=False)

        # 추정 분기 모두에 'E' 마커
        for k in range(1, n_estimated + 1):
            fig.add_annotation(
                x=df.index[-k], y=df['Rev'].iloc[-k] / 1e8,
                text="<b>E</b>", showarrow=True, arrowhead=2, arrowsize=1,
                ax=0, ay=-30, font=dict(color="#e74c3c", size=12),
                row=1, col=1, secondary_y=True,
                bgcolor="rgba(255,255,255,0.7)", bordercolor="#e74c3c"
            )

        fig.update_xaxes(showgrid=True, gridcolor='rgba(0,0,0,0.1)', dtick="M12", tickformat="%Y년")
        
        fig.update_xaxes(showgrid=True, gridcolor='rgba(0,0,0,0.1)', dtick="M12", tickformat="%Y년")

        st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("💡 듀퐁 분석 (ROE 분해 트리)")
        st.markdown("**ROE (자기자본이익률)** = **순이익률** (마진) × **총자산회전율** (자산 효율성) × **재무레버리지** (부채 활용도) *(※ TTM 후행 4분기 합산 기준)*")
        
        fig_dupont = make_subplots(rows=2, cols=2, shared_xaxes=True, vertical_spacing=0.1,
                                   subplot_titles=("TTM 분기 ROE (%)", "TTM 순이익률 (%)", "TTM 총자산회전율 (배수)", "기말 재무레버리지 (배수)"))

        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['ROE'], fill='tozeroy', name="ROE", line=dict(color="#8e44ad", width=2)), row=1, col=1)
        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['NI_Margin'], fill='tozeroy', name="순이익률", line=dict(color="#2980b9", width=2)), row=1, col=2)
        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['Asset_Turnover'], fill='tozeroy', name="총자산회전율", line=dict(color="#d35400", width=2)), row=2, col=1)
        fig_dupont.add_trace(go.Scatter(x=df.index, y=df['Leverage'], fill='tozeroy', name="재무레버리지", line=dict(color="#c0392b", width=2)), row=2, col=2)

        fig_dupont.update_layout(height=500, showlegend=False, hovermode="x unified", template="plotly_white", margin=dict(l=40, r=40, t=40, b=40))
        fig_dupont.update_xaxes(showgrid=True, gridcolor='rgba(0,0,0,0.05)', dtick="M12", tickformat="%Y년")
        fig_dupont.update_yaxes(showgrid=True, gridcolor='rgba(0,0,0,0.05)', zeroline=True, zerolinecolor='black')

        st.plotly_chart(fig_dupont, use_container_width=True)

        st.divider()
        st.subheader("🧠 무자비한 AI 추론 결과")
        
        inference_result = RuthlessInferenceEngine.analyze_dupont(st.session_state['df'])
        
        if inference_result['status'] == 'error':
            st.error(inference_result['message'])
        else:
            synth = inference_result['synthesis']
            getattr(st, synth['level'])(f"**{synth['title']}**\n{synth['desc']}")
            
            st.markdown("---")
            st.markdown("**🔍 세부 모듈 스캔 결과**")
            for detail in inference_result['details']:
                st.markdown(detail)

if __name__ == "__main__":
    render_dashboard()