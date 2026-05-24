from __future__ import annotations

import io
import re
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network


st.set_page_config(
    page_title="Transnetyx Mouse Pedigree Explorer",
    page_icon="🐭",
    layout="wide",
)

REQUIRED_COLUMNS = ["Mouse ID", "Father ID", "Mother ID", "DOB"]
ID_COLUMNS = ["Mouse ID", "Father ID", "Mother ID", "Cage ID"]

DISPLAY_COLUMNS = [
    "Mouse ID",
    "Sex",
    "Status",
    "DOB",
    "DOD",
    "Age",
    "Strain",
    "Genotype",
    "Use",
    "Owner",
    "Cage ID",
    "Room",
    "Rack",
    "Position",
    "Wean Date",
    "Litter Name",
    "Father ID",
    "Father Genotype",
    "Mother ID",
    "Mother Genotype",
]

DATE_COLUMNS = ["DOB", "DOD", "Wean Date"]

SEX_COLORS = {
    "Female": "#F9A8D4",
    "Male": "#93C5FD",
    "Unknown": "#D1D5DB",
    "nan": "#D1D5DB",
    "": "#D1D5DB",
}


@dataclass
class PedigreeData:
    mice: pd.DataFrame
    edges: pd.DataFrame
    graph: nx.DiGraph
    original_rows: int


@dataclass
class FilterContext:
    date_start: pd.Timestamp | None
    date_end: pd.Timestamp | None
    date_mode: str


def clean_id(value) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null", "count"}:
        return None
    return re.sub(r"\s+", "", text)


def clean_text(value) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def normalize_column_names(columns: Iterable) -> list[str]:
    return [str(c).strip() for c in columns]


def add_missing_optional_columns(df: pd.DataFrame) -> pd.DataFrame:
    for col in ["Status", "DOD", "Wean Date", "Litter Name", "Cage ID", "Owner", "Use", "Sex", "Strain", "Genotype"]:
        if col not in df.columns:
            df[col] = None
    return df


@st.cache_data(show_spinner=False)
def load_excel(uploaded_file_bytes: bytes, sheet_name: str | int = 0) -> PedigreeData:
    raw = pd.read_excel(io.BytesIO(uploaded_file_bytes), sheet_name=sheet_name)
    original_rows = len(raw)
    raw.columns = normalize_column_names(raw.columns)

    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    df = raw.copy()
    df = add_missing_optional_columns(df)

    df["Mouse ID"] = df["Mouse ID"].map(clean_id)
    df = df[df["Mouse ID"].notna()].copy()

    for col in ID_COLUMNS:
        if col in df.columns:
            df[col] = df[col].map(clean_id)

    for col in df.columns:
        if col not in ID_COLUMNS:
            df[col] = df[col].map(clean_text)

    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    df["DOB Month"] = df["DOB"].dt.to_period("M").astype(str)
    df.loc[df["DOB"].isna(), "DOB Month"] = None

    graph_df = df.drop_duplicates(subset=["Mouse ID"], keep="first").copy()
    mouse_ids = set(graph_df["Mouse ID"].dropna())

    edges = []
    for _, row in graph_df.iterrows():
        child = row.get("Mouse ID")
        for parent_col, parent_type in [("Father ID", "father"), ("Mother ID", "mother")]:
            parent = row.get(parent_col)
            if parent and child:
                edges.append(
                    {
                        "parent": parent,
                        "child": child,
                        "parent_type": parent_type,
                        "parent_exists_in_file": parent in mouse_ids,
                    }
                )
    edges_df = pd.DataFrame(edges)
    if edges_df.empty:
        edges_df = pd.DataFrame(columns=["parent", "child", "parent_type", "parent_exists_in_file"])

    graph = nx.DiGraph()
    for _, row in graph_df.iterrows():
        graph.add_node(row["Mouse ID"], **row.to_dict())

    for _, edge in edges_df.iterrows():
        graph.add_edge(edge["parent"], edge["child"], parent_type=edge["parent_type"])
        if edge["parent"] not in mouse_ids:
            graph.nodes[edge["parent"]]["Mouse ID"] = edge["parent"]
            graph.nodes[edge["parent"]]["missing_from_file"] = True
            graph.nodes[edge["parent"]]["Sex"] = "Unknown"

    return PedigreeData(mice=df, edges=edges_df, graph=graph, original_rows=original_rows)


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d")
    return out.to_csv(index=False).encode("utf-8")


def infer_sheet_names(uploaded_file_bytes: bytes) -> list[str]:
    xls = pd.ExcelFile(io.BytesIO(uploaded_file_bytes))
    return xls.sheet_names


