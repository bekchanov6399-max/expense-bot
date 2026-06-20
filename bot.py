#!/usr/bin/env python3
import os, io, sqlite3, csv, re, asyncio
from datetime import datetime, date, time as dt_time
from calendar import monthrange

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)

TOKEN = os.environ["BOT_TOKEN"]
DB    = "expenses.db"

# ── Categories ─────────────────────────────────────────────────────────────────
CATEGORIES = [
    ("🍔 Еда",          "food"),
    ("🚗 Транспорт",    "transport"),
    ("🎬 Развлечения",  "entertainment"),
    ("👕 Одежда",       "clothes"),
    ("💊 Здоровье",     "health"),
    ("🏠 Коммунальные", "utilities"),
    ("📦 Другое",       "other"),
]
CAT_NAME  = {k: n for n, k in CATEGORIES}
CAT_COLOR = {
    "food":"#FF6B6B","transport":"#4ECDC4","entertainment":"#45B7D1",
    "clothes":"#96CEB4","health":"#FFEAA7","utilities":"#DDA0DD","other":"#98D8C8",
}
CAT_KW = {
    "еда":"food","продукты":"food","обед":"food","ужин":"food","завтрак":"food",
    "кофе":"food","кафе":"food","ресторан":"food","пицца":"food","суши":"food",
    "такси":"transport","метро":"transport","автобус":"transport",
    "транспорт":"transport","бензин":"transport","заправка":"transport",
    "кино":"entertainment","развлечения":"entertainment","игры":"entertainment",
    "спорт":"entertainment","клуб":"entertainment","театр":"entertainment",
    "одежда":"clothes","обувь":"clothes",
    "аптека":"health","здоровье":"health","врач":"health","лекарства":"health",
    "аренда":"utilities","квартплата":"utilities","коммунальные":"utilities",
    "интернет":"utilities","свет":"utilities","газ":"utilities",
    "другое":"other","прочее":"other",
}
MONTH_RU = {
    1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",6:"Июнь",
    7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",11:"Ноябрь",12:"Декабрь",
}
MONTH_SH = {
    1:"Янв",2:"Фев",3:"Мар",4:"Апр",5:"Май",6:"Июн",
    7:"Июл",8:"Авг",9:"Сен",10:"Окт",11:"Ноя",12:"Дек",
}
BG = "#0f0f1a"

# ── States ─────────────────────────────────────────────────────────────────────
(
    ADD_CAT, ADD_AMOUNT, ADD_COMMENT,
    BUD_CAT, BUD_AMOUNT,
    REC_CAT, REC_AMOUNT, REC_DAY, REC_COMMENT,
    SEARCH,
) = range(10)

