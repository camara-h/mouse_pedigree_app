from __future__ import annotations

import io
import re
import tempfile
from dataclasses import dataclass
from typing import Iterable

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
    "DOB",
    "Age",
    "Strain",
    "Genotype",
    "Use",
    "Owner",
    "Cage ID",
    "Room",
    "Rack",
    "Position",
    "Father ID",
    "Father Genotype",
    "Mother ID",
    "Mother Genotype",
]

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


def clean_id(value) -> str | None:
    """Normalize Transnetyx-style IDs while preserving the original ID text."""
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


@st.cache_data(show_spinner=False)
def load_excel(uploaded_file_bytes: bytes, sheet_name: str | int = 0) -> PedigreeData:
    raw = pd.read_excel(io.BytesIO(uploaded_file_bytes), sheet_name=sheet_name)
    original_rows = len(raw)

    # Normalize column names because exports often include hidden spaces.
    raw.columns = [str(c).strip() for c in raw.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    df = raw.copy()

    # Keep only rows that represent actual animals.
    # Transnetyx exports may include grouping rows such as "Strain: ..." and count rows.
    df["Mouse ID"] = df["Mouse ID"].map(clean_id)
    df = df[df["Mouse ID"].notna()].copy()

    for col in ID_COLUMNS:
        if col in df.columns:
            df[col] = df[col].map(clean_id)

    for col in df.columns:
        if col not in ID_COLUMNS:
            df[col] = df[col].map(clean_text)

    df["DOB"] = pd.to_datetime(df["DOB"], errors="coerce")
    df["DOB Month"] = df["DOB"].dt.to_period("M").astype(str)
    df.loc[df["DOB"].isna(), "DOB Month"] = None

    # Keep first instance of duplicated Mouse ID for graph construction.
    # Full duplicated records are still reported in the QC tab.
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

    graph = nx.DiGraph()
    for _, row in graph_df.iterrows():
        attrs = row.to_dict()
        graph.add_node(row["Mouse ID"], **attrs)

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


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.header("Filters")

    filtered = df.copy()

    min_date = filtered["DOB"].min()
    max_date = filtered["DOB"].max()
    if pd.notna(min_date) and pd.notna(max_date):
        date_range = st.sidebar.date_input(
            "DOB range",
            value=(min_date.date(), max_date.date()),
            min_value=min_date.date(),
            max_value=max_date.date(),
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start, end = pd.to_datetime(date_range[0]), pd.to_datetime(date_range[1])
            filtered = filtered[(filtered["DOB"].isna()) | ((filtered["DOB"] >= start) & (filtered["DOB"] <= end))]

    for label, col in [
        ("Strain", "Strain"),
        ("Sex", "Sex"),
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

    text_query = st.sidebar.text_input("Search Mouse ID / parent ID")
    if text_query:
        q = clean_id(text_query) or text_query.strip()
        mask = pd.Series(False, index=filtered.index)
        for col in ["Mouse ID", "Father ID", "Mother ID"]:
            if col in filtered.columns:
                mask = mask | filtered[col].fillna("").str.contains(q, case=False, regex=False)
        filtered = filtered[mask]

    return filtered


def get_ancestors(graph: nx.DiGraph, node: str, generations: int) -> set[str]:
    seen = set()
    current = {node}
    for _ in range(generations):
        parents = set()
        for n in current:
            parents.update(graph.predecessors(n))
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
            children.update(graph.successors(n))
        children -= seen
        seen.update(children)
        current = children
        if not current:
            break
    return seen


def get_siblings(graph: nx.DiGraph, node: str) -> set[str]:
    siblings = set()
    parents = list(graph.predecessors(node))
    for parent in parents:
        siblings.update(graph.successors(parent))
    siblings.discard(node)
    return siblings


def build_pyvis_graph(
    graph: nx.DiGraph,
    selected_id: str,
    ancestors: int,
    descendants: int,
    include_siblings: bool,
    height: str = "720px",
) -> str:
    nodes = {selected_id}
    nodes.update(get_ancestors(graph, selected_id, ancestors))
    nodes.update(get_descendants(graph, selected_id, descendants))
    if include_siblings:
        nodes.update(get_siblings(graph, selected_id))

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
        color = "#FDE68A" if node == selected_id else SEX_COLORS.get(str(sex), "#D1D5DB")
        missing = attrs.get("missing_from_file", False)
        if missing:
            color = "#E5E7EB"
        dob = attrs.get("DOB")
        dob_text = ""
        if pd.notna(dob):
            try:
                dob_text = pd.to_datetime(dob).strftime("%Y-%m-%d")
            except Exception:
                dob_text = str(dob)

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
                f"DOB: {dob_text}",
                f"Strain: {attrs.get('Strain') or ''}",
                f"Genotype: {attrs.get('Genotype') or ''}",
                f"Use: {attrs.get('Use') or ''}",
                f"Owner: {attrs.get('Owner') or ''}",
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
        tmp.seek(0)
        html = open(tmp.name, "r", encoding="utf-8").read()
    return html


def show_mouse_card(df: pd.DataFrame, mouse_id: str) -> None:
    row = df[df["Mouse ID"] == mouse_id]
    if row.empty:
        st.warning("This ID is only present as a parent reference, but not as a full animal row in the file.")
        return
    row = row.iloc[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Mouse ID", mouse_id)
    c2.metric("Sex", row.get("Sex") or "Unknown")
    c3.metric("DOB", row.get("DOB").strftime("%Y-%m-%d") if pd.notna(row.get("DOB")) else "Unknown")
    c4.metric("Use", row.get("Use") or "Unknown")

    details = {col: row.get(col) for col in DISPLAY_COLUMNS if col in row.index}
    details_df = pd.DataFrame([details]).T.rename(columns={0: "Value"})
    st.dataframe(details_df, use_container_width=True)


def overview_tab(data: PedigreeData, filtered: pd.DataFrame) -> None:
    df = data.mice
    edges = data.edges

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Animals", f"{len(df):,}")
    c2.metric("Filtered", f"{len(filtered):,}")
    c3.metric("Strains", f"{df['Strain'].nunique(dropna=True):,}")
    c4.metric("Parent links", f"{len(edges):,}")
    missing_parent_refs = 0 if edges.empty else int((~edges["parent_exists_in_file"]).sum())
    c5.metric("Parent refs not found", f"{missing_parent_refs:,}")

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

    st.markdown("Search one animal and render its ancestors, descendants, and optionally siblings.")
    c1, c2, c3, c4 = st.columns([1.6, 1, 1, 1])

    typed_id = c1.text_input("Mouse ID", placeholder="Example: SDK015_205_1")
    selected_from_list = c1.selectbox("Or select from filtered animals", options=[""] + default_pool, index=0)
    selected_id = clean_id(typed_id) if typed_id else selected_from_list

    ancestors = c2.slider("Ancestor generations", min_value=1, max_value=8, value=3)
    descendants = c3.slider("Descendant generations", min_value=0, max_value=8, value=2)
    include_siblings = c4.checkbox("Include siblings", value=True)

    if not selected_id:
        st.info("Choose or type a Mouse ID to show the pedigree.")
        return

    if selected_id not in graph.nodes:
        st.error("Mouse ID not found. Check spaces, capitalization, or whether it is included in the current file.")
        return

    show_mouse_card(df, selected_id)

    parent_count = len(list(graph.predecessors(selected_id)))
    child_count = len(list(graph.successors(selected_id)))
    sibling_count = len(get_siblings(graph, selected_id))
    c1, c2, c3 = st.columns(3)
    c1.metric("Known parents", parent_count)
    c2.metric("Known children", child_count)
    c3.metric("Possible siblings", sibling_count)

    html = build_pyvis_graph(graph, selected_id, ancestors, descendants, include_siblings)
    components.html(html, height=760, scrolling=True)

    export_nodes = {selected_id}
    export_nodes.update(get_ancestors(graph, selected_id, ancestors))
    export_nodes.update(get_descendants(graph, selected_id, descendants))
    if include_siblings:
        export_nodes.update(get_siblings(graph, selected_id))
    sub_edges = data.edges[data.edges["parent"].isin(export_nodes) & data.edges["child"].isin(export_nodes)]
    st.download_button(
        "Download displayed pedigree edges as CSV",
        data=dataframe_to_csv_bytes(sub_edges),
        file_name=f"{selected_id}_pedigree_edges.csv",
        mime="text/csv",
    )


def timeline_tab(data: PedigreeData, filtered: pd.DataFrame) -> None:
    st.subheader("Timeline")
    st.markdown("This is the view closest to the idea of organizing the colony by month.")

    df = filtered.dropna(subset=["DOB"]).copy()
    if df.empty:
        st.info("No animals with DOB available in the current filter.")
        return

    color_by = st.selectbox("Color timeline by", options=["Strain", "Sex", "Use", "Owner"], index=0)
    hover_cols = [c for c in ["Mouse ID", "Sex", "DOB", "Strain", "Genotype", "Use", "Owner", "Father ID", "Mother ID"] if c in df.columns]

    # Timeline-like scatter. Each mouse is a point positioned by birth date and grouped by strain/use/owner.
    max_categories = 40
    group_col = color_by if color_by in df.columns else "Strain"
    top_values = df[group_col].value_counts().head(max_categories).index.tolist()
    plot_df = df[df[group_col].isin(top_values)].copy()

    fig = px.scatter(
        plot_df,
        x="DOB",
        y=group_col,
        color=color_by if color_by in plot_df.columns else None,
        hover_data=hover_cols,
        title=f"Animals by DOB and {group_col}",
    )
    fig.update_layout(height=max(550, 25 * min(max_categories, plot_df[group_col].nunique()) + 220))
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Birth counts by month")
    monthly = (
        df.assign(month=lambda x: x["DOB"].dt.to_period("M").dt.to_timestamp())
        .groupby(["month", color_by], dropna=False)
        .size()
        .reset_index(name="count")
    )
    fig2 = px.bar(monthly, x="month", y="count", color=color_by, title="Monthly births")
    fig2.update_layout(xaxis_title="Month", yaxis_title="Animals born")
    st.plotly_chart(fig2, use_container_width=True)


def qc_tab(data: PedigreeData) -> None:
    df = data.mice
    edges = data.edges

    st.subheader("Data quality checks")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Original export rows", f"{data.original_rows:,}")
    c2.metric("Animal rows kept", f"{len(df):,}")
    c3.metric("Duplicate Mouse IDs", f"{int(df['Mouse ID'].duplicated().sum()):,}")
    missing_ref = 0 if edges.empty else int((~edges["parent_exists_in_file"]).sum())
    c4.metric("Parent IDs not found", f"{missing_ref:,}")

    tabs = st.tabs(["Missing parents", "Parent IDs not found", "Duplicate IDs", "Potential founders"])

    with tabs[0]:
        missing_parents = df[df["Father ID"].isna() | df["Mother ID"].isna()]
        st.write(f"Animals missing father or mother ID: {len(missing_parents):,}")
        cols = [c for c in DISPLAY_COLUMNS if c in missing_parents.columns]
        st.dataframe(missing_parents[cols], use_container_width=True)

    with tabs[1]:
        if edges.empty:
            st.info("No parent links were found.")
        else:
            absent = edges[~edges["parent_exists_in_file"]].copy()
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

    st.download_button(
        "Download cleaned animal table as CSV",
        data=dataframe_to_csv_bytes(df),
        file_name="cleaned_transnetyx_mice.csv",
        mime="text/csv",
    )
    st.download_button(
        "Download all parent-child edges as CSV",
        data=dataframe_to_csv_bytes(edges),
        file_name="transnetyx_parent_child_edges.csv",
        mime="text/csv",
    )


def table_tab(filtered: pd.DataFrame) -> None:
    st.subheader("Filtered animal table")
    cols = st.multiselect(
        "Columns to show",
        options=filtered.columns.tolist(),
        default=[c for c in DISPLAY_COLUMNS if c in filtered.columns],
    )
    if cols:
        st.dataframe(filtered[cols], use_container_width=True)
    else:
        st.dataframe(filtered, use_container_width=True)

    st.download_button(
        "Download current filtered table as CSV",
        data=dataframe_to_csv_bytes(filtered),
        file_name="filtered_transnetyx_mice.csv",
        mime="text/csv",
    )


def main() -> None:
    st.title("Transnetyx Mouse Pedigree Explorer")
    st.caption("Upload a Transnetyx mouse history Excel export to explore pedigrees, timelines, and colony QC.")

    uploaded = st.file_uploader("Upload Transnetyx Excel export", type=["xlsx", "xls"])

    if uploaded is None:
        st.info("Upload the Excel export from Transnetyx to start.")
        st.markdown(
            """
            Expected columns include **Mouse ID**, **Father ID**, **Mother ID**, **DOB**, **Sex**, **Strain**, **Genotype**, **Use**, and **Owner**.
            """
        )
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

    filtered = apply_filters(data.mice)

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Overview", "Animal pedigree", "Timeline by month", "Data QC", "Table"])
    with tab1:
        overview_tab(data, filtered)
    with tab2:
        pedigree_tab(data, filtered)
    with tab3:
        timeline_tab(data, filtered)
    with tab4:
        qc_tab(data)
    with tab5:
        table_tab(filtered)


if __name__ == "__main__":
    main()