def alive_anytime_mask(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    dob_ok = df["DOB"].notna() & (df["DOB"] <= end)
    if "DOD" in df.columns:
        dod_ok = df["DOD"].isna() | (df["DOD"] >= start)
    else:
        dod_ok = pd.Series(True, index=df.index)
    return dob_ok & dod_ok


def get_default_date_range(df: pd.DataFrame) -> tuple[date, date]:
    today = date.today()
    min_dob = df["DOB"].min()
    max_dob = df["DOB"].max()
    if pd.isna(min_dob):
        return today - timedelta(days=60), today
    end_date = max(today, max_dob.date() if pd.notna(max_dob) else today)
    return min_dob.date(), end_date


def apply_filters(df: pd.DataFrame) -> tuple[pd.DataFrame, FilterContext]:
    st.sidebar.header("Filters")
    filtered = df.copy()

    default_start, default_end = get_default_date_range(df)
    date_mode = st.sidebar.radio(
        "Date filter mode",
        options=["DOB within selected range", "Alive anytime during selected range", "No date filter"],
        index=0,
    )

    date_range = st.sidebar.date_input(
        "Date range",
        value=(default_start, default_end),
        help="Used for DOB filtering, alive-at-range reports, and cage reports. No hard-coded end date limit is applied.",
    )

    start = end = None
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start = pd.to_datetime(date_range[0])
        end = pd.to_datetime(date_range[1])
        if start > end:
            st.sidebar.warning("Start date is after end date. The dates were swapped.")
            start, end = end, start

        if date_mode == "DOB within selected range":
            filtered = filtered[(filtered["DOB"].isna()) | ((filtered["DOB"] >= start) & (filtered["DOB"] <= end))]
        elif date_mode == "Alive anytime during selected range":
            filtered = filtered[alive_anytime_mask(filtered, start, end)]

    for label, col in [
        ("Strain", "Strain"),
        ("Sex", "Sex"),
        ("Status", "Status"),
        ("Use", "Use"),
        ("Owner", "Owner"),
    ]:
        if col in filtered.columns:
            options = sorted([x for x in filtered[col].dropna().unique().tolist() if x])
            selected = st.sidebar.multiselect(label, options=options, default=[])
            if selected:
                filtered = filtered[filtered[col].isin(selected)]

    genotype_query = st.sidebar.text_input("Genotype contains")
    if genotype_query and "Genotype" in filtered.columns:
        filtered = filtered[filtered["Genotype"].fillna("").str.contains(genotype_query, case=False, na=False)]

    text_query = st.sidebar.text_input("Search Mouse ID / parent ID / cage ID")
    if text_query:
        q_id = clean_id(text_query) or text_query.strip()
        q_text = text_query.strip()
        mask = pd.Series(False, index=filtered.index)
        for col in ["Mouse ID", "Father ID", "Mother ID", "Cage ID"]:
            if col in filtered.columns:
                mask = mask | filtered[col].fillna("").str.contains(q_id, case=False, regex=False)
        if "Genotype" in filtered.columns:
            mask = mask | filtered["Genotype"].fillna("").str.contains(q_text, case=False, regex=False)
        filtered = filtered[mask]

    return filtered, FilterContext(start, end, date_mode)


def safe_predecessors(graph: nx.DiGraph, node: str) -> list[str]:
    if node not in graph:
        return []
    return list(graph.predecessors(node))


def safe_successors(graph: nx.DiGraph, node: str) -> list[str]:
    if node not in graph:
        return []
    return list(graph.successors(node))


def get_ancestors(graph: nx.DiGraph, node: str, generations: int) -> set[str]:
    seen = set()
    current = {node}
    for _ in range(generations):
        parents = set()
        for n in current:
            parents.update(safe_predecessors(graph, n))
        parents -= seen
        seen.update(parents)
        current = parents
        if not current:
            break
    return seen


def get_descendants(graph: nx.DiGraph, node: str, generations: int) -> set[str]:
    seen = set()
    current = {node}
    for _ in range(generations):
        children = set()
        for n in current:
            children.update(safe_successors(graph, n))
        children -= seen
        seen.update(children)
        current = children
        if not current:
            break
    return seen


def get_siblings(graph: nx.DiGraph, node: str) -> set[str]:
    siblings = set()
    for parent in safe_predecessors(graph, node):
        siblings.update(safe_successors(graph, parent))
    siblings.discard(node)
    return siblings


def max_generations_available(graph: nx.DiGraph, node: str, direction: str, max_limit: int = 25) -> int:
    if node not in graph.nodes:
        return 0
    seen = {node}
    current = {node}
    depth = 0
    for _ in range(max_limit):
        next_nodes = set()
        for n in current:
            if direction == "ancestors":
                next_nodes.update(safe_predecessors(graph, n))
            elif direction == "descendants":
                next_nodes.update(safe_successors(graph, n))
        next_nodes -= seen
        if not next_nodes:
            break
        seen.update(next_nodes)
        current = next_nodes
        depth += 1
    return depth


def max_generations_for_selection(graph: nx.DiGraph, selected_ids: list[str], direction: str) -> int:
    if not selected_ids:
        return 0
    return max(max_generations_available(graph, mouse_id, direction) for mouse_id in selected_ids)


def parse_mouse_ids(raw_text: str) -> list[str]:
    if not raw_text:
        return []
    tokens = re.split(r"[\s,;]+", raw_text.strip())
    cleaned = []
    for token in tokens:
        mouse_id = clean_id(token)
        if mouse_id and mouse_id not in cleaned:
            cleaned.append(mouse_id)
    return cleaned


def build_displayed_nodes(
    graph: nx.DiGraph,
    selected_ids: list[str],
    ancestors: int,
    descendants: int,
    include_siblings: bool,
) -> set[str]:
    nodes = set(selected_ids)
    for selected_id in selected_ids:
        nodes.update(get_ancestors(graph, selected_id, ancestors))
        nodes.update(get_descendants(graph, selected_id, descendants))
        if include_siblings:
            nodes.update(get_siblings(graph, selected_id))
    return nodes


def build_pyvis_graph(
    graph: nx.DiGraph,
    selected_ids: list[str],
    ancestors: int,
    descendants: int,
    include_siblings: bool,
    height: str = "720px",
) -> str:
    selected_set = set(selected_ids)
    nodes = build_displayed_nodes(graph, selected_ids, ancestors, descendants, include_siblings)
    sub = graph.subgraph(nodes).copy()

    net = Network(height=height, width="100%", directed=True, bgcolor="#FFFFFF", font_color="#111827")
    net.set_options(
        """
        {
          "layout": {
            "hierarchical": {
              "enabled": true,
              "direction": "UD",
              "sortMethod": "directed",
              "levelSeparation": 140,
              "nodeSpacing": 130,
              "treeSpacing": 220
            }
          },
          "physics": {"enabled": false},
          "interaction": {"hover": true, "navigationButtons": true, "keyboard": true},
          "edges": {
            "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}},
            "smooth": {"type": "cubicBezier", "forceDirection": "vertical", "roundness": 0.35}
          }
        }
        """
    )

    for node in sub.nodes:
        attrs = graph.nodes[node]
        sex = attrs.get("Sex") or "Unknown"
        color = "#FDE68A" if node in selected_set else SEX_COLORS.get(str(sex), "#D1D5DB")
        missing = attrs.get("missing_from_file", False)
        if missing:
            color = "#E5E7EB"
        dob = attrs.get("DOB")
        dob_text = pd.to_datetime(dob).strftime("%Y-%m-%d") if pd.notna(dob) else ""
        dod = attrs.get("DOD")
        dod_text = pd.to_datetime(dod).strftime("%Y-%m-%d") if pd.notna(dod) else ""

        label_bits = [node]
        if sex and sex != "Unknown":
            label_bits.append(str(sex))
        if dob_text:
            label_bits.append(dob_text)
        label = "\n".join(label_bits)

        title = "<br>".join(
            [
                f"<b>{node}</b>",
                f"Sex: {sex or ''}",
                f"Status: {attrs.get('Status') or ''}",
                f"DOB: {dob_text}",
                f"DOD: {dod_text}",
                f"Strain: {attrs.get('Strain') or ''}",
                f"Genotype: {attrs.get('Genotype') or ''}",
                f"Use: {attrs.get('Use') or ''}",
                f"Owner: {attrs.get('Owner') or ''}",
                f"Cage ID: {attrs.get('Cage ID') or ''}",
                "Missing from file: yes" if missing else "",
            ]
        )
        net.add_node(node, label=label, title=title, color=color, shape="box" if missing else "dot")

    for source, target, attrs in sub.edges(data=True):
        parent_type = attrs.get("parent_type", "parent")
        edge_color = "#2563EB" if parent_type == "father" else "#DB2777" if parent_type == "mother" else "#6B7280"
        net.add_edge(source, target, title=parent_type, color=edge_color)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp:
        net.save_graph(tmp.name)
        html = open(tmp.name, "r", encoding="utf-8").read()
    return html


def pedigree_png_bytes(graph: nx.DiGraph, nodes: set[str], selected_ids: list[str]) -> bytes:
    sub = graph.subgraph(nodes).copy()
    if len(sub.nodes) == 0:
        return b""

    fig_width = max(8, min(26, len(sub.nodes) * 0.35))
    fig_height = max(6, min(22, len(sub.nodes) * 0.22))
    fig, ax = plt.subplots(figsize=(fig_width, fig_height), dpi=180)

    try:
        pos = nx.nx_agraph.graphviz_layout(sub, prog="dot")
    except Exception:
        pos = nx.spring_layout(sub, seed=42, k=1.2 / max(1, len(sub.nodes) ** 0.5))

    node_colors = []
    for node in sub.nodes:
        attrs = graph.nodes[node]
        if node in selected_ids:
            node_colors.append("#FDE68A")
        else:
            node_colors.append(SEX_COLORS.get(str(attrs.get("Sex") or "Unknown"), "#D1D5DB"))

    nx.draw_networkx_edges(sub, pos, ax=ax, arrows=True, arrowsize=8, width=0.7, alpha=0.55)
    nx.draw_networkx_nodes(sub, pos, ax=ax, node_color=node_colors, node_size=360, linewidths=0.6, edgecolors="#111827")
    nx.draw_networkx_labels(sub, pos, labels={n: n for n in sub.nodes}, font_size=5, ax=ax)
    ax.axis("off")
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()


def show_mouse_card(df: pd.DataFrame, mouse_id: str) -> None:
    row = df[df["Mouse ID"] == mouse_id]
    if row.empty:
        st.warning("This ID is only present as a parent reference, but not as a full animal row in the file.")
        return
    row = row.iloc[0]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Mouse ID", mouse_id)
    c2.metric("Sex", row.get("Sex") or "Unknown")
    c3.metric("Status", row.get("Status") or "Unknown")
    c4.metric("DOB", row.get("DOB").strftime("%Y-%m-%d") if pd.notna(row.get("DOB")) else "Unknown")
    c5.metric("Cage", row.get("Cage ID") or "Unknown")

    details = {col: row.get(col) for col in DISPLAY_COLUMNS if col in row.index}
    details_df = pd.DataFrame([details]).T.rename(columns={0: "Value"})
    st.dataframe(details_df, use_container_width=True)


def overview_tab(data: PedigreeData, filtered: pd.DataFrame) -> None:
    df = data.mice
    edges = data.edges
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Animals", f"{len(df):,}")
    c2.metric("Filtered", f"{len(filtered):,}")
    c3.metric("Strains", f"{df['Strain'].nunique(dropna=True):,}")
    c4.metric("Cages", f"{df['Cage ID'].nunique(dropna=True):,}")
    c5.metric("Parent links", f"{len(edges):,}")
    missing_parent_refs = 0 if edges.empty else int((~edges["parent_exists_in_file"]).sum())
    c6.metric("Parent refs not found", f"{missing_parent_refs:,}")

    st.divider()
    left, right = st.columns([1.2, 1])
    with left:
        births = (
            filtered.dropna(subset=["DOB"])
            .assign(month=lambda x: x["DOB"].dt.to_period("M").dt.to_timestamp())
            .groupby("month", as_index=False)
            .size()
        )
        if not births.empty:
            fig = px.line(births, x="month", y="size", markers=True, title="Births by month")
            fig.update_layout(xaxis_title="Month", yaxis_title="Number of mice")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No DOB values available for the current filters.")

    with right:
        top_strains = filtered["Strain"].value_counts(dropna=True).head(15).reset_index()
        top_strains.columns = ["Strain", "count"]
        if not top_strains.empty:
            fig = px.bar(top_strains, x="count", y="Strain", orientation="h", title="Top strains in current filter")
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, xaxis_title="Animals")
            st.plotly_chart(fig, use_container_width=True)

    st.subheader("Filtered data preview")
    preview_cols = [c for c in DISPLAY_COLUMNS if c in filtered.columns]
    st.dataframe(filtered[preview_cols].head(1000), use_container_width=True)


