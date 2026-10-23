"""Необязательная LLM (Ollama или OpenAI-совместимый сервер): разбор вопроса, без расчётов.

Настройка: LLM_BASE_URL, LLM_MODEL (например qwen3:4b-instruct-2507-q4_K_M).
"""
import functools
import os
import re

import requests

TYPES = ["search", "compare", "analytics", "anomaly", "fairness", "volume"]

CLASSIFY_PROMPT = """Classify a question about public procurement in Kazakhstan into exactly one type:
search    — find/list procurements, contracts, lots
compare   — compare customers, years, items or prices
analytics — totals, shares, structure, dynamics, rankings
anomaly   — price deviations, overpricing, outliers
fairness  — is the price of a specific lot/contract fair vs similar contracts
volume    — unusual increase of purchased quantity vs previous years
Answer with one word only."""

REWRITE_PROMPT = """Ты помогаешь искать коды в классификаторе ЕНС ТРУ (госзакупки Казахстана).
На входе — название товара, работы или услуги на русском или казахском, возможно разговорное.
Верни ОДНО название на русском в стиле справочника ЕНС ТРУ:
- именительный падеж, единственное число, главное слово первым;
- услуги — в форме «Услуги по …» или «Услуги …», работы — «Работы по …»;
- без брендов, количества, года и лишних слов.
Только название, без пояснений.

Примеры:
канцелярские скрепки → Скрепка канцелярская
ремонт крыши → Работы по ремонту кровли
аренда автобуса с водителем → Услуги по аренде автобуса с водителем
қағаз сүлгі → Полотенце бумажное
тауық еті → Мясо кур
өрт сөндіргіштер → Огнетушитель"""


@functools.lru_cache(maxsize=1)
def enabled():
    """Сервер LLM настроен и отвечает (проверяется один раз за процесс)."""
    base = os.getenv("LLM_BASE_URL")
    if not base:
        return False
    try:
        url = f"{base.rstrip('/').removesuffix('/v1')}/api/tags" if os.getenv("LLM_API", "ollama") == "ollama" \
            else f"{base.rstrip('/')}/models"
        return requests.get(url, timeout=3).status_code == 200
    except requests.RequestException:
        return False


# Пустой <think> в начале ответа — так шаблон Qwen3 отключает рассуждения (enable_thinking=False)
QWEN3_RAW = ("<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
             "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def _chat(system, user, max_tokens=60):
    base = os.getenv("LLM_BASE_URL")
    if not base or not enabled():
        return None
    model = os.getenv("LLM_MODEL", "qwen3:4b")
    try:
        if os.getenv("LLM_API", "ollama") == "ollama" and "qwen3" in model:
            r = requests.post(f"{base.rstrip('/').removesuffix('/v1')}/api/generate", timeout=60, json={
                "model": model, "raw": True, "stream": False,
                "prompt": QWEN3_RAW.format(system=system, user=user),
                "options": {"temperature": 0, "num_predict": max_tokens, "stop": ["<|im_end|>"]},
            })
            text = r.json()["response"]
        else:
            r = requests.post(f"{base.rstrip('/')}/chat/completions", timeout=60, json={
                "model": model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            text = r.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, ValueError):
        return None
    return re.sub(r"(?s)<think>.*?</think>", "", text).strip()


SELECT_PROMPT = """Ты подбираешь позиции справочника ЕНС ТРУ (госзакупки Казахстана) под запрос пользователя.
Запрос может быть на русском или казахском. Выбери из списка ТОЛЬКО позиции, которые являются тем же
товаром, работой или услугой, что и запрос (синонимы и уточнения подходят; смежные, но другие — нет).
Ответ — номера через запятую, например: 2, 5. Если ничего не подходит — 0. Без пояснений."""


@functools.lru_cache(maxsize=2048)
def select_items(phrase, names):
    """Выбранные названия из names; () — ничего не подошло, None — ответ не разобран."""
    if not phrase or not names:
        return None
    listing = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    text = _chat(SELECT_PROMPT, f"Запрос: {phrase}\nСписок:\n{listing}", max_tokens=40)
    if not text:
        return None
    nums = [int(x) for x in re.findall(r"\d+", text.splitlines()[0])]
    if nums == [0]:
        return ()
    picked = tuple(names[i - 1] for i in nums if 1 <= i <= len(names))
    return picked or None


def classify(question):
    text = _chat(CLASSIFY_PROMPT, question, max_tokens=10)
    if not text:
        return None
    return next((t for t in TYPES if t in text.lower()), None)


@functools.lru_cache(maxsize=2048)
def rewrite_item(phrase):
    """Название в стиле ЕНС ТРУ или None."""
    if not phrase:
        return None
    text = _chat(REWRITE_PROMPT, phrase, max_tokens=30)
    if not text:
        return None
    line = text.splitlines()[0].strip(" «»\"'.→-")
    if not 2 <= len(line) <= 80 or not re.search("[А-Яа-яЁё]", line):
        return None
    return line
