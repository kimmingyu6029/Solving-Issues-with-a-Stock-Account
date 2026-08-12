from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

try:
    import FinanceDataReader as fdr
except ImportError:  # 자동 시세는 선택 기능이다.
    fdr = None


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "lotfolio.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stocks (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                market TEXT DEFAULT 'KRX',
                manual_price REAL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
                stock_code TEXT NOT NULL REFERENCES stocks(code),
                trade_type TEXT NOT NULL CHECK(trade_type IN ('BUY','SELL')),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price >= 0),
                fee REAL NOT NULL DEFAULT 0 CHECK(fee >= 0),
                tax REAL NOT NULL DEFAULT 0 CHECK(tax >= 0),
                traded_at TEXT NOT NULL,
                memo TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buy_trade_id INTEGER NOT NULL REFERENCES trades(id),
                portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
                stock_code TEXT NOT NULL REFERENCES stocks(code),
                original_quantity INTEGER NOT NULL,
                remaining_quantity INTEGER NOT NULL,
                buy_price REAL NOT NULL,
                bought_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sell_allocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sell_trade_id INTEGER NOT NULL REFERENCES trades(id),
                lot_id INTEGER NOT NULL REFERENCES lots(id),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                UNIQUE(sell_trade_id, lot_id)
            );
            INSERT OR IGNORE INTO portfolios(name) VALUES ('기본 포트폴리오');
            """
        )


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def krx_listing() -> pd.DataFrame:
    if fdr is None:
        return pd.DataFrame(columns=["Code", "Name", "Market"])
    try:
        df = fdr.StockListing("KRX")
        needed = [c for c in ["Code", "Name", "Market"] if c in df.columns]
        return df[needed].fillna("")
    except Exception:
        return pd.DataFrame(columns=["Code", "Name", "Market"])


@st.cache_data(ttl=300, show_spinner=False)
def online_price(code: str) -> float | None:
    if fdr is None:
        return None
    try:
        start = (date.today() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        df = fdr.DataReader(code, start)
        return float(df["Close"].dropna().iloc[-1]) if not df.empty else None
    except Exception:
        return None


def portfolios() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM portfolios ORDER BY name").fetchall()


def stocks() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM stocks ORDER BY name").fetchall()


def add_stock(code: str, name: str, market: str, manual_price: float | None) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO stocks(code,name,market,manual_price) VALUES(?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name,
               market=excluded.market, manual_price=excluded.manual_price""",
            (code.strip().upper(), name.strip(), market.strip(), manual_price),
        )


