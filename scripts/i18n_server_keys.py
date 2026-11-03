"""
Собирает русские фразы сервера (бот, сводки, выписка, ошибки API) — ключи словаря locales/server.json.

    python scripts/i18n_server_keys.py            → сколько ключей и сколько без перевода
    python scripts/i18n_server_keys.py --missing  → какие без перевода (en, kk, az)
    python scripts/i18n_server_keys.py --json     → все ключи

Ключ — первый аргумент tr(…), tn(…), i18n.L(…) (строка или имя константы модуля), плюс описания команд
(telegram_handlers.COMMANDS) и кнопки панели (BTN_*).
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CYR = re.compile("[А-Яа-яЁё]")
SKIP = {"groq_client.py", "i18n.py"}
LANGS = ("en", "kk", "az")


def _str(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def collect():
    keys = set()
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".py") or name in SKIP:
            continue
        tree = ast.parse(open(os.path.join(ROOT, name), encoding="utf-8").read())
        consts = {}
        for st in tree.body:
            if isinstance(st, ast.Assign):
                if len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    v = _str(st.value, {})
                    if v is not None:
                        consts[st.targets[0].id] = v
                    if st.targets[0].id == "COMMANDS" and isinstance(st.value, ast.List):
                        keys.update(e.elts[1].value for e in st.value.elts if isinstance(e, ast.Tuple))
                elif len(st.targets) == 1 and isinstance(st.targets[0], ast.Tuple) and isinstance(st.value, ast.Tuple):
                    for t, v in zip(st.targets[0].elts, st.value.elts):
                        if isinstance(t, ast.Name) and _str(v, {}) is not None:
                            consts[t.id] = v.value
                            if t.id.startswith("BTN_"):
                                keys.add(v.value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func
            fn = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fn in ("tr", "L", "lookup"):
                v = _str(node.args[0], consts)
            elif fn == "tn" and len(node.args) > 1:
                v = _str(node.args[1], consts)
            else:
                continue
            if v is not None and CYR.search(v):
                keys.add(v.strip() if v.strip() else v)
    return sorted(keys)


def load():
    path = os.path.join(ROOT, "locales", "server.json")
    if not os.path.exists(path):
        return {lang: {} for lang in LANGS}
    data = json.load(open(path, encoding="utf-8"))
    return {lang: data.get(lang, {}) for lang in LANGS}


if __name__ == "__main__":
    keys = collect()
    missing = {lang: [k for k in keys if k not in d] for lang, d in load().items()}
    if "--missing" in sys.argv:
        print(json.dumps(missing, ensure_ascii=False, indent=1))
    elif "--json" in sys.argv:
        print(json.dumps(keys, ensure_ascii=False, indent=0))
    else:
        print(f"ключей: {len(keys)}, без перевода: " + ", ".join(f"{lang} {len(m)}" for lang, m in missing.items()))
