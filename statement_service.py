"""
Выписка по кошельку в PDF в стиле JulYera (решение владельца 10 октября 2026): по образцу банковской выписки —
шапка со знаком, период, итоги по валютам (и в основной по курсу Нацбанка), расходы по категориям, все операции,
цели накопления (отложено за период и всего).

Шрифты — фирменные Onest (текст) и Unbounded (заголовки), файлы в assets/fonts (OFL, с Google Fonts). В Unbounded
нет казахских букв, поэтому им пишутся только постоянные заголовки (в казахской выписке и они — Onest), имена — Onest. Только данные пользователя.
В логи — только количество операций.
"""
from i18n import tr
import i18n
import io
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import money_service
import rates_service
import users_service
from database import supabase
from money_service import MoneyError

logger = logging.getLogger(__name__)

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
INK = colors.HexColor("#17181C")
MUTED = colors.HexColor("#5D5F69")
ACCENT = colors.HexColor("#557546")
LINE = colors.HexColor("#E6E6E1")
SOFT = colors.HexColor("#F3F5F0")
DANGER = colors.HexColor("#B3261E")
SYMBOL = {"EUR": "€", "KZT": "₸", "USD": "$", "AZN": "₼", "TRY": "₺"}
MAX_DAYS = 366
_fonts_ready = False


def _fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    for name in ("Onest-Regular", "Onest-SemiBold", "Unbounded-SemiBold"):
        pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, f"{name}.ttf")))
    _fonts_ready = True


def money(amount, currency: str, sign: bool = False) -> str:
    """«1 240,50 €», со знаком «+» / «−» по желанию; пробел между разрядами неразрывный узкий."""
    value = Decimal(str(amount))
    text = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",")
    prefix = ("−" if value < 0 else "+") if sign and value != 0 else ("−" if value < 0 else "")
    return f"{prefix}{text} {SYMBOL.get(currency, currency)}"


def _period(date_from: str, date_to: str) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except (TypeError, ValueError):
        raise MoneyError(422, tr("Период — даты в формате ГГГГ-ММ-ДД"), "bad_period")
    if end < start:
        raise MoneyError(422, tr("Дата «по» раньше даты «с»"), "bad_period")
    if (end - start).days > MAX_DAYS:
        raise MoneyError(422, tr("Выписка — не больше чем за год"), "bad_period")
    return start, end


def collect(user_id: str, start: date, end: date) -> dict:
    """Всё для выписки, уже посчитанное (Decimal)."""
    rows = money_service._fetch_all(lambda: supabase.table(money_service.TX_TABLE).select("*").eq("user_id", user_id)
                                    .gte("spent_on", start.isoformat()).lte("spent_on", end.isoformat()))
    rows.sort(key=lambda r: (str(r["spent_on"])[:10], r.get("created_at") or "", r["id"]), reverse=True)
    cats = {c["id"]: c["name"] for c in supabase.table(money_service.CAT_TABLE).select("id,name")
            .eq("user_id", user_id).execute().data}
    totals, by_cat = {}, {}
    for r in rows:
        amount, cur = Decimal(str(r["amount"])), r["currency"]
        t = totals.setdefault(cur, {"income": Decimal(0), "expense": Decimal(0)})
        t[r["kind"]] += amount
        if r["kind"] == "expense":
            by_cat.setdefault(cats.get(r["category_id"], tr("Без категории")), {}).setdefault(cur, Decimal(0))
            by_cat[cats.get(r["category_id"], "Без категории")][cur] += amount
    main = money_service.default_currency(user_id)
    rates = rates_service.get_rates() if set(totals) - {main} else None
    main_totals = None
    if totals and rates_service.can_convert(rates, set(totals), main):
        conv = lambda kind: sum((rates_service.convert(t[kind], c, main, rates) for c, t in totals.items()), Decimal(0))
        main_totals = {"income": conv("income"), "expense": conv("expense"), "approx": bool(set(totals) - {main}),
                       "rate": rates_service.rate_info(rates, main) if rates else None}

    def cat_weight(item):
        name, sums = item
        if rates_service.can_convert(rates, set(sums), main):
            return sum((rates_service.convert(v, c, main, rates) for c, v in sums.items()), Decimal(0))
        return sum(sums.values(), Decimal(0))
    categories = sorted(by_cat.items(), key=cat_weight, reverse=True)

    goals = []
    try:
        g_rows = supabase.table("money_goals").select("*").eq("user_id", user_id).order("created_at").execute().data
        moves = money_service._fetch_all(lambda: supabase.table("money_goal_moves").select("*").eq("user_id", user_id))
        for g in g_rows:
            mine = [m for m in moves if m["goal_id"] == g["id"]]
            in_period = [m for m in mine if start.isoformat() <= str(m["created_at"])[:10] <= end.isoformat()]
            goals.append({"name": g["name"], "currency": g["currency"], "target": Decimal(str(g["target"])),
                          "saved": sum((Decimal(str(m["amount"])) for m in mine), Decimal(0)),
                          "period": sum((Decimal(str(m["amount"])) for m in in_period), Decimal(0))})
    except Exception as exc:  # до миграции 011 целей нет — выписка всё равно собирается
        logger.warning("Цели в выписку не попали: %s", type(exc).__name__)
    return {"rows": rows, "cats": cats, "totals": totals, "main": main, "main_totals": main_totals,
            "categories": categories, "goals": goals}