def add_buy(portfolio_id: int, code: str, qty: int, price: float, fee: float,
            traded_at: str, memo: str) -> None:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO trades(portfolio_id,stock_code,trade_type,quantity,price,fee,traded_at,memo)
               VALUES(?,?,'BUY',?,?,?,?,?)""",
            (portfolio_id, code, qty, price, fee, traded_at, memo),
        )
        conn.execute(
            """INSERT INTO lots(buy_trade_id,portfolio_id,stock_code,original_quantity,
               remaining_quantity,buy_price,bought_at) VALUES(?,?,?,?,?,?,?)""",
            (cur.lastrowid, portfolio_id, code, qty, qty, price, traded_at),
        )


def open_lots(portfolio_id: int | None = None, code: str | None = None) -> list[sqlite3.Row]:
    query = """SELECT l.*, s.name, p.name portfolio_name
               FROM lots l JOIN stocks s ON s.code=l.stock_code
               JOIN portfolios p ON p.id=l.portfolio_id
               WHERE l.remaining_quantity > 0"""
    args: list[object] = []
    if portfolio_id is not None:
        query += " AND l.portfolio_id=?"
        args.append(portfolio_id)
    if code is not None:
        query += " AND l.stock_code=?"
        args.append(code)
    query += " ORDER BY l.bought_at, l.id"
    with connect() as conn:
        return conn.execute(query, args).fetchall()


def add_sell(portfolio_id: int, code: str, allocations: dict[int, int], price: float,
             fee: float, tax: float, traded_at: str, memo: str) -> None:
    allocations = {lot_id: qty for lot_id, qty in allocations.items() if qty > 0}
    if not allocations:
        raise ValueError("매도할 로트와 수량을 선택하세요.")
    with connect() as conn:
        selected = conn.execute(
            f"SELECT * FROM lots WHERE id IN ({','.join('?' * len(allocations))})",
            list(allocations),
        ).fetchall()
        if len(selected) != len(allocations):
            raise ValueError("존재하지 않는 로트가 포함되어 있습니다.")
        for lot in selected:
            qty = allocations[lot["id"]]
            if lot["portfolio_id"] != portfolio_id or lot["stock_code"] != code:
                raise ValueError("다른 포트폴리오 또는 종목의 로트입니다.")
            if qty > lot["remaining_quantity"]:
                raise ValueError(f"로트 #{lot['id']}의 잔여 수량을 초과했습니다.")
        total_qty = sum(allocations.values())
        cur = conn.execute(
            """INSERT INTO trades(portfolio_id,stock_code,trade_type,quantity,price,fee,tax,traded_at,memo)
               VALUES(?,?,'SELL',?,?,?,?,?,?)""",
            (portfolio_id, code, total_qty, price, fee, tax, traded_at, memo),
        )
        for lot_id, qty in allocations.items():
            conn.execute("UPDATE lots SET remaining_quantity=remaining_quantity-? WHERE id=?", (qty, lot_id))
            conn.execute(
                "INSERT INTO sell_allocations(sell_trade_id,lot_id,quantity) VALUES(?,?,?)",
                (cur.lastrowid, lot_id, qty),
            )


def current_price(code: str, manual: float | None) -> tuple[float | None, str]:
    fetched = online_price(code)
    if fetched is not None:
        return fetched, "자동"
    return manual, "수동"


def lot_frame() -> pd.DataFrame:
    rows = open_lots()
    records = []
    for r in rows:
        price, source = current_price(r["stock_code"], get_manual_price(r["stock_code"]))
        value = price * r["remaining_quantity"] if price is not None else None
        cost = r["buy_price"] * r["remaining_quantity"]
        records.append({
            "로트": r["id"], "포트폴리오": r["portfolio_name"], "종목": r["name"],
            "코드": r["stock_code"], "매수일": r["bought_at"][:10], "잔여수량": r["remaining_quantity"],
            "매수가": r["buy_price"], "현재가": price, "시세": source,
            "평가손익": None if value is None else value - cost,
            "수익률(%)": None if price is None or r["buy_price"] == 0 else (price / r["buy_price"] - 1) * 100,
        })
    return pd.DataFrame(records)


def get_manual_price(code: str) -> float | None:
    with connect() as conn:
        row = conn.execute("SELECT manual_price FROM stocks WHERE code=?", (code,)).fetchone()
        return row[0] if row else None


def trade_frame() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            """SELECT t.id, t.traded_at 거래일, p.name 포트폴리오, s.name 종목,
               t.stock_code 코드, t.trade_type 구분, t.quantity 수량, t.price 체결가,
               t.fee 수수료, t.tax 세금, t.memo 메모
               FROM trades t JOIN portfolios p ON p.id=t.portfolio_id
               JOIN stocks s ON s.code=t.stock_code ORDER BY t.traded_at DESC,t.id DESC""", conn
        )


def realized_frame() -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            """SELECT t.id 매도번호,t.traded_at 매도일,p.name 포트폴리오,s.name 종목,
               t.price 매도가,l.buy_price 매수가,a.quantity 수량,
               (t.price-l.buy_price)*a.quantity
                - (t.fee+t.tax)*a.quantity*1.0/t.quantity 실현손익
               FROM sell_allocations a JOIN trades t ON t.id=a.sell_trade_id
               JOIN lots l ON l.id=a.lot_id JOIN portfolios p ON p.id=t.portfolio_id
               JOIN stocks s ON s.code=t.stock_code ORDER BY t.traded_at DESC""", conn
        )


def money(v: float | None) -> str:
    return "-" if v is None else f"{v:,.0f}원"


def dashboard() -> None:
    st.subheader("내 포지션")
    df = lot_frame()
    if df.empty:
        st.info("먼저 종목을 등록하고 매수 거래를 입력하세요.")
        return
    cost = (df["매수가"] * df["잔여수량"]).sum()
    known = df.dropna(subset=["현재가"])
    value = (known["현재가"] * known["잔여수량"]).sum()
    known_cost = (known["매수가"] * known["잔여수량"]).sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("잔여 매수원금", money(cost))
    c2.metric("평가금액", money(value) if len(known) == len(df) else money(value) + " (일부)")
    c3.metric("평가손익", money(value - known_cost),
              None if known_cost == 0 else f"{(value / known_cost - 1) * 100:.2f}%")
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config={"수익률(%)": st.column_config.NumberColumn(format="%.2f%%"),
                                "매수가": st.column_config.NumberColumn(format="%,.0f원"),
                                "현재가": st.column_config.NumberColumn(format="%,.0f원"),
                                "평가손익": st.column_config.NumberColumn(format="%,.0f원")})
    chart = df.dropna(subset=["평가손익"]).groupby("포트폴리오", as_index=False)["평가손익"].sum()
    if not chart.empty:
        st.bar_chart(chart.set_index("포트폴리오"))


def stock_page() -> None:
    st.subheader("종목 검색 및 등록")
    listing = krx_listing()
    query = st.text_input("종목명 또는 코드 검색", placeholder="예: 삼성전자 또는 005930")
    selected = None
    if query and not listing.empty:
        mask = listing["Name"].str.contains(query, case=False, na=False) | listing["Code"].str.contains(query, na=False)
        found = listing[mask].head(20)
        if not found.empty:
            label_map = {f"{r.Name} ({r.Code}, {r.Market})": r for r in found.itertuples()}
            selected = label_map[st.selectbox("검색 결과", list(label_map))]
    if listing.empty:
        st.caption("자동 종목 목록을 불러오지 못했습니다. 아래에서 직접 등록할 수 있습니다.")
    with st.form("stock_form"):
        code = st.text_input("종목코드", value=getattr(selected, "Code", ""))
        name = st.text_input("종목명", value=getattr(selected, "Name", ""))
        market = st.text_input("시장", value=getattr(selected, "Market", "KRX"))
        price = st.number_input("현재가(자동 조회 실패 시 사용)", min_value=0.0, step=100.0)
        if st.form_submit_button("종목 저장", type="primary"):
            if not code.strip() or not name.strip():
                st.error("종목코드와 종목명을 입력하세요.")
            else:
                add_stock(code, name, market, price or None)
                st.success("종목을 저장했습니다.")
                st.rerun()
    saved = stocks()
    if saved:
        st.dataframe(pd.DataFrame([dict(r) for r in saved]), use_container_width=True, hide_index=True)


def trade_page() -> None:
    st.subheader("매수 · 매도 입력")
    ps, ss = portfolios(), stocks()
    if not ss:
        st.warning("먼저 ‘종목 관리’에서 종목을 등록하세요.")
        return
    pmap = {r["name"]: r["id"] for r in ps}
    smap = {f"{r['name']} ({r['code']})": r["code"] for r in ss}
    side = st.radio("거래 구분", ["매수", "매도"], horizontal=True)
    pname = st.selectbox("가상 포트폴리오", list(pmap))
    slabel = st.selectbox("종목", list(smap))
    pid, code = pmap[pname], smap[slabel]
    if side == "매수":
        with st.form("buy_form"):
            qty = st.number_input("수량", min_value=1, step=1)
            price = st.number_input("체결가", min_value=0.0, step=100.0)
            fee = st.number_input("수수료", min_value=0.0, step=1.0)
            d = st.date_input("거래일", value=date.today())
            memo = st.text_area("투자 근거·메모")
            if st.form_submit_button("매수 기록", type="primary"):
                add_buy(pid, code, int(qty), price, fee, datetime.combine(d, datetime.min.time()).isoformat(), memo)
                st.success("매수 로트를 생성했습니다.")
                st.rerun()
    else:
        lots = open_lots(pid, code)
        if not lots:
            st.info("이 포트폴리오에 매도 가능한 잔여 로트가 없습니다.")
            return
        with st.form("sell_form"):
            allocations = {}
            st.markdown("매도할 매수 로트별 수량")
            for lot in lots:
                label = f"#{lot['id']} · {lot['bought_at'][:10]} · {lot['buy_price']:,.0f}원 · 잔여 {lot['remaining_quantity']}주"
                allocations[lot["id"]] = st.number_input(label, min_value=0, max_value=lot["remaining_quantity"], step=1)
            price = st.number_input("매도 체결가", min_value=0.0, step=100.0)
            fee = st.number_input("수수료", min_value=0.0, step=1.0)
            tax = st.number_input("세금", min_value=0.0, step=1.0)
            d = st.date_input("거래일", value=date.today())
            memo = st.text_area("매도 이유·메모")
            if st.form_submit_button("선택한 로트 매도 기록", type="primary"):
                try:
                    add_sell(pid, code, {k: int(v) for k, v in allocations.items()}, price, fee, tax,
                             datetime.combine(d, datetime.min.time()).isoformat(), memo)
                    st.success("선택한 로트에서 매도 수량을 차감했습니다.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


def portfolio_page() -> None:
    st.subheader("가상 포트폴리오")
    with st.form("portfolio_form"):
        name = st.text_input("새 포트폴리오 이름", placeholder="예: 장기투자, 단기매매")
        if st.form_submit_button("추가"):
            try:
                with connect() as conn:
                    conn.execute("INSERT INTO portfolios(name) VALUES(?)", (name.strip(),))
                st.success("포트폴리오를 추가했습니다.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("이미 존재하는 이름입니다.")
    st.dataframe(pd.DataFrame([dict(r) for r in portfolios()]), use_container_width=True, hide_index=True)


def history_page() -> None:
    st.subheader("거래내역 및 CSV")
    trades = trade_frame()
    realized = realized_frame()
    tab1, tab2 = st.tabs(["전체 거래", "실현손익"])
    with tab1:
        st.dataframe(trades, use_container_width=True, hide_index=True)
        st.download_button("거래내역 CSV 다운로드", trades.to_csv(index=False).encode("utf-8-sig"),
                           "lotfolio_trades.csv", "text/csv")
    with tab2:
        st.dataframe(realized, use_container_width=True, hide_index=True)
        st.download_button("실현손익 CSV 다운로드", realized.to_csv(index=False).encode("utf-8-sig"),
                           "lotfolio_realized.csv", "text/csv")


def main() -> None:
    st.set_page_config(page_title="Lotfolio", page_icon="📊", layout="wide")
    init_db()
    st.title("Lotfolio")
    st.caption("한 증권계좌 안의 매수분을 로트와 전략별로 분리하는 투자 기록장")
    page = st.sidebar.radio("메뉴", ["대시보드", "종목 관리", "거래 입력", "가상 포트폴리오", "거래내역·CSV"])
    {"대시보드": dashboard, "종목 관리": stock_page, "거래 입력": trade_page,
     "가상 포트폴리오": portfolio_page, "거래내역·CSV": history_page}[page]()
    st.sidebar.caption("표시 수익률은 투자 기록용이며 증권사의 세금·손익 계산과 다를 수 있습니다.")


if __name__ == "__main__":
    main()
