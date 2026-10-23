"""Вопрос → параметры → инструмент (SQL к витринам) → текст ответа."""
from .db import DB
from .i18n import t
from .render import render
from .router import route
from .tools import TOOLS


class Agent:
    def __init__(self, db=None):
        self.db = db or DB()
        rows = self.db.query("SELECT bin, name_ru FROM core.subject WHERE is_target")
        self.subjects = {r["bin"]: r["name_ru"] for r in rows}
        self.freshness = self._freshness()
        self.db.log.clear()

    def _freshness(self):
        last = self.db.one("""SELECT finished_at, now() - finished_at > interval '24 hours' AS stale
                              FROM etl.load_run WHERE status = 'ok' ORDER BY finished_at DESC LIMIT 1""")
        src = self.db.one("SELECT max(index_date) AS d FROM core.contract WHERE is_target")
        if not last:
            return None
        # finished_at в UTC, indexDate из API уже в местном времени
        return {"load": f"{last['finished_at'].astimezone():%d.%m.%Y %H:%M}",
                "src": f"{src['d']:%d.%m.%Y %H:%M}" if src.get("d") else "—",
                "stale": last["stale"]}

    def ask(self, question, with_sql=True):
        self.db.log.clear()
        p = route(question, self.subjects)
        result = TOOLS[p.qtype](self.db, p, self.subjects)
        # слова товара сузили выборку до нуля — не отвечаем «0» с высокой уверенностью
        if p.item_terms and not p.enstru and not result.n_analyzed and result.confidence != "none":
            result.confidence = "low"
            result.limitations.insert(0, t("terms_empty", p.lang, terms=", ".join(p.item_terms)))
        text = render(result, p, list(self.db.log) if with_sql else None, self.freshness)
        return {"params": p, "result": result, "text": text, "sql": list(self.db.log)}
