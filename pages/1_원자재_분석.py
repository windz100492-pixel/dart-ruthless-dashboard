import streamlit as st
import yfinance as yf
import pandas as pd
import plotly.graph_objects as go

st.set_page_config(page_title="매크로 대시보드", page_icon="🌐", layout="wide")

# ==========================================
# [HARNESS CORE] 글로벌 매크로 지표 도메인 레지스트리
# ==========================================
MACRO_MAP = {
    "🏦 환율 및 금리": {
        "원/달러 환율": "KRW=X",
        "달러 인덱스": "DX-Y.NYB",
        "미 국채 10년물": "^TNX",
        "미 국채 2년물": "^IRX"
    },
    "👑 귀금속": { "금 (Gold)": "GC=F", "은 (Silver)": "SI=F", "백금 (Platinum)": "PL=F", "팔라듐 (Palladium)": "PA=F" },
    "⚡ 에너지": { "WTI 원유": "CL=F", "브렌트 원유": "BZ=F", "천연가스": "NG=F", "가솔린": "RB=F" },
    "🏭 비철금속": { "구리 (Copper)": "HG=F", "알루미늄": "ALI=F" },
    "🌾 곡물 & 소프트": { "옥수수 (Corn)": "ZC=F", "대두 (Soybean)": "ZS=F", "소맥 (Wheat)": "KE=F", "코코아 (Cocoa)": "CC=F", "커피 (Coffee)": "KC=F" },
    "🚢 해운 및 조선": {
        "KODEX 조선 (Proxy)": "091160.KS",
        "해운 운임 BDI (BDRY)": "BDRY"
    }
}

# ==========================================
# [DATA LAYER] 네트워크 I/O 및 캐시 계층
# 왜?: I/O 호출은 가장 비싼 작업이다. 1시간(3600초) 단위로 메모리에 박제하여 외부 API 의존도를 최소화한다.
# ==========================================
@st.cache_data(ttl=3600, show_spinner=False)
def load_all_macro_data(period="2y"):
    data_store = {}
    for category, items in MACRO_MAP.items():
        data_store[category] = {}
        for name, sym in items.items():
            try:
                df = yf.download(sym, period=period, progress=False)
                if df.empty: continue
                
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                    
                data_store[category][name] = df.dropna()
            except Exception:
                # 왜?: 특정 티커 하나가 죽었다고 전체 앱이 뻗는 스파게티를 막기 위한 방어 로직
                continue
    return data_store

# ==========================================
# [STATE MANAGEMENT] 콜백 라우팅
# ==========================================
def change_selected_macro(new_macro, new_category):
    st.session_state['selected_macro'] = new_macro
    st.session_state['selected_category'] = new_category

if 'selected_macro' not in st.session_state:
    st.session_state['selected_macro'] = "원/달러 환율"
    st.session_state['selected_category'] = "🏦 환율 및 금리"