def _styles() -> dict:
    base = dict(fontName="Onest-Regular", fontSize=9, leading=12, textColor=INK)
    # В Unbounded нет казахских букв (ә, қ, ү…): в казахской выписке заголовки — Onest
    head = "Onest-SemiBold" if i18n.current() == "kk" else "Unbounded-SemiBold"
    return {
        "brand": ParagraphStyle("brand", fontName="Unbounded-SemiBold", fontSize=13, leading=16, textColor=INK),
        "title": ParagraphStyle("title", fontName=head, fontSize=17, leading=22, textColor=INK),
        "h2": ParagraphStyle("h2", fontName=head, fontSize=10.5, leading=14, textColor=INK,
                             spaceBefore=10, spaceAfter=6),
        "text": ParagraphStyle("text", **base),
        "muted": ParagraphStyle("muted", **{**base, "textColor": MUTED, "fontSize": 8.5}),
        "num": ParagraphStyle("num", **{**base, "alignment": TA_RIGHT}),
        "big": ParagraphStyle("big", fontName="Unbounded-SemiBold", fontSize=15, leading=19, textColor=ACCENT),
        "bold": ParagraphStyle("bold", **{**base, "fontName": "Onest-SemiBold"}),
        "boldnum": ParagraphStyle("boldnum", **{**base, "fontName": "Onest-SemiBold", "alignment": TA_RIGHT}),
    }


