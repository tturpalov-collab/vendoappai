#!/usr/bin/env python3
"""
Сводный отчёт по одной организации за последние месяцы.

    python3 vendo_org_report.py              # tazizi, 3 месяца
    ORG=upay-general-trading python3 vendo_org_report.py
    ORG=tazizi MONTHS=6 python3 vendo_org_report.py

Только чтение.
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
ORG = os.environ.get("ORG", "tazizi")
MONTHS = int(os.environ.get("MONTHS", 3))
REPORT = os.path.join(HERE, f"vendo_org_{ORG}_report.txt")

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



# Промахи по чужой карте (MiFare) — не отказ в оплате, исключаем из всех метрик отказов.
MISTAP = ("coalesce(c.pos_entry_mode,'') = 'CONTACTLESS' "
          "and coalesce(c.aid,'') = '' and coalesce(c.pan,'') = ''")
TECH = "('', 'CE', 'TO', '907', '909', '911')"

BASE = f"""
with bounds as (
  select date_trunc('month', max(pos_localtime_at))
         - interval '{MONTHS - 1} months' as lo,
         max(pos_localtime_at) as hi
  from vendotek_payment
),
p as (
  select p.id, p.vend_id, p.approved, p.pos_localtime_at as ts,
         coalesce(p.cash_amount,0) as cash,
         coalesce(p.cashless_amount,0) as cashless,
         coalesce(p.cash_amount,0) + coalesce(p.cashless_amount,0) as amount
  from vendotek_payment p, bounds b
  where p.pos_localtime_at >= b.lo
),
f as (
  select p.*, v.unit_id, v.organization_name as org, v.cancelled,
         nullif(v.product_name,'') as product,
         u.sn, coalesce(nullif(u.location_name,''), '—') as loc,
         coalesce(c.response_code,'') as code,
         coalesce(c.response_code,'') in {TECH} as is_tech,
         nullif(c.application_label,'') as card,
         c.transaction_duration_s as txn_s, c.vend_duration_s as vend_s,
         ({MISTAP}) as mistap
  from p
  join vendotek_vend v on v.id = p.vend_id
  join vendotek_unit u on u.id = v.unit_id
  left join vendotek_payment_cashless c on c.payment_id = p.id
),
fc as (select * from f where org = %(org)s and not mistap)
"""

QUERIES = [

("1. СВОДКА ПО МЕСЯЦАМ", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(distinct sn) as terminals,
       count(*) as attempts,
       count(*) filter (where approved) as approved,
       count(*) filter (where not approved) as declined,
       round(100.0 * count(*) filter (where approved) / count(*), 2) as approve_pct,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check,
       round(percentile_cont(0.5) within group (order by amount)
             filter (where approved and amount > 0)::numeric, 2) as median_check
from fc group by 1 order by 1"""),

("2. ОШИБКИ ПО ТИПАМ И МЕСЯЦАМ", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) filter (where not approved) as declined_total,
       count(*) filter (where not approved and code = 'CE') as ce,
       count(*) filter (where not approved and code = '') as no_answer,
       count(*) filter (where not approved and code = 'TO') as timeout,
       count(*) filter (where not approved and code in ('907','909','911')) as host_down,
       count(*) filter (where not approved and code = '116') as no_funds,
       count(*) filter (where not approved and not is_tech and code <> '116') as other_bank
from fc group by 1 order by 1"""),

("3. ДИНАМИКА ОШИБОК: НА СКОЛЬКО СНИЗИЛИСЬ", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) as attempts,
       count(*) filter (where not approved) as declined,
       round(100.0 * count(*) filter (where not approved) / count(*), 2) as decline_pct,
       count(*) filter (where not approved and is_tech) as tech,
       round(100.0 * count(*) filter (where not approved and is_tech) / count(*), 2) as tech_pct,
       round(1000.0 * count(*) filter (where not approved) / count(*), 1) as declined_per_1000
from fc group by 1 order by 1"""),