# ── DB ─────────────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                is_income INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                year_month TEXT NOT NULL,
                category TEXT,
                limit_amt REAL NOT NULL,
                UNIQUE(user_id, year_month, category)
            );
            CREATE TABLE IF NOT EXISTS recurring (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT,
                day_of_month INTEGER NOT NULL,
                last_date TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                notify_enabled INTEGER DEFAULT 0
            );
        """)

def db(sql, params=(), fetchall=False, fetchone=False):
    with sqlite3.connect(DB) as c:
        cur = c.execute(sql, params)
        if fetchall: return cur.fetchall()
        if fetchone: return cur.fetchone()
        return cur.lastrowid

def save_expense(uid, cat, amount, comment, is_income=False):
    db("INSERT INTO expenses(user_id,category,amount,comment,is_income,created_at) VALUES(?,?,?,?,?,?)",
       (uid, cat, amount, comment, 1 if is_income else 0, datetime.now().isoformat()))

def get_last_expense(uid):
    return db("SELECT id,category,amount,comment,is_income,created_at FROM expenses WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
              (uid,), fetchone=True)

def monthly_stats(uid, year, month, is_income=False):
    rows = db("SELECT category,SUM(amount) FROM expenses WHERE user_id=? AND is_income=? AND strftime('%Y-%m',created_at)=? GROUP BY category",
              (uid, 1 if is_income else 0, f"{year:04d}-{month:02d}"), fetchall=True)
    return dict(rows)

def monthly_total(uid, year, month, is_income=False):
    r = db("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND is_income=? AND strftime('%Y-%m',created_at)=?",
           (uid, 1 if is_income else 0, f"{year:04d}-{month:02d}"), fetchone=True)
    return r[0] if r else 0

def daily_amounts(uid, year, month):
    return db("SELECT date(created_at),SUM(amount) FROM expenses WHERE user_id=? AND is_income=0 AND strftime('%Y-%m',created_at)=? GROUP BY date(created_at) ORDER BY 1",
              (uid, f"{year:04d}-{month:02d}"), fetchall=True)

def recent_expenses(uid, n=10):
    return db("SELECT id,category,amount,comment,is_income,created_at FROM expenses WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
              (uid, n), fetchall=True)

def search_expenses(uid, q):
    like = f"%{q}%"
    return db("SELECT id,category,amount,comment,is_income,created_at FROM expenses WHERE user_id=? AND (comment LIKE ? OR CAST(amount AS TEXT) LIKE ?) ORDER BY created_at DESC LIMIT 20",
              (uid, like, like), fetchall=True)

def available_months(uid):
    return [r[0] for r in db("SELECT DISTINCT strftime('%Y-%m',created_at) FROM expenses WHERE user_id=? ORDER BY 1 DESC LIMIT 12",
                              (uid,), fetchall=True)]

def top_expenses(uid, year, month, n=3):
    return db("SELECT category,amount,comment FROM expenses WHERE user_id=? AND is_income=0 AND strftime('%Y-%m',created_at)=? ORDER BY amount DESC LIMIT ?",
              (uid, f"{year:04d}-{month:02d}", n), fetchall=True)

def get_budget(uid, ym, category=None):
    r = db("SELECT limit_amt FROM budgets WHERE user_id=? AND year_month=? AND category IS ?",
           (uid, ym, category), fetchone=True)
    return r[0] if r else None

def set_budget(uid, ym, limit_amt, category=None):
    db("INSERT OR REPLACE INTO budgets(user_id,year_month,category,limit_amt) VALUES(?,?,?,?)",
       (uid, ym, category, limit_amt))

def get_all_budgets(uid, ym):
    rows = db("SELECT category,limit_amt FROM budgets WHERE user_id=? AND year_month=?",
              (uid, ym), fetchall=True)
    return {(r[0] if r[0] else "total"): r[1] for r in rows}

def get_recurring(uid):
    return db("SELECT id,category,amount,comment,day_of_month FROM recurring WHERE user_id=? ORDER BY day_of_month",
              (uid,), fetchall=True)

def get_settings(uid):
    r = db("SELECT notify_enabled FROM settings WHERE user_id=?", (uid,), fetchone=True)
    return {"notify_enabled": r[0] if r else 0}

def toggle_notify(uid):
    db("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (uid,))
    current = get_settings(uid)["notify_enabled"]
    db("UPDATE settings SET notify_enabled=? WHERE user_id=?", (0 if current else 1, uid))
    return not current

# ── Budget warning ─────────────────────────────────────────────────────────────
async def check_budget(uid, cat, ctx):
    now = datetime.now()
    ym  = now.strftime("%Y-%m")
    total_lim = get_budget(uid, ym)
    if total_lim:
        spent = monthly_total(uid, now.year, now.month)
        pct   = spent / total_lim * 100
        if pct >= 100:
            await ctx.bot.send_message(uid, f"🔴 *Бюджет превышен!*\n{spent:,.0f} / {total_lim:,.0f} ₽ ({pct:.0f}%)", parse_mode="Markdown")
        elif pct >= 80:
            await ctx.bot.send_message(uid, f"🟡 Использовано *{pct:.0f}%* бюджета — {spent:,.0f} / {total_lim:,.0f} ₽", parse_mode="Markdown")
    cat_lim = get_budget(uid, ym, cat)
    if cat_lim:
        r = db("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND category=? AND is_income=0 AND strftime('%Y-%m',created_at)=?",
               (uid, cat, ym), fetchone=True)
        spent = r[0] if r else 0
        pct   = spent / cat_lim * 100
        name  = CAT_NAME.get(cat, cat)
        if pct >= 100:
            await ctx.bot.send_message(uid, f"🔴 Лимит *«{name}»* превышен!\n{spent:,.0f} / {cat_lim:,.0f} ₽", parse_mode="Markdown")
        elif pct >= 80:
            await ctx.bot.send_message(uid, f"🟡 *«{name}»*: {pct:.0f}% лимита — {spent:,.0f} / {cat_lim:,.0f} ₽", parse_mode="Markdown")

# ── Charts ─────────────────────────────────────────────────────────────────────
def _ax(ax):
    ax.set_facecolor(BG)
    for sp in ax.spines.values(): sp.set_color("#333355")
    ax.tick_params(colors="#aaaacc")

def pie_chart(stats, title):
    labels = [CAT_NAME.get(k, k) for k in stats]
    values = list(stats.values())
    colors = [CAT_COLOR.get(k, "#ccc") for k in stats]
    fig, ax = plt.subplots(figsize=(7, 5.5), facecolor=BG)
    _ax(ax)
    wedges, _, autotexts = ax.pie(values, colors=colors, autopct="%1.1f%%", startangle=90,
        pctdistance=0.76, wedgeprops=dict(width=0.58, edgecolor=BG, linewidth=2))
    for at in autotexts: at.set(color="white", fontsize=8.5, fontweight="bold")
    ax.text(0, 0, f"{sum(values):,.0f}\n₽", ha="center", va="center", fontsize=13, color="white", fontweight="bold")
    legend = ax.legend(wedges, labels, loc="lower center", bbox_to_anchor=(0.5, -0.18), ncol=3, fontsize=8, frameon=False)
    for t in legend.get_texts(): t.set_color("#ccccee")
    ax.set_title(title, color="white", fontsize=13, pad=16)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
    buf.seek(0); plt.close(); return buf

def bar_chart(months_data):
    months   = list(months_data.keys())
    all_cats = sorted({c for s in months_data.values() for c in s})
    labels   = [f"{MONTH_SH[int(m.split('-')[1])]}\n{m.split('-')[0]}" for m in months]
    x = np.arange(len(months)); n = len(all_cats); w = 0.7 / max(n, 1)
    fig, ax = plt.subplots(figsize=(9, 5.5), facecolor=BG)
    _ax(ax); ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    for i, cat in enumerate(all_cats):
        offset = (i - n / 2 + 0.5) * w
        ax.bar(x + offset, [months_data[m].get(cat, 0) for m in months],
               w * 0.88, label=CAT_NAME.get(cat, cat), color=CAT_COLOR.get(cat, "#ccc"), alpha=0.88)
    ax.set_xticks(x); ax.set_xticklabels(labels, color="white", fontsize=10)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_ylabel("Сумма (₽)", color="#aaaacc", fontsize=9)
    ax.set_title("Сравнение расходов по месяцам", color="white", fontsize=13)
    legend = ax.legend(loc="upper right", fontsize=8, frameon=False)
    for t in legend.get_texts(): t.set_color("#ccccee")
    ax.grid(axis="y", color="#222244", linewidth=0.7)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
    buf.seek(0); plt.close(); return buf

def line_chart(uid, year, month):
    rows = daily_amounts(uid, year, month)
    if not rows: return None
    today      = date.today()
    end_day    = today.day if (today.year == year and today.month == month) else monthrange(year, month)[1]
    data       = {r[0]: r[1] for r in rows}
    dates      = [date(year, month, d) for d in range(1, end_day + 1)]
    daily_vals = [data.get(str(date(year, month, d)), 0) for d in range(1, end_day + 1)]
    cum = []; s = 0
    for v in daily_vals: s += v; cum.append(s)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), facecolor=BG)
    _ax(ax1); ax1.bar(dates, daily_vals, color="#4ECDC4", alpha=0.8, width=0.8)
    ax1.set_title(f"Расходы по дням — {MONTH_RU[month]} {year}", color="white", fontsize=12)
    ax1.set_ylabel("За день (₽)", color="#aaaacc", fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d"))
    ax1.spines["top"].set_visible(False); ax1.spines["right"].set_visible(False)
    ax1.grid(axis="y", color="#222244", linewidth=0.5)
    _ax(ax2); ax2.plot(dates, cum, color="#FF6B6B", linewidth=2, marker="o", markersize=3)
    ax2.fill_between(dates, cum, alpha=0.2, color="#FF6B6B")
    ax2.set_ylabel("Нарастающий итог (₽)", color="#aaaacc", fontsize=9)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d"))
    ax2.spines["top"].set_visible(False); ax2.spines["right"].set_visible(False)
    ax2.grid(axis="y", color="#222244", linewidth=0.5)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=BG)
    buf.seek(0); plt.close(); return buf

# ── Export ─────────────────────────────────────────────────────────────────────
def export_csv(uid):
    rows = db("SELECT created_at,category,amount,comment,is_income FROM expenses WHERE user_id=? ORDER BY created_at",
              (uid,), fetchall=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Дата", "Категория", "Сумма (₽)", "Комментарий", "Тип"])
    for ca, cat, amt, comment, is_inc in rows:
        dt = datetime.fromisoformat(ca).strftime("%d.%m.%Y %H:%M")
        w.writerow([dt, CAT_NAME.get(cat, cat), f"{amt:.2f}", comment or "", "Доход" if is_inc else "Расход"])
    buf.seek(0)
    return io.BytesIO(buf.getvalue().encode("utf-8-sig"))

# ── Helpers ─────────────────────────────────────────────────────────────────────
def parse_quick(text):
    tokens = text.strip().split()
    amount = None; cat = None; rest = []
    for t in tokens:
        if amount is None:
            try: amount = float(t.replace(",", ".")); continue
            except: pass
        if cat is None:
            found = CAT_KW.get(t.lower())
            if found: cat = found; continue
        rest.append(t)
    return amount, cat, (" ".join(rest) if rest else None)

def cat_kb(prefix="cat_"):
    rows = []; pair = []
    for name, key in CATEGORIES:
        pair.append(InlineKeyboardButton(name, callback_data=f"{prefix}{key}"))
        if len(pair) == 2: rows.append(pair); pair = []
    if pair: rows.append(pair)
    return rows

CANCEL = InlineKeyboardButton("❌ Отмена", callback_data="menu")
BACK   = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu")]])

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Расход",      callback_data="add_exp"),
         InlineKeyboardButton("💚 Доход",       callback_data="add_inc")],
        [InlineKeyboardButton("📊 Статистика",  callback_data="stats"),
         InlineKeyboardButton("📋 История",     callback_data="history")],
        [InlineKeyboardButton("💰 Бюджет",      callback_data="budget"),
         InlineKeyboardButton("🔄 Регулярные",  callback_data="recurring")],
        [InlineKeyboardButton("🔍 Поиск",       callback_data="search"),
         InlineKeyboardButton("📤 Экспорт CSV", callback_data="export")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="notify")],
    ])

async def send_menu(update: Update, text="💰 *Трекер расходов*\n\nЧто хотите сделать?"):
    kb = main_kb()
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
            try: await update.callback_query.message.delete()
            except: pass
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_menu(update)

async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data.clear()
    await send_menu(update)

# ── Add expense / income ───────────────────────────────────────────────────────
async def cb_add_exp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["is_income"] = False
    rows = cat_kb(); rows.append([CANCEL])
    await update.callback_query.edit_message_text("📂 *Выберите категорию расхода:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return ADD_CAT

async def cb_add_inc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["is_income"] = True
    rows = cat_kb(); rows.append([CANCEL])
    await update.callback_query.edit_message_text("📂 *Выберите категорию дохода:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return ADD_CAT

async def cb_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["category"] = update.callback_query.data.removeprefix("cat_")
    await update.callback_query.edit_message_text(
        f"✅ *{CAT_NAME.get(ctx.user_data['category'], '')}*\n\n💵 Введите сумму:",
        reply_markup=InlineKeyboardMarkup([[CANCEL]]), parse_mode="Markdown")
    return ADD_AMOUNT

async def msg_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(raw)
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите число больше нуля, например `1500`", parse_mode="Markdown")
        return ADD_AMOUNT
    ctx.user_data["amount"] = amount
    await update.message.reply_text(
        f"💵 Сумма: *{amount:,.0f} ₽*\n\n💬 Комментарий или пропустите:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Пропустить", callback_data="skip")], [CANCEL]]),
        parse_mode="Markdown")
    return ADD_COMMENT

async def msg_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _finish_add(update, ctx, update.message.text.strip())
    return ConversationHandler.END

async def cb_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _finish_add(update, ctx, None)
    return ConversationHandler.END

async def _finish_add(update, ctx, comment):
    uid = update.effective_user.id
    cat = ctx.user_data["category"]
    amt = ctx.user_data["amount"]
    inc = ctx.user_data.get("is_income", False)
    save_expense(uid, cat, amt, comment, inc)
    icon = "💚" if inc else "💸"
    text = f"{icon} *{'Доход' if inc else 'Расход'} сохранён!*\n\n{CAT_NAME.get(cat,cat)}\n*{amt:,.0f} ₽*"
    if comment: text += f"\n💬 _{comment}_"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Ещё", callback_data="add_inc" if inc else "add_exp"),
        InlineKeyboardButton("🏠 Меню", callback_data="menu"),
    ]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    if not inc:
        await check_budget(uid, cat, ctx)

# ── Quick text input ───────────────────────────────────────────────────────────
async def msg_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    amount, cat, comment = parse_quick(update.message.text)
    if amount is None:
        await update.message.reply_text(
            "❓ Не понял. Быстрый ввод: `500 кофе` или `еда 1500 обед`\n\nИли /start для меню.",
            parse_mode="Markdown")
        return
    if cat is None:
        ctx.user_data["quick_amount"]  = amount
        ctx.user_data["quick_comment"] = comment
        rows = cat_kb("qcat_"); rows.append([CANCEL])
        await update.message.reply_text(
            f"*{amount:,.0f} ₽* — выберите категорию:",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
        return
    uid = update.effective_user.id
    save_expense(uid, cat, amount, comment)
    text = f"✅ {CAT_NAME.get(cat,cat)} · *{amount:,.0f} ₽*"
    if comment: text += f"\n💬 _{comment}_"
    await update.message.reply_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
        parse_mode="Markdown")
    await check_budget(uid, cat, ctx)

async def cb_qcat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    cat    = update.callback_query.data.removeprefix("qcat_")
    uid    = update.effective_user.id
    amount = ctx.user_data.get("quick_amount", 0)
    comm   = ctx.user_data.get("quick_comment")
    save_expense(uid, cat, amount, comm)
    text = f"✅ {CAT_NAME.get(cat,cat)} · *{amount:,.0f} ₽*"
    if comm: text += f"\n💬 _{comm}_"
    await update.callback_query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]]),
        parse_mode="Markdown")
    await check_budget(uid, cat, ctx)

# ── Delete last ────────────────────────────────────────────────────────────────
async def cb_del_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid = update.effective_user.id
    row = get_last_expense(uid)
    if not row:
        await update.callback_query.edit_message_text("Нет записей для удаления.", reply_markup=BACK)
        return
    eid, cat, amt, comm, inc, ca = row
    ctx.user_data["del_id"] = eid
    dt   = datetime.fromisoformat(ca).strftime("%d.%m %H:%M")
    icon = "💚" if inc else "💸"
    text = f"🗑 Удалить?\n\n{icon} {CAT_NAME.get(cat,cat)}\n*{amt:,.0f} ₽*"
    if comm: text += f"\n💬 {comm}"
    text += f"\n🕐 {dt}"
    await update.callback_query.edit_message_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Удалить", callback_data="del_ok"),
            InlineKeyboardButton("❌ Отмена",  callback_data="menu"),
        ]]))

async def cb_del_ok(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    eid = ctx.user_data.get("del_id")
    if eid: db("DELETE FROM expenses WHERE id=?", (eid,))
    await update.callback_query.edit_message_text("✅ Удалено.", reply_markup=BACK)

# ── Statistics ─────────────────────────────────────────────────────────────────
async def cb_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("📊 *Статистика* — выберите раздел:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Текущий месяц",   callback_data="stats_now")],
            [InlineKeyboardButton("🗓 Выбрать месяц",   callback_data="stats_pick")],
            [InlineKeyboardButton("📈 По дням",         callback_data="stats_days")],
            [InlineKeyboardButton("📉 Сравнить месяцы", callback_data="compare")],
            [InlineKeyboardButton("◀️ Назад",           callback_data="menu")],
        ]), parse_mode="Markdown")

async def cb_stats_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    now = datetime.now()
    await _month_stats(update, ctx, now.year, now.month)

async def cb_stats_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid    = update.effective_user.id
    months = available_months(uid)
    if not months:
        await update.callback_query.edit_message_text("Данных нет.", reply_markup=BACK)
        return
    rows = []; pair = []
    for m in months:
        y, mo = map(int, m.split("-"))
        pair.append(InlineKeyboardButton(f"{MONTH_SH[mo]} {y}", callback_data=f"sm_{m}"))
        if len(pair) == 3: rows.append(pair); pair = []
    if pair: rows.append(pair)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="stats")])
    await update.callback_query.edit_message_text("🗓 Выберите месяц:", reply_markup=InlineKeyboardMarkup(rows))

async def cb_stats_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    m = update.callback_query.data.removeprefix("sm_")
    y, mo = map(int, m.split("-"))
    await _month_stats(update, ctx, y, mo)

async def _month_stats(update, ctx, year, month):
    uid   = update.effective_user.id
    try: await update.callback_query.edit_message_text("⏳ Загружаю статистику...")
    except: pass
    stats = monthly_stats(uid, year, month)
    if not stats:
        try: await update.callback_query.edit_message_text(f"За {MONTH_RU[month]} {year} расходов нет.", reply_markup=BACK)
        except: pass
        return
    total      = sum(stats.values())
    now        = datetime.now()
    days_gone  = now.day if (now.year == year and now.month == month) else monthrange(year, month)[1]
    days_total = monthrange(year, month)[1]
    avg        = total / days_gone if days_gone else 0
    forecast   = avg * days_total
    pm, py     = (12, year - 1) if month == 1 else (month - 1, year)
    prev       = monthly_total(uid, py, pm)
    loop  = asyncio.get_event_loop()
    chart = await loop.run_in_executor(None, pie_chart, stats, f"{MONTH_RU[month]} {year}")
    cap = f"📊 *{MONTH_RU[month]} {year}*\n\n"
    for cat, amt in sorted(stats.items(), key=lambda x: -x[1]):
        cap += f"{CAT_NAME.get(cat,cat)}: *{amt:,.0f} ₽* ({amt/total*100:.1f}%)\n"
    cap += f"\n💸 *Итого: {total:,.0f} ₽*"
    cap += f"\n📆 В день: *{avg:,.0f} ₽*"
    if now.year == year and now.month == month and days_gone < days_total:
        cap += f"\n🔮 Прогноз: *{forecast:,.0f} ₽*"
    if prev > 0:
        d = (total - prev) / prev * 100
        cap += f"\n{'📈' if d>0 else '📉'} К прошлому: *{d:+.1f}%*"
    income = monthly_total(uid, year, month, is_income=True)
    if income > 0:
        cap += f"\n\n💚 Доходы: *{income:,.0f} ₽*\n⚖️ Баланс: *{income-total:+,.0f} ₽*"
    ym = f"{year:04d}-{month:02d}"
    bl = get_budget(uid, ym)
    if bl:
        cap += f"\n\n💰 Бюджет: {total:,.0f} / {bl:,.0f} ₽ ({total/bl*100:.0f}%)"
    top = top_expenses(uid, year, month)
    if top:
        cap += "\n\n🏆 *Топ трат:*\n"
        for cat, amt, comm in top:
            cap += f"  {CAT_NAME.get(cat,cat)} · {amt:,.0f} ₽"
            if comm: cap += f" — _{comm}_"
            cap += "\n"
    await ctx.bot.send_photo(update.callback_query.message.chat_id,
        photo=chart, caption=cap, reply_markup=BACK, parse_mode="Markdown")
    try: await update.callback_query.delete_message()
    except: pass

async def cb_stats_days(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid = update.effective_user.id
    now = datetime.now()
    try: await update.callback_query.edit_message_text("⏳ Строю график...")
    except: pass
    loop  = asyncio.get_event_loop()
    chart = await loop.run_in_executor(None, line_chart, uid, now.year, now.month)
    if not chart:
        await update.callback_query.edit_message_text("Данных за этот месяц нет.", reply_markup=BACK)
        return
    await ctx.bot.send_photo(update.callback_query.message.chat_id, photo=chart,
        caption=f"📈 *{MONTH_RU[now.month]} {now.year} — по дням*",
        reply_markup=BACK, parse_mode="Markdown")
    try: await update.callback_query.delete_message()
    except: pass

async def cb_compare(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid    = update.effective_user.id
    months = available_months(uid)
    if len(months) < 2:
        await update.callback_query.edit_message_text(
            "Нужны данные за *2+ месяца*.", reply_markup=BACK, parse_mode="Markdown")
        return
    try: await update.callback_query.edit_message_text("⏳ Строю сравнение...")
    except: pass
    md = {}
    for m in months[:4]:
        y, mo = map(int, m.split("-"))
        md[m] = monthly_stats(uid, y, mo)
    loop  = asyncio.get_event_loop()
    chart = await loop.run_in_executor(None, bar_chart, md)
    cap = "📅 *Сравнение по месяцам:*\n\n"
    for m, s in md.items():
        y, mo = map(int, m.split("-"))
        cap += f"*{MONTH_SH[mo]} {y}:*  {sum(s.values()):,.0f} ₽\n"
    await ctx.bot.send_photo(update.callback_query.message.chat_id,
        photo=chart, caption=cap, reply_markup=BACK, parse_mode="Markdown")
    try: await update.callback_query.delete_message()
    except: pass

# ── History ────────────────────────────────────────────────────────────────────
async def cb_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid  = update.effective_user.id
    rows = recent_expenses(uid)
    if not rows:
        await update.callback_query.edit_message_text("История пуста.", reply_markup=BACK)
        return
    text = "📋 *Последние 10 записей:*\n\n"
    for eid, cat, amt, comm, inc, ca in rows:
        dt   = datetime.fromisoformat(ca).strftime("%d.%m  %H:%M")
        icon = "💚" if inc else "💸"
        text += f"`{dt}`  {icon} {CAT_NAME.get(cat,cat)}\n   *{amt:,.0f} ₽*"
        if comm: text += f"  ·  _{comm}_"
        text += "\n\n"
    await update.callback_query.edit_message_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Удалить последнюю", callback_data="del_last")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu")],
        ]))

# ── Search ─────────────────────────────────────────────────────────────────────
async def cb_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "🔍 Введите слово из комментария или сумму:",
        reply_markup=InlineKeyboardMarkup([[CANCEL]]))
    return SEARCH

async def msg_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    rows = search_expenses(uid, update.message.text.strip())
    if not rows:
        await update.message.reply_text("❌ Ничего не найдено.", reply_markup=BACK)
        return ConversationHandler.END
    text = f"🔍 *Найдено ({len(rows)}):*\n\n"
    for _, cat, amt, comm, inc, ca in rows[:15]:
        dt   = datetime.fromisoformat(ca).strftime("%d.%m.%y")
        icon = "💚" if inc else "💸"
        text += f"`{dt}` {icon} {CAT_NAME.get(cat,cat)} · *{amt:,.0f} ₽*"
        if comm: text += f"\n   _{comm}_"
        text += "\n"
    await update.message.reply_text(text, reply_markup=BACK, parse_mode="Markdown")
    return ConversationHandler.END

# ── Budget ─────────────────────────────────────────────────────────────────────
async def cb_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid = update.effective_user.id
    now = datetime.now(); ym = now.strftime("%Y-%m")
    spent = monthly_total(uid, now.year, now.month)
    all_b = get_all_budgets(uid, ym)
    text  = f"💰 *Бюджет — {MONTH_RU[now.month]} {now.year}*\n\n"
    bl = all_b.get("total")
    if bl:
        pct = spent / bl * 100
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        text += f"Общий: {spent:,.0f} / {bl:,.0f} ₽\n`{bar}` {pct:.0f}%\n\n"
    else:
        text += "Общий лимит: не задан\n\n"
    for key, _ in CATEGORIES:
        lim = all_b.get(key[3:] if key[3:] in all_b else key)
        pass
    for cat_key in [k for _, k in CATEGORIES]:
        lim = all_b.get(cat_key)
        if lim:
            r = db("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND category=? AND is_income=0 AND strftime('%Y-%m',created_at)=?",
                   (uid, cat_key, ym), fetchone=True)
            cs = r[0] if r else 0
            text += f"{CAT_NAME.get(cat_key,cat_key)}: {cs:,.0f} / {lim:,.0f} ₽ ({cs/lim*100:.0f}%)\n"
    await update.callback_query.edit_message_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Общий лимит",   callback_data="bud_total")],
            [InlineKeyboardButton("📂 По категории",  callback_data="bud_cat")],
            [InlineKeyboardButton("◀️ Назад",         callback_data="menu")],
        ]))

async def cb_bud_total(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["bud_cat"] = None
    await update.callback_query.edit_message_text(
        "💰 Введите *общий лимит расходов* на этот месяц (₽):",
        reply_markup=InlineKeyboardMarkup([[CANCEL]]), parse_mode="Markdown")
    return BUD_AMOUNT

async def cb_bud_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    rows = cat_kb("bc_"); rows.append([CANCEL])
    await update.callback_query.edit_message_text("📂 Выберите категорию:",
        reply_markup=InlineKeyboardMarkup(rows))
    return BUD_CAT

async def cb_bud_cat_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["bud_cat"] = update.callback_query.data.removeprefix("bc_")
    name = CAT_NAME.get(ctx.user_data["bud_cat"], "")
    await update.callback_query.edit_message_text(
        f"💰 Введите лимит для *{name}* (₽):",
        reply_markup=InlineKeyboardMarkup([[CANCEL]]), parse_mode="Markdown")
    return BUD_AMOUNT

async def msg_bud_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amt = float(raw)
        if amt <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите число больше нуля.")
        return BUD_AMOUNT
    uid = update.effective_user.id
    cat = ctx.user_data.get("bud_cat")
    set_budget(uid, datetime.now().strftime("%Y-%m"), amt, cat)
    name = CAT_NAME.get(cat, cat) if cat else "общий"
    await update.message.reply_text(f"✅ Лимит *{name}* — {amt:,.0f} ₽ установлен.",
        reply_markup=BACK, parse_mode="Markdown")
    return ConversationHandler.END

# ── Recurring ──────────────────────────────────────────────────────────────────
async def cb_recurring(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid  = update.effective_user.id
    recs = get_recurring(uid)
    text = "🔄 *Регулярные траты*\n\n"
    if recs:
        for rid, cat, amt, comm, day in recs:
            text += f"• {day}-го числа — {CAT_NAME.get(cat,cat)}, *{amt:,.0f} ₽*"
            if comm: text += f" _{comm}_"
            text += "\n"
    else:
        text += "Нет регулярных трат.\n"
    text += "\nДобавляются автоматически каждый месяц."
    rows = []
    for rid, cat, amt, comm, day in recs:
        rows.append([InlineKeyboardButton(
            f"🗑 {CAT_NAME.get(cat,cat)} {amt:,.0f}₽ ({day}-го)",
            callback_data=f"rdel_{rid}")])
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data="rec_add")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu")])
    await update.callback_query.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")

async def cb_rec_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    db("DELETE FROM recurring WHERE id=?",
       (int(update.callback_query.data.removeprefix("rdel_")),))
    await cb_recurring(update, ctx)

async def cb_rec_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    rows = cat_kb("rc_"); rows.append([CANCEL])
    await update.callback_query.edit_message_text("📂 Выберите категорию:",
        reply_markup=InlineKeyboardMarkup(rows))
    return REC_CAT

async def cb_rec_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["rec_cat"] = update.callback_query.data.removeprefix("rc_")
    await update.callback_query.edit_message_text(
        f"✅ *{CAT_NAME.get(ctx.user_data['rec_cat'],'')}*\n\nВведите сумму:",
        reply_markup=InlineKeyboardMarkup([[CANCEL]]), parse_mode="Markdown")
    return REC_AMOUNT

async def msg_rec_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amt = float(raw)
        if amt <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите число больше нуля.")
        return REC_AMOUNT
    ctx.user_data["rec_amt"] = amt
    await update.message.reply_text(f"*{amt:,.0f} ₽*\n\nКакого числа каждого месяца? (1–28):",
        reply_markup=InlineKeyboardMarkup([[CANCEL]]), parse_mode="Markdown")
    return REC_DAY

async def msg_rec_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        day = int(update.message.text.strip())
        if not (1 <= day <= 28): raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите число от 1 до 28.")
        return REC_DAY
    ctx.user_data["rec_day"] = day
    await update.message.reply_text(f"📅 Каждое *{day}-е* число\n\n💬 Комментарий или пропустите:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Пропустить", callback_data="rec_skip")], [CANCEL]]),
        parse_mode="Markdown")
    return REC_COMMENT

async def msg_rec_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _save_recurring(update, ctx, update.message.text.strip())
    return ConversationHandler.END

async def cb_rec_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await _save_recurring(update, ctx, None)
    return ConversationHandler.END

async def _save_recurring(update, ctx, comment):
    uid = update.effective_user.id
    db("INSERT INTO recurring(user_id,category,amount,comment,day_of_month) VALUES(?,?,?,?,?)",
       (uid, ctx.user_data["rec_cat"], ctx.user_data["rec_amt"], comment, ctx.user_data["rec_day"]))
    name = CAT_NAME.get(ctx.user_data["rec_cat"], "")
    text = f"✅ *Регулярная трата сохранена!*\n\n{name} · {ctx.user_data['rec_amt']:,.0f} ₽\nКаждое {ctx.user_data['rec_day']}-е число"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ── Export ─────────────────────────────────────────────────────────────────────
async def cb_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid = update.effective_user.id
    buf = export_csv(uid)
    now = datetime.now().strftime("%Y%m%d")
    await ctx.bot.send_document(
        update.callback_query.message.chat_id,
        document=buf,
        filename=f"expenses_{now}.csv",
        caption="📤 Все записи (CSV — откройте в Excel или Google Sheets)",
    )

# ── Notifications ──────────────────────────────────────────────────────────────
async def cb_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid     = update.effective_user.id
    enabled = toggle_notify(uid)
    status  = "включены ✅" if enabled else "выключены ❌"
    await update.callback_query.edit_message_text(
        f"🔔 Ежедневные уведомления в 21:00 — *{status}*\n\nКаждый вечер бот пришлёт итог дня.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Переключить", callback_data="notify")],
            [InlineKeyboardButton("◀️ Назад",       callback_data="menu")],
        ]), parse_mode="Markdown")

# ── Jobs ───────────────────────────────────────────────────────────────────────
async def job_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    uids = db("SELECT user_id FROM settings WHERE notify_enabled=1", fetchall=True)
    now  = datetime.now()
    for (uid,) in uids:
        r = db("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE user_id=? AND date(created_at)=? AND is_income=0",
               (uid, now.date().isoformat()), fetchone=True)
        total = r[0] if r else 0
        try:
            await ctx.bot.send_message(uid,
                f"🕘 *Вечерний итог*\n\nСегодня потрачено: *{total:,.0f} ₽*\n\nЗапишите если что-то пропустили!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Добавить расход", callback_data="add_exp")]]),
                parse_mode="Markdown")
        except: pass

async def job_recurring(ctx: ContextTypes.DEFAULT_TYPE):
    today = datetime.now()
    ym    = today.strftime("%Y-%m")
    rows  = db("SELECT id,user_id,category,amount,comment FROM recurring WHERE day_of_month=? AND (last_date IS NULL OR last_date < ?)",
               (today.day, ym), fetchall=True)
    for rid, uid, cat, amt, comm in rows:
        save_expense(uid, cat, amt, comm or "Регулярный платёж")
        db("UPDATE recurring SET last_date=? WHERE id=?", (ym, rid))
        try:
            await ctx.bot.send_message(uid,
                f"🔄 *Регулярная трата добавлена*\n\n{CAT_NAME.get(cat,cat)} · {amt:,.0f} ₽",
                parse_mode="Markdown")
        except: pass

async def job_month_report(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    if now.day != monthrange(now.year, now.month)[1]:
        return
    uids = db("SELECT DISTINCT user_id FROM expenses", fetchall=True)
    for (uid,) in uids:
        stats = monthly_stats(uid, now.year, now.month)
        if not stats: continue
        total  = sum(stats.values())
        income = monthly_total(uid, now.year, now.month, is_income=True)
        chart  = pie_chart(stats, f"Итоги {MONTH_RU[now.month]} {now.year}")
        cap = f"📊 *Итоги {MONTH_RU[now.month]} {now.year}*\n\n"
        for cat, amt in sorted(stats.items(), key=lambda x: -x[1]):
            cap += f"{CAT_NAME.get(cat,cat)}: *{amt:,.0f} ₽*\n"
        cap += f"\n💸 Расходы: *{total:,.0f} ₽*"
        if income > 0:
            cap += f"\n💚 Доходы: *{income:,.0f} ₽*\n⚖️ Баланс: *{income-total:+,.0f} ₽*"
        try: await ctx.bot.send_photo(uid, photo=chart, caption=cap, parse_mode="Markdown")
        except: pass

# ── Cancel ─────────────────────────────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await send_menu(update)
    return ConversationHandler.END

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(10).read_timeout(10)
        .write_timeout(30).pool_timeout(10)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_add_exp,      pattern="^add_exp$"),
            CallbackQueryHandler(cb_add_inc,      pattern="^add_inc$"),
            CallbackQueryHandler(cb_bud_total,    pattern="^bud_total$"),
            CallbackQueryHandler(cb_bud_cat,      pattern="^bud_cat$"),
            CallbackQueryHandler(cb_rec_add,      pattern="^rec_add$"),
            CallbackQueryHandler(cb_search,       pattern="^search$"),
        ],
        states={
            ADD_CAT:    [CallbackQueryHandler(cb_cat,           pattern="^cat_")],
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_amount)],
            ADD_COMMENT:[
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_comment),
                CallbackQueryHandler(cb_skip, pattern="^skip$"),
            ],
            BUD_CAT:    [CallbackQueryHandler(cb_bud_cat_chosen, pattern="^bc_")],
            BUD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_bud_amount)],
            REC_CAT:    [CallbackQueryHandler(cb_rec_cat,       pattern="^rc_")],
            REC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_rec_amount)],
            REC_DAY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_rec_day)],
            REC_COMMENT:[
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_rec_comment),
                CallbackQueryHandler(cb_rec_skip, pattern="^rec_skip$"),
            ],
            SEARCH:     [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_search)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel, pattern="^menu$"),
            CommandHandler("cancel", cancel),
        ],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_menu,        pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(cb_stats,       pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(cb_stats_now,   pattern="^stats_now$"))
    app.add_handler(CallbackQueryHandler(cb_stats_pick,  pattern="^stats_pick$"))
    app.add_handler(CallbackQueryHandler(cb_stats_month, pattern="^sm_"))
    app.add_handler(CallbackQueryHandler(cb_stats_days,  pattern="^stats_days$"))
    app.add_handler(CallbackQueryHandler(cb_compare,     pattern="^compare$"))
    app.add_handler(CallbackQueryHandler(cb_history,     pattern="^history$"))
    app.add_handler(CallbackQueryHandler(cb_del_last,    pattern="^del_last$"))
    app.add_handler(CallbackQueryHandler(cb_del_ok,      pattern="^del_ok$"))
    app.add_handler(CallbackQueryHandler(cb_budget,      pattern="^budget$"))
    app.add_handler(CallbackQueryHandler(cb_recurring,   pattern="^recurring$"))
    app.add_handler(CallbackQueryHandler(cb_rec_del,     pattern="^rdel_"))
    app.add_handler(CallbackQueryHandler(cb_export,      pattern="^export$"))
    app.add_handler(CallbackQueryHandler(cb_notify,      pattern="^notify$"))
    app.add_handler(CallbackQueryHandler(cb_qcat,        pattern="^qcat_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_quick))

    jq = app.job_queue
    jq.run_daily(job_reminder,     time=dt_time(18, 0))  # 21:00 МСК
    jq.run_daily(job_recurring,    time=dt_time(6,  0))  # 09:00 МСК
    jq.run_daily(job_month_report, time=dt_time(20, 0))  # 23:00 МСК

    print("✅ Бот запущен...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
