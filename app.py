from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
from st_aggrid.shared import DataReturnMode, GridUpdateMode
from streamlit_autorefresh import st_autorefresh

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "bonds.sqlite"
STATUS_FILE = DATA_DIR / "snapshot_status.json"
SNAPSHOT_LOG = DATA_DIR / "snapshot.log"

st.set_page_config(page_title="Облигации T-Bank", layout="wide")

LABELS: dict[str, str] = {
    "isin":                     "ISIN",
    "name":                     "Наименование",
    "yield_date":               "Дата расчёта доходности",
    "yield_date_type":          "Тип даты",
    "yield_pct":                "Доходность, %",
    "yield_formula":            "Формула флоатера",
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

NUMERIC_COLS = {
    "yield_pct", "years_to_date", "last_price_pct",
    "coupon_yield_pct", "current_yield_pct", "coupon_quantity_per_year",
    "aci", "nominal",
}
BOOL_COLS = ["amortization", "perpetual", "floater"]
TABLE_COLS = [
    "isin", "name", "yield_date", "yield_date_type",
    "yield_pct", "yield_formula", "years_to_date",
    "maturity_date", "last_price_pct",
    "coupon_yield_pct", "current_yield_pct", "coupon_quantity_per_year",
    "aci", "aci_currency", "nominal", "nominal_currency",
    "risk_level", "amortization", "perpetual", "floater", "figi",
]


@st.cache_data
def list_snapshots() -> list[str]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql(
            "SELECT DISTINCT snapshot_ts FROM snapshots ORDER BY snapshot_ts DESC", con
        )["snapshot_ts"].tolist()


@st.cache_data
def load_snapshot(snapshot_ts: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql(
            "SELECT * FROM snapshots WHERE snapshot_ts = ?",
            con, params=[snapshot_ts],
        )
    for col in BOOL_COLS:
        if col in df.columns:
            df[col] = df[col].astype("boolean").fillna(False).astype(bool)
    return df


@st.cache_data
def load_history(isin: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql(
            """SELECT snapshot_ts, last_price_pct, yield_pct, current_yield_pct
               FROM snapshots WHERE isin = ? ORDER BY snapshot_ts""",
            con, params=[isin],
        )
    df["snapshot_ts"] = pd.to_datetime(df["snapshot_ts"])
    return df


def label_of(col: str) -> str:
    return LABELS.get(col, col)


# ---------- Snapshot trigger / status ----------
PHASE_LABELS = {
    "starting": "Запуск…",
    "catalog":  "Загружаем каталог облигаций…",
    "prices":   "Получаем последние цены…",
    "coupons":  "Купоны и расчёт доходностей",
    "saving":   "Сохраняем в БД и Excel…",
    "done":     "Готово",
    "error":    "Ошибка",
}


def _is_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def read_status() -> dict | None:
    if not STATUS_FILE.exists():
        return None
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def snapshot_running(status: dict | None) -> bool:
    if not status or not status.get("running"):
        return False
    return _is_pid_alive(status.get("pid", 0))


def trigger_snapshot() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    log = open(SNAPSHOT_LOG, "a", buffering=1, encoding="utf-8")
    popen_kwargs: dict = {
        "cwd": str(ROOT),
        "stdout": log,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, str(ROOT / "bonds_snapshot.py")],
        **popen_kwargs,
    )


# ---------- UI ----------
st.title("Облигации T-Bank — аналитический дашборд")

# --- Sidebar: refresh button + status (всегда виден, даже если данных ещё нет) ---
status = read_status()
running = snapshot_running(status)

# Если был запуск и он завершился — сбросить кэш данных, чтобы подтянуть новое
if st.session_state.get("snapshot_was_running") and not running:
    st.session_state["snapshot_was_running"] = False
    st.cache_data.clear()
    if status and status.get("error"):
        st.sidebar.error(f"Прошлый прогон упал: {status['error'][:200]}")
    else:
        st.toast("Данные обновлены")

st.sidebar.header("Обновление данных")

if running:
    st.session_state["snapshot_was_running"] = True
    phase = (status or {}).get("phase", "")
    phase_label = PHASE_LABELS.get(phase, phase)
    total = (status or {}).get("total") or 0
    current = (status or {}).get("current") or 0

    if total > 0:
        pct = min(current / total, 1.0)
        st.sidebar.progress(
            pct, text=f"{phase_label} — {current}/{total}"
        )
    else:
        st.sidebar.info(phase_label)

    started_at = (status or {}).get("started_at")
    if started_at:
        st.sidebar.caption(f"Старт: {started_at[:16].replace('T', ' ')} UTC")

    st_autorefresh(interval=2000, key="snapshot_poll")
else:
    if st.sidebar.button(
        "Получить актуальные данные",
        type="primary",
        use_container_width=True,
    ):
        trigger_snapshot()
        st.session_state["snapshot_was_running"] = True
        time.sleep(0.5)  # дать subprocess'у записать "starting"
        st.rerun()

    if status:
        fin = status.get("finished_at")
        if fin:
            ts_short = fin[:16].replace("T", " ")
            if status.get("error"):
                st.sidebar.caption(f"Прошлый прогон упал в {ts_short} UTC")
            else:
                st.sidebar.caption(f"Последнее обновление: {ts_short} UTC")

# --- Проверка наличия данных ---
snapshots = list_snapshots()
if not snapshots:
    st.info(
        "В базе пока нет снапшотов. Нажмите **«Получить актуальные данные»** "
        "в левой панели — первый снапшот занимает 5–7 минут."
    )
    st.stop()

selected_ts = st.sidebar.selectbox(
    "Снапшот", snapshots,
    format_func=lambda s: s.replace("T", " ")[:16] + " UTC",
)

raw = load_snapshot(selected_ts)
st.sidebar.caption(f"В снапшоте: **{len(raw)}** облигаций")
st.sidebar.markdown(
    "**Фильтры** — в заголовках столбцов таблицы (значок «☰» справа от названия).\n\n"
    "Поддерживается: содержит / равно / больше / меньше / между, сортировка по клику."
)

# ---------- Table with per-column Excel-style filters ----------
st.subheader("Таблица")

display_df = raw.copy()
# Преобразуем булевы в «да»/«нет» — для текстового фильтра AgGrid Community.
for col in BOOL_COLS:
    if col in display_df.columns:
        display_df[col] = display_df[col].map({True: "да", False: "нет"})

table_cols = [c for c in TABLE_COLS if c in display_df.columns]
display_df = display_df[table_cols]

num_formatter = JsCode(
    "function(p){return p.value==null||p.value===''?'':Number(p.value).toFixed(2)}"
)

gb = GridOptionsBuilder.from_dataframe(display_df)
gb.configure_default_column(
    filter=True, sortable=True, resizable=True, minWidth=110,
)
# Разрешаем выделять текст в ячейках и копировать (Ctrl+C).
# enableRangeSelection — Enterprise-фича, но enableCellTextSelection достаточно.
gb.configure_grid_options(
    enableCellTextSelection=True,
    ensureDomOrder=True,
)
for col in display_df.columns:
    header = label_of(col)
    if col == "coupon_quantity_per_year":
        gb.configure_column(
            col, header_name=header,
            type=["numericColumn"], filter="agNumberColumnFilter",
        )
    elif col in NUMERIC_COLS:
        gb.configure_column(
            col, header_name=header,
            type=["numericColumn"], filter="agNumberColumnFilter",
            valueFormatter=num_formatter,
        )
    else:
        gb.configure_column(col, header_name=header, filter="agTextColumnFilter")

gb.configure_pagination(paginationAutoPageSize=False, paginationPageSize=30)
grid_options = gb.build()

grid_response = AgGrid(
    display_df,
    gridOptions=grid_options,
    height=520,
    update_mode=GridUpdateMode.MODEL_CHANGED,
    data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
    fit_columns_on_grid_load=False,
    allow_unsafe_jscode=True,
    theme="streamlit",
)

filtered = pd.DataFrame(grid_response["data"])
# AgGrid иногда возвращает числовые колонки строками — приводим обратно.
for col in NUMERIC_COLS:
    if col in filtered.columns:
        filtered[col] = pd.to_numeric(filtered[col], errors="coerce")
# Восстанавливаем booleans для графика.
for col in BOOL_COLS:
    if col in filtered.columns:
        filtered[col] = filtered[col].map({"да": True, "нет": False}).fillna(False).astype(bool)

st.caption(f"Отфильтровано в таблице: **{len(filtered)}** из {len(display_df)}")

# Скачать данные: xlsx (полный снапшот) и CSV (текущая выборка)
xlsx_files = sorted(DATA_DIR.glob("bonds_*.xlsx"), key=lambda p: p.stat().st_mtime)
latest_xlsx = xlsx_files[-1] if xlsx_files else None

col_xlsx, col_csv = st.columns(2)
with col_xlsx:
    if latest_xlsx:
        st.download_button(
            "Скачать xlsx (полный снапшот)",
            data=latest_xlsx.read_bytes(),
            file_name=latest_xlsx.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            help="Файл с диска сервера — все облигации, с форматированием.",
        )
    else:
        st.caption("xlsx-файл снапшота не найден.")
with col_csv:
    csv_bytes = (
        filtered.rename(columns={c: label_of(c) for c in filtered.columns})
        .to_csv(index=False).encode("utf-8-sig")
    )
    st.download_button(
        "Скачать CSV (текущая выборка)",
        csv_bytes,
        file_name=f"bonds_filtered_{selected_ts[:10]}.csv",
        mime="text/csv",
        use_container_width=True,
        help="Только то, что сейчас в таблице после фильтров.",
    )

# ---------- Dynamic chart on filtered data ----------
st.subheader("Динамический график — по отфильтрованной таблице")

if filtered.empty:
    st.info("В таблице нет строк под текущие фильтры — график не строится.")
else:
    numeric_cols = [
        c for c in filtered.columns
        if pd.api.types.is_numeric_dtype(filtered[c]) and not pd.api.types.is_bool_dtype(filtered[c])
    ]
    all_cols = list(filtered.columns)

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

    x_col = c2.selectbox("X", x_options, index=x_default, format_func=label_of)

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

    hover_cols = [
        c for c in ["isin", "name", "yield_date_type", "yield_pct", "yield_formula"]
        if c in filtered.columns
    ]

    plot_df = filtered
    if size_col != "—":
        plot_df = filtered.dropna(subset=[size_col]).copy()
        if (plot_df[size_col] < 0).any():
            plot_df[size_col] = plot_df[size_col].abs()
        dropped = len(filtered) - len(plot_df)
        if dropped:
            st.caption(
                f"Для размера точки исключено {dropped} строк без значения «{label_of(size_col)}»."
            )

    kwargs: dict = {
        "data_frame": plot_df, "x": x_col,
        "hover_data": hover_cols,
        "labels": LABELS,
    }
    if y_col:
        kwargs["y"] = y_col
    if color_col != "—":
        kwargs["color"] = color_col
    if size_col != "—":
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

# ---------- History ----------
st.subheader("История по облигации")

if len(snapshots) < 2:
    st.info("История появится после второго запуска `python bonds_snapshot.py`.")
else:
    options = sorted(filtered["isin"].dropna().tolist()) if "isin" in filtered.columns else []
    if not options:
        st.info("Уточните фильтры в таблице — список ISIN пуст.")
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
