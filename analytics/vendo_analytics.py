#!/usr/bin/env python3
"""
Аналитика платежей Vendotek — одной командой.

    python3 vendo_analytics.py

Считает выручку, средний чек, отказы, разрезы по автоматам, городам,
товарам и картам. Только чтение.
"""
import os, re, ssl, sys, socket, getpass, subprocess, textwrap
from datetime import datetime

SSH_HOST = os.environ.get("SSH_HOST", "18.196.28.193")
SSH_USER = os.environ.get("SSH_USER", "ec2-user")
SSH_KEY  = os.environ.get("SSH_KEY") or os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/keys/VendoAI/timurAI")
RDS_HOST = os.environ.get("RDS_HOST", "vendo-app.cvwxd0jliglu.eu-central-1.rds.amazonaws.com")
RDS_PORT = int(os.environ.get("RDS_PORT", 5432))
LOCAL_PORT = int(os.environ.get("LOCAL_PORT", 54322))
PGUSER = os.environ.get("PGUSER", "vendo_ai_reader")
PGDATABASE = os.environ.get("PGDATABASE", "app")

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "vendo_analytics_report.txt")

PAY_RE = re.compile(r"pay|transact|invoice|charge|order|purchase|subscript|billing|"
                    r"refund|checkout|tariff|price|balance|wallet|receipt|deal", re.I)
AMOUNT_RE = re.compile(r"amount|sum|total|price|cost|value|revenue", re.I)
STATUS_RE = re.compile(r"status|state|result|type|method|provider|currency", re.I)
TS_RE = re.compile(r"created|updated|paid|date|time|at$", re.I)

out_lines = []
def say(msg=""):
    print(msg)
    out_lines.append(msg)

def head(title):
    say(); say("=" * 78); say(title); say("=" * 78)

# ── шаг 1: зависимости ────────────────────────────────────────────────────────
def ensure_psycopg():
    try:
        import psycopg  # noqa
        return "psycopg"
    except ImportError:
        pass
    try:
        import psycopg2  # noqa
        return "psycopg2"
    except ImportError:
        pass
    print("Ставлю psycopg (без sudo, в домашнюю папку)…")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--user", "--quiet",
                          "psycopg[binary]"])
    if rc != 0:
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "--user", "--quiet",
                              "--break-system-packages", "psycopg[binary]"])
    if rc != 0:
        sys.exit("Не удалось поставить psycopg. Покажите этот вывод ассистенту.")
    import importlib, site
    importlib.reload(site)
    return "psycopg"

# ── шаг 2: туннель ────────────────────────────────────────────────────────────
def port_open(port):
    s = socket.socket(); s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port)); return True
    except OSError:
        return False
    finally:
        s.close()