def pedigree_tab(data: PedigreeData, filtered: pd.DataFrame) -> None:
    graph = data.graph
    df = data.mice
    all_ids = sorted(df["Mouse ID"].dropna().unique().tolist())
    filtered_ids = sorted(filtered["Mouse ID"].dropna().unique().tolist())
    default_pool = filtered_ids if filtered_ids else all_ids

    st.markdown("Search one or more animals and render their shared pedigree network.")
    input_col, settings_col = st.columns([1.4, 1])
    with input_col:
        typed_ids = st.text_area(
            "Mouse ID(s)",
            placeholder="Example: SDK015_205_1\nOr paste multiple IDs separated by commas or new lines",
            height=90,
        )
        selected_from_list = st.multiselect("Or select from filtered animals", options=default_pool, default=[])

    typed_id_list = parse_mouse_ids(typed_ids)
    selected_ids = []
    for mouse_id in typed_id_list + selected_from_list:
        cleaned = clean_id(mouse_id)
        if cleaned and cleaned not in selected_ids:
            selected_ids.append(cleaned)

    with settings_col:
        requested_ancestors = st.slider("Ancestor generations", min_value=0, max_value=8, value=3)
        requested_descendants = st.slider("Descendant generations", min_value=0, max_value=8, value=2)
        include_siblings = st.checkbox("Include siblings", value=True)

    if not selected_ids:
        st.info("Choose or type at least one Mouse ID to show the pedigree.")
        return

    missing_ids = [mouse_id for mouse_id in selected_ids if mouse_id not in graph.nodes]
    valid_ids = [mouse_id for mouse_id in selected_ids if mouse_id in graph.nodes]
    if missing_ids:
        st.warning("The following Mouse ID(s) were not found and will be skipped: " + ", ".join(missing_ids))
    if not valid_ids:
        st.error("None of the selected Mouse IDs were found.")
        return

    max_ancestor_generations = max_generations_for_selection(graph, valid_ids, "ancestors")
    max_descendant_generations = max_generations_for_selection(graph, valid_ids, "descendants")
    ancestors = min(requested_ancestors, max_ancestor_generations)
    descendants = min(requested_descendants, max_descendant_generations)

    warning_parts = []
    if requested_ancestors > max_ancestor_generations:
        warning_parts.append(f"only {max_ancestor_generations} ancestor generation(s) found")
    if requested_descendants > max_descendant_generations:
        warning_parts.append(f"only {max_descendant_generations} descendant generation(s) found")
    if warning_parts:
        st.warning("Requested more generations than available: " + "; ".join(warning_parts) + ".")

    if len(valid_ids) == 1:
        show_mouse_card(df, valid_ids[0])

    displayed_nodes = build_displayed_nodes(graph, valid_ids, ancestors, descendants, include_siblings)
    sub_edges = data.edges[data.edges["parent"].isin(displayed_nodes) & data.edges["child"].isin(displayed_nodes)]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Selected animals", len(valid_ids))
    c2.metric("Displayed nodes", f"{len(displayed_nodes):,}")
    c3.metric("Displayed links", f"{len(sub_edges):,}")
    c4.metric("Possible siblings", f"{sum(len(get_siblings(graph, x)) for x in valid_ids):,}")

    if len(displayed_nodes) > 500:
        st.warning("This graph has more than 500 nodes and may render slowly. Lower generations or turn off siblings if needed.")

    html = build_pyvis_graph(graph, valid_ids, ancestors, descendants, include_siblings)
    components.html(html, height=760, scrolling=True)

    left, right = st.columns(2)
    with left:
        st.download_button(
            "Download displayed pedigree edges as CSV",
            data=dataframe_to_csv_bytes(sub_edges),
            file_name="displayed_pedigree_edges.csv",
            mime="text/csv",
        )
    with right:
        png_bytes = pedigree_png_bytes(graph, displayed_nodes, valid_ids)
        st.download_button(
            "Download displayed pedigree as PNG",
            data=png_bytes,
            file_name="displayed_pedigree.png",
            mime="image/png",
        )


