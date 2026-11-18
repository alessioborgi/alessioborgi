"""ICLR 2027 Review Atlas — run with: streamlit run app.py"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from analytics import compare_to_average, rank_papers, score_papers
from collector import (
    COMPLETE_DB, WORKING_DB, CollectionError, collect_public_reviews,
    collection_progress, load_tables,
)

st.set_page_config(page_title="ICLR 2027 | Review Atlas", page_icon="📊", layout="wide", initial_sidebar_state="expanded")

COLORS = {
    "bg": "#0B1020", "card": "#151D32", "ink": "#E8EDF8", "muted": "#9AA9C1",
    "teal": "#58D7BF", "blue": "#6CA8FF", "purple": "#AE8AFB", "line": "#26334C",
    "pink": "#FF8EA1",
}

st.markdown("""
<style>
.stApp {background: #0B1020; color: #E8EDF8;}
.block-container {padding-top: 1.75rem; padding-bottom: 4rem; max-width: 1550px;}
[data-testid="stSidebar"] {background: #11192B; border-right: 1px solid #26334C;}
[data-testid="stMetric"] {padding: 0.9rem 1.05rem; background: #151D32; border:1px solid #26334C; border-radius: 14px;}
[data-testid="stMetricLabel"] {color: #B0BCD3; font-size: 0.86rem;}
[data-testid="stMetricValue"] {font-weight: 730; color:#E8EDF8; font-variant-numeric: tabular-nums;}
.stButton>button[kind="primary"] {background-color: #58D7BF; color:#0B1020; border:none; font-weight:700;}
.stTabs [data-baseweb="tab-list"] {gap:1.2rem;}
.stTabs [data-baseweb="tab"] {font-weight:650;}
h1,h2,h3 {letter-spacing:-0.03em;}
.kicker {font-size:.8rem; font-weight:750; letter-spacing:.16em; text-transform:uppercase; color:#58D7BF;}
.intro {color:#B0BCD3; font-size:1rem; margin-top:-.4rem; max-width:1000px;}
.data-note {padding: 12px 15px; background:#151D32; border-left:3px solid #58D7BF; border-radius:5px; color:#B0BCD3; font-size:.9rem;}
[data-testid="stDownloadButton"] button {border:1px solid #4E5A73;}
</style>
""", unsafe_allow_html=True)


def style_chart(fig, *, height=375, xtitle=None, ytitle=None):
    fig.update_layout(
        height=height, template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", font_color=COLORS["ink"],
        margin=dict(l=15, r=15, t=42, b=20),
        xaxis=dict(showgrid=False, zeroline=False, title=xtitle),
        yaxis=dict(gridcolor=COLORS["line"], zeroline=False, title=ytitle),
        legend=dict(orientation="h", y=-0.25),
    )
    return fig


@st.cache_data(show_spinner=False)
def read_db_cached(path: str, modification_ns: int):
    del modification_ns
    return load_tables(Path(path))


def import_flat_csv(file):
    frame = pd.read_csv(BytesIO(file.getvalue()))
    required = {"paper_id", "title", "rating", "confidence"}
    if not required.issubset(frame.columns):
        raise ValueError("CSV requires columns: paper_id, title, rating, confidence. One row represents one review.")
    if frame.empty:
        raise ValueError("CSV contains no rows.")
    for name, default in [
        ("number", None), ("area", "Unspecified"), ("status", "Active"),
        ("reviewer", "Anonymous"), ("review_id", None), ("url", None), ("venueid", ""),
    ]:
        if name not in frame:
            frame[name] = default
    frame["paper_id"] = frame["paper_id"].astype(str)
    frame["review_id"] = frame["review_id"].where(frame["review_id"].notna(), "import_" + frame.index.astype(str))
    frame["url"] = frame["url"].where(frame["url"].notna(), "https://openreview.net/forum?id=" + frame["paper_id"])
    for name in ["rating", "confidence"]:
        frame[name] = pd.to_numeric(frame[name].astype(str).str.extract(r"^\s*([+-]?\d+(?:\.\d+)?)")[0], errors="coerce")
    columns = ["paper_id", "number", "title", "area", "status", "venueid", "url"]
    papers = frame[columns].drop_duplicates(subset=["paper_id"]).copy()
    reviews = frame[["review_id", "paper_id", "rating", "confidence", "reviewer"]].copy()
    return papers, reviews, {"last_updated_utc": "User-provided CSV", "total_submissions": str(len(papers)), "source": "CSV import"}


st.markdown('<div class="kicker">Research intelligence · ICLR 2027</div>', unsafe_allow_html=True)
st.title("Review Atlas")
st.markdown('<p class="intro">Explore official reviewer ratings and confidence scores across ICLR 2027 submissions. Rank papers, inspect individual reviews, and see exactly how many papers score above or below your average.</p>', unsafe_allow_html=True)

with st.sidebar:
    st.markdown("### Data source")
    st.caption("Public OpenReview API v2 · ICLR.cc/2027/Conference")
    source = st.radio("Choose source", ["OpenReview snapshot", "Upload review CSV"], label_visibility="collapsed")
    if source == "OpenReview snapshot":
        saved_count, after = collection_progress()
        st.caption("Snapshot stored locally in `data/`. It does not refresh automatically.")
        if COMPLETE_DB.exists():
            st.success("Complete local snapshot available")
        if saved_count:
            st.info(f"Unfinished retrieval: {saved_count:,} papers saved. Resume is supported.")
        with st.expander("Collector options"):
            page_size = st.select_slider("Papers per request", options=[25, 50, 75, 100, 150, 200], value=75)
            new_run = st.checkbox("Start a fresh retrieval (discard incomplete progress)", value=False)
        if st.button("↻  Fetch / refresh from OpenReview", type="primary", use_container_width=True):
            bar = st.progress(0.0)
            message = st.empty()

            def update(count, expected, n_reviews):
                if expected and expected > 0:
                    bar.progress(min(count / expected, 1.0))
                    message.caption(f"Collected {count:,} / {expected:,} submissions, {n_reviews:,} official reviews")
                else:
                    bar.progress(0.0)
                    message.caption(f"Collected {count:,} submissions, {n_reviews:,} official reviews")

            try:
                with st.spinner("Collecting public reviews. Partial progress is saved for resume."):
                    result = collect_public_reviews(start_fresh=new_run, page_size=page_size, on_progress=update)
                st.cache_data.clear()
                st.success(f"Complete snapshot: {result['total_submissions']:,} submissions, {result['total_reviews']:,} reviews.")
                st.rerun()
            except (CollectionError, Exception) as exc:
                st.error(str(exc))
                st.caption("Incomplete progress was retained. A previously completed snapshot, if any, remains intact.")
        uploader = None
    else:
        st.caption("Import a flat CSV: one row per reviewer assessment; the same paper can occur on several rows.")
        uploader = st.file_uploader("CSV with columns paper_id, title, rating, confidence", type=["csv"])
        with st.expander("CSV format example"):
            st.code("paper_id,title,rating,confidence,number,area,status,reviewer\n"
                    'abc123,"An Example Paper",3,4,12,Representation Learning,Active,Reviewer_1\n'
                    'abc123,"An Example Paper",2,3,12,Representation Learning,Active,Reviewer_2', language="csv")
    st.divider()
    st.markdown("### Ranking settings")
    ranking_label = st.selectbox("Paper score", ["Arithmetic mean rating", "Confidence-weighted mean rating"])
    metric = "mean_rating" if ranking_label.startswith("Arithmetic") else "weighted_rating"
    min_reviews = st.slider("Minimum rated reviews per paper", min_value=1, max_value=8, value=1)
    show_inactive = st.checkbox("Include withdrawn / rejected submissions", value=False)
    st.divider()
    st.caption("Public official reviews only. No desk-rejects or withdrawals by default. Not an acceptance prediction.")

if source == "OpenReview snapshot":
    if not COMPLETE_DB.exists():
        st.info("No complete local snapshot yet. Use **Fetch / refresh from OpenReview** in the sidebar to collect the submissions and reviews, or upload a CSV snapshot.")
        st.markdown("**This application collects the complete public set, rather than inventing or sampling statistics.** The initial import can involve tens of thousands of submissions and many API pages.")
        st.stop()
    papers, reviews, info = read_db_cached(str(COMPLETE_DB), COMPLETE_DB.stat().st_mtime_ns)
else:
    if uploader is None:
        st.info("Upload a review-level CSV in the sidebar to explore an existing dataset.")
        st.stop()
    try:
        papers, reviews, info = import_flat_csv(uploader)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

if papers.empty:
    st.warning("The dataset contains no submissions.")
    st.stop()

all_scored = score_papers(papers, reviews)
areas = sorted(str(a) for a in all_scored["area"].dropna().unique() if str(a))
filter_top = st.columns([2, 1, 1])
with filter_top[0]:
    selected_areas = st.multiselect("Research area", options=areas, placeholder="All areas")
with filter_top[1]:
    search_text = st.text_input("Search title / paper ID", placeholder="Filter rankings…")
with filter_top[2]:
    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.caption(f"Snapshot: {info.get('last_updated_utc', 'unknown')} · {len(papers):,} total records")

filtered = all_scored.copy()
if not show_inactive:
    filtered = filtered[filtered["status"] == "Active"]
if selected_areas:
    filtered = filtered[filtered["area"].isin(selected_areas)]
filtered = filtered[filtered["n_ratings"] >= min_reviews].copy()
if metric == "weighted_rating":
    filtered = filtered[filtered[metric].notna()]
# Search is for table discovery; it does not affect comparison cohort or charts.
cohort = filtered
ranked = rank_papers(cohort, metric)

if ranked.empty:
    st.warning("No papers match the selected cohort. Lower the minimum review count or change the filters.")
    st.stop()

active_n = int((all_scored["status"] == "Active").sum())
scored_n = int(((all_scored["status"] == "Active") & (all_scored["n_ratings"] > 0)).sum())
review_n = int(reviews[reviews["paper_id"].isin(cohort["paper_id"])]["rating"].notna().sum())

st.markdown("<div class='data-note'>Comparison universe: <b>{:,} papers</b> with {} (at least {} rated reviewer{}). The search box only filters the visible table, not the statistics.</div>".format(
    len(ranked), "active status" if not show_inactive else "any submission status", min_reviews,
    "" if min_reviews == 1 else "s"), unsafe_allow_html=True)

metrics = st.columns(5)
metrics[0].metric("Papers in cohort", f"{len(ranked):,}")
metrics[1].metric("Public rated reviews", f"{review_n:,}")
metrics[2].metric("Mean paper rating", f"{ranked[metric].mean():.3f}")
metrics[3].metric("Median paper rating", f"{ranked[metric].median():.3f}")
metrics[4].metric("Average confidence", f"{ranked['mean_confidence'].mean():.2f}" if ranked["mean_confidence"].notna().any() else "N/A")

st.markdown("")
tab_overview, tab_compare, tab_rankings, tab_reviews, tab_paper = st.tabs([
    "Overview", "Compare my score", "All papers & ranking", "Reviewer statistics", "Paper inspector",
])

with tab_overview:
    st.subheader("Conference score landscape")
    left, right = st.columns([1.75, 1], gap="large")
    with left:
        fig = px.histogram(ranked, x=metric, nbins=65, title="Distribution of paper-average scores", color_discrete_sequence=[COLORS["teal"]])
        fig.add_vline(x=ranked[metric].median(), line_dash="dash", line_color=COLORS["pink"], annotation_text="Median", annotation_position="top right")
        fig.update_traces(hovertemplate="Average rating: %{x:.3f}<br>Papers: %{y}<extra></extra>")
        st.plotly_chart(style_chart(fig, xtitle=ranking_label, ytitle="Number of papers"), use_container_width=True)
    with right:
        bands = pd.cut(ranked[metric], bins=[0.99, 1.5, 2, 2.5, 3, 3.5, 4.01],
                       labels=["1.0–1.5", "1.5–2.0", "2.0–2.5", "2.5–3.0", "3.0–3.5", "3.5–4.0"], include_lowest=True)
        counts = bands.value_counts().sort_index().reset_index()
        counts.columns = ["Rating range", "Papers"]
        fig = px.bar(counts, x="Papers", y="Rating range", orientation="h", title="Paper counts by score band",
                     color_discrete_sequence=[COLORS["blue"]])
        st.plotly_chart(style_chart(fig, xtitle="Number of papers", ytitle=""), use_container_width=True)
    l2, r2 = st.columns([1.2, 1], gap="large")
    with l2:
        plot_data = ranked[["mean_confidence", "mean_rating", "n_ratings", "title", "url"]].dropna(subset=["mean_confidence", "mean_rating"])
        fig = px.density_heatmap(plot_data, x="mean_rating", y="mean_confidence", nbinsx=25, nbinsy=20,
                                 title="Paper rating vs. mean reviewer confidence", color_continuous_scale="Tealgrn")
        st.plotly_chart(style_chart(fig, xtitle="Mean rating", ytitle="Mean confidence"), use_container_width=True)
    with r2:
        fig = px.histogram(ranked, x="n_ratings", title="Number of rated reviews per paper",
                           color_discrete_sequence=[COLORS["purple"]])
        st.plotly_chart(style_chart(fig, xtitle="Review count", ytitle="Papers"), use_container_width=True)
    st.caption(f"Dataset coverage: {active_n:,} active submissions; {scored_n:,} active submissions with at least one public official rating. Missing public ratings are not treated as zeros.")

with tab_compare:
    st.subheader("Where does your average stand?")
    compare_left, compare_right = st.columns([1.03, 1.4], gap="large")
    with compare_left:
        st.write("Enter your **paper-average rating** on the ICLR 2027 scale (1–4). This is compared against the same metric used for the ranking.")
        my_score = st.number_input("Your mean rating", min_value=1.0, max_value=4.0, value=2.67, step=0.01, format="%.3f")
        comparison = compare_to_average(ranked[metric], my_score)
        st.metric("Your position (shared rank)", f"#{comparison['rank']:,} / {comparison['n']:,}")
        stat_cols = st.columns(3)
        stat_cols[0].metric("Ahead of you", f"{comparison['ahead']:,}")
        stat_cols[1].metric("Tied with you", f"{comparison['tied']:,}")
        stat_cols[2].metric("Below you", f"{comparison['below']:,}")
        st.metric("Midrank percentile", f"{comparison['percentile']:.2f}%")
        st.caption("Strictly higher = ahead; strictly lower = below; exactly equal = tied (within 1e−8). Your paper is hypothetical and is not added to the denominator. Higher percentile means a stronger relative score.")
    with compare_right:
        fig = px.histogram(ranked, x=metric, nbins=65, color_discrete_sequence=[COLORS["blue"]],
                           title="Your score in the full cohort distribution")
        fig.add_vline(x=my_score, line_width=3, line_color=COLORS["pink"],
                      annotation_text=f"You: {my_score:.3f}", annotation_position="top right")
        st.plotly_chart(style_chart(fig, height=430, xtitle=ranking_label, ytitle="Papers"), use_container_width=True)
        donut = go.Figure(go.Pie(
            labels=["Higher score", "Same score", "Lower score"],
            values=[comparison["ahead"], comparison["tied"], comparison["below"]],
            hole=.76, marker=dict(colors=[COLORS["pink"], COLORS["purple"], COLORS["teal"]]),
            sort=False, textinfo="percent", hovertemplate="%{label}: %{value:,} papers<extra></extra>"
        ))
        donut.update_layout(title="Papers relative to your average", showlegend=True)
        st.plotly_chart(style_chart(donut, height=330), use_container_width=True)
    st.markdown("#### Nearby papers")
    near = ranked.assign(distance=(ranked[metric] - my_score).abs()).nsmallest(25, "distance")
    st.dataframe(near[["rank", "title", metric, "n_ratings", "mean_confidence", "url"]],
                 column_config={"url": st.column_config.LinkColumn("OpenReview", display_text="Open paper")},
                 column_order=["rank", "title", metric, "n_ratings", "mean_confidence", "url"],
                 hide_index=True, use_container_width=True)

with tab_rankings:
    st.subheader("All ranked submissions")
    displayed = ranked
    if search_text.strip():
        query = search_text.strip().lower()
        displayed = ranked[
            ranked["title"].fillna("").str.lower().str.contains(query, regex=False)
            | ranked["paper_id"].fillna("").str.lower().str.contains(query, regex=False)
            | ranked["number"].fillna("").astype(str).str.contains(query, regex=False)
        ]
    st.caption(f"Showing {len(displayed):,} matching papers, sorted by {ranking_label.lower()}. Tied scores share a rank.")
    view_cols = ["rank", "number", "title", "area", "n_ratings", "mean_rating",
                 "weighted_rating", "mean_confidence", "rating_std", "min_rating", "max_rating", "url"]
    st.dataframe(
        displayed[view_cols],
        column_config={
            "rank": st.column_config.NumberColumn("Rank", format="%d"),
            "number": st.column_config.NumberColumn("#", format="%d"),
            "n_ratings": st.column_config.NumberColumn("Reviews", format="%d"),
            "mean_rating": st.column_config.NumberColumn("Mean", format="%.3f"),
            "weighted_rating": st.column_config.NumberColumn("Weighted", format="%.3f"),
            "mean_confidence": st.column_config.NumberColumn("Confidence", format="%.2f"),
            "rating_std": st.column_config.NumberColumn("Rating SD", format="%.2f"),
            "min_rating": st.column_config.NumberColumn("Min", format="%.0f"),
            "max_rating": st.column_config.NumberColumn("Max", format="%.0f"),
            "url": st.column_config.LinkColumn("OpenReview", display_text="View paper"),
        },
        hide_index=True, use_container_width=True, height=620,
    )
    st.download_button(
        "↓ Download filtered paper ranking (CSV)",
        data=displayed.to_csv(index=False).encode("utf-8-sig"),
        file_name="iclr2027_rankings.csv", mime="text/csv"
    )

with tab_reviews:
    st.subheader("Reviewer-level statistics")
    rv = reviews[reviews["paper_id"].isin(cohort["paper_id"])].copy()
    rv["rating"] = pd.to_numeric(rv["rating"], errors="coerce")
    rv["confidence"] = pd.to_numeric(rv["confidence"], errors="coerce")
    ra, rb = st.columns(2, gap="large")
    with ra:
        freq = rv["rating"].dropna().value_counts().sort_index().reset_index()
        freq.columns = ["Rating", "Reviews"]
        fig = px.bar(freq, x="Rating", y="Reviews", title="Individual official review ratings",
                     color_discrete_sequence=[COLORS["teal"]])
        st.plotly_chart(style_chart(fig, xtitle="Rating (1–4)", ytitle="Review count"), use_container_width=True)
    with rb:
        freq = rv["confidence"].dropna().value_counts().sort_index().reset_index()
        freq.columns = ["Confidence", "Reviews"]
        fig = px.bar(freq, x="Confidence", y="Reviews", title="Individual reviewer confidence",
                     color_discrete_sequence=[COLORS["purple"]])
        st.plotly_chart(style_chart(fig, xtitle="Confidence", ytitle="Review count"), use_container_width=True)
    pair = rv.dropna(subset=["rating", "confidence"])
    if not pair.empty:
        cross = pair.pivot_table(index="confidence", columns="rating", values="review_id", aggfunc="count", fill_value=0)
        fig = px.imshow(cross, text_auto=True, aspect="auto", color_continuous_scale="Tealgrn",
                        labels=dict(x="Rating", y="Confidence", color="Reviews"), title="Rating × confidence review counts")
        st.plotly_chart(style_chart(fig, height=420), use_container_width=True)
    st.download_button("↓ Download review-level scores (CSV)", rv.to_csv(index=False).encode("utf-8-sig"),
                       file_name="iclr2027_official_review_scores.csv", mime="text/csv")
    st.caption("Confidence is shown as reported by each reviewer; it is not an acceptance probability.")

with tab_paper:
    st.subheader("Paper inspector")
    st.write("Find a paper by its title, numeric submission number, or OpenReview forum ID.")
    q = st.text_input("Paper search", value=search_text, key="paper_search", placeholder="Search title, paper number, forum ID…")
    pool = all_scored
    if q.strip():
        query = q.strip().lower()
        pool = pool[
            pool["title"].str.lower().str.contains(query, regex=False, na=False)
            | pool["paper_id"].str.lower().str.contains(query, regex=False, na=False)
            | pool["number"].fillna("").astype(str).str.contains(query, regex=False)
        ]
    pool = pool.sort_values(["n_ratings", "title"], ascending=[False, True]).head(120)
    if pool.empty:
        st.info("No matching papers.")
    else:
        by_id = {row["paper_id"]: row for _, row in pool.iterrows()}
        choice = st.selectbox("Choose paper", list(by_id), format_func=lambda p: f"#{by_id[p]['number'] or '—'} · {by_id[p]['title']}")
        paper = by_id[choice]
        st.markdown(f"**{paper['title']}**")
        st.markdown(f"[Open on OpenReview]({paper['url']}) · Status: {paper['status']} · Area: {paper['area']}")
        paper_metrics = st.columns(4)
        paper_metrics[0].metric("Mean rating", f"{paper['mean_rating']:.3f}" if pd.notna(paper["mean_rating"]) else "N/A")
        paper_metrics[1].metric("Confidence-weighted", f"{paper['weighted_rating']:.3f}" if pd.notna(paper["weighted_rating"]) else "N/A")
        paper_metrics[2].metric("Mean confidence", f"{paper['mean_confidence']:.2f}" if pd.notna(paper["mean_confidence"]) else "N/A")
        paper_metrics[3].metric("Rated reviews", f"{paper['n_ratings']}")
        this_reviews = reviews[reviews["paper_id"] == choice].copy()
        if this_reviews.empty:
            st.info("No publicly accessible official reviews found for this submission.")
        else:
            this_reviews["OpenReview review"] = "https://openreview.net/forum?id=" + choice + "&noteId=" + this_reviews["review_id"]
            st.dataframe(this_reviews[["reviewer", "rating", "confidence", "OpenReview review"]],
                         column_config={"OpenReview review": st.column_config.LinkColumn(display_text="View review")},
                         hide_index=True, use_container_width=True)

st.markdown("---")
st.caption("Data: public official reviews from OpenReview. Rankings are descriptive and unofficial; decisions are not determined by a mechanical rating cutoff. Review edits may change averages. Statistics reflect the timestamped local snapshot, not a real-time OpenReview connection.")
