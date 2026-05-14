from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "bonds.sqlite"

st.set_page_config(page_title="Облигации T-Bank", layout="wide")

LABELS: dict[str, str] = {
    "isin":                     "ISIN",
    "name":                     "Наименование",
    "yield_date":               "Дата расчёта доходности",
    "yield_date_type":          "Тип даты",
    "yield_pct":                "Доходность, %",
    "yield_formula":            "Формула флоатера",
    "yield_display":            "Доходность, %",
    "years_to_date":            "Лет до даты",
    "maturity_date":            "Дата погашения",
    "last_price_pct":           "Цена, % от номинала",
    "coupon_yield_pct":         "Купонная доходность, %",
    "current_yield_pct":        "Текущая купонная доходность, %",
    "coupon_quantity_per_year": "Купонов в год",
    "aci":                      "НКД",
    "aci_currency":             "Валюта НКД",
    "nominal":                  "Номинал",
    "nominal_currency":         "Валюта",
    "risk_level":               "Уровень риска",
    "amortization":             "Амортизация",
    "perpetual":                "Бессрочные",
    "floater":                  "Плавающий купон",
    "figi":                     "FIGI",
    "snapshot_ts":              "Снапшот",
}

NUMERIC_FILTERS = [
    "yield_pct", "years_to_date", "last_price_pct",
    "coupon_yield_pct", "current_yield_pct",
]
CATEGORICAL_FILTERS = ["yield_date_type", "risk_level", "nominal_currency"]
BOOL_FILTERS = ["amortization", "perpetual", "floater"]


@st.cache_data
def list_snapshots() -> list[str]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql(
            "SELECT DISTINCT snapshot_ts FROM snapshots ORDER BY snapshot_ts DESC",
            con,
        )["snapshot_ts"].tolist()


@st.cache_data
def load_snapshot(snapshot_ts: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql(
            "SELECT * FROM snapshots WHERE snapshot_ts = ?",
            con,
            params=[snapshot_ts],
        )
    for col in BOOL_FILTERS:
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False).astype(bool)
    return df


@st.cache_data
def load_history(isin: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql(
            """
            SELECT snapshot_ts, last_price_pct, yield_pct, current_yield_pct
            FROM snapshots
            WHERE isin = ?
            ORDER BY snapshot_ts
            """,
            con,
            params=[isin],
        )
    df["snapshot_ts"] = pd.to_datetime(df["snapshot_ts"])
    return df


def label_of(col: str) -> str:
    return LABELS.get(col, col)


# ---------- UI ----------
st.title("Облигации T-Bank — аналитический дашборд")

if not DB_PATH.exists():
    st.error(
        f"Не нашёл базу `{DB_PATH}`. Сначала запустите снапшот:\n\n"
        "```cmd\npython bonds_snapshot.py\n```"
    )
    st.stop()

snapshots = list_snapshots()
if not snapshots:
    st.error("В базе нет снапшотов. Запустите `python bonds_snapshot.py`.")
    st.stop()

selected_ts = st.sidebar.selectbox(
    "Снапшот",
    snapshots,
    format_func=lambda s: s.replace("T", " ")[:16] + " UTC",
)

raw = load_snapshot(selected_ts)
total = len(raw)
df = raw.copy()

st.sidebar.header("Фильтры")

# Числовые фильтры (range slider)
for col in NUMERIC_FILTERS:
    if col not in df.columns:
        continue
    series = df[col].dropna()
    if series.empty:
        continue
    vmin, vmax = float(series.min()), float(series.max())
    if vmin == vmax:
        continue
    rng = st.sidebar.slider(label_of(col), vmin, vmax, (vmin, vmax))
    df = df[df[col].between(rng[0], rng[1]) | df[col].isna()]

# Категориальные (multiselect)
for col in CATEGORICAL_FILTERS:
    if col not in df.columns:
        continue
    opts = sorted([v for v in df[col].dropna().unique().tolist() if v != ""])
    if not opts:
        continue
    sel = st.sidebar.multiselect(label_of(col), opts, default=opts)
    df = df[df[col].isin(sel)]

# Булевы (Все / Да / Нет)
for col in BOOL_FILTERS:
    if col not in df.columns:
        continue
    choice = st.sidebar.radio(
        label_of(col), ["Все", "Да", "Нет"], horizontal=True, key=f"bool_{col}"
    )
    if choice == "Да":
        df = df[df[col]]
    elif choice == "Нет":
        df = df[~df[col]]

# Поиск
search = st.sidebar.text_input("Поиск (ISIN / название)")
if search:
    mask = (
        df["isin"].str.contains(search, case=False, na=False)
        | df["name"].str.contains(search, case=False, na=False)
    )
    df = df[mask]

st.sidebar.caption(f"Найдено: **{len(df)}** из {total}")

# ---------- Таблица ----------
st.subheader(f"Таблица — {len(df)} облигаций")

display_df = df.copy()
display_df["yield_display"] = display_df["yield_pct"].apply(
    lambda v: f"{v:.2f}" if pd.notna(v) else None
)
display_df["yield_display"] = display_df["yield_display"].fillna(
    display_df["yield_formula"]
)

table_cols = [
    "isin", "name", "yield_date", "yield_date_type", "yield_display",
    "years_to_date", "maturity_date", "last_price_pct",
    "coupon_yield_pct", "current_yield_pct", "coupon_quantity_per_year",
    "aci", "nominal", "nominal_currency", "risk_level",
    "amortization", "perpetual", "floater", "figi",
]
table_cols = [c for c in table_cols if c in display_df.columns]
st.dataframe(
    display_df[table_cols].rename(columns={c: label_of(c) for c in table_cols}),
    use_container_width=True,
    hide_index=True,
)

# Скачать отфильтрованное в CSV
csv_bytes = display_df[table_cols].rename(
    columns={c: label_of(c) for c in table_cols}
).to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "Скачать отфильтрованное (CSV)",
    csv_bytes,
    file_name=f"bonds_filtered_{selected_ts[:10]}.csv",
    mime="text/csv",
)