def timeline_tab(data: PedigreeData, filtered: pd.DataFrame) -> None:
    st.subheader("Timeline")
    st.markdown("This view organizes animals by DOB and can be grouped by strain, owner, use, status, or sex.")

    df = filtered.dropna(subset=["DOB"]).copy()
    if df.empty:
        st.info("No animals with DOB available in the current filter.")
        return

    control_cols = st.columns(4)
    with control_cols[0]:
        group_col = st.selectbox("Row/group animals by", options=["Strain", "Owner", "Use", "Status", "Sex"], index=0)
    with control_cols[1]:
        color_choice = st.selectbox("Color timeline by", options=["Same color", "Strain", "Owner", "Use", "Status", "Sex"], index=1)
    with control_cols[2]:
        row_order_choice = st.selectbox(
            "Row order",
            options=["Alphabetical", "Most recent birth first", "Oldest birth first", "Most animals first"],
            index=0,
        )
    with control_cols[3]:
        max_categories = st.slider("Max groups shown", min_value=10, max_value=100, value=40, step=5)

    group_col = group_col if group_col in df.columns else "Strain"
    df[group_col] = df[group_col].fillna("Unknown")
    top_values = df[group_col].value_counts().head(max_categories).index.tolist()
    plot_df = df[df[group_col].isin(top_values)].copy()

    if row_order_choice == "Most recent birth first":
        y_axis_order = plot_df.groupby(group_col)["DOB"].max().sort_values(ascending=False).index.tolist()
    elif row_order_choice == "Oldest birth first":
        y_axis_order = plot_df.groupby(group_col)["DOB"].min().sort_values(ascending=True).index.tolist()
    elif row_order_choice == "Most animals first":
        y_axis_order = plot_df[group_col].value_counts().index.tolist()
    else:
        y_axis_order = sorted(plot_df[group_col].dropna().unique().tolist())

    hover_cols = [c for c in ["Mouse ID", "Sex", "Status", "DOB", "DOD", "Strain", "Genotype", "Use", "Owner", "Cage ID", "Father ID", "Mother ID"] if c in plot_df.columns]
    color_arg = None if color_choice == "Same color" else color_choice

    fig = px.scatter(
        plot_df,
        x="DOB",
        y=group_col,
        color=color_arg if color_arg in plot_df.columns else None,
        hover_data=hover_cols,
        title=f"Animals by DOB and {group_col}",
    )
    fig.update_yaxes(categoryorder="array", categoryarray=y_axis_order)
    fig.update_layout(height=max(550, 25 * min(max_categories, plot_df[group_col].nunique()) + 220))
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Birth counts by month")
    monthly_color = st.selectbox(
        "Color monthly births by",
        options=["Same color", "Owner", "Strain", "Sex", "Status", "Use"],
        index=0,
    )
    monthly_df = df.assign(month=lambda x: x["DOB"].dt.to_period("M").dt.to_timestamp())
    if monthly_color == "Same color":
        monthly = monthly_df.groupby("month", dropna=False).size().reset_index(name="count")
        fig2 = px.bar(monthly, x="month", y="count", title="Monthly births")
    else:
        monthly = monthly_df.groupby(["month", monthly_color], dropna=False).size().reset_index(name="count")
        fig2 = px.bar(monthly, x="month", y="count", color=monthly_color, title=f"Monthly births by {monthly_color}")
    fig2.update_layout(xaxis_title="Month", yaxis_title="Animals born")
    st.plotly_chart(fig2, use_container_width=True)


