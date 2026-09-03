"""
Retail Marketplace Performance & Retention Analysis -- Olist Dataset
=======================================================================
SQL (DuckDB, queries CSVs directly, no server) for the relational joins,
Python (pandas/scipy) for the statistical work. Validated against synthetic
data shaped like the real Olist schema before being handed off.
"""

import pandas as pd
import duckdb
from scipy.stats import chi2_contingency, fisher_exact

DATA_DIR = "data/raw"


def load_views(con):
    for name in [
        "customers",
        "orders",
        "order_items",
        "order_reviews",
        "products",
        "sellers",
    ]:
        con.execute(
            f"CREATE VIEW {name} AS SELECT * FROM read_csv_auto('{DATA_DIR}/olist_{name}_dataset.csv')"
        )
    con.execute(
        f"CREATE VIEW category_translation AS "
        f"SELECT * FROM read_csv_auto('{DATA_DIR}/product_category_name_translation.csv')"
    )


def schema_check(con):
    print("=== Row counts (sanity check) ===")
    tables = [
        "customers",
        "orders",
        "order_items",
        "order_reviews",
        "products",
        "sellers",
        "category_translation",
    ]
    for name in tables:
        n = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  {name}: {n:,}")
    print()


def repeat_purchase_rate(con):
    """The North Star Metric. Delivered orders only -- cancelled/unavailable
    orders aren't real purchases and would inflate the denominator."""
    print("=== North Star Metric: Repeat Purchase Rate ===")
    result = con.execute("""
        WITH customer_orders AS (
            SELECT c.customer_unique_id, COUNT(DISTINCT o.order_id) AS n_orders
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_status = 'delivered'
            GROUP BY c.customer_unique_id
        )
        SELECT
            COUNT(*) AS total_customers,
            SUM(CASE WHEN n_orders > 1 THEN 1 ELSE 0 END) AS repeat_customers,
            ROUND(100.0 * SUM(CASE WHEN n_orders > 1 THEN 1 ELSE 0 END) / COUNT(*), 2) AS repeat_rate_pct
        FROM customer_orders
    """).fetchdf()
    print(result.to_string(index=False))
    print()
    return result


def q1_monthly_gmv_new_vs_repeat(con):
    print("=== Q1: Monthly GMV, new vs. repeat customers ===")
    result = con.execute("""
        WITH order_rank AS (
            SELECT
                o.order_id, c.customer_unique_id, o.order_purchase_timestamp,
                ROW_NUMBER() OVER (PARTITION BY c.customer_unique_id ORDER BY o.order_purchase_timestamp) AS order_seq
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_status = 'delivered'
        ),
        order_revenue AS (
            SELECT order_id, SUM(price + freight_value) AS order_value
            FROM order_items GROUP BY order_id
        )
        SELECT
            strftime(order_rank.order_purchase_timestamp::TIMESTAMP, '%Y-%m') AS month,
            CASE WHEN order_seq = 1 THEN 'new' ELSE 'repeat' END AS customer_type,
            ROUND(SUM(order_revenue.order_value), 2) AS gmv,
            COUNT(*) AS n_orders
        FROM order_rank
        JOIN order_revenue ON order_rank.order_id = order_revenue.order_id
        GROUP BY month, customer_type
        ORDER BY month, customer_type
    """).fetchdf()
    result.to_csv("data/q1_monthly_gmv_new_vs_repeat.csv", index=False)
    print(f"  {len(result)} rows -> data/q1_monthly_gmv_new_vs_repeat.csv")
    print()
    return result


def q2_delivery_vs_retention(con):
    print("=== Q2: Delivery performance vs. review score vs. retention ===")

    # Customer-level table: on_time flag + review score for FIRST order, plus became_repeat flag
    df = con.execute("""
        WITH first_orders AS (
            SELECT
                c.customer_unique_id, o.order_id,
                o.order_delivered_customer_date::TIMESTAMP <= o.order_estimated_delivery_date::TIMESTAMP AS on_time,
                ROW_NUMBER() OVER (PARTITION BY c.customer_unique_id ORDER BY o.order_purchase_timestamp) AS order_seq
            FROM orders o JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_status = 'delivered'
        ),
        totals AS (
            SELECT c.customer_unique_id, COUNT(DISTINCT o.order_id) AS n_orders
            FROM orders o JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_status = 'delivered'
            GROUP BY c.customer_unique_id
        )
        SELECT
            f.customer_unique_id, f.on_time, r.review_score,
            (t.n_orders > 1) AS became_repeat
        FROM first_orders f
        JOIN totals t ON f.customer_unique_id = t.customer_unique_id
        LEFT JOIN order_reviews r ON f.order_id = r.order_id
        WHERE f.order_seq = 1
    """).fetchdf()

    # Summary table: repeat rate + avg review score by on-time bucket
    summary = (
        df.groupby("on_time")
        .agg(
            customers=("customer_unique_id", "count"),
            repeat_rate_pct=("became_repeat", lambda x: round(100 * x.mean(), 2)),
            avg_review_score=("review_score", "mean"),
        )
        .reset_index()
    )
    print(summary.to_string(index=False))

    # Statistical test on the on_time x became_repeat relationship
    ct = pd.crosstab(df["on_time"], df["became_repeat"])
    chi2, p_chi2, dof, expected = chi2_contingency(ct)
    if (expected < 5).any():
        odds_ratio, p_value = fisher_exact(ct)
        print(
            f"\n  Expected cell count < 5 -> chi-square not valid, used Fisher's exact test instead."
        )
        print(f"  Fisher's exact: odds_ratio={odds_ratio:.3f}, p={p_value:.4f}")
    else:
        print(f"\n  Chi-square test: chi2={chi2:.3f}, p={p_chi2:.4f}")

    df.to_csv("data/q2_delivery_vs_retention_customer_level.csv", index=False)
    summary.to_csv("data/q2_delivery_vs_retention_summary.csv", index=False)
    print(
        f"  -> data/q2_delivery_vs_retention_summary.csv, q2_delivery_vs_retention_customer_level.csv"
    )
    print()
    return summary