def open_tunnel():
    if port_open(LOCAL_PORT):
        print(f"Туннель уже поднят на 127.0.0.1:{LOCAL_PORT}")
        return
    if not os.path.exists(SSH_KEY):
        sys.exit(f"Не нахожу SSH-ключ: {SSH_KEY}\n"
                 "Если он в iCloud — откройте папку в Finder и дождитесь загрузки,\n"
                 "либо задайте путь: SSH_KEY=/путь/к/ключу python3 vendo_payments.py")
    mode = oct(os.stat(SSH_KEY).st_mode)[-3:]
    if mode not in ("600", "400"):
        os.chmod(SSH_KEY, 0o600)
        print(f"Права на ключ были {mode} — поправил на 600")
    print(f"Поднимаю туннель через {SSH_USER}@{SSH_HOST} …")
    cmd = ["ssh", "-f", "-N",
           "-o", "ExitOnForwardFailure=yes",
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=15",
           "-o", "ServerAliveInterval=30",
           "-i", SSH_KEY,
           "-L", f"{LOCAL_PORT}:{RDS_HOST}:{RDS_PORT}",
           f"{SSH_USER}@{SSH_HOST}"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not port_open(LOCAL_PORT):
        sys.exit("Туннель не поднялся.\n"
                 f"ssh сказал: {(p.stderr or p.stdout).strip()}\n"
                 "Покажите это ассистенту.")
    print(f"Туннель поднят: 127.0.0.1:{LOCAL_PORT} -> {RDS_HOST}:{RDS_PORT}")

def close_tunnel():
    subprocess.call(["pkill", "-f", f"L {LOCAL_PORT}:{RDS_HOST}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ── шаг 3: подключение ────────────────────────────────────────────────────────
def get_password():
    pw = os.environ.get("PGPASSWORD")
    if pw:
        return pw
    envfile = os.path.join(HERE, ".env")
    if os.path.exists(envfile):
        for line in open(envfile, encoding="utf-8"):
            if line.strip().startswith("PGPASSWORD"):
                return line.split("=", 1)[1].strip().strip("'\"")
    return getpass.getpass("Пароль пользователя vendo_ai_reader: ")

def connect(driver, password, dbname=None):
    kw = dict(host="127.0.0.1", port=LOCAL_PORT, user=PGUSER,
              password=password, dbname=dbname or PGDATABASE, connect_timeout=15)
    if driver == "psycopg":
        import psycopg
        return psycopg.connect(**kw)
    import psycopg2
    kw["database"] = kw.pop("dbname")
    return psycopg2.connect(**kw)

def q(cur, sql, args=None, limit=None):
    """Безопасный SELECT: ошибка не роняет отчёт."""
    try:
        if args:
            cur.execute(sql, args)
        else:
            cur.execute(sql)          # без args, иначе '%' в литералах ломает парсер
        rows = cur.fetchall()
        return rows[:limit] if limit else rows
    except Exception as e:
        cur.connection.rollback()
        say(f"  [!] запрос не выполнился: {str(e).splitlines()[0]}")
        return []

def table(cur, sql, args=None, maxw=42):
    rows = q(cur, sql, args)
    if not rows:
        say("  (пусто)"); return rows
    cols = [d[0] for d in cur.description]
    def cell(v):
        s = "NULL" if v is None else str(v)
        return s if len(s) <= maxw else s[:maxw - 1] + "…"
    widths = [max(len(c), *(len(cell(r[i])) for r in rows)) for i, c in enumerate(cols)]
    say("  " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    say("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        say("  " + " | ".join(cell(r[i]).ljust(widths[i]) for i in range(len(cols))))
    return rows


# ── аналитика ─────────────────────────────────────────────────────────────────
# Факт-таблица: сумма платежа = наличные + безнал, время операции = pos_localtime_at
P = """
with p as (
  select id, vend_id, approved,
         pos_localtime_at as ts,
         coalesce(cash_amount, 0)     as cash,
         coalesce(cashless_amount, 0) as cashless,
         coalesce(cash_amount, 0) + coalesce(cashless_amount, 0) as amount
  from vendotek_payment
)
"""

QUERIES = [
("1. ОБЩАЯ СВОДКА", P + """
select count(*) as payments,
       min(ts)::date as first_day, max(ts)::date as last_day,
       count(*) filter (where approved) as approved,
       round(100.0 * count(*) filter (where approved) / nullif(count(*),0), 2) as approved_pct,
       round(sum(amount) filter (where approved), 2) as revenue_aed,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check,
       round(percentile_cont(0.5) within group (order by amount)
             filter (where approved and amount > 0)::numeric, 2) as median_check
from p"""),

("2. ПО МЕСЯЦАМ (только успешные)", P + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) as payments,
       count(*) filter (where approved) as ok,
       round(100.0 * count(*) filter (where approved) / nullif(count(*),0), 1) as ok_pct,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(sum(cash) filter (where approved), 2) as cash,
       round(sum(cashless) filter (where approved), 2) as cashless,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check
from p group by 1 order by 1"""),

("3. НАЛИЧНЫЕ vs БЕЗНАЛ (успешные, сумма > 0)", P + """
select case when cash > 0 and cashless > 0 then 'смешанный'
            when cashless > 0 then 'безнал'
            when cash > 0 then 'наличные'
            else 'нулевая сумма' end as kind,
       count(*) as payments,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as share_pct,
       round(sum(amount), 2) as revenue,
       round(avg(amount), 2) as avg_check
from p where approved group by 1 order by 4 desc nulls last"""),

("4. ОТКАЗЫ ПО МЕСЯЦАМ", P + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) filter (where not approved) as declined,
       count(*) as total,
       round(100.0 * count(*) filter (where not approved) / nullif(count(*),0), 2) as decline_pct
from p group by 1 order by 1"""),

("5. КОДЫ ОТВЕТА БАНКА ПРИ ОТКАЗЕ", """
select coalesce(nullif(c.response_code, ''), '(пусто)') as response_code,
       count(*) as cnt
from vendotek_payment_cashless c
join vendotek_payment p on p.id = c.payment_id
where not p.approved
group by 1 order by 2 desc limit 15"""),

("6. ПО ОРГАНИЗАЦИЯМ", P + """
select coalesce(nullif(v.organization_name, ''), '(пусто)') as org,
       count(*) as payments,
       round(sum(p.amount), 2) as revenue,
       round(avg(p.amount) filter (where p.amount > 0), 2) as avg_check
from p join vendotek_vend v on v.id = p.vend_id
where p.approved group by 1 order by 3 desc nulls last limit 20"""),

("7. ПО ГОРОДАМ", P + """
select coalesce(nullif(u.city, ''), '(не указан)') as city,
       coalesce(nullif(u.region, ''), '') as region,
       count(distinct u.id) as units,
       count(*) as payments,
       round(sum(p.amount), 2) as revenue
from p
join vendotek_vend v on v.id = p.vend_id
join vendotek_unit u on u.id = v.unit_id
where p.approved group by 1, 2 order by 5 desc nulls last limit 25"""),

("8. ТОП-25 АВТОМАТОВ ПО ВЫРУЧКЕ", P + """
select u.sn, coalesce(nullif(u.location_name, ''), '(без названия)') as location,
       coalesce(nullif(u.city, ''), '') as city,
       count(*) as payments,
       round(sum(p.amount), 2) as revenue,
       round(avg(p.amount) filter (where p.amount > 0), 2) as avg_check
from p
join vendotek_vend v on v.id = p.vend_id
join vendotek_unit u on u.id = v.unit_id
where p.approved group by 1, 2, 3 order by 5 desc nulls last limit 25"""),

("9. АВТОМАТЫ БЕЗ ПРОДАЖ ЗА 30 ДНЕЙ", P + """
select count(*) as silent_units
from vendotek_unit u
where not exists (
  select 1 from vendotek_vend v join p on p.vend_id = v.id
  where v.unit_id = u.id
    and p.ts > (select max(ts) from p) - interval '30 days')"""),

("10. ТОП-25 ТОВАРОВ", P + """
select coalesce(nullif(v.product_name, ''), '(без названия)') as product,
       v.product_id,
       count(*) as sales,
       round(sum(p.amount), 2) as revenue,
       round(avg(p.amount) filter (where p.amount > 0), 2) as avg_price
from p join vendotek_vend v on v.id = p.vend_id
where p.approved group by 1, 2 order by 4 desc nulls last limit 25"""),

("11. ТИПЫ КАРТ", """
select coalesce(nullif(c.application_label, ''), '(пусто)') as card,
       count(*) as cnt,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as share_pct,
       round(sum(c.amount), 2) as revenue
from vendotek_payment_cashless c
join vendotek_payment p on p.id = c.payment_id
where p.approved group by 1 order by 2 desc limit 15"""),

("12. СПОСОБ СЧИТЫВАНИЯ КАРТЫ", """
select coalesce(nullif(c.pos_entry_mode, ''), '(пусто)') as entry_mode,
       count(*) as cnt,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as share_pct
from vendotek_payment_cashless c
join vendotek_payment p on p.id = c.payment_id
where p.approved group by 1 order by 2 desc limit 10"""),

("13. РАСПРЕДЕЛЕНИЕ ЧЕКА (успешные)", P + """
select case when amount = 0 then 'a. ноль'
            when amount < 0.10 then 'b. < 0.10 (тестовые?)'
            when amount < 1 then 'c. 0.10 - 1'
            when amount < 5 then 'd. 1 - 5'
            when amount < 10 then 'e. 5 - 10'
            when amount < 20 then 'f. 10 - 20'
            when amount < 50 then 'g. 20 - 50'
            else 'h. 50+' end as bucket,
       count(*) as payments,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as share_pct,
       round(sum(amount), 2) as revenue
from p where approved group by 1 order by 1"""),

("14. ПО ЧАСАМ СУТОК (успешные)", P + """
select extract(hour from ts)::int as hour,
       count(*) as payments,
       round(sum(amount), 2) as revenue
from p where approved group by 1 order by 1"""),

("15. ПО ДНЯМ НЕДЕЛИ (успешные)", P + """
select to_char(ts, 'ID') as dow_num,
       to_char(ts, 'Dy') as dow,
       count(*) as payments,
       round(sum(amount), 2) as revenue
from p where approved group by 1, 2 order by 1"""),

("16. ОТМЕНЁННЫЕ И НЕЗАВЕРШЁННЫЕ ВЫДАЧИ", """
select count(*) as vends,
       count(*) filter (where completed) as completed,
       count(*) filter (where cancelled) as cancelled,
       round(100.0 * count(*) filter (where cancelled) / nullif(count(*),0), 2) as cancelled_pct,
       count(*) filter (where not completed and not cancelled) as hanging
from vendotek_vend"""),

("17. ДЛИТЕЛЬНОСТЬ ОПЕРАЦИИ (безнал, секунды)", """
select round(avg(c.transaction_duration_s), 1) as avg_txn_s,
       max(c.transaction_duration_s) as max_txn_s,
       round(avg(c.vend_duration_s), 1) as avg_vend_s,
       max(c.vend_duration_s) as max_vend_s
from vendotek_payment_cashless c
join vendotek_payment p on p.id = c.payment_id
where p.approved"""),

("18. ВАЛЮТЫ И ПАРК АВТОМАТОВ", """
select (select count(*) from vendotek_unit) as units_total,
       (select count(distinct unit_id) from vendotek_vend) as units_with_sales,
       (select count(*) from vendotek_org) as orgs,
       (select string_agg(distinct currency, ', ') from vendotek_vend) as currencies"""),
]

def run_analytics(cur):
    for title, sql in QUERIES:
        head(title)
        table(cur, sql, maxw=34)

def main():
    driver = ensure_psycopg()
    open_tunnel()
    pw = get_password()
    try:
        conn = connect(driver, pw, PGDATABASE)
    except Exception as e:
        sys.exit(f"Не подключился к базе '{PGDATABASE}': {str(e).splitlines()[0]}")
    conn.autocommit = True
    cur = conn.cursor()
    say(f"Аналитика платежей Vendotek — {datetime.now():%Y-%m-%d %H:%M}")
    say(f"База: {PGDATABASE}")
    run_analytics(cur)
    cur.close(); conn.close()
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print("\n" + "=" * 78)
    print(f"Готово. Отчёт: {REPORT}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
