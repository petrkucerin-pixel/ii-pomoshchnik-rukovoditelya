"""Примеры ответов API кабинета (контракт TASK_07 / TASK_08_GPT). Данные — из учебного набора, вымышленные.

Запуск: python tests/fixtures/cabinet/make_fixtures.py — перезаписывает *.json рядом.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
T = "+05:00"

task_overdue = {"id": "t005", "task": "Подписать договор с «Облако Плюс»", "owner": "Сергей Морозов",
                "due": "2026-12-01", "status": "в работе", "overdue": True, "source": "tasks.csv"}
task_soon = {"id": "t007", "task": "Отчёт по e-com за ноябрь", "owner": "Анна Волкова",
             "due": "2026-12-17", "status": "не начата", "overdue": False, "source": "tasks.csv"}
meeting_short = {"uid": "ev001@company.example", "start": f"2026-12-07T10:00:00{T}", "end": f"2026-12-07T10:30:00{T}",
                 "title": "Планёрка отдела", "category": "операционная",
                 "attendees": [{"name": "Ольга Смирнова", "email": "p02@company.example"},
                               {"name": "Сергей Морозов", "email": "p05@company.example"}],
                 "protocol_id": 2, "protocol_status": "черновик"}
meeting_121 = {"uid": "ev014@company.example", "start": f"2026-12-08T15:00:00{T}", "end": f"2026-12-08T15:45:00{T}",
               "title": "1:1 с Ольгой Смирновой", "category": "один_на_один",
               "attendees": [{"name": "Ольга Смирнова", "email": "p02@company.example"}],
               "protocol_id": None, "protocol_status": None}
focus = {"start": f"2026-12-07T13:00:00{T}", "end": f"2026-12-07T15:00:00{T}", "title": "Окно для фокуса"}
metrics = {"letters": 82, "letters_without_leader": 37, "share_without_leader": 0.451, "hours_saved": 3.55,
           "protocol_minutes_to_draft": 0.1, "protocol_minutes_to_approved": None, "leader_edits": 0,
           "llm_cost_usd": 0.0603,
           "cards": {"писем разобрано": 82, "без руководителя": "45 %", "часов освобождено": 3.55,
                     "встреча → протокол, мин": 0.1, "правок руководителя": 0, "расходы на модель, $": 0.0603}}
protocol = {"id": 2, "title": "Планёрка отдела", "status": "черновик", "meeting_end": f"2026-12-07T10:30:00{T}",
            "minutes_to_draft": 0.8, "minutes_to_approved": None, "edits": 0,
            "decisions": [{"text": "Договор с «Облако Плюс» подписываем до 12.12", "quote": "договор подписываем до двенадцатого",
                           "quote_ok": True}],
            "tasks": [{"owner": "Сергей Морозов", "what": "Подписать договор с «Облако Плюс»", "due": f"2026-12-12T18:00:00{T}",
                       "quote": "Сергей, договор с «Облако Плюс» нужно подписать", "quote_ok": True, "flags": []},
                      {"owner": "Ольга", "what": "КП для «ТехноСтрой»", "due": f"2026-12-10T18:00:00{T}",
                       "quote": "Ольга подготовит КП", "quote_ok": True, "flags": ["срок по умолчанию"]}],
            "open_questions": ["Бюджет на пилот не решён"],
            "markdown": "# Планёрка отдела\n\n## Задачи\n- [ ] Сергей Морозов: подписать договор до 12.12"}

F = {
    "today": {
        "day": "2026-12-11", "leader": "Иван Петров",
        "metrics": metrics["cards"],
        "decisions": [{"event_id": 12, "at": f"2026-12-07T08:38:00{T}", "from": "Клиент «Ромашка-Ритейл»",
                       "subject": "Сроки запуска интеграции",
                       "summary": "Клиент просит подтвердить запуск до 25 декабря",
                       "reason": "обязательства клиентам — только с вашим подтверждением"}],
        "risks": ["Риск по цели «план продаж»: прогноз 94 % (Ольга Смирнова)"],
        "meetings": [meeting_short],
        "overdue": [task_overdue], "due_soon": [task_soon],
        "focus": [focus],
        "digest": ["Передано Ольге Смирновой: Запрос КП «ТехноСтрой»"],
    },
    "mail": {
        "groups": [{"key": "counterparty", "title": "Контрагенты"},
                   {"key": "team", "title": "Отделы: участники совещаний"},
                   {"key": "internal", "title": "Отделы: другие"},
                   {"key": "newsletter", "title": "Рассылки"},
                   {"key": "spam", "title": "Спам"}],
        "letters": [
            {"id": "m001", "direction": "in", "at": f"2026-12-07T08:38:00{T}",
             "from_name": "Клиент «Ромашка-Ритейл»", "from_email": "client1@romashka.example",
             "to_name": "Иван Петров", "to_email": "p01@company.example",
             "subject": "Сроки запуска интеграции",
             "body": "Подтвердите, пожалуйста, что запуск будет до 25 декабря — нам важно для отчётности.",
             "category": "обязательства_клиентам", "action": "спросить_руководителя",
             "reason": "обязательства клиентам — только с вашим подтверждением",
             "group": "counterparty", "party": "Ромашка-Ритейл", "party_note": "клиент"},
            {"id": "m022", "direction": "in", "at": f"2026-12-08T14:10:00{T}",
             "from_name": "Сергей Морозов", "from_email": "p05@company.example",
             "to_name": "Иван Петров", "to_email": "p01@company.example",
             "subject": "Статус по партнёру «Облако Плюс»", "body": "Договор на подписи у юристов, ждём до 12.12.",
             "category": "статус", "action": "в_сводку", "reason": "отчёты и статусы — в сводку",
             "group": "team", "party": "Сергей Морозов", "party_note": "Руководитель партнёрского канала"},
            {"id": "out-7", "direction": "out", "at": f"2026-12-11T17:45:00{T}",
             "from_name": "ИИ-помощник Ивана Петрова", "from_email": "p01@company.example",
             "to_name": "Сергей Морозов", "to_email": "p05@company.example",
             "subject": "Вопрос по отчёту: Статус по партнёру «Облако Плюс»",
             "body": "Сергей, по «Облако Плюс» статус не меняется 4 дн. Что мешает и чем помочь?",
             "category": "вопрос_ревизора", "action": "отправлено", "reason": "шаблон «Вопрос ревизора»",
             "group": "team", "party": "Сергей Морозов", "party_note": "Руководитель партнёрского канала"},
            {"id": "m045", "direction": "in", "at": f"2026-12-09T11:00:00{T}",
             "from_name": "Виктор Орлов", "from_email": "p09@company.example",
             "to_name": "Иван Петров", "to_email": "p01@company.example",
             "subject": "Итоги встречи с партнёром", "body": "Как прошла встреча с «Облако Плюс»? Коротко в двух строках.",
             "category": "руководство", "action": "спросить_руководителя", "reason": "письма руководству — только с подтверждением",
             "group": "internal", "party": "Виктор Орлов", "party_note": "руководитель руководителя"},
            {"id": "m050", "direction": "in", "at": f"2026-12-09T12:00:00{T}",
             "from_name": "CRM Digest", "from_email": "digest@crm.example", "to_name": "Иван Петров",
             "to_email": "p01@company.example", "subject": "Новости CRM за неделю", "body": "Пять новых функций…",
             "category": "рассылка", "action": "игнорировать", "reason": "рассылки — без ответа",
             "group": "newsletter", "party": "CRM Digest", "party_note": "рассылка"},
            {"id": "s001", "direction": "in", "at": f"2026-12-09T03:12:00{T}",
             "from_name": "Выигрыш", "from_email": "prize@lucky.example", "to_name": "Иван Петров",
             "to_email": "p01@company.example", "subject": "Вы выиграли iPhone", "body": "Перейдите по ссылке…",
             "category": "спам", "action": "игнорировать", "reason": "спам — не показываем руководителю",
             "group": "spam", "party": "Выигрыш", "party_note": "спам"},
        ],
    },
    "templates": {"templates": [
        {"key": "delegate", "title": "Передача исполнителю", "used_for": "письмо передано сотруднику по правилам профиля",
         "subject": "Fwd: {тема}", "body": "{имя}, возьмите, пожалуйста, в работу и сообщите итог.\n\n— {отправитель}: {текст}",
         "variables": ["имя", "тема", "отправитель", "текст"]},
        {"key": "auto_reply", "title": "Автоответ", "used_for": "письмо, которое помощник подтверждает сам",
         "subject": "Re: {тема}", "body": "Принято, подтверждаю.\n\n{подпись}", "variables": ["тема", "подпись"]},
    ]},
    "template_discuss": {"reply": "Сделал теплее и короче, обращение по имени оставил.",
                         "subject": "Fwd: {тема}", "body": "{имя}, подхватите, пожалуйста, — и дайте знать, как сделаете.\n\n— {отправитель}: {текст}"},
    "meetings": {"start": "2026-12-07", "days": 7, "meetings": [meeting_short, meeting_121], "focus": [focus]},
    "meeting": {"meeting": meeting_short,
                "previous_questions": ["Сергей Морозов: Подписать договор с «Облако Плюс» — срок 12.12. Что сделано?"],
                "history_questions": ["Сергей Морозов — Статус по партнёру «Облако Плюс»: статус не меняется 4 дн. Что мешает?"],
                "tasks": [task_overdue], "protocol": protocol, "avatar_url": "/avatar?title=Планёрка отдела"},
    "protocols": {"protocols": [protocol]},
    "tasks": {"tasks": [task_overdue, task_soon]},
    "memory": {"q": "", "facts": 75, "topics": [
        {"topic": "Облако Плюс", "kind": "партнёр", "last_at": f"2026-12-11T17:32:00{T}",
         "last_what": "Договор на подписи у юристов, ждём до 12.12",
         "history": [{"period": "08.12–11.12, 7 раз без изменений", "who": "Сергей Морозов",
                      "what": "Договор на подписи у юристов, ждём до 12.12",
                      "quote": "Договор на подписи у юристов, ждём до 12.12", "sources": ["m022", "m032", "m035"]}]}]},
    "edits": {"protocol_edits": [{"at": f"2026-12-07T11:02:00{T}", "protocol_id": 2,
                                  "instruction": "Срок по договору — пятница, без изменений"}],
              "template_edits": [{"at": f"2026-12-07T12:00:00{T}", "key": "delegate", "title": "Передача исполнителю",
                                  "summary": "сделать теплее и короче"}]},
    "profile": {"id": "ivan_petrov", "name": "Иван Петров", "title": "Руководитель направления SMB и Digital",
                "goals": ["Выполнить план продаж SMB."],
                "confirm": ["деньги", "кадры", "обязательства_клиентам", "руководство"],
                "rules": {"рассылка": {"action": "игнорировать", "delegate_to": None,
                                       "source": "отказ от рассылок и неприоритетных приглашений;"}},
                "plan_time": "08:30", "week_time": "пятница 17:00", "style": "коротко, по делу"},
    "metrics": metrics,
    "journal": {"actions": [{"at": f"2026-12-11T17:45:00{T}", "actor": "assistant", "kind": "report_review",
                             "summary": "«Отчёт по e-com за ноябрь» (Анна Волкова) не пришёл — напомнил"}]},
}

for name, data in F.items():
    (HERE / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print("примеров:", len(F))