def q3_category_and_seller_concentration(con):
    print("=== Q3: Revenue/AOV by category ===")
    by_category = con.execute("""
        SELECT
            ct.product_category_name_english AS category,
            COUNT(DISTINCT oi.order_id) AS n_orders,
            ROUND(SUM(oi.price), 2) AS revenue,
            ROUND(AVG(oi.price), 2) AS avg_item_price
        FROM order_items oi
        JOIN products p ON oi.product_id = p.product_id
        JOIN category_translation ct ON p.product_category_name = ct.product_category_name
        GROUP BY category
        ORDER BY revenue DESC
    """).fetchdf()
    by_category.to_csv("data/q3_category_performance.csv", index=False)
    print(f"  {len(by_category)} categories -> data/q3_category_performance.csv")

    print("\n=== Q3: Seller concentration (Pareto) ===")
    seller_conc = con.execute("""
        WITH seller_revenue AS (
            SELECT seller_id, ROUND(SUM(price), 2) AS revenue
            FROM order_items GROUP BY seller_id
        )
        SELECT
            seller_id, revenue,
            ROUND(100.0 * SUM(revenue) OVER (ORDER BY revenue DESC) / SUM(revenue) OVER (), 2) AS cumulative_pct
        FROM seller_revenue
        ORDER BY revenue DESC
    """).fetchdf()
    seller_conc.to_csv("data/q3_seller_concentration.csv", index=False)
    n_sellers_80pct = (seller_conc["cumulative_pct"] <= 80).sum() + 1
    pct_of_sellers = round(100 * n_sellers_80pct / len(seller_conc), 1)
    print(f"  {len(seller_conc)} sellers -> data/q3_seller_concentration.csv")
    print(
        f"  {n_sellers_80pct} sellers ({pct_of_sellers}% of all sellers) generate 80% of revenue"
    )
    print()


def headline_metrics(con):
    """BAN-row numbers, computed directly from the base tables -- not
    re-derived from any of the Q1/Q2/Q3 exports. avg_review_score covers
    ALL reviews on delivered orders, not just customers' first orders
    (that population-scoping distinction matters -- see DASHBOARD_SPEC.md)."""
    print("=== Headline metrics (for dashboard BAN row) ===")

    customers_result = con.execute("""
        WITH customer_orders AS (
            SELECT c.customer_unique_id, COUNT(DISTINCT o.order_id) AS n_orders
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            WHERE o.order_status = 'delivered'
            GROUP BY c.customer_unique_id
        )
        SELECT
            COUNT(*) AS total_customers,
            SUM(CASE WHEN n_orders > 1 THEN 1 ELSE 0 END) AS repeat_customers
        FROM customer_orders
    """).fetchone()
    total_customers, repeat_customers = customers_result
    repeat_rate_pct = round(100.0 * repeat_customers / total_customers, 2)

    revenue_result = con.execute("""
        SELECT COUNT(DISTINCT o.order_id) AS total_orders,
               ROUND(SUM(oi.price + oi.freight_value), 2) AS total_gmv
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.order_id
        WHERE o.order_status = 'delivered'
    """).fetchone()
    total_orders, total_gmv = revenue_result

    avg_review_score = con.execute("""
        SELECT ROUND(AVG(r.review_score), 2)
        FROM order_reviews r
        JOIN orders o ON r.order_id = o.order_id
        WHERE o.order_status = 'delivered'
    """).fetchone()[0]

    result = pd.DataFrame(
        [
            {
                "metric": "total_customers",
                "value": total_customers,
            },
            {
                "metric": "repeat_customers",
                "value": repeat_customers,
            },
            {
                "metric": "repeat_rate_pct",
                "value": repeat_rate_pct,
            },
            {
                "metric": "total_orders",
                "value": total_orders,
            },
            {
                "metric": "total_gmv",
                "value": total_gmv,
            },
            {
                "metric": "avg_review_score",
                "value": avg_review_score,
            },
        ]
    )
    result.to_csv("data/headline_metrics.csv", index=False)
    print(result.to_string(index=False))
    print("  -> data/headline_metrics.csv")
    print()
    return result


def main():
    con = duckdb.connect()
    load_views(con)
    schema_check(con)
    headline_metrics(con)
    repeat_purchase_rate(con)
    q1_monthly_gmv_new_vs_repeat(con)
    q2_delivery_vs_retention(con)
    q3_category_and_seller_concentration(con)
    print("Done.")


if __name__ == "__main__":
    main()