def build_cage_summary(alive_df: pd.DataFrame) -> pd.DataFrame:
    cage_df = alive_df[alive_df["Cage ID"].notna()].copy()
    if cage_df.empty:
        return pd.DataFrame()

    def joined_unique(series: pd.Series) -> str:
        values = sorted([str(x) for x in series.dropna().unique().tolist() if str(x).strip()])
        return "; ".join(values)

    summary = (
        cage_df.groupby("Cage ID", dropna=False)
        .agg(
            alive_mouse_count=("Mouse ID", "nunique"),
            owners=("Owner", joined_unique),
            owner_count=("Owner", lambda s: s.dropna().nunique()),
            strains=("Strain", joined_unique),
            uses=("Use", joined_unique),
            statuses=("Status", joined_unique),
            rooms=("Room", joined_unique) if "Room" in cage_df.columns else ("Mouse ID", lambda s: ""),
            racks=("Rack", joined_unique) if "Rack" in cage_df.columns else ("Mouse ID", lambda s: ""),
            positions=("Position", joined_unique) if "Position" in cage_df.columns else ("Mouse ID", lambda s: ""),
            first_dob=("DOB", "min"),
            last_dob=("DOB", "max"),
        )
        .reset_index()
    )

    def owner_category(row) -> str:
        if not row["owners"]:
            return "Unknown"
        if row["owner_count"] > 1:
            return "Mixed / multiple owners"
        return row["owners"]

    summary["Owner category"] = summary.apply(owner_category, axis=1)
    return summary


