"""
Собирает все русские фразы интерфейса web/index.html — ключи словаря перевода (web/i18n.js).

    python scripts/i18n_keys.py            → печатает, сколько ключей и сколько ещё без перевода
    python scripts/i18n_keys.py --missing  → список ключей без перевода (en, kk, az)

Ключ — русская фраза как есть (без пробелов по краям): строки в скриптах, тексты и подписи (aria-label,
placeholder, title, alt) в разметке и внутри HTML-кусочков, формы окончаний «задача|задачи|задач».
"""
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CYR = re.compile('[А-Яа-яЁё]')
ATTRS = ('aria-label', 'placeholder', 'title', 'alt')
# Ничего не значащие для перевода куски: подстановки дат, «≈», служебные строки
NOT_TEXT = {'е'}  # не надписи: замена «ё» при поиске
SKIP = re.compile(r'^[\s{}0-9.,:;·—–\-+%≈№«»()!?/]*$')


class Collector(HTMLParser):
    def __init__(self, keys):
        super().__init__(convert_charrefs=True)
        self.keys = keys
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        for k, v in attrs:
            if k in ATTRS and v and CYR.search(v):
                self.keys.add(v.strip())

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and CYR.search(data):
            self.keys.add(' '.join(data.split()))


def js_literals(code):
    """Строки '…', "…" и шаблоны `…` (без кусков ${…}) из кода: комментарии и регулярные выражения пропускаются."""
    out, i, n = [], 0, len(code)
    prev = ''  # последний значимый символ — чтобы отличить регулярку от деления

    def read_quoted(i, q):
        buf = []
        while i < n and code[i] != q and code[i] != '\n':
            if code[i] == '\\':
                nxt = code[i + 1]
                buf.append({'n': '\n', 't': '\t'}.get(nxt, nxt))
                i += 2
                continue
            buf.append(code[i])
            i += 1
        return ''.join(buf), i + 1

    def read_template(i):
        parts, buf = [], []
        while i < n and code[i] != '`':
            if code[i] == '\\':
                buf.append(code[i + 1]); i += 2; continue
            if code[i] == '$' and code[i + 1] == '{':
                parts.append(''.join(buf)); buf = []
                depth, i = 1, i + 2
                while depth:
                    c = code[i]
                    if c == '`':
                        sub, i = read_template(i + 1)
                        out.extend(p for p in sub if CYR.search(p) and (len(sub) == 1 or '<' in p))
                        continue
                    if c in "'\"":
                        lit, i = read_quoted(i + 1, c)
                        if CYR.search(lit):
                            out.append(lit)
                        continue
                    depth += c == '{'
                    depth -= c == '}'
                    i += 1
                continue
            buf.append(code[i]); i += 1
        parts.append(''.join(buf))
        return parts, i + 1

    while i < n:
        c = code[i]
        if c == '/' and code[i + 1] == '/':
            j = code.find('\n', i); i = n if j < 0 else j; continue
        if c == '/' and code[i + 1] == '*':
            j = code.find('*/', i + 2); i = n if j < 0 else j + 2; continue
        if c == '/' and (prev in '(,=:[!&|?{};+-*%<>~^' or prev == ''):
            i += 1; in_class = False
            while i < n and (code[i] != '/' or in_class) and code[i] != '\n':
                if code[i] == '\\': i += 2; continue
                if code[i] == '[': in_class = True
                elif code[i] == ']': in_class = False
                i += 1
            i += 1; prev = 'r'; continue
        if c in "'\"":
            lit, i = read_quoted(i + 1, c)
            if CYR.search(lit):
                out.append(lit)
            prev = 's'; continue
        if c == '`':
            parts, i = read_template(i + 1)
            if len(parts) == 1 and CYR.search(parts[0]):
                out.append(parts[0])
            else:  # шаблон с подстановками: тексты из его HTML-кусочков
                out.extend(p for p in parts if '<' in p and '>' in p and CYR.search(p))
            prev = 's'; continue
        if not c.isspace():
            prev = c if not (c.isalnum() or c in '_$') else 'w'
        i += 1
    return out


def collect():
    html = open(os.path.join(ROOT, 'web', 'index.html')).read()
    keys = set()
    markup = re.sub(r'<script>.*?</script>', '', html, flags=re.S)
    Collector(keys).feed(markup)
    for code in re.findall(r'<script>(.*?)</script>', html, re.S):
        for lit in js_literals(code):
            if '<' in lit and '>' in lit:  # HTML-кусочек: его тексты и подписи
                Collector(keys).feed(lit)
            else:
                keys.add(lit.strip())
    for noun in ('задачу', 'заметку', 'операцию', 'платёж'):
        keys.update({'Изменить ' + noun, 'Удалить ' + noun})
    keys -= NOT_TEXT
    return sorted(k for k in keys if k and CYR.search(k) and not SKIP.match(k))


LANGS = ('en', 'kk', 'az')


def load_dicts():
    path = os.path.join(ROOT, 'web', 'i18n.js')
    if not os.path.exists(path):
        return {lang: {} for lang in LANGS}
    src = open(path).read()
    m = re.search(r'/\*DICT-START\*/(.*?)/\*DICT-END\*/', src, re.S)
    data = json.loads(m.group(1)) if m else {}
    return {lang: data.get(lang, {}) for lang in LANGS}


if __name__ == '__main__':
    keys = collect()
    dicts = load_dicts()
    missing = {lang: [k for k in keys if k not in d] for lang, d in dicts.items()}
    if '--missing' in sys.argv:
        print(json.dumps(missing, ensure_ascii=False, indent=1))
    elif '--json' in sys.argv:
        print(json.dumps(keys, ensure_ascii=False, indent=0))
    else:
        print(f'ключей: {len(keys)}, без перевода: ' + ', '.join(f'{lang} {len(m)}' for lang, m in missing.items()))