("4. ТОП-5 ТЕРМИНАЛОВ ПО ВЫРУЧКЕ ЗА ПЕРИОД", BASE + """
select sn, loc,
       count(*) filter (where approved) as payments,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_payment,
       round(percentile_cont(0.5) within group (order by amount)
             filter (where approved and amount > 0)::numeric, 2) as median_payment,
       round(100.0 * count(*) filter (where not approved) / count(*), 2) as decline_pct,
       round(count(*) filter (where approved)
             / greatest(count(distinct ts::date), 1)::numeric, 1) as payments_per_day
from fc group by 1, 2 order by 4 desc nulls last limit 5"""),

("5. ТОП-5 ПО МЕСЯЦАМ — КАК МЕНЯЛИСЬ ЛИДЕРЫ", BASE + """
select month, sn, loc, payments, revenue, avg_payment from (
  select to_char(date_trunc('month', ts), 'YYYY-MM') as month, sn, loc,
         count(*) filter (where approved) as payments,
         round(sum(amount) filter (where approved), 0) as revenue,
         round(avg(amount) filter (where approved and amount > 0), 2) as avg_payment,
         row_number() over (partition by date_trunc('month', ts)
                            order by sum(amount) filter (where approved) desc nulls last) as rn
  from fc group by 1, 2, 3, date_trunc('month', ts)
) t where rn <= 5 order by month, revenue desc"""),

("6. ВСЕ ТЕРМИНАЛЫ ЗА ПЕРИОД", BASE + """
select sn, loc,
       count(*) filter (where approved) as payments,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_payment,
       round(100.0 * count(*) filter (where not approved) / count(*), 2) as decline_pct,
       min(ts)::date as first_sale, max(ts)::date as last_sale
from fc group by 1, 2 order by 4 desc nulls last"""),

("7. КОНЦЕНТРАЦИЯ ВЫРУЧКИ", BASE + """
select count(*) as terminals,
       round(sum(revenue), 0) as total_revenue,
       round(100.0 * sum(revenue) filter (where rn <= 5) / sum(revenue), 1) as top5_share_pct,
       round(100.0 * sum(revenue) filter (where rn <= 10) / sum(revenue), 1) as top10_share_pct,
       round(avg(revenue), 0) as avg_per_terminal,
       round(percentile_cont(0.5) within group (order by revenue)::numeric, 0) as median_per_terminal
from (
  select sn, sum(amount) filter (where approved) as revenue,
         row_number() over (order by sum(amount) filter (where approved) desc nulls last) as rn
  from fc group by 1
) t"""),

("8. НАЛИЧНЫЕ И БЕЗНАЛ ПО МЕСЯЦАМ", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) filter (where approved and cashless > 0) as cashless_cnt,
       round(sum(cashless) filter (where approved), 2) as cashless_sum,
       count(*) filter (where approved and cash > 0) as cash_cnt,
       round(sum(cash) filter (where approved), 2) as cash_sum,
       round(100.0 * sum(cashless) filter (where approved)
             / nullif(sum(amount) filter (where approved), 0), 1) as cashless_share_pct
from fc group by 1 order by 1"""),

("9. ПАРК ПО МЕСЯЦАМ: АКТИВНЫЕ, ПЕРВЫЕ ПРОДАЖИ, ЗАМОЛЧАВШИЕ", BASE + """
select to_char(mm.m, 'YYYY-MM') as month,
       count(*) as active_terminals,
       count(*) filter (where mm.m = agg.first_m) as first_month_selling,
       count(*) filter (where mm.m = agg.last_m
                        and agg.last_m < (select max(date_trunc('month', ts)) from fc)) as went_silent
from (select sn, date_trunc('month', ts) as m from fc group by 1, 2) mm
join (select sn, min(date_trunc('month', ts)) as first_m,
             max(date_trunc('month', ts)) as last_m from fc group by 1) agg on agg.sn = mm.sn
