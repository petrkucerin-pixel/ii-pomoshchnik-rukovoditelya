"""Замер протокола на настоящей расшифровке: время, стоимость, доля подтверждённых цитат.

python -m pomoshnik.eval_protocol <файл расшифровки> [--model deepseek-flash] [--show]
Текст протокола печатается только с --show: расшифровки заказчика в репозиторий не кладём.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from yadro.clock import TZ_EKB

from .llm import from_env, load_dotenv
from .org import Org
from .protocol import build_protocol, render_markdown

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("--model", default=None)
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    load_dotenv(str(ROOT / ".env"))
    spent = []
    llm = from_env(on_usage=spent.append)
    text = Path(a.transcript).read_text(encoding="utf-8")
    t0 = time.monotonic()
    p = build_protocol(llm, text, title=Path(a.transcript).stem.replace("-transcript", ""),
                       meeting_end=datetime.now(TZ_EKB), org=Org.load(ROOT / "data" / "task" / "org_structure.csv"))
    sec = time.monotonic() - t0
    items = p.tasks + p.decisions
    res = {"transcript_chars": len(text), "seconds": round(sec, 1), "cost_usd": round(sum(u.cost_usd for u in spent), 5),
           "input_tokens": sum(u.input_tokens for u in spent), "output_tokens": sum(u.output_tokens for u in spent),
           "tasks": len(p.tasks), "decisions": len(p.decisions), "open_questions": len(p.open_questions),
           "quotes_ok": sum(1 for x in items if x.quote_ok), "quotes_total": len(items),
           "tasks_default_due": sum(1 for t in p.tasks if t.default_due),
           "tasks_unknown_owner": sum(1 for t in p.tasks if t.owner_id is None)}
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.show:
        print(render_markdown(p))


if __name__ == "__main__":
    main()
