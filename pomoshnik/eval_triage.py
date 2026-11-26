"""Точность разбора почты по эталону expected_action из inbox.csv.

--mode rules  категория из эталона: проверяем правила профиля (бесплатно)
--mode llm    категорию определяет модель: проверяем всю цепочку (платно)
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .profile import load_profile
from .triage import classify, decide

ROOT = Path(__file__).resolve().parent.parent
GUARDED = {"деньги", "кадры", "обязательства_клиентам", "руководство"}


def evaluate(profile, rows, llm=None) -> dict:
    def one(r):
        if llm is None:
            d = decide(profile, r["category"], r["from_email"])
        else:
            cat, summ, conf = classify(llm, r["subject"], r["body"], r["from_name"])
            d = decide(profile, cat, r["from_email"], summary=summ, confidence=conf)
        return r, d

    with ThreadPoolExecutor(max_workers=8 if llm else 1) as ex:
        results = list(ex.map(one, rows))
    ok = sum(1 for r, d in results if d.action == r["expected_action"])
    leaks = [r["id"] for r, d in results if r["category"] in GUARDED and d.action != "спросить_руководителя"]
    no_leader = sum(1 for _, d in results if d.action != "спросить_руководителя")
    confusion = collections.Counter((r["expected_action"], d.action) for r, d in results if d.action != r["expected_action"])
    return {"total": len(rows), "correct": ok, "accuracy": ok / len(rows), "guarded_leaks": leaks,
            "share_without_leader": no_leader / len(rows),
            "errors": [{"id": r["id"], "expected": r["expected_action"], "got": d.action, "category": d.category,
                        "reason": d.reason} for r, d in results if d.action != r["expected_action"]],
            "confusion": {f"{a} → {b}": n for (a, b), n in confusion.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rules", "llm"], default="rules")
    ap.add_argument("--profile", default=str(ROOT / "profiles" / "ivan_petrov"))
    ap.add_argument("--inbox", default=str(ROOT / "data" / "task" / "inbox.csv"))
    a = ap.parse_args()
    llm = None
    if a.mode == "llm":
        from .llm import from_env, load_dotenv
        load_dotenv(str(ROOT / ".env"))
        spent = []
        llm = from_env(on_usage=spent.append)
    profile = load_profile(a.profile, org_path=ROOT / "data" / "task" / "org_structure.csv")
    rows = list(csv.DictReader(open(a.inbox, encoding="utf-8")))
    res = evaluate(profile, rows, llm)
    if llm:
        res["llm_cost_usd"] = round(sum(u.cost_usd for u in spent), 5)
        res["llm_calls"] = len(spent)
        res["avg_ms"] = int(sum(u.duration_ms for u in spent) / max(len(spent), 1))
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