def alive_and_cages_tab(data: PedigreeData, filtered: pd.DataFrame, context: FilterContext) -> None:
    st.subheader("Alive-at-range and cage reports")

    if context.date_start is None or context.date_end is None:
        st.info("Select a date range in the sidebar to calculate alive mice and occupied cages.")
        return

    start = context.date_start
    end = context.date_end
    base = filtered.copy()
    alive_df = base[alive_anytime_mask(base, start, end)].copy()
    cage_summary = build_cage_summary(alive_df)

    date_text = f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"
    st.caption(f"Alive-anytime logic for {date_text}: DOB is on or before the range end, and DOD is blank or on or after the range start.")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Alive mice in range", f"{alive_df['Mouse ID'].nunique():,}")
    c2.metric("Unique cages with alive mice", f"{0 if cage_summary.empty else cage_summary['Cage ID'].nunique():,}")
    c3.metric("Alive mice with cage ID", f"{alive_df[alive_df['Cage ID'].notna()]['Mouse ID'].nunique():,}")
    c4.metric("Alive mice missing cage ID", f"{alive_df[alive_df['Cage ID'].isna()]['Mouse ID'].nunique():,}")
    mixed = 0 if cage_summary.empty else int((cage_summary["owner_count"] > 1).sum())
    c5.metric("Mixed-owner cages", f"{mixed:,}")

    if not cage_summary.empty and (cage_summary["owner_count"] > 1).any():
        st.warning("Some cages contain alive animals assigned to more than one owner. The overall cage total counts each cage once. Owner-specific counts may double-count mixed-owner cages unless you use the Owner category table.")

    tab1, tab2, tab3 = st.tabs(["Cage summary", "By owner", "Alive mice table"])

    with tab1:
        st.markdown("One row per unique cage that contained at least one alive animal during the selected range.")
        display_cols = [
            "Cage ID",
            "Owner category",
            "alive_mouse_count",
            "owners",
            "owner_count",
            "strains",
            "uses",
            "statuses",
            "rooms",
            "racks",
            "positions",
            "first_dob",
            "last_dob",
        ]
        display_cols = [c for c in display_cols if c in cage_summary.columns]
        st.dataframe(cage_summary[display_cols], use_container_width=True)
        st.download_button(
            "Download cage summary as CSV",
            data=dataframe_to_csv_bytes(cage_summary),
            file_name="alive_range_cage_summary.csv",
            mime="text/csv",
        )

    with tab2:
        if cage_summary.empty:
            st.info("No cages found for the selected range and filters.")
        else:
            owner_summary = (
                cage_summary.groupby("Owner category", dropna=False)
                .agg(
                    unique_cages=("Cage ID", "nunique"),
                    alive_mice=("alive_mouse_count", "sum"),
                )
                .reset_index()
                .sort_values("unique_cages", ascending=False)
            )
            fig = px.bar(owner_summary, x="Owner category", y="unique_cages", title="Unique occupied cages by owner category")
            fig.update_layout(xaxis_title="Owner category", yaxis_title="Unique cages")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(owner_summary, use_container_width=True)
            st.download_button(
                "Download cage counts by owner category as CSV",
                data=dataframe_to_csv_bytes(owner_summary),
                file_name="alive_range_cage_counts_by_owner.csv",
                mime="text/csv",
            )

            animal_owner_counts = (
                alive_df[alive_df["Cage ID"].notna()]
                .groupby("Owner", dropna=False)
                .agg(
                    cages_counted_within_owner=("Cage ID", "nunique"),
                    alive_mice=("Mouse ID", "nunique"),
                )
                .reset_index()
                .sort_values("cages_counted_within_owner", ascending=False)
            )
            st.markdown("Animal-level owner assignment. A mixed-owner cage can appear under more than one owner here.")
            st.dataframe(animal_owner_counts, use_container_width=True)

    with tab3:
        cols = [c for c in DISPLAY_COLUMNS if c in alive_df.columns]
        st.dataframe(alive_df[cols], use_container_width=True)
        st.download_button(
            "Download alive mice table as CSV",
            data=dataframe_to_csv_bytes(alive_df),
            file_name="alive_range_mice.csv",
            mime="text/csv",
        )


