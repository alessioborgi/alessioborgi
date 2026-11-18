"""Pure statistics helpers, deliberately independent of Streamlit/network calls."""
from __future__ import annotations

import numpy as np
import pandas as pd


def score_papers(papers: pd.DataFrame, reviews: pd.DataFrame) -> pd.DataFrame:
    """One row per paper; official reviewer scores only. Weighted mean uses rated+confident reviews."""
    result = papers.copy()
    if result.empty:
        return result
    if reviews.empty:
        for name in ["mean_rating", "weighted_rating", "mean_confidence", "rating_std",
                     "min_rating", "max_rating"]:
            result[name] = np.nan
        result["n_reviews"] = 0
        result["n_ratings"] = 0
        return result
    rv = reviews.copy()
    rv["rating"] = pd.to_numeric(rv["rating"], errors="coerce")
    rv["confidence"] = pd.to_numeric(rv["confidence"], errors="coerce")
    rv["weighted_product"] = rv["rating"] * rv["confidence"].where(rv["confidence"] > 0)
    rv["confidence_for_weight"] = rv["confidence"].where(rv["rating"].notna() & (rv["confidence"] > 0))
    grouped = rv.groupby("paper_id", as_index=False).agg(
        n_reviews=("review_id", "size"),
        n_ratings=("rating", "count"),
        mean_rating=("rating", "mean"),
        mean_confidence=("confidence", "mean"),
        rating_std=("rating", "std"),
        min_rating=("rating", "min"),
        max_rating=("rating", "max"),
        weighted_sum=("weighted_product", "sum"),
        total_weight=("confidence_for_weight", "sum"),
    )
    grouped["weighted_rating"] = grouped["weighted_sum"] / grouped["total_weight"].replace(0, np.nan)
    grouped = grouped.drop(columns=["weighted_sum", "total_weight"])
    result = result.merge(grouped, how="left", on="paper_id", validate="one_to_one")
    result[["n_reviews", "n_ratings"]] = result[["n_reviews", "n_ratings"]].fillna(0).astype(int)
    return result


def compare_to_average(scores: pd.Series, target: float, tolerance: float = 1e-8) -> dict:
    """Ahead/tied/below based on *paper-level* scores and strict comparisons."""
    values = pd.to_numeric(scores, errors="coerce").dropna().to_numpy(dtype=float)
    n = len(values)
    if not n:
        return {"n": 0, "ahead": 0, "tied": 0, "below": 0, "rank": None, "percentile": None}
    diff = values - target
    ahead = int((diff > tolerance).sum())
    tied = int((np.abs(diff) <= tolerance).sum())
    below = n - ahead - tied
    return {
        "n": n,
        "ahead": ahead,
        "tied": tied,
        "below": below,
        "rank": ahead + 1,
        "percentile": 100.0 * (below + 0.5 * tied) / n,
    }


def rank_papers(frame: pd.DataFrame, metric: str = "mean_rating") -> pd.DataFrame:
    scored = frame[frame[metric].notna()].copy()
    scored = scored.sort_values([metric, "n_ratings", "mean_confidence", "number"],
                                ascending=[False, False, False, True], na_position="last")
    # Equal means have the same competitive rank, regardless of secondary display sorting.
    scored["rank"] = scored[metric].round(8).rank(ascending=False, method="min").astype(int)
    return scored
