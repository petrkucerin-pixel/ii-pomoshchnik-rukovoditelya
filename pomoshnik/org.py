"""Оргструктура: кто есть кто для руководителя."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

LEADER = "руководитель"
BOSS = "руководитель руководителя"


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    position: str
    role: str          # руководитель | подчинённый | руководитель руководителя | коллега
    email: str = ""


class Org:
    def __init__(self, people: list[Person]):
        self.people = people
        self.by_id = {p.id: p for p in people}

    @classmethod
    def load(cls, path: str | Path) -> "Org":
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        return cls([Person(r["id"], r["name"], r["position"], r["role_for_leader"], r.get("email", "") or "")
                    for r in rows])

    @property
    def leader(self) -> Person:
        return next(p for p in self.people if p.role == LEADER)

    def find_by_email(self, email: str) -> Person | None:
        email = (email or "").lower().strip()
        for p in self.people:
            if p.email and p.email.lower() == email:
                return p
        local = email.split("@")[0]
        if email.endswith("@company.example") and local in self.by_id:   # учебный домен: адрес = id@
            return self.by_id[local]
        return None

    def find_by_position(self, phrase: str) -> Person | None:
        """Человек, в должности которого есть все основы слов фразы: «директор по продажам» → Директор по продажам SMB."""
        stems = [w[:5] for w in phrase.lower().replace("ё", "е").split() if len(w) > 2]
        best = None
        for p in self.people:
            pos = p.position.lower().replace("ё", "е")
            if stems and all(s in pos for s in stems):
                if best is None or len(p.position) < len(best.position):
                    best = p
        return best

    def email_of(self, p: Person) -> str:
        return p.email or f"{p.id}@company.example"