def qc_tab(data: PedigreeData) -> None:
    df = data.mice
    edges = data.edges
    st.subheader("Data quality checks")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Original export rows", f"{data.original_rows:,}")
    c2.metric("Animal rows kept", f"{len(df):,}")
    c3.metric("Duplicate Mouse IDs", f"{int(df['Mouse ID'].duplicated().sum()):,}")
    missing_ref = 0 if edges.empty else int((~edges["parent_exists_in_file"]).sum())
    c4.metric("Parent IDs not found", f"{missing_ref:,}")
    c5.metric("Missing cage ID", f"{int(df['Cage ID'].isna().sum()):,}")

    tabs = st.tabs(["Missing parents", "Parent IDs not found", "Duplicate IDs", "Potential founders", "Missing cage IDs"])
    with tabs[0]:
        missing_parents = df[df["Father ID"].isna() | df["Mother ID"].isna()]
        st.write(f"Animals missing father or mother ID: {len(missing_parents):,}")
        cols = [c for c in DISPLAY_COLUMNS if c in missing_parents.columns]
        st.dataframe(missing_parents[cols], use_container_width=True)
    with tabs[1]:
        absent = edges[~edges["parent_exists_in_file"]].copy() if not edges.empty else pd.DataFrame()
        st.write(f"Parent references that do not appear as Mouse ID rows: {len(absent):,}")
        st.dataframe(absent, use_container_width=True)
    with tabs[2]:
        dup = df[df["Mouse ID"].duplicated(keep=False)].sort_values("Mouse ID")
        st.write(f"Rows with duplicated Mouse IDs: {len(dup):,}")
        cols = [c for c in DISPLAY_COLUMNS if c in dup.columns]
        st.dataframe(dup[cols], use_container_width=True)
    with tabs[3]:
        has_children = set(edges["parent"].dropna()) if not edges.empty else set()
        no_known_parents = df[df["Father ID"].isna() & df["Mother ID"].isna()].copy()
        founders = no_known_parents[no_known_parents["Mouse ID"].isin(has_children)]
        st.write(f"Potential founders with children but no recorded parents: {len(founders):,}")
        cols = [c for c in DISPLAY_COLUMNS if c in founders.columns]
        st.dataframe(founders[cols], use_container_width=True)
    with tabs[4]:
        missing_cage = df[df["Cage ID"].isna()].copy()
        st.write(f"Animals missing Cage ID: {len(missing_cage):,}")
        cols = [c for c in DISPLAY_COLUMNS if c in missing_cage.columns]
        st.dataframe(missing_cage[cols], use_container_width=True)

    st.download_button("Download cleaned animal table as CSV", data=dataframe_to_csv_bytes(df), file_name="cleaned_transnetyx_mice.csv", mime="text/csv")
    st.download_button("Download all parent-child edges as CSV", data=dataframe_to_csv_bytes(edges), file_name="transnetyx_parent_child_edges.csv", mime="text/csv")