# ==========================================
# [PRESENTATION LAYER] 렌더링 엔진
# ==========================================
def render_dashboard():
    with st.spinner("글로벌 매크로 지표 동기화 중..."):
        data_store = load_all_macro_data()

    if not data_store:
        st.error("네트워크 I/O 에러: 데이터를 불러올 수 없다.")
        return

    selected_item = st.session_state['selected_macro']
    selected_cat = st.session_state['selected_category']
    
    # 엣지 케이스 방어: 캐시 미스나 티커 유실 시 안전한 폴백(Fallback) 스위칭
    if selected_item not in data_store.get(selected_cat, {}):
        selected_cat = list(data_store.keys())[0]
        selected_item = list(data_store[selected_cat].keys())[0]

    df_main = data_store[selected_cat][selected_item]
    
    col_header_1, col_header_2 = st.columns([1, 4])
    
    with col_header_1:
        st.caption(f"{selected_cat}")
        st.header(selected_item)
        
        latest_close = df_main['Close'].iloc[-1]
        prev_close = df_main['Close'].iloc[-2]
        change_val = latest_close - prev_close
        
        # 왜?: 금리(Zero) 환경에서 발생할 수 있는 ZeroDivisionError 원천 차단
        change_pct = (change_val / prev_close) * 100 if prev_close != 0 else 0.0
            
        # [Architect Logic] 데이터 타입 동적 추론
        is_rate = selected_cat == "🏦 환율 및 금리" and ("금리" in selected_item or "국채" in selected_item)
        is_krw = "원/달러" in selected_item or "Proxy" in selected_item
        is_index = "인덱스" in selected_item
        
        unit = "%" if is_rate else ("" if is_index else ("₩" if is_krw else "$"))
        color = "#e74c3c" if change_val < 0 else "#2ecc71"
        
        # 왜?: 원화(KRW)에 센트 단위 연산을 태우는 무식한 메모리 낭비를 제거. 정수형 강제 캐스팅.
        if is_rate:
            display_price = f"{latest_close:,.2f}{unit}"
            change_str = f"{change_pct:+.2f}% ({change_val:+.2f})"
        elif is_krw:
            display_price = f"{unit}{int(latest_close):,}"
            change_str = f"{change_pct:+.2f}% ({int(change_val):+,})"
        else:
            display_price = f"{unit}{latest_close:,.2f}"
            change_str = f"{change_pct:+.2f}% ({change_val:+.2f})"

        st.markdown(f"<h2 style='margin-bottom:0px; padding-bottom:0px;'>{display_price}</h2>", unsafe_allow_html=True)
        st.markdown(f"<span style='color:{color}; font-weight:bold;'>{change_str}</span>", unsafe_allow_html=True)

    with col_header_2:
        fig = go.Figure()
        
        # 왜?: 금리와 환율은 변동폭이 작아 캔들스틱으로 그리면 노이즈만 발생. Line 차트로 추세 가시성 확보.
        if is_rate or "환율" in selected_item or "인덱스" in selected_item:
            fig.add_trace(go.Scatter(
                x=df_main.index, y=df_main['Close'], 
                mode='lines', line=dict(color='#3498db', width=2), name="종가"
            ))
        else:
            fig.add_trace(go.Candlestick(
                x=df_main.index, open=df_main['Open'], high=df_main['High'], low=df_main['Low'], close=df_main['Close'],
                increasing_line_color='#ef5350', decreasing_line_color='#2980b9', name="시세"
            ))
        
        ma20 = df_main['Close'].rolling(20).mean()
        ma50 = df_main['Close'].rolling(50).mean()
        fig.add_trace(go.Scatter(x=df_main.index, y=ma20, line=dict(color='#f39c12', width=1.5), name='MA20'))
        fig.add_trace(go.Scatter(x=df_main.index, y=ma50, line=dict(color='#9b59b6', width=1.5), name='MA50'))

        fig.update_layout(
            height=400, margin=dict(l=0, r=0, t=30, b=0),
            template="plotly_white", hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0)
        )
        # 왜?: 브라우저 DOM 트리를 무겁게 만드는 쓸데없는 Range Slider 강제 절단
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])], rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    st.subheader("매크로 지표 실시간 시세")
    
    # 6개 도메인 확장에 대응하는 동적 그리드 렌더링
    num_cats = len(data_store)
    cols = st.columns(min(num_cats, 6))
    
    col_idx = 0
    for category, items in data_store.items():
        with cols[col_idx % min(num_cats, 6)]:
            with st.container(border=True):
                st.markdown(f"**{category}**")
                for name, df in items.items():
                    if df.empty: continue
                    
                    last_px = df['Close'].iloc[-1]
                    prev_px = df['Close'].iloc[-2]
                    chg_pct = ((last_px - prev_px) / prev_px) * 100 if prev_px != 0 else 0.0
                    
                    btn_col, val_col = st.columns([0.6, 0.4])
                    with btn_col:
                        is_selected = (name == selected_item)
                        btn_type = "primary" if is_selected else "secondary"
                        st.button(name, key=f"btn_{name}", type=btn_type, use_container_width=True, 
                                  on_click=change_selected_macro, args=(name, category))
                        
                    with val_col:
                        color = "red" if chg_pct < 0 else "green"
                        
                        is_rate_card = category == "🏦 환율 및 금리" and ("금리" in name or "국채" in name)
                        is_krw_card = "원/달러" in name or "Proxy" in name
                        is_index_card = "인덱스" in name
                        
                        u = "%" if is_rate_card else ("" if is_index_card else ("₩" if is_krw_card else "$"))
                        
                        if is_rate_card:
                            display_card_price = f"{last_px:,.2f}{u}"
                        elif is_krw_card:
                            display_card_price = f"{u}{int(last_px):,}"
                        else:
                            display_card_price = f"{u}{last_px:,.2f}"
                            
                        st.markdown(f"<div style='text-align:right; font-size:14px;'><b>{display_card_price}</b><br><span style='color:{color};'>{chg_pct:+.2f}%</span></div>", unsafe_allow_html=True)
        col_idx += 1

if __name__ == "__main__":
    render_dashboard()