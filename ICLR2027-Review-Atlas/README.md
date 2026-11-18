# ICLR 2027 Review Atlas

A local Streamlit dashboard for **all publicly accessible ICLR 2027 submissions and their public official review ratings/confidence**. It scans the conference's OpenReview API v2 and calculates a separate score for each paper.

## Run

Install Python 3.10+ and run from the extracted folder:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Then click **Fetch / refresh from OpenReview** in the left sidebar. The first retrieval may involve **tens of thousands of papers** and takes multiple API pages. The dashboard opens locally in your browser; Internet access is required only to fetch new data.

### Data and cache

- Public API: `https://api2.openreview.net`
- Venue: `ICLR.cc/2027/Conference`
- Submission invitation is discovered from the venue group (`submission_name`).
- For each submission, fetch `details=directReplies` and retain only `/-/Official_Review` (name obtained from group if configured).
- Ignore non-public reviews (`readers` without `everyone`). No rebuttals, meta-reviews, author responses, or reviewer-quality `/-/Rating` notes are included.
- Follow API pagination using `after` cursors. Retry transient failures and respect HTTP 429 Retry-After.
- Store a work-in-progress database for **resuming failed imports**. Only replace the complete database after the *last page* succeeds. The dashboard never pretends a partial scrape is complete.
- Local SQLite cache is in `data/` and does not auto-update. Press Fetch again to refresh; select **Start a fresh retrieval** to restart if needed.
- `Active` is the default: exclude withdrawn, desk-rejected, and rejected papers, using OpenReview group status IDs and venue text. Select **Include withdrawn / rejected submissions** to override.

**Note:** OpenReview sometimes applies bot challenges to API requests. A 403 error is reported honestly; it is not a signal that zero papers have reviews. You can retry from your machine or import a local review-level CSV. This package contains **no fabricated conference statistics**.

## Dashboard

1. **Overview:** paper-mean distribution, rating bands, mean confidence, rated-review counts, confidence-vs-rating map.
2. **Compare my score:** enter any average on **ICLR 2027's 1–4 scale**. Get the number of papers with strictly higher, tied, and strictly lower average, shared rank (`higher + 1`), and midrank percentile (`(lower + 0.5 * ties) / total`).
3. **Rankings:** sortable/searchable paper table and CSV export with title, number, research area, mean, confidence-weighted mean, reviewer count, min/max, standard deviation, and direct OpenReview links.
4. **Reviewer statistics:** score counts, confidence counts and rating × confidence heatmap, with CSV export.
5. **Paper inspector:** individual review ratings/confidence for any title, number or forum ID.

### Calculation details

- **Arithmetic mean (default):** sum of official review ratings divided by the number of scored official reviews. Ratings missing a numeric value do not affect the denominator.
- **Confidence-weighted mean (optional):** sum of `rating_i × confidence_i` divided by sum of confidence values, including only reviews with **both** numeric values and positive confidence. Papers without a valid weighted rating are excluded in weighted mode.
- **Ties:** strict mathematical comparison with a `1e-8` tolerance for floating-point rounding. Tied scores share rank. For comparisons, a hypothetical paper is **not** added to the conference count.
- **Cohort:** excludes inactive statuses by default and supports research area and minimum rated-review filtering. Search text affects the table display only, not the statistics. All comparisons are **paper-level**, not review-level.
- Review ratings and confidence can be updated over time during the rebuttal; the snapshot timestamp is displayed.

### CSV import fallback

The sidebar supports uploading a CSV with one row per official review. Required headers are:

```
paper_id,title,rating,confidence
```

Recommended optional headers: `review_id,number,area,status,reviewer,url`. Example:

```
paper_id,title,rating,confidence,number,area,status,reviewer
fake123,Illustration Only,3,4,12,Representation Learning,Active,Reviewer_1
fake123,Illustration Only,2,3,12,Representation Learning,Active,Reviewer_2
```

This CSV is **illustrative only**, and is not installed as real conference data.

### Tests

```bash
python -m unittest discover -s tests -v
```

## Provenance and cautions

This is an independent, unofficial academic research tool. Source: [OpenReview API documentation](https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc). Do not interpret a paper's rank as an acceptance decision or official conference prediction. Respect OpenReview's rate limits and terms of use.

**Convenient launchers:** on macOS, run `run_mac.command` (Terminal may first require `chmod +x run_mac.command`); on Windows, double-click `run_windows.bat`. These create a local `.venv`, install dependencies, and start Streamlit.
