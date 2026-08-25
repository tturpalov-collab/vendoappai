#!/usr/bin/env python3
"""
Детализация потерь: какие автоматы рвут связь и сколько это стоит.

    python3 vendo_drilldown.py

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
REPORT = os.path.join(HERE, "vendo_drilldown_report.txt")

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



# ── детализация: где именно теряются деньги ───────────────────────────────────
# Технический отказ = терминал не получил внятного ответа от хоста.
TECH = "('', 'CE', 'TO', '907', '909', '911')"

BASE = f"""
with p as (
  select id, vend_id, approved, pos_localtime_at as ts,
         coalesce(cash_amount,0) + coalesce(cashless_amount,0) as amount
  from vendotek_payment
),
pu as (
  select p.*, v.unit_id, v.organization_name as org
  from p join vendotek_vend v on v.id = p.vend_id
),
f as (
  select pu.*, u.sn, coalesce(nullif(u.location_name,''), '—') as loc
  from pu join vendotek_unit u on u.id = pu.unit_id
),
fc as (
  select f.*, coalesce(c.response_code, '') as code,
         coalesce(c.response_code, '') in {TECH} as is_tech
  from f left join vendotek_payment_cashless c on c.payment_id = f.id
)
"""

Q11 = """
select to_char(m, 'YYYY-MM') as month,
       count(*) as launched,
       sum(count(*)) over (order by m) as cumulative
from (select sn, date_trunc('month', min(ts)) as m from fc group by 1) t
group by m order by m"""

Q12 = """
select to_char(u.m2, 'YYYY-MM') as month,
       count(*) as active_units,
       round(sum(u.unit_rev), 0) as revenue,
       round(avg(u.unit_rev), 0) as per_unit_avg,
       round(percentile_cont(0.5) within group (order by u.unit_rev)::numeric, 0) as per_unit_median
from (select sn as s2, date_trunc('month', ts) as m2,
             sum(amount) filter (where approved) as unit_rev
      from fc group by 1, 2) u
group by 1, u.m2 order by 1"""

Q13 = """
select count(*) filter (where to_timestamp(case when u.last_seen_at > 100000000000 then u.last_seen_at/1000.0 else u.last_seen_at end) > now() - interval '7 days') as seen_last_7d,
       count(*) filter (where to_timestamp(case when u.last_seen_at > 100000000000 then u.last_seen_at/1000.0 else u.last_seen_at end) > now() - interval '30 days') as seen_last_30d,
       count(*) as not_launched_total
from vendotek_unit u
where not exists (select 1 from vendotek_vend v where v.unit_id = u.id)"""

Q14 = """
select org,
       count(*) as attempts,
       count(*) filter (where not approved and code = 'CE') as ce,
       count(*) filter (where not approved and code = '') as no_answer,
       count(*) filter (where not approved and code = 'TO') as timeout,
       count(*) filter (where not approved and is_tech) as tech_total,
       round(100.0 * count(*) filter (where not approved and is_tech) / count(*), 1) as tech_pct,
       round(count(*) filter (where not approved and is_tech)
             * avg(amount) filter (where approved and amount > 0), 0) as lost_aed
from fc where ts >= '2026-08-01'
group by 1 having count(*) >= 50
order by ce desc nulls last"""

Q15 = """
select sn, loc, org,
       count(*) as attempts,
       count(*) filter (where not approved and code = 'CE') as ce,
       count(*) filter (where not approved and code = '') as no_answer,
       round(100.0 * count(*) filter (where not approved and is_tech) / count(*), 1) as tech_pct,
       round(count(*) filter (where not approved and is_tech)
             * avg(amount) filter (where approved and amount > 0), 0) as lost_aed
from fc where ts >= '2026-08-01'
group by 1, 2, 3 having count(*) >= 50
order by ce desc nulls last limit 25"""

Q16 = """
select org,
       count(*) filter (where not approved and code = 'CE'
                        and ts >= '2026-03-01' and ts < '2026-05-01') as ce_mar_apr,
       count(*) filter (where not approved and code = 'CE'
                        and ts >= '2026-05-01' and ts < '2026-06-01') as ce_may,
       count(*) filter (where not approved and code = 'CE'
                        and ts >= '2026-06-01' and ts < '2026-07-01') as ce_jun,
       count(*) filter (where not approved and code = 'CE'
                        and ts >= '2026-07-01' and ts < '2026-08-01') as ce_jul,
       count(*) filter (where not approved and code = 'CE'
                        and ts >= '2026-08-01') as ce_aug
