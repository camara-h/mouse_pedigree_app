from __future__ import annotations

import io
import re
import tempfile
from dataclasses import dataclass
from datetime import date
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
DATE_COLUMNS = ["DOB", "DOD", "Wean Date"]
DISPLAY_COLUMNS = [
    "Mouse ID",
    "Sex",
    "Status",
    "DOB",
    "DOD",
    "Wean Date",
    "Age",
    "Alive at Range",
    "Strain",
    "Genotype",
    "Use",
    "Owner",
    "Litter Name",
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


def normalize_bool_for_csv(value):
    if pd.isna(value):
        return "Unknown"
    return value


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

    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    if "DOB" in df.columns:
        df["DOB Month"] = df["DOB"].dt.to_period("M").astype(str)
        df.loc[df["DOB"].isna(), "DOB Month"] = None

    # Placeholder. It is recalculated from the sidebar date range.
    df["Alive at Range"] = pd.NA

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
    if "Alive at Range" in out.columns:
        out["Alive at Range"] = out["Alive at Range"].map(normalize_bool_for_csv)
    return out.to_csv(index=False).encode("utf-8")


def infer_sheet_names(uploaded_file_bytes: bytes) -> list[str]:
    xls = pd.ExcelFile(io.BytesIO(uploaded_file_bytes))
    return xls.sheet_names


def compute_alive_at_range(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """True when the animal was alive at any point during the selected range.

    Definition:
    - DOB must be known and on or before the range end.
    - DOD is either unknown or on/after the range start.
    """
    dob = pd.to_datetime(df["DOB"], errors="coerce") if "DOB" in df.columns else pd.Series(pd.NaT, index=df.index)
    dod = pd.to_datetime(df["DOD"], errors="coerce") if "DOD" in df.columns else pd.Series(pd.NaT, index=df.index)
    return dob.notna() & (dob <= end) & (dod.isna() | (dod >= start))


def apply_filters(df: pd.DataFrame) -> tuple[pd.DataFrame, tuple[pd.Timestamp, pd.Timestamp] | None, str]:
    st.sidebar.header("Filters")

    filtered = df.copy()
    today = pd.Timestamp(date.today())

    valid_dob = pd.to_datetime(filtered.get("DOB"), errors="coerce")
    min_date = valid_dob.min() if valid_dob.notna().any() else today
    max_date = valid_dob.max() if valid_dob.notna().any() else today

    default_start = min_date.date() if pd.notna(min_date) else today.date()
    default_end = max(today, max_date).date() if pd.notna(max_date) else today.date()

    date_mode = st.sidebar.selectbox(
        "Date filter mode",
        options=["DOB within selected range", "Alive anytime during selected range", "No date filter"],
        index=0,
        help="The date picker is not capped by the last DOB in the file, so you can use today's date for current monthly reports.",
    )

    selected_range = None
    if date_mode != "No date filter":
        date_range = st.sidebar.date_input(
            "Selected date range",
            value=(default_start, default_end),
            help="Used either as a DOB filter or as the alive-at-range window.",
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            start = pd.to_datetime(date_range[0])
            end = pd.to_datetime(date_range[1])
            if start > end:
                st.sidebar.error("Start date is after end date. Please choose a valid range.")
            else:
                selected_range = (start, end)
                filtered["Alive at Range"] = compute_alive_at_range(filtered, start, end)
                if date_mode == "DOB within selected range":
                    filtered = filtered[
                        (filtered["DOB"].isna())
                        | ((filtered["DOB"] >= start) & (filtered["DOB"] <= end))
                    ]
                elif date_mode == "Alive anytime during selected range":
                    filtered = filtered[filtered["Alive at Range"] == True]
    else:
        filtered["Alive at Range"] = pd.NA

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

    text_query = st.sidebar.text_input("Search Mouse ID / parent ID")
    if text_query:
        q = clean_id(text_query) or text_query.strip()
        mask = pd.Series(False, index=filtered.index)
        for col in ["Mouse ID", "Father ID", "Mother ID"]:
            if col in filtered.columns:
                mask = mask | filtered[col].fillna("").str.contains(q, case=False, regex=False)
        filtered = filtered[mask]

    return filtered, selected_range, date_mode


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


def max_generations_available(graph: nx.DiGraph, start_node: str, direction: str) -> int:
    if start_node not in graph:
        return 0
    seen = {start_node}
    current = {start_node}
    depth = 0
    while current:
        next_nodes = set()
        for n in current:
            if direction == "ancestors":
                next_nodes.update(safe_predecessors(graph, n))
            elif direction == "descendants":
                next_nodes.update(safe_successors(graph, n))
            else:
                raise ValueError("direction must be 'ancestors' or 'descendants'")
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


def format_date(value) -> str:
    if pd.isna(value):
        return ""
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return str(value)


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

        dob_text = format_date(attrs.get("DOB"))
        dod_text = format_date(attrs.get("DOD"))

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


def create_pedigree_png_bytes(graph: nx.DiGraph, displayed_nodes: set[str], selected_ids: list[str]) -> bytes:
    sub = graph.subgraph(displayed_nodes).copy()
    if sub.number_of_nodes() == 0:
        return b""

    # Matplotlib export is intentionally simpler than the interactive PyVis view.
    # It gives users a static file they can put in reports or email.
    width = min(28, max(10, sub.number_of_nodes() * 0.28))
    height = min(24, max(7, sub.number_of_nodes() * 0.18))
    plt.figure(figsize=(width, height))

    try:
        generations = list(nx.topological_generations(sub))
        pos = {}
        for y, generation in enumerate(generations):
            generation = sorted(generation)
            x_offset = (len(generation) - 1) / 2
            for x, node in enumerate(generation):
                pos[node] = (x - x_offset, -y)
    except Exception:
        pos = nx.spring_layout(sub, seed=42, k=1.2)

    node_colors = []
    for node in sub.nodes:
        attrs = graph.nodes[node]
        if node in selected_ids:
            node_colors.append("#FDE68A")
        elif attrs.get("missing_from_file", False):
            node_colors.append("#E5E7EB")
        else:
            node_colors.append(SEX_COLORS.get(str(attrs.get("Sex") or "Unknown"), "#D1D5DB"))

    labels = {node: node for node in sub.nodes}
    nx.draw_networkx_edges(sub, pos, arrows=True, arrowstyle="-|>", arrowsize=10, width=1.2, edge_color="#6B7280")
    nx.draw_networkx_nodes(sub, pos, node_color=node_colors, node_size=650, edgecolors="#374151", linewidths=0.6)
    nx.draw_networkx_labels(sub, pos, labels=labels, font_size=7)
    plt.axis("off")
    plt.tight_layout()

    out = io.BytesIO()
    plt.savefig(out, format="png", dpi=220, bbox_inches="tight")
    plt.close()
    out.seek(0)
    return out.getvalue()


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
    c4.metric("DOB", format_date(row.get("DOB")) or "Unknown")
    c5.metric("Use", row.get("Use") or "Unknown")

    details = {col: row.get(col) for col in DISPLAY_COLUMNS if col in row.index}
    details_df = pd.DataFrame([details]).T.rename(columns={0: "Value"})
    st.dataframe(details_df, use_container_width=True)


def overview_tab(data: PedigreeData, filtered: pd.DataFrame, selected_range: tuple[pd.Timestamp, pd.Timestamp] | None) -> None:
    df = data.mice
    edges = data.edges

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Animals", f"{len(df):,}")
    c2.metric("Filtered", f"{len(filtered):,}")
    c3.metric("Strains", f"{df['Strain'].nunique(dropna=True):,}" if "Strain" in df.columns else "0")
    c4.metric("Parent links", f"{len(edges):,}")
    missing_parent_refs = 0 if edges.empty else int((~edges["parent_exists_in_file"]).sum())
    c5.metric("Parent refs not found", f"{missing_parent_refs:,}")

    if selected_range is not None:
        alive_count = int(filtered.get("Alive at Range", pd.Series(False, index=filtered.index)).fillna(False).sum())
        st.caption(f"Alive at selected range among current filtered rows: {alive_count:,}")

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
        if "Strain" in filtered.columns:
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

    st.markdown(
        "Search one or more animals and render their shared pedigree network, including ancestors, descendants, and optionally siblings."
    )

    input_col, settings_col = st.columns([1.4, 1])
    with input_col:
        typed_ids = st.text_area(
            "Mouse ID(s)",
            placeholder="Example: SDK015_205_1\nOr paste multiple IDs separated by commas or new lines",
            height=90,
        )
        selected_from_list = st.multiselect(
            "Or select from filtered animals",
            options=default_pool,
            default=[],
            help="You can select more than one animal here.",
        )

    typed_id_list = parse_mouse_ids(typed_ids)
    selected_ids = []
    for mouse_id in typed_id_list + selected_from_list:
        cleaned = clean_id(mouse_id)
        if cleaned and cleaned not in selected_ids:
            selected_ids.append(cleaned)

    with settings_col:
        st.caption("Generation settings")
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
        st.error("None of the selected Mouse IDs were found. Check spaces, capitalization, or whether they are included in the file.")
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

    displayed_nodes = build_displayed_nodes(graph, valid_ids, ancestors, descendants, include_siblings)
    if len(displayed_nodes) > 500:
        st.warning(
            f"This pedigree contains {len(displayed_nodes):,} nodes. It may render slowly. Try lowering generations or turning off siblings if needed."
        )

    st.subheader("Selected animal details")
    if len(valid_ids) == 1:
        show_mouse_card(df, valid_ids[0])
    else:
        selected_rows = df[df["Mouse ID"].isin(valid_ids)].copy()
        cols = [c for c in DISPLAY_COLUMNS if c in selected_rows.columns]
        st.dataframe(selected_rows[cols], use_container_width=True)

    known_parents = sum(len(safe_predecessors(graph, mouse_id)) for mouse_id in valid_ids)
    known_children = sum(len(safe_successors(graph, mouse_id)) for mouse_id in valid_ids)
    possible_siblings = len(set().union(*(get_siblings(graph, mouse_id) for mouse_id in valid_ids))) if valid_ids else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Selected animals", len(valid_ids))
    c2.metric("Known parent links", known_parents)
    c3.metric("Known child links", known_children)
    c4.metric("Possible siblings", possible_siblings)

    html = build_pyvis_graph(graph, valid_ids, ancestors, descendants, include_siblings)
    components.html(html, height=760, scrolling=True)

    sub_edges = data.edges[data.edges["parent"].isin(displayed_nodes) & data.edges["child"].isin(displayed_nodes)]
    export_name = valid_ids[0] if len(valid_ids) == 1 else "multiple_animals"

    export_col1, export_col2 = st.columns(2)
    with export_col1:
        st.download_button(
            "Download displayed pedigree edges as CSV",
            data=dataframe_to_csv_bytes(sub_edges),
            file_name=f"{export_name}_pedigree_edges.csv",
            mime="text/csv",
        )
    with export_col2:
        png_bytes = create_pedigree_png_bytes(graph, displayed_nodes, valid_ids)
        st.download_button(
            "Download displayed pedigree as PNG",
            data=png_bytes,
            file_name=f"{export_name}_pedigree.png",
            mime="image/png",
            disabled=not bool(png_bytes),
        )


def timeline_tab(data: PedigreeData, filtered: pd.DataFrame) -> None:
    st.subheader("Timeline")
    st.markdown("This is the view closest to the idea of organizing the colony by month.")

    df = filtered.dropna(subset=["DOB"]).copy()
    if df.empty:
        st.info("No animals with DOB available in the current filter.")
        return

    color_by = st.selectbox("Color timeline by", options=[c for c in ["Strain", "Sex", "Status", "Use", "Owner"] if c in df.columns], index=0)
    group_by = st.selectbox("Rows/group animals by", options=[c for c in ["Strain", "Owner", "Use", "Status", "Sex"] if c in df.columns], index=0)
    row_order = st.selectbox(
        "Row order",
        options=["Alphabetical", "Most recent birth first", "Oldest birth first", "Most animals first"],
        index=0,
    )
    hover_cols = [c for c in ["Mouse ID", "Sex", "Status", "DOB", "DOD", "Strain", "Genotype", "Use", "Owner", "Father ID", "Mother ID"] if c in df.columns]

    max_categories = st.slider("Maximum row categories to show", min_value=10, max_value=100, value=40, step=5)
    group_col = group_by if group_by in df.columns else "Strain"

    if row_order == "Most animals first":
        ordered_values = df[group_col].value_counts().head(max_categories).index.tolist()
    else:
        summary = (
            df.groupby(group_col, dropna=True)
            .agg(last_born=("DOB", "max"), first_born=("DOB", "min"), count=("Mouse ID", "count"))
            .reset_index()
        )
        if row_order == "Most recent birth first":
            summary = summary.sort_values("last_born", ascending=False)
        elif row_order == "Oldest birth first":
            summary = summary.sort_values("first_born", ascending=True)
        else:
            summary = summary.sort_values(group_col, ascending=True)
        ordered_values = summary[group_col].head(max_categories).tolist()

    plot_df = df[df[group_col].isin(ordered_values)].copy()
    plot_df[group_col] = pd.Categorical(plot_df[group_col], categories=list(reversed(ordered_values)), ordered=True)

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
    monthly_color = st.selectbox(
        "Color monthly births by",
        options=["Single color"] + [c for c in ["Owner", "Strain", "Sex", "Status", "Use"] if c in df.columns],
        index=0,
    )
    if monthly_color == "Single color":
        monthly = (
            df.assign(month=lambda x: x["DOB"].dt.to_period("M").dt.to_timestamp())
            .groupby("month", dropna=False)
            .size()
            .reset_index(name="count")
        )
        fig2 = px.bar(monthly, x="month", y="count", title="Monthly births")
    else:
        monthly = (
            df.assign(month=lambda x: x["DOB"].dt.to_period("M").dt.to_timestamp())
            .groupby(["month", monthly_color], dropna=False)
            .size()
            .reset_index(name="count")
        )
        fig2 = px.bar(monthly, x="month", y="count", color=monthly_color, title=f"Monthly births colored by {monthly_color}")
    fig2.update_layout(xaxis_title="Month", yaxis_title="Animals born")
    st.plotly_chart(fig2, use_container_width=True)


def alive_report_tab(filtered: pd.DataFrame, selected_range: tuple[pd.Timestamp, pd.Timestamp] | None) -> None:
    st.subheader("Alive at Range report")
    if selected_range is None:
        st.info("Choose a date range in the sidebar to calculate Alive at Range.")
        return

    start, end = selected_range
    st.caption(f"Alive at Range means DOB ≤ {end.date()} and DOD is blank or ≥ {start.date()}.")

    if "Alive at Range" not in filtered.columns:
        st.info("Alive at Range has not been calculated for the current table.")
        return

    alive_df = filtered[filtered["Alive at Range"] == True].copy()
    c1, c2, c3 = st.columns(3)
    c1.metric("Rows in current filter", f"{len(filtered):,}")
    c2.metric("Alive anytime in range", f"{len(alive_df):,}")
    c3.metric("Not alive or unknown", f"{len(filtered) - len(alive_df):,}")

    group_options = [c for c in ["Owner", "Strain", "Use", "Status", "Sex"] if c in alive_df.columns]
    if group_options and not alive_df.empty:
        group_by = st.selectbox("Summarize alive animals by", options=group_options, index=0)
        summary = alive_df[group_by].fillna("Unknown").value_counts().reset_index()
        summary.columns = [group_by, "count"]
        fig = px.bar(summary, x="count", y=group_by, orientation="h", title=f"Alive animals by {group_by}")
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, xaxis_title="Animals")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(summary, use_container_width=True)

    cols = [c for c in DISPLAY_COLUMNS if c in alive_df.columns]
    st.dataframe(alive_df[cols], use_container_width=True)
    st.download_button(
        "Download Alive at Range table as CSV",
        data=dataframe_to_csv_bytes(alive_df),
        file_name="alive_at_range_mice.csv",
        mime="text/csv",
    )


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
    default_cols = [c for c in DISPLAY_COLUMNS if c in filtered.columns]
    cols = st.multiselect("Columns to show", options=filtered.columns.tolist(), default=default_cols)
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
    st.caption("Upload a Transnetyx mouse history Excel export to explore pedigrees, timelines, colony QC, and alive-at-range reports.")

    uploaded = st.file_uploader("Upload Transnetyx Excel export", type=["xlsx", "xls"])

    if uploaded is None:
        st.info("Upload the Excel export from Transnetyx to start.")
        st.markdown(
            """
            Expected columns include **Mouse ID**, **Father ID**, **Mother ID**, **DOB**, **Sex**, **Strain**, **Genotype**, **Use**, and **Owner**. Optional columns such as **Status**, **DOD**, **Wean Date**, and **Litter Name** are used when present.
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

    filtered, selected_range, date_mode = apply_filters(data.mice)

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Overview",
        "Animal pedigree",
        "Timeline by month",
        "Alive at Range",
        "Data QC",
        "Table",
    ])
    with tab1:
        overview_tab(data, filtered, selected_range)
    with tab2:
        pedigree_tab(data, filtered)
    with tab3:
        timeline_tab(data, filtered)
    with tab4:
        alive_report_tab(filtered, selected_range)
    with tab5:
        qc_tab(data)
    with tab6:
        table_tab(filtered)


if __name__ == "__main__":
    main()