def table_tab(filtered: pd.DataFrame) -> None:
    st.subheader("Filtered animal table")
    cols = st.multiselect("Columns to show", options=filtered.columns.tolist(), default=[c for c in DISPLAY_COLUMNS if c in filtered.columns])
    st.dataframe(filtered[cols] if cols else filtered, use_container_width=True)
    st.download_button("Download current filtered table as CSV", data=dataframe_to_csv_bytes(filtered), file_name="filtered_transnetyx_mice.csv", mime="text/csv")


def main() -> None:
    st.title("Transnetyx Mouse Pedigree Explorer")
    st.caption("Upload a Transnetyx mouse history Excel export to explore pedigrees, timelines, colony QC, alive-at-range counts, and cage counts.")

    uploaded = st.file_uploader("Upload Transnetyx Excel export", type=["xlsx", "xls"])
    if uploaded is None:
        st.info("Upload the Excel export from Transnetyx to start.")
        st.markdown("Expected columns include **Mouse ID**, **Father ID**, **Mother ID**, **DOB**, **Sex**, **Strain**, **Genotype**, **Use**, **Owner**, **Cage ID**, **Status**, and ideally **DOD**.")
        return

    uploaded_bytes = uploaded.getvalue()
    try:
        sheet_names = infer_sheet_names(uploaded_bytes)
    except Exception:
        sheet_names = [0]
    sheet = st.selectbox("Sheet", options=sheet_names, index=0) if len(sheet_names) > 1 else sheet_names[0]

    try:
        data = load_excel(uploaded_bytes, sheet)
    except Exception as e:
        st.error(f"Could not read this file: {e}")
        return

    filtered, context = apply_filters(data.mice)

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Overview",
        "Animal pedigree",
        "Timeline by month",
        "Alive mice & cages",
        "Data QC",
        "Table",
    ])
    with tab1:
        overview_tab(data, filtered)
    with tab2:
        pedigree_tab(data, filtered)
    with tab3:
        timeline_tab(data, filtered)
    with tab4:
        alive_and_cages_tab(data, filtered, context)
    with tab5:
        qc_tab(data)
    with tab6:
        table_tab(filtered)


if __name__ == "__main__":
    main()