from fc where ts >= '2026-03-01'
group by 1
having count(*) filter (where not approved and code = 'CE') > 0
order by ce_aug desc, ce_mar_apr desc"""

Q17 = """
select to_char(date_trunc('week', ts), 'YYYY-MM-DD') as week_start,
       count(*) as attempts,
       count(*) filter (where not approved and code = 'CE') as ce,
       count(*) filter (where not approved and code = '') as no_answer,
       round(100.0 * count(*) filter (where not approved and is_tech) / count(*), 2) as tech_pct
from fc where ts >= '2026-06-01'
group by 1 order by 1"""

QUERIES = [

("1. ТОП-30 АВТОМАТОВ ПО ПОТЕРЯННОЙ ВЫРУЧКЕ", BASE + """
select sn, loc,
       count(*) as attempts,
       count(*) filter (where not approved) as declined,
       round(100.0 * count(*) filter (where not approved) / count(*), 1) as decline_pct,
       count(*) filter (where not approved and is_tech) as tech_declined,
       round(avg(amount) filter (where approved and amount > 0), 2) as avg_check,
       round(count(*) filter (where not approved)
             * avg(amount) filter (where approved and amount > 0), 0) as lost_aed
from fc group by 1, 2
having count(*) >= 200
order by lost_aed desc nulls last limit 30"""),

("2. ТОП-30 АВТОМАТОВ ПО ДОЛЕ ТЕХНИЧЕСКИХ ОТКАЗОВ (от 200 попыток)", BASE + """
select sn, loc,
       count(*) as attempts,
       count(*) filter (where not approved and is_tech) as tech_declined,
       round(100.0 * count(*) filter (where not approved and is_tech) / count(*), 1) as tech_pct,
       round(count(*) filter (where not approved and is_tech)
             * avg(amount) filter (where approved and amount > 0), 0) as lost_aed
from fc group by 1, 2
having count(*) >= 200
order by tech_pct desc nulls last limit 30"""),

("3. СКОЛЬКО АВТОМАТОВ В КАКОЙ ЗОНЕ ПО ТЕХНИЧЕСКИМ ОТКАЗАМ", BASE + """
select case when tech_pct >= 30 then 'a. 30 %+   — связь не работает'
            when tech_pct >= 15 then 'b. 15-30 % — тяжёлые'
            when tech_pct >= 5  then 'c. 5-15 %  — заметные'
            when tech_pct >= 1  then 'd. 1-5 %   — фоновые'
            else 'e. < 1 %   — здоровые' end as zone,
       count(*) as units,
       sum(attempts) as attempts,
       round(sum(lost), 0) as lost_aed
from (
  select sn,
         count(*) as attempts,
         100.0 * count(*) filter (where not approved and is_tech) / count(*) as tech_pct,
         count(*) filter (where not approved and is_tech)
           * avg(amount) filter (where approved and amount > 0) as lost
  from fc group by 1 having count(*) >= 100
) t group by 1 order by 1"""),

("4. ЧТО СЛУЧИЛОСЬ В МАРТЕ-АПРЕЛЕ: ОТКАЗЫ ПО ОРГАНИЗАЦИЯМ", BASE + """
select org,
       count(*) filter (where ts >= '2026-02-01' and ts < '2026-03-01') as feb_all,
       round(100.0 * count(*) filter (where ts >= '2026-02-01' and ts < '2026-03-01' and not approved)
             / nullif(count(*) filter (where ts >= '2026-02-01' and ts < '2026-03-01'), 0), 1) as feb_dec_pct,
       count(*) filter (where ts >= '2026-03-01' and ts < '2026-05-01') as mar_apr_all,
       round(100.0 * count(*) filter (where ts >= '2026-03-01' and ts < '2026-05-01' and not approved)
             / nullif(count(*) filter (where ts >= '2026-03-01' and ts < '2026-05-01'), 0), 1) as mar_apr_dec_pct,
       round(100.0 * count(*) filter (where ts >= '2026-05-01' and not approved)
             / nullif(count(*) filter (where ts >= '2026-05-01'), 0), 1) as after_dec_pct
from fc group by 1
having count(*) filter (where ts >= '2026-03-01' and ts < '2026-05-01') >= 500
order by mar_apr_dec_pct desc nulls last"""),

("5. КОДЫ ОТКАЗА ПО МЕСЯЦАМ — ГДЕ ИМЕННО ВСПЛЕСК", BASE + """
select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
       count(*) filter (where not approved and code = '')    as no_answer,
       count(*) filter (where not approved and code = 'CE')  as ce,
       count(*) filter (where not approved and code = 'TO')  as timeout,
       count(*) filter (where not approved and code = '116') as no_funds,
       count(*) filter (where not approved and not is_tech and code <> '116') as other_bank
from fc where ts >= '2025-10-01'
group by 1 order by 1"""),

("6. СКОЛЬКО АВТОМАТОВ ЗАТРОНУЛ СБОЙ МАРТА-АПРЕЛЯ", BASE + """
select count(*) filter (where mar_apr_pct >= 20) as units_over_20pct,
       count(*) filter (where mar_apr_pct >= 50) as units_over_50pct,
       count(*) as units_active_then
