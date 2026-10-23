"""Быстрая проверка агента: python question_test.py ["вопрос" ...] — без аргументов берёт QUESTIONS."""
import sys

from agent.core import Agent

QUESTIONS = [
    "Какие организации закупили наибольшее количество дизельного топлива в 2025 году?",
    "2024–2026 жылдар аралығында компьютер сатып алу көлемі қалай өзгерді?",
]

SHOW_SQL = False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    agent = Agent()
    for q in sys.argv[1:] or QUESTIONS:
        res = agent.ask(q, with_sql=SHOW_SQL)
        print("=" * 100)
        print("ВОПРОС:", q)
        print("=" * 100)
        print(res["text"], "\n")