# ---------- Динамический график ----------
st.subheader("Динамический график")

if df.empty:
    st.info("Нет данных под текущие фильтры.")
else:
    numeric_cols = [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
    ]
    all_cols = list(df.columns)

    c1, c2, c3, c4, c5 = st.columns(5)
    chart_type = c1.selectbox("Тип", ["Scatter", "Histogram", "Box", "Bar"])

    def _default_idx(cols: list[str], pref: str) -> int:
        return cols.index(pref) if pref in cols else 0

    if chart_type == "Histogram":
        x_options = numeric_cols
        x_default = _default_idx(x_options, "yield_pct")
    else:
        x_options = numeric_cols + [c for c in all_cols if c not in numeric_cols]
        x_default = _default_idx(x_options, "years_to_date")

    x_col = c2.selectbox(
        "X", x_options, index=x_default, format_func=label_of,
    )

    y_col = None
    if chart_type in ("Scatter", "Bar", "Box"):
        y_col = c3.selectbox(
            "Y", numeric_cols,
            index=_default_idx(numeric_cols, "yield_pct"),
            format_func=label_of,
        )

    color_col = c4.selectbox(
        "Цвет", ["—"] + all_cols,
        index=(all_cols.index("yield_date_type") + 1) if "yield_date_type" in all_cols else 0,
        format_func=lambda c: "—" if c == "—" else label_of(c),
    )

    size_col = "—"
    if chart_type == "Scatter":
        size_col = c5.selectbox(
            "Размер", ["—"] + numeric_cols,
            format_func=lambda c: "—" if c == "—" else label_of(c),
        )

    hover_cols = [c for c in ["isin", "name", "yield_date_type", "yield_pct", "yield_formula"]
                  if c in df.columns]

    kwargs: dict = {
        "data_frame": df, "x": x_col,
        "hover_data": hover_cols,
        "labels": LABELS,
    }
    if y_col:
        kwargs["y"] = y_col
    if color_col != "—":
        kwargs["color"] = color_col
    if size_col != "—":
        # size требует положительные значения
        kwargs["size"] = size_col

    try:
        if chart_type == "Scatter":
            fig = px.scatter(**kwargs)
        elif chart_type == "Histogram":
            fig = px.histogram(**{k: v for k, v in kwargs.items() if k != "size"})
        elif chart_type == "Box":
            fig = px.box(**{k: v for k, v in kwargs.items() if k != "size"})
        elif chart_type == "Bar":
            fig = px.bar(**{k: v for k, v in kwargs.items() if k != "size"})
        fig.update_layout(height=550)
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.error(f"Ошибка построения графика: {e}")

# ---------- История по облигации ----------
st.subheader("История по облигации")

if len(snapshots) < 2:
    st.info("История появится после второго запуска `python bonds_snapshot.py`.")
else:
    options = sorted(df["isin"].tolist())
    if not options:
        st.info("Уточните фильтры — список ISIN пуст.")
    else:
        sel_isin = st.selectbox("ISIN", options)
        hist = load_history(sel_isin)
        if len(hist) < 2:
            st.info(f"По {sel_isin} пока один снапшот.")
        else:
            metric = st.radio(
                "Что показать",
                ["last_price_pct", "yield_pct", "current_yield_pct"],
                format_func=label_of,
                horizontal=True,
            )
            fig = px.line(
                hist, x="snapshot_ts", y=metric, markers=True,
                labels={"snapshot_ts": "Время", metric: label_of(metric)},
            )
            fig.update_layout(height=400)
            st.plotly_chart(fig, use_container_width=True)