def _esc(text) -> str:
    return (str(text or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table(data, widths, header=True, zebra=True):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6),
             ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]
    if header:
        style += [("LINEBELOW", (0, 0), (-1, 0), 0.8, INK)]
    if zebra:
        style += [("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, SOFT])]
    t.setStyle(TableStyle(style))
    return t


def _draw_frame(canvas, doc, owner: str, period: str):
    """Знак JulYera в шапке и номер страницы внизу (на каждой странице)."""
    canvas.saveState()
    w, h = A4
    x, y, k = 18 * mm, h - 15 * mm, 0.42 * mm  # знак 56×48 единиц
    canvas.setLineWidth(5.5 * k)
    canvas.setLineCap(1)
    canvas.setLineJoin(1)
    canvas.setStrokeColor(INK)
    p = canvas.beginPath()
    p.moveTo(x + 12 * k, y - 9 * k)
    p.lineTo(x + 12 * k, y - 30 * k)
    p.curveTo(x + 12 * k, y - 36 * k, x + 9 * k, y - 39 * k, x + 5 * k, y - 39 * k)
    canvas.drawPath(p, stroke=1, fill=0)
    canvas.setStrokeColor(ACCENT)
    p = canvas.beginPath()
    p.moveTo(x + 24 * k, y - 16 * k)
    p.lineTo(x + 34 * k, y - 26 * k)
    p.lineTo(x + 49 * k, y - 7 * k)
    p.moveTo(x + 34 * k, y - 26 * k)
    p.lineTo(x + 34 * k, y - 40 * k)
    canvas.drawPath(p, stroke=1, fill=0)
    canvas.setFont("Unbounded-SemiBold", 12)
    canvas.setFillColor(INK)
    canvas.drawString(x + 27 * mm, y - 12.5 * mm, "JulYera")
    canvas.setFont("Onest-Regular", 8.5)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(w - 18 * mm, y - 9 * mm, tr("Выписка по кошельку"))
    canvas.drawRightString(w - 18 * mm, y - 13.5 * mm, period)
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(18 * mm, h - 33 * mm, w - 18 * mm, h - 33 * mm)
    canvas.drawString(18 * mm, 11 * mm, owner)
    canvas.drawRightString(w - 18 * mm, 11 * mm, tr('Страница {0}', doc.page))
    canvas.restoreState()


def build(user_id: str, date_from: str, date_to: str) -> tuple[str, bytes, int]:
    """(имя файла, PDF, сколько операций)."""
    _fonts()
    start, end = _period(date_from, date_to)
    data = collect(user_id, start, end)
    user = users_service.get_by_id(user_id) or {}
    owner = user.get("name") or tr("Пользователь JulYera")
    st = _styles()
    period = f"{start.strftime('%d.%m.%Y')} — {end.strftime('%d.%m.%Y')}"
    width = A4[0] - 36 * mm
    story = [Paragraph(tr("Выписка по кошельку"), st["title"]), Spacer(1, 4),
             Paragraph(tr('{0} · за период {1}', _esc(owner), period), st["text"]),
             Paragraph(tr('Сформирована {0} · основная валюта {1}', datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M'), data['main']), st["muted"]), Spacer(1, 10)]

    # --- итоги
    story.append(Paragraph(tr("Итоги"), st["h2"]))
    mt = data["main_totals"]
    if mt:
        net = mt["income"] - mt["expense"]
        approx = "≈ " if mt["approx"] else ""
        cells = [[Paragraph(tr("Доходы"), st["muted"]), Paragraph(tr("Расходы"), st["muted"]), Paragraph(tr("Разница"), st["muted"])],
                 [Paragraph(approx + money(mt["income"], data["main"]), st["big"]),
                  Paragraph(approx + money(mt["expense"], data["main"]), st["big"]),
                  Paragraph(approx + money(net, data["main"], sign=True), st["big"])]]
        box = Table(cells, colWidths=[width / 3] * 3)
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                 ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, -1), (-1, -1), 10)]))
        story += [box, Spacer(1, 6)]
        if mt["approx"] and mt["rate"]:
            r = mt["rate"]
            story.append(Paragraph(tr('Пересчёт в {0} по курсу Нацбанка РК на {1}: 1 {2} = {3} KZT. Операции хранятся в своей валюте.', data['main'], r['date'], r['currency'], r['kzt']), st["muted"]))
    rows = [[Paragraph(tr("Валюта"), st["bold"]), Paragraph(tr("Доходы"), st["boldnum"]), Paragraph(tr("Расходы"), st["boldnum"]),
             Paragraph(tr("Разница"), st["boldnum"])]]
    for cur in money_service.CURRENCIES:
        t = data["totals"].get(cur)
        if not t:
            continue
        rows.append([Paragraph(cur, st["text"]), Paragraph(money(t["income"], cur), st["num"]),
                     Paragraph(money(t["expense"], cur), st["num"]),
                     Paragraph(money(t["income"] - t["expense"], cur, sign=True), st["num"])])
    if len(rows) > 1:
        story += [Spacer(1, 6), _table(rows, [width * 0.16, width * 0.28, width * 0.28, width * 0.28])]
    else:
        story.append(Paragraph(tr("За этот период операций нет."), st["muted"]))

    # --- по категориям
    if data["categories"]:
        story.append(Paragraph(tr("Расходы по категориям"), st["h2"]))
        rows = [[Paragraph(tr("Категория"), st["bold"]), Paragraph(tr("Сумма"), st["boldnum"])]]
        for name, sums in data["categories"]:
            parts = " · ".join(money(-sums[c], c) for c in money_service.CURRENCIES if c in sums)
            rows.append([Paragraph(_esc(name), st["text"]), Paragraph(parts, st["num"])])
        story.append(_table(rows, [width * 0.45, width * 0.55]))

    # --- цели
    if data["goals"]:
        story.append(Paragraph(tr("Цели накопления"), st["h2"]))
        rows = [[Paragraph(tr("Цель"), st["bold"]), Paragraph(tr("За период"), st["boldnum"]), Paragraph(tr("Отложено"), st["boldnum"]),
                 Paragraph(tr("Нужно"), st["boldnum"])]]
        for g in data["goals"]:
            pct = int(g["saved"] / g["target"] * 100) if g["target"] else 0
            rows.append([Paragraph(f"{_esc(g['name'])} <font color='#5D5F69'>· {min(pct, 100)}%</font>", st["text"]),
                         Paragraph(money(g["period"], g["currency"], sign=True), st["num"]),
                         Paragraph(money(g["saved"], g["currency"]), st["num"]),
                         Paragraph(money(g["target"], g["currency"]), st["num"])])
        story.append(_table(rows, [width * 0.34, width * 0.22, width * 0.22, width * 0.22]))
        story.append(Paragraph(tr("Отложенное в цели не считается расходом."), st["muted"]))

    # --- операции
    story.append(Paragraph(tr('Операции · {0}', len(data['rows'])), st["h2"]))
    if data["rows"]:
        rows = [[Paragraph(tr("Дата"), st["bold"]), Paragraph(tr("Описание"), st["bold"]), Paragraph(tr("Категория"), st["bold"]),
                 Paragraph(tr("Сумма"), st["boldnum"])]]
        for r in data["rows"]:
            amount = Decimal(str(r["amount"])) * (-1 if r["kind"] == "expense" else 1)
            d = str(r["spent_on"])[:10]
            what = r.get("note") or (tr("Доход") if r["kind"] == "income" else tr("Расход"))
            src = {"applepay": " · Apple Pay", "recurring": tr(" · повтор")}.get(r.get("source"), "")
            color = "#557546" if amount > 0 else "#17181C"
            rows.append([Paragraph(f"{d[8:10]}.{d[5:7]}.{d[:4]}", st["text"]),
                         Paragraph(f"{_esc(what)}<font color='#5D5F69'>{src}</font>", st["text"]),
                         Paragraph(_esc(data["cats"].get(r["category_id"], "")), st["muted"]),
                         Paragraph(f"<font color='{color}'>{money(amount, r['currency'], sign=True)}</font>", st["num"])])
        story.append(_table(rows, [width * 0.15, width * 0.42, width * 0.2, width * 0.23]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=38 * mm,
                            bottomMargin=18 * mm, title=tr("Выписка JulYera"), author="JulYera")
    frame = lambda c, d: _draw_frame(c, d, owner, period)
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    logger.info("Выписка собрана: %s операций", len(data["rows"]))
    return f"julyera-statement-{start.isoformat()}_{end.isoformat()}.pdf", buf.getvalue(), len(data["rows"])


def caption(start: str, end: str) -> str:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    return tr('🧾 Выписка JulYera за {0} — {1}', s.strftime('%d.%m.%Y'), e.strftime('%d.%m.%Y'))
