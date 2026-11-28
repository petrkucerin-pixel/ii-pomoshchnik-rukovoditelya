"""Собирает питч: стиль и навигация — из питча «Смена» (клининг), сцены — свои; картинки встраиваются в итоговый файл."""
import base64
import re
import sys
from pathlib import Path

src_style, deck, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
s = src_style.read_text(encoding="utf-8")
head = s[:s.index('<div id="deck">')]
head = head.replace("<title>Питч «Смена»</title>", "<title>ИИ-помощник руководителя — питч</title>")
head = re.sub(r"/\* Питч фичи 3 «Смена».*?\*/", "/* Питч «ИИ-помощник руководителя» — тем же языком, что питч «Смена» (кейс 1). Экраны — снимки стенда. */",
              head, flags=re.S)
script = s[s.index("<script>"):]
# Сцена 9 клининга (смена бизнеса) нам не нужна — обработчик входа убираем.
script = re.sub(r"const enter = \{.*?\n  \};\n", "const enter = {};\n", script, flags=re.S)
head = head.replace("</style>", ".duo{max-width:none;grid-template-columns:minmax(0,3fr) minmax(0,2fr);align-items:center}.desk{border-radius:10px}</style>", 1)
page = head + deck.read_text(encoding="utf-8") + "\n" + script
(out_dir / "исходники" / "питч.html").write_text(page, encoding="utf-8")

media = out_dir / "исходники"


def inline(m):
    p = media / m.group(1)
    return 'src="data:image/jpeg;base64,' + base64.b64encode(p.read_bytes()).decode() + '"'


full = re.sub(r'src="(media/[^"]+)"', inline, page)
(out_dir / "Питч для жюри.html").write_text(full, encoding="utf-8")
print("ok", len(full) // 1024, "КБ")
