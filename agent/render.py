"""Сборка ответа в Markdown: шесть разделов + SQL для воспроизведения."""
from datetime import date, datetime

from .i18n import t, verdict, conf
from .tools import money, num, pct

MONEY_KEYS = {"excess", "contract_sum_wnds", "total", "med", "excess_amount_est", "amount"}
PRICE_KEYS = {"price_real", "weighted_avg", "p_actual", "p_fair", "price_base", "median", "wavg", "market_wavg"}
PCT_KEYS = {"dev_pct", "deviation_pct", "vs_market"}
VERDICT_KEYS = {"verdict", "price_verdict"}


def cell(key, v, lang):
    if v is None:
        return "—"
    if key in MONEY_KEYS:
        return money(v)
    if key in PRICE_KEYS:
        return num(v)
    if key in PCT_KEYS:
        return pct(v)
    if key in VERDICT_KEYS:
        return verdict(v, lang)
    if key == "confidence":
        return conf(v, lang)
    if key in ("robust_z", "growth", "peer_median_growth", "base_qty", "plan_qty", "share", "single_source_pct",
               "quantity_base"):
        return num(v, 1)
    if key == "anno_url":
        return f"[объявление]({v})"
    if isinstance(v, (datetime, date)):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, list):
        return ", ".join(map(str, v))
    if isinstance(v, float):
        return num(v)
    s = str(v).replace("|", "/").replace("\n", " ")
    return s if len(s) <= 60 else s[:57] + "…"


def table(rows, columns, lang):
    out = ["| " + " | ".join(h for _, h in columns) + " |", "|" + "---|" * len(columns)]
    out += ["| " + " | ".join(cell(key, row.get(key), lang) for key, _ in columns) + " |" for row in rows]
    return out


def render(r, p, sql_log=None, freshness=None):
    lang = p.lang
    out = []
    out.append(f"**{t('qtype', lang)}:** {t('qt_' + r.qtype, lang)}"
               + (" _(определён LLM)_" if p.type_source == "llm" else ""))

    out.append(f"\n### {t('h_summary', lang)}")
    out.append(" ".join(r.summary))

    out.append(f"\n### {t('h_data', lang)}")
    out += [f"- {x}" for x in r.data_used]
    if r.n_analyzed is not None:
        out.append(f"- {t('n_analyzed', lang)}: {r.n_analyzed}")

    out.append(f"\n### {t('h_comparison', lang)}")
    out += [f"- {x}" for x in r.comparison] or ([] if r.groups else ["- —"])
    if r.n_compared is not None:
        out.append(f"- {t('n_compared', lang)}: {r.n_compared}")
    if r.groups and r.group_columns:
        out.append("")
        out += table(r.groups, r.group_columns, lang)

    out.append(f"\n### {t('h_metric', lang)}")
    out += [f"- {x}" for x in r.metric] or ["- —"]

    out.append(f"\n### {t('h_limits', lang)}")
    why = f" — {r.confidence_why}" if r.confidence_why else ""
    out.append(f"- **{t('confidence', lang)}: {conf(r.confidence, lang)}**{why}")
    out += [f"- {x}" for x in r.data_quality]
    if freshness:
        out.append(f"- {t('freshness', lang, load=freshness['load'], src=freshness['src'])}")
        if freshness["stale"]:
            out.append(f"- **{t('stale', lang)}**")
    out += [f"- {x}" for x in r.limitations]

    k = len(r.examples)
    out.append(f"\n### {t('h_examples', lang, k=k)}")
    if r.examples and r.columns:
        out += table(r.examples, r.columns, lang)
        no_link = sum(1 for row in r.examples if "anno_url" in row and not row["anno_url"])
        if no_link and any(key == "anno_url" for key, _ in r.columns):
            out.append("\n" + t("no_anno", lang, n=no_link, k=k))
        if r.n_examples_total and r.n_examples_total > k:
            out.append("\n" + t("more_rows", lang, k=k, n=r.n_examples_total))
    else:
        out.append(t("no_examples", lang))

    if sql_log:
        out.append(f"\n<details><summary>{t('h_repro', lang)}</summary>\n")
        out += [f"```sql\n{q.strip()}\n```" for q in sql_log]
        out.append("</details>")
    return "\n".join(out)