group by mm.m order by mm.m"""),

("10. ПРОФИЛЬ ПО ЧАСАМ", BASE + """
select extract(hour from ts)::int as hour,
       count(*) filter (where approved) as payments,
       round(sum(amount) filter (where approved), 0) as revenue,
       round(100.0 * sum(amount) filter (where approved)
             / sum(sum(amount) filter (where approved)) over (), 1) as share_pct
from fc group by 1 order by 1"""),

("11. ПРОФИЛЬ ПО ДНЯМ НЕДЕЛИ", BASE + """
select to_char(ts, 'ID') as dow_num, to_char(ts, 'Dy') as dow,
       count(*) filter (where approved) as payments,
       round(sum(amount) filter (where approved), 0) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check
from fc group by 1, 2 order by 1"""),

("12. РАСПРЕДЕЛЕНИЕ ЧЕКА", BASE + """
select case when amount < 1 then 'a. < 1'
            when amount < 5 then 'b. 1-5'
            when amount < 10 then 'c. 5-10'
            when amount < 20 then 'd. 10-20'
            when amount < 50 then 'e. 20-50'
            else 'f. 50+' end as bucket,
       count(*) as payments,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as share_pct,
       round(sum(amount), 0) as revenue
from fc where approved and amount > 0 group by 1 order by 1"""),

("13. ТИПЫ КАРТ", BASE + """
select coalesce(card, '(нет данных)') as card,
       count(*) as payments,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as share_pct,
       round(sum(amount), 0) as revenue,
       round(avg(amount) filter (where amount > 0), 2) as avg_check
from fc where approved group by 1 order by 2 desc limit 12"""),

("14. ТОП-10 ТОВАРОВ", BASE + """
select coalesce(product, '(без названия)') as product,
       count(*) filter (where approved) as sales,
       round(sum(amount) filter (where approved), 0) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_price
from fc group by 1 order by 3 desc nulls last limit 10"""),

("15. ЛУЧШИЕ И ХУДШИЕ ДНИ", BASE + """
(select 'лучший' as kind, ts::date as day,
        count(*) filter (where approved) as payments,
        round(sum(amount) filter (where approved), 0) as revenue
 from fc group by 2 order by 4 desc limit 5)
union all
(select 'худший', ts::date,
        count(*) filter (where approved),
        round(sum(amount) filter (where approved), 0)
 from fc group by 2 order by 4 asc limit 5)
order by 1 desc, 4 desc"""),

("16. СКОРОСТЬ ОБСЛУЖИВАНИЯ И ОТМЕНЫ", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       round(avg(txn_s) filter (where approved), 1) as avg_txn_s,
       round(avg(vend_s) filter (where approved), 1) as avg_vend_s,
       max(vend_s) as max_vend_s,
       count(*) filter (where cancelled) as cancelled_vends,
       round(100.0 * count(*) filter (where cancelled) / count(*), 2) as cancelled_pct
from fc group by 1 order by 1"""),

("17. СРЕДНЯЯ ДНЕВНАЯ ВЫРУЧКА НА ТЕРМИНАЛ", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(distinct ts::date) as days_with_data,
       count(distinct sn) as terminals,
       round(sum(amount) filter (where approved)
             / count(distinct sn), 0) as revenue_per_terminal,
       round(sum(amount) filter (where approved)
             / count(distinct sn) / count(distinct ts::date), 1) as revenue_per_terminal_per_day,
       round(count(*) filter (where approved)::numeric
             / count(distinct sn) / count(distinct ts::date), 1) as payments_per_terminal_per_day
from fc group by 1 order by 1"""),
]

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
    say(f"Сводный отчёт по организации: {ORG}")
    say(f"Период: последние {MONTHS} мес. по данным, собран {datetime.now():%Y-%m-%d %H:%M}")
    say("Промахи по чужой карте (MiFare) исключены из всех метрик отказов.")
    for title, sql in QUERIES:
        head(title)
        table(cur, sql, args={"org": ORG}, maxw=32)
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
