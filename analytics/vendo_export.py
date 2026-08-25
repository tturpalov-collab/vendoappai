#!/usr/bin/env python3
"""
Выгрузка платежей и состояния терминалов в CSV.

    python3 vendo_export.py

Кладёт файлы в analytics/out/ рядом со скриптом. Только чтение.
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
OUTDIR = os.path.join(HERE, "out")

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



import csv, gzip

TECH = "('', 'CE', 'TO', '907', '909', '911')"

BASE = f"""
with p as (
  select id, vend_id, approved, pos_localtime_at as ts,
         coalesce(cash_amount,0) as cash, coalesce(cashless_amount,0) as cashless,
         coalesce(cash_amount,0) + coalesce(cashless_amount,0) as amount
  from vendotek_payment
),
pu as (
  select p.*, v.unit_id, v.organization_name as org,
         v.product_id, nullif(v.product_name,'') as product, v.cancelled
  from p join vendotek_vend v on v.id = p.vend_id
),
fc as (
  select pu.*, u.sn, nullif(u.location_name,'') as loc, nullif(u.city,'') as city,
         coalesce(c.response_code, '') as code,
         coalesce(c.response_code, '') in {TECH} as is_tech,
         nullif(c.application_label,'') as card, c.vend_duration_s
  from pu
  join vendotek_unit u on u.id = pu.unit_id
  left join vendotek_payment_cashless c on c.payment_id = pu.id
)
"""

RAW_SQL = BASE + """
select to_char(ts, 'YYYY-MM-DD HH24:MI') as ts, sn, org,
       round(amount, 2) as amount,
       case when cash > 0 and cashless > 0 then 'mixed'
            when cash > 0 then 'cash' else 'cashless' end as pay_type,
       approved::int as approved, code, is_tech::int as tech_fail,
       product_id, product, cancelled::int as vend_cancelled
from fc
where ts >= %(a)s and ts < %(b)s
order by ts"""

RAW_ALL = RAW_SQL.replace("where ts >= %(a)s and ts < %(b)s\n", "")


EXPORTS = [

("daily.csv", "Платежи по дням", BASE + """
select ts::date as day,
       count(*) as attempts,
       count(*) filter (where approved) as approved,
       count(*) filter (where not approved) as declined,
       count(*) filter (where not approved and is_tech) as tech_declined,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(sum(cash) filter (where approved), 2) as cash,
       round(sum(cashless) filter (where approved), 2) as cashless,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check,
       count(distinct unit_id) as active_units
from fc group by 1 order by 1"""),

("units.csv", "Состояние терминалов и их показатели", """
with p as (
  select id, vend_id, approved, pos_localtime_at as ts,
         coalesce(cash_amount,0) + coalesce(cashless_amount,0) as amount
  from vendotek_payment
),
s as (
  select v.unit_id,
         count(*) as attempts,
         count(*) filter (where p.approved) as approved,
         count(*) filter (where not p.approved) as declined,
         count(*) filter (where not p.approved
           and coalesce(c.response_code,'') in ('', 'CE', 'TO', '907', '909', '911')) as tech_declined,
         round(sum(p.amount) filter (where p.approved), 2) as revenue,
         round(avg(p.amount) filter (where p.approved and p.amount > 0), 2) as avg_check,
         min(p.ts)::date as first_sale, max(p.ts)::date as last_sale
  from p join vendotek_vend v on v.id = p.vend_id
  left join vendotek_payment_cashless c on c.payment_id = p.id
  group by 1
)
select u.sn, u.tid, nullif(u.org_name,'') as org, nullif(u.location_name,'') as location,
       nullif(u.city,'') as city, nullif(u.region,'') as region, nullif(u.address,'') as address,
       u.country, u.tz,
       case when u.last_seen_at is null or u.last_seen_at = 0 then null
            else to_char(to_timestamp(u.last_seen_at / 1000.0), 'YYYY-MM-DD HH24:MI') end as last_seen,
       coalesce(s.attempts, 0) as attempts,
       coalesce(s.approved, 0) as approved,
       coalesce(s.declined, 0) as declined,
       coalesce(s.tech_declined, 0) as tech_declined,
       coalesce(s.revenue, 0) as revenue,
       s.avg_check, s.first_sale, s.last_sale,
       case when s.attempts is null then 'нет продаж' else 'торгует' end as status
from vendotek_unit u left join s on s.unit_id = u.id
order by coalesce(s.revenue, 0) desc"""),

("unit_month.csv", "Автомат x месяц", BASE + """
select sn, to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) as attempts,
       count(*) filter (where approved) as approved,
       count(*) filter (where not approved and is_tech) as tech_declined,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check
from fc group by 1, 2 order by 1, 2"""),

("org_month.csv", "Организация x месяц", BASE + """
select org, to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) as attempts,
       count(*) filter (where approved) as approved,
       count(*) filter (where not approved) as declined,
       count(*) filter (where not approved and is_tech) as tech_declined,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check,
       count(distinct unit_id) as units
