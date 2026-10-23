"""Isolation Forest по позициям с Fair Price; risk_level объединяет ML с правилами и статистикой.

Запуск: python ml_anomaly.py   (load.py вызывает run() сам)
"""
import json
import warnings
import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

MODEL_VERSION = "iforest-v1"
CONTAMINATION = 0.02
RANDOM_STATE = 42

QUERY = """
WITH supplier_share AS (
    SELECT customer_bin, fin_year, supplier_bin,
           sum(contract_sum) / nullif(sum(sum(contract_sum)) OVER (PARTITION BY customer_bin, fin_year), 0) AS share
    FROM core.contract
    WHERE is_target AND NOT coalesce(is_deleted, false)
    GROUP BY 1, 2, 3
)
SELECT f.unit_id, f.p_actual, f.p_fair, f.robust_z, f.quantity_base, f.n, f.rel_iqr,
       f.price_verdict,
       coalesce(tm.name_ru ~* 'одного источника', false) AS single_source,
       coalesce(ss.share, 0)                            AS supplier_share,
       q.growth                                         AS qty_growth,
       q.verdict                                        AS qty_verdict
FROM mart.fair_price f
JOIN core.contract c          ON c.id = f.contract_id
LEFT JOIN ref.trade_method tm ON tm.id = c.trade_method_id
LEFT JOIN supplier_share ss   ON ss.customer_bin = f.customer_bin AND ss.fin_year = f.fin_year
                             AND ss.supplier_bin = f.supplier_bin
LEFT JOIN mart.qty_anomaly q  ON q.customer_bin = f.customer_bin AND q.enstru_code = f.enstru_code
                             AND q.base_unit_code IS NOT DISTINCT FROM f.base_unit_code AND q.year = f.fin_year
WHERE f.tier IS NOT NULL
  AND f.price_verdict <> 'data_error_suspect'
"""

FEATURES = {
    "log_price_dev": "отклонение цены от Fair Price (лог)",
    "robust_z": "z-оценка цены по MAD",
    "log_amount": "сумма позиции (лог)",
    "single_source": "закупка из одного источника",
    "supplier_share": "доля поставщика в закупках заказчика за год",
    "log_qty_growth": "рост планового количества к прошлым годам (лог)",
}
# разброс цен аналогов и размер выборки не берём: это свойства группы сравнения, а не закупки


def build_features(df):
    x = pd.DataFrame(index=df.index)
    x["log_price_dev"] = np.log(df["p_actual"] / df["p_fair"])
    x["robust_z"] = df["robust_z"].fillna(0).clip(-20, 20)
    x["log_amount"] = np.log1p(df["p_actual"] * df["quantity_base"])
    x["single_source"] = df["single_source"].astype(float)
    x["supplier_share"] = df["supplier_share"].astype(float)
    x["log_qty_growth"] = np.log(df["qty_growth"].astype(float)).fillna(0).clip(-5, 5)
    return x.replace([np.inf, -np.inf], 0).fillna(0)


def risk(row):
    reasons = []
    if row.price_verdict == "overpriced":
        reasons.append("цена: отклонение > 30% и статистический выброс (MAD/IQR)")
    if row.qty_verdict == "anomaly":
        reasons.append("объём: рост >= 2 раз, выбивается из тренда других заказчиков")
    if row.ml_risky:
        reasons.append("ML: необычное сочетание признаков в сторону переплаты (Isolation Forest)")
    rule_or_stat = row.price_verdict == "overpriced" or row.qty_verdict == "anomaly"
    if rule_or_stat and row.ml_risky:
        level = "high"
    elif rule_or_stat or row.ml_risky:
        level = "medium"
    else:
        level = "low"
    return level, reasons


def run(conn):
    with warnings.catch_warnings():   # pandas ругается на psycopg2-соединение, но читает корректно
        warnings.simplefilter("ignore", UserWarning)
        df = pd.read_sql(QUERY, conn)
    if len(df) < 100:
        print("ML: слишком мало позиций для обучения:", len(df))
        return
    x = build_features(df)
    scaled = RobustScaler().fit_transform(x)

    model = IsolationForest(n_estimators=300, contamination=CONTAMINATION,
                            random_state=RANDOM_STATE, n_jobs=-1)
    model.fit(scaled)
    df["if_score"] = -model.score_samples(scaled)
    df["if_percentile"] = df["if_score"].rank(pct=True) * 100
    df["ml_flag"] = model.predict(scaled) == -1
    # в риск идёт только направление переплаты: дешёвые и уменьшившиеся закупки тоже «необычны»
    df["ml_risky"] = df["ml_flag"] & (
        (x["log_price_dev"] > 0)
        | (x["log_qty_growth"] > 0)
        | (x["supplier_share"] >= x["supplier_share"].quantile(0.95))
    )

    # объяснение — три признака с самым крайним процентилем
    names = list(x.columns)
    pct = x.rank(pct=True).to_numpy()
    extremeness = np.abs(pct - 0.5)
    top_idx = np.argsort(-extremeness, axis=1)[:, :3]

    rows = []
    for i, r in enumerate(df.itertuples(index=False)):
        level, reasons = risk(r)
        top = [f"{FEATURES[names[j]]}: {x.iloc[i, j]:+.2f} (процентиль {pct[i, j] * 100:.0f})"
               for j in top_idx[i]]
        feats = {k: round(float(x.iloc[i][k]), 4) for k in names}
        rows.append((int(r.unit_id), float(r.if_score), float(r.if_percentile), bool(r.ml_flag),
                     json.dumps(feats), top, r.price_verdict, r.qty_verdict, level, reasons, MODEL_VERSION))

    cur = conn.cursor()
    cur.execute("TRUNCATE mart.ml_anomaly")
    execute_values(cur, """
        INSERT INTO mart.ml_anomaly (unit_id, if_score, if_percentile, ml_flag, features, top_factors,
                                     price_verdict, qty_verdict, risk_level, risk_reasons, model_version)
        VALUES %s""", rows, template="(%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)", page_size=2000)
    conn.commit()

    counts = pd.Series([r[8] for r in rows]).value_counts().to_dict()
    print(f"ML: позиций {len(rows)}, ml_flag {int(df['ml_flag'].sum())}, риск {counts}")


if __name__ == "__main__":
    from load import DSN
    with psycopg2.connect(**DSN) as conn:
        run(conn)
