"""python -m agent "вопрос" | python -m agent (интерактивно) | --no-sql"""
import sys

from .core import Agent


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if a != "--no-sql"]
    with_sql = "--no-sql" not in sys.argv
    agent = Agent()
    if args:
        print(agent.ask(" ".join(args), with_sql)["text"])
        return
    print("Вопрос (пустая строка — выход):")
    while True:
        try:
            q = input("> ").strip()
        except EOFError:
            break
        if not q:
            break
        print(agent.ask(q, with_sql)["text"], "\n")


if __name__ == "__main__":
    main()