from fc group by 1, 2 order by 1, 2"""),

("declines.csv", "Отказы: код x месяц x организация", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month, org,
       case when code = '' then '(нет ответа)' else code end as response_code,
       is_tech::int as is_tech,
       count(*) as cnt
from fc where not approved
group by 1, 2, 3, 4 order by 1, 5 desc"""),

("hourly.csv", "Профиль по часам и дням недели", BASE + """
select extract(hour from ts)::int as hour, to_char(ts, 'ID')::int as dow,
       count(*) filter (where approved) as payments,
       round(sum(amount) filter (where approved), 2) as revenue
from fc group by 1, 2 order by 1, 2"""),

("products.csv", "Товары", BASE + """
select product_id, product,
       count(*) filter (where approved) as sales,
       round(sum(amount) filter (where approved), 2) as revenue,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_price,
       count(distinct unit_id) as units
from fc group by 1, 2 order by 4 desc nulls last"""),

("modules.csv", "Состояние модулей автоматов (телеметрия)", """
select u.sn, nullif(u.org_name,'') as org, nullif(u.location_name,'') as location,
       m.module_key, m.name, nullif(m.full_name,'') as full_name, m.status,
       nullif(m.source,'') as source, nullif(m.text,'') as text, m.visibility, m.external,
       case when m.status_changed_at is null or m.status_changed_at = 0 then null
            else to_char(to_timestamp(m.status_changed_at / 1000.0), 'YYYY-MM-DD HH24:MI') end as status_changed
from vendotek_module m
left join vendotek_unit u on u.id = m.unit_id
order by u.sn nulls last, m.name"""),

("module_details.csv", "Параметры модулей", """
select u.sn, m.name as module, d.name as param, d.value
from vendotek_module_detail d
join vendotek_module m on m.id = d.module_id
left join vendotek_unit u on u.id = m.unit_id
order by u.sn nulls last, m.name, d.name"""),
]

def dump(cur, fname, sql, gz=False, params=None):
    path = os.path.join(OUTDIR, fname + (".gz" if gz else ""))
    cur.execute(sql, params) if params else cur.execute(sql)
    cols = [d[0] for d in cur.description]
    opener = (lambda: gzip.open(path, "wt", newline="", encoding="utf-8")) if gz \
             else (lambda: open(path, "w", newline="", encoding="utf-8"))
    n = 0
    with opener() as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        while True:
            rows = cur.fetchmany(20000)
            if not rows:
                break
            w.writerows(rows)
            n += len(rows)
    return path, n, os.path.getsize(path)

def dump_raw(cur, print_row):
    """Сырые платежи: полный архив + поквартальные файлы под лимит загрузки."""
    path, n, size = dump(cur, "payments_slim.csv", RAW_ALL, gz=True)
    print_row(os.path.basename(path), n, size, "Сырые платежи, весь период (архив)")

    cur.execute("select min(pos_localtime_at)::date, max(pos_localtime_at)::date "
                "from vendotek_payment")
    lo, hi = cur.fetchone()
    if not lo:
        return
    y, q = lo.year, (lo.month - 1) // 3 + 1
    while (y, q) <= (hi.year, (hi.month - 1) // 3 + 1):
        a = f"{y}-{q*3-2:02d}-01"
        ny, nq = (y + 1, 1) if q == 4 else (y, q + 1)
        b = f"{ny}-{nq*3-2:02d}-01"
        fname = f"payments_{y}q{q}.csv"
        path, n, size = dump(cur, fname, RAW_SQL, params={"a": a, "b": b})
        if n:
            print_row(fname, n, size, f"Сырые платежи {a} — {b}")
        else:
            os.remove(path)
        y, q = ny, nq


def human(b):
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if b < 1024:
            return f"{b:.0f} {u}"
        b /= 1024
    return f"{b:.1f} ТБ"

def main():
    driver = ensure_psycopg()
    open_tunnel()
    pw = get_password()
    os.makedirs(OUTDIR, exist_ok=True)
    try:
        conn = connect(driver, pw, PGDATABASE)
    except Exception as e:
        sys.exit(f"Не подключился к базе '{PGDATABASE}': {str(e).splitlines()[0]}")
    conn.autocommit = True
    cur = conn.cursor()

    print(f"Выгружаю в {OUTDIR}\n")
    total = 0

    def print_row(name, n, size, descr):
        nonlocal total
        total += size
        print(f"  {name:<26} {n:>8,} строк   {human(size):>9}   {descr}")

    dump_raw(cur, print_row)

    for fname, descr, sql in EXPORTS:
        try:
            path, n, size = dump(cur, fname, sql)
        except Exception as e:
            print(f"  [!] {fname}: {str(e).splitlines()[0]}")
            conn.rollback()
            continue
        print_row(os.path.basename(path), n, size, descr)
    cur.close(); conn.close()
    print(f"\n  Всего: {human(total)}")
    print(f"\nФайлы лежат в {OUTDIR} — их и присылайте.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