from (
  select sn, 100.0 * count(*) filter (where not approved) / count(*) as mar_apr_pct
  from fc where ts >= '2026-03-01' and ts < '2026-05-01'
  group by 1 having count(*) >= 100
) t"""),

("7. ЕЩЁ НЕ ЗАПУЩЕННЫЕ АВТОМАТЫ ПО ОРГАНИЗАЦИЯМ", """
select coalesce(nullif(u.org_name,''), '(пусто)') as org,
       count(*) as silent_units,
       count(*) filter (where u.last_seen_at is not null and u.last_seen_at > 0) as have_last_seen,
       count(*) filter (where coalesce(u.address,'') <> '') as have_address
from vendotek_unit u
where not exists (select 1 from vendotek_vend v where v.unit_id = u.id)
group by 1 order by 2 desc limit 25"""),

("8. НЕ ЗАПУЩЕННЫЕ: КОГДА ПОСЛЕДНИЙ РАЗ ВЫХОДИЛИ НА СВЯЗЬ", """
select case when u.last_seen_at is null or u.last_seen_at = 0 then '(нет отметки)'
            else to_char(to_timestamp(case when u.last_seen_at > 100000000000 then u.last_seen_at/1000.0 else u.last_seen_at end), 'YYYY-MM') end as last_seen_month,
       count(*) as units
from vendotek_unit u
where not exists (select 1 from vendotek_vend v where v.unit_id = u.id)
group by 1 order by 1 desc nulls last limit 25"""),

("9. НЕ ЗАПУЩЕННЫЕ: ПРИМЕРЫ 25 СТРОК", """
select u.sn, coalesce(nullif(u.org_name,''),'—') as org,
       coalesce(nullif(u.location_name,''),'—') as loc,
       coalesce(nullif(u.city,''),'—') as city,
       coalesce(nullif(u.address,''),'—') as address,
       case when u.last_seen_at is null or u.last_seen_at = 0 then '—'
            else to_char(to_timestamp(case when u.last_seen_at > 100000000000 then u.last_seen_at/1000.0 else u.last_seen_at end), 'YYYY-MM-DD') end as last_seen
from vendotek_unit u
where not exists (select 1 from vendotek_vend v where v.unit_id = u.id)
order by u.last_seen_at desc nulls last limit 25"""),

("10. ДЛИТЕЛЬНОСТЬ ВЫДАЧИ У ПРОБЛЕМНЫХ АВТОМАТОВ", BASE + """
select case when tech_pct >= 15 then 'проблемные (15 %+ технических)'
            else 'остальные' end as grp,
       count(*) as units,
       round(avg(avg_vend), 1) as avg_vend_s
from (
  select f.sn,
         100.0 * count(*) filter (where not f.approved and f.is_tech) / count(*) as tech_pct,
         avg(c.vend_duration_s) as avg_vend
  from fc f left join vendotek_payment_cashless c on c.payment_id = f.id
  group by 1 having count(*) >= 100
) t group by 1 order by 1"""),
("11. ТЕМП ЗАПУСКА: СКОЛЬКО АВТОМАТОВ НАЧАЛО ПРОДАВАТЬ В МЕСЯЦ", BASE + Q11),

("12. ВЫРУЧКА НА ЗАПУЩЕННЫЙ АВТОМАТ ПО МЕСЯЦАМ", BASE + Q12),

("13. ВКЛЮЧЕНЫ, НО НЕ ПРОДАЮТ — КАНДИДАТЫ НА ПРОВЕРКУ", Q13),
("14. АВГУСТ: ТЕХНИЧЕСКИЕ ОТКАЗЫ ПО ОРГАНИЗАЦИЯМ", BASE + Q14),

("15. АВГУСТ: ТОП-25 АВТОМАТОВ ПО ОШИБКАМ CE", BASE + Q15),

("16. CE ПО ОРГАНИЗАЦИЯМ: МАРТ-АПРЕЛЬ vs ПОСЛЕДУЮЩИЕ МЕСЯЦЫ", BASE + Q16),

("17. ПОНЕДЕЛЬНАЯ ДИНАМИКА ТЕХНИЧЕСКИХ ОТКАЗОВ С ИЮНЯ", BASE + Q17),
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
    say(f"Детализация потерь Vendotek — {datetime.now():%Y-%m-%d %H:%M}")
    say(f"База: {PGDATABASE}")
    say("Технический отказ = код ответа пустой, CE, TO, 907, 909 или 911.")
    for title, sql in QUERIES:
        head(title)
        table(cur, sql, maxw=30)
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
