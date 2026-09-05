#!/usr/bin/env python3
"""
Платежи и выручка по дням: динамика роста.

    python3 vendo_daily.py            # окно по умолчанию — 45 дней
    DAYS=90 python3 vendo_daily.py     # другое окно

Считает платежи, выручку, средний чек и активные терминалы по дням,
скользящее среднее, неделя к неделе, месяц к месяцу и разбор роста.
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
REPORT = os.path.join(HERE, "vendo_daily_report.txt")

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



# ── настройки отчёта ─────────────────────────────────────────────────────────
DAYS = int(os.environ.get("DAYS", 45))          # окно подневной таблицы
TEST_SN = "221400017147"                        # тестовый терминал IntersoftPOS

# Промах по чужой карте (MiFare) — это не платёж и не отказ, см. CLAUDE.md
MISTAP = ("coalesce(c.pos_entry_mode,'') = 'CONTACTLESS' "
          "and coalesce(c.aid,'') = '' and coalesce(c.pan,'') = ''")

# Технические отказы (терминал не получил внятного ответа от хоста)
TECH = "('', 'CE', 'TO', '907', '909', '911')"

# left join, чтобы не потерять платежи без vend/unit: считаем ВСЕ платежи
BASE = f"""
with f as (
  select p.id, p.approved,
         p.pos_localtime_at as ts,
         coalesce(p.cash_amount, 0)     as cash,
         coalesce(p.cashless_amount, 0) as cashless,
         coalesce(p.cash_amount, 0) + coalesce(p.cashless_amount, 0) as amount,
         coalesce(v.organization_name, '—') as org,
         coalesce(u.sn, '—')                as sn,
         coalesce(nullif(u.location_name, ''), '—') as loc,
         coalesce(c.response_code, '')      as code,
         ({MISTAP}) as mistap
  from vendotek_payment p
  left join vendotek_vend v on v.id = p.vend_id
  left join vendotek_unit u on u.id = v.unit_id
  left join vendotek_payment_cashless c on c.payment_id = p.id
),
fc as (select * from f where not mistap and sn <> '{TEST_SN}'),
lastday as (select max(ts)::date as d from fc)
"""


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

    say(f"Платежи по дням и динамика роста — {datetime.now():%Y-%m-%d %H:%M}")
    say(f"Окно подневной таблицы: {DAYS} дней.")
    say("Исключены: промахи по чужой карте (MiFare) и тестовый терминал "
        f"{TEST_SN}.")
    say("Выручка и средний чек — только по одобренным платежам "
        "(cash_amount + cashless_amount).")

    # ── 0. свежесть данных ────────────────────────────────────────────────────
    head("0. ГРАНИЦЫ ДАННЫХ")
    table(cur, BASE + """
        select count(*)                       as payments,
               min(ts)::date                  as first_day,
               max(ts)::date                  as last_day,
               max(ts)                        as last_payment_at,
               round(sum(amount) filter (where approved), 0) as revenue_aed
        from fc""")

    # ── 1. по дням ────────────────────────────────────────────────────────────
    head(f"1. ПО ДНЯМ ЗА ПОСЛЕДНИЕ {DAYS} ДНЕЙ")
    say("  ma7_rev — среднее за 7 дней, сглаживает выходные.")
    say("  wow_%   — выручка к тому же дню недели неделей раньше.")
    table(cur, BASE + f"""
        , d as (
          select ts::date as day,
                 count(*)                                  as pays,
                 count(*) filter (where approved)           as ok,
                 sum(amount) filter (where approved)        as rev,
                 count(distinct sn) filter (where approved) as terms,
                 count(*) filter (where not approved and code in {TECH}) as tech_fail
          from fc, lastday
          where ts::date > lastday.d - {DAYS}
          group by 1
        )
        select d.day,
               to_char(d.day, 'Dy')                  as dow,
               d.pays,
               d.ok,
               round(d.rev, 0)                       as revenue,
               round(d.rev / nullif(d.ok, 0), 2)     as avg_check,
               d.terms,
               d.tech_fail,
               round(avg(d.rev) over (order by d.day rows between 6 preceding
                                      and current row), 0) as ma7_rev,
               case when w.rev > 0
                    then round(100 * (d.rev - w.rev) / w.rev, 1) end as wow_pct
        from d
        left join d w on w.day = d.day - 7
        order by d.day""", maxw=20)

    # ── 2. текущий месяц по дням, крупно ──────────────────────────────────────
    head("2. ТЕКУЩИЙ МЕСЯЦ ПО ДНЯМ (включая 4 сентября)")
    table(cur, BASE + f"""
        select ts::date                                   as day,
               to_char(ts::date, 'Dy')                    as dow,
               count(*)                                   as pays,
               count(*) filter (where approved)           as ok,
               round(100.0 * count(*) filter (where not approved)
                     / nullif(count(*), 0), 2)            as decline_pct,
               count(*) filter (where not approved and code in {TECH}) as tech_fail,
               count(*) filter (where not approved and code = 'CE')    as ce,
               round(sum(amount) filter (where approved), 0)           as revenue,
               round(sum(cash) filter (where approved), 0)             as cash,
               round(sum(cashless) filter (where approved), 0)         as cashless,
               round(sum(amount) filter (where approved)
                     / nullif(count(*) filter (where approved), 0), 2) as avg_check,
               count(distinct sn) filter (where approved)              as terms,
               count(distinct org) filter (where approved)             as orgs
        from fc, lastday
        where ts >= date_trunc('month', lastday.d)
        group by 1, 2
        order by 1""", maxw=20)

    # ── 3. месяц к месяцу на одинаковом числе дней ────────────────────────────
    head("3. МЕСЯЦ К МЕСЯЦУ НА ОДИНАКОВОМ ЧИСЛЕ ДНЕЙ")
    say("  Текущий месяц неполный, поэтому у прошлых месяцев взято "
        "столько же первых дней.")
    table(cur, BASE + """
        , cut as (select extract(day from d)::int as n from lastday)
        select to_char(date_trunc('month', ts), 'YYYY-MM')        as month,
               (select n from cut)                                as days,
               count(*)                                           as pays,
               count(*) filter (where approved)                   as ok,
               round(sum(amount) filter (where approved), 0)      as revenue,
               round(sum(amount) filter (where approved)
                     / (select n from cut), 0)                    as rev_per_day,
               round(sum(amount) filter (where approved)
                     / nullif(count(*) filter (where approved), 0), 2) as avg_check,
               count(distinct sn) filter (where approved)         as terms
        from fc
        where extract(day from ts) <= (select n from cut)
          and ts >= '2025-10-01'
        group by 1
        order by 1""", maxw=20)

    # ── 4. полные месяцы ──────────────────────────────────────────────────────
    head("4. МЕСЯЦЫ ЦЕЛИКОМ: ВЫРУЧКА, ПЛАТЕЖИ, ТЕМП РОСТА")
    say("  Последний месяц неполный — сравнивать его mom_pct с прошлым нельзя,")
    say("  для этого есть раздел 3. Здесь смотреть на rev_per_day.")
    table(cur, BASE + """
        , m as (
          select date_trunc('month', ts)::date               as month,
                 count(*)                                    as pays,
                 count(*) filter (where approved)            as ok,
                 sum(amount) filter (where approved)         as rev,
                 count(distinct sn) filter (where approved)  as terms,
                 count(distinct ts::date)                    as days
          from fc
          where ts >= '2025-06-01'
          group by 1
        )
        select to_char(month, 'YYYY-MM')                 as month,
               days,
               pays,
               ok,
               round(rev, 0)                             as revenue,
               round(rev / nullif(days, 0), 0)           as rev_per_day,
               terms,
               round(rev / nullif(terms, 0), 0)          as rev_per_term,
               case when lag(rev) over (order by month) > 0
                    then round(100 * (rev - lag(rev) over (order by month))
                               / lag(rev) over (order by month), 1) end as mom_pct
        from m
        order by month""", maxw=20)

    # ── 5. недели ─────────────────────────────────────────────────────────────
    head("5. ПО НЕДЕЛЯМ (последние 14)")
    table(cur, BASE + """
        , w as (
          select date_trunc('week', ts)::date              as week,
                 count(*)                                  as pays,
                 sum(amount) filter (where approved)       as rev,
                 count(distinct ts::date)                  as days,
                 count(distinct sn) filter (where approved) as terms
          from fc, lastday
          where ts::date > lastday.d - 98
          group by 1
        )
        select week,
               days,
               pays,
               round(rev, 0)                       as revenue,
               round(rev / nullif(days, 0), 0)     as rev_per_day,
               terms,
               case when lag(rev / nullif(days, 0)) over (order by week) > 0
                    then round(100 * (rev / nullif(days, 0)
                               - lag(rev / nullif(days, 0)) over (order by week))
                               / lag(rev / nullif(days, 0)) over (order by week), 1)
               end                                 as wow_per_day_pct
        from w
        order by week""", maxw=20)

    # ── 6. профиль дня недели ────────────────────────────────────────────────
    head("6. ПРОФИЛЬ ДНЯ НЕДЕЛИ (последние 8 недель)")
    table(cur, BASE + """
        select to_char(ts, 'ID Dy')                        as dow,
               count(distinct ts::date)                    as days,
               count(*)                                    as pays,
               round(sum(amount) filter (where approved), 0)             as revenue,
               round(sum(amount) filter (where approved)
                     / nullif(count(distinct ts::date), 0), 0)           as rev_per_day,
               round(sum(amount) filter (where approved)
                     / nullif(count(*) filter (where approved), 0), 2)   as avg_check
        from fc, lastday
        where ts::date > lastday.d - 56
        group by 1
        order by 1""", maxw=20)

    # ── 7. из чего сложился рост: новые терминалы против старых ──────────────
    head("7. ИЗ ЧЕГО СЛОЖИЛСЯ РОСТ: НОВЫЕ ТЕРМИНАЛЫ ПРОТИВ ПРЕЖНИХ")
    say("  Сравниваются равные отрезки: первые N дней текущего месяца "
        "против первых N дней прошлого.")
    table(cur, BASE + """
        , cut as (select extract(day from d)::int as n,
                         date_trunc('month', d)::date as cur_m,
                         (date_trunc('month', d) - interval '1 month')::date as prev_m
                  from lastday)
        , cur as (select sn, sum(amount) filter (where approved) as rev,
                         count(*) as pays
                  from fc, cut
                  where ts >= cut.cur_m and extract(day from ts) <= cut.n
                        and sn <> '—'
                  group by 1)
        , prv as (select sn, sum(amount) filter (where approved) as rev,
                         count(*) as pays
                  from fc, cut
                  where ts >= cut.prev_m and ts < cut.cur_m
                        and extract(day from ts) <= cut.n
                        and sn <> '—'
                  group by 1)
        select case when prv.sn is null then 'только текущий месяц (новые)'
                    when cur.sn is null then 'только прошлый месяц (замолчали)'
                    else 'работали в обоих' end                 as group_,
               count(*)                                          as terminals,
               round(coalesce(sum(prv.rev), 0), 0)               as rev_prev,
               round(coalesce(sum(cur.rev), 0), 0)               as rev_cur,
               round(coalesce(sum(cur.rev), 0)
                     - coalesce(sum(prv.rev), 0), 0)             as delta
        from cur full outer join prv on prv.sn = cur.sn
        group by 1
        order by 5 desc""", maxw=34)

    # ── 8. организации: текущий месяц против прошлого, тот же отрезок ────────
    head("8. ОРГАНИЗАЦИИ: ТЕКУЩИЙ МЕСЯЦ ПРОТИВ ПРОШЛОГО (равные отрезки)")
    table(cur, BASE + """
        , cut as (select extract(day from d)::int as n,
                         date_trunc('month', d)::date as cur_m,
                         (date_trunc('month', d) - interval '1 month')::date as prev_m
                  from lastday)
        select org,
               round(coalesce(sum(amount) filter (where approved and ts <  cut.cur_m), 0), 0) as rev_prev,
               round(coalesce(sum(amount) filter (where approved and ts >= cut.cur_m), 0), 0) as rev_cur,
               round(coalesce(sum(amount) filter (where approved and ts >= cut.cur_m), 0)
                     - coalesce(sum(amount) filter (where approved and ts < cut.cur_m), 0), 0) as delta,
               count(distinct sn) filter (where approved and ts < cut.cur_m)  as terms_prev,
               count(distinct sn) filter (where approved and ts >= cut.cur_m) as terms_cur
        from fc, cut
        where ts >= cut.prev_m and extract(day from ts) <= cut.n
        group by 1
        order by 4 desc""", maxw=30)

    # ── 9. терминалы: кто вытянул рост и кто просел ──────────────────────────
    head("9. ТЕРМИНАЛЫ: ВКЛАД В РОСТ (равные отрезки месяцев)")
    table(cur, BASE + """
        , cut as (select extract(day from d)::int as n,
                         date_trunc('month', d)::date as cur_m,
                         (date_trunc('month', d) - interval '1 month')::date as prev_m
                  from lastday)
        , t as (
          select sn, max(loc) as loc, max(org) as org,
                 sum(amount) filter (where approved and ts <  cut.cur_m) as rev_prev,
                 sum(amount) filter (where approved and ts >= cut.cur_m) as rev_cur
          from fc, cut
          where ts >= cut.prev_m and extract(day from ts) <= cut.n
                and sn <> '—'
          group by 1
        )
        (select 'рост' as side, sn, loc, org,
                round(coalesce(rev_prev, 0), 0) as rev_prev,
                round(coalesce(rev_cur, 0), 0)  as rev_cur,
                round(coalesce(rev_cur, 0) - coalesce(rev_prev, 0), 0) as delta
         from t order by coalesce(rev_cur, 0) - coalesce(rev_prev, 0) desc limit 15)
        union all
        (select 'падение', sn, loc, org,
                round(coalesce(rev_prev, 0), 0),
                round(coalesce(rev_cur, 0), 0),
                round(coalesce(rev_cur, 0) - coalesce(rev_prev, 0), 0)
         from t order by coalesce(rev_cur, 0) - coalesce(rev_prev, 0) asc limit 15)
        order by 7 desc""", maxw=28)

    # ── 10. запуск новых терминалов по месяцам ───────────────────────────────
    head("10. ТЕМП ЗАПУСКА: ПЕРВАЯ ПРОДАЖА ТЕРМИНАЛА ПО МЕСЯЦАМ")
    table(cur, BASE + """
        , first_sale as (
          select sn, min(ts)::date as first_day
          from fc where approved and sn <> '—'
          group by 1
        )
        select to_char(date_trunc('month', first_day), 'YYYY-MM') as month,
               count(*)                                           as started,
               sum(count(*)) over (order by date_trunc('month', first_day)) as total
        from first_sale
        where first_day >= '2025-06-01'
        group by 1, date_trunc('month', first_day)
        order by 1""", maxw=20)

    # ── 11. наличные против безнала по месяцам ───────────────────────────────
    head("11. НАЛИЧНЫЕ ПРОТИВ БЕЗНАЛА ПО МЕСЯЦАМ")
    table(cur, BASE + """
        select to_char(date_trunc('month', ts), 'YYYY-MM')          as month,
               round(sum(cash) filter (where approved), 0)          as cash,
               round(sum(cashless) filter (where approved), 0)      as cashless,
               round(100.0 * sum(cash) filter (where approved)
                     / nullif(sum(amount) filter (where approved), 0), 1) as cash_pct
        from fc
        where ts >= '2025-10-01'
        group by 1
        order by 1""", maxw=20)

    # ── 12. часы суток за последнюю неделю ───────────────────────────────────
    head("12. ЧАСЫ СУТОК ЗА ПОСЛЕДНЮЮ НЕДЕЛЮ")
    table(cur, BASE + """
        select extract(hour from ts)::int                        as hour,
               count(*)                                          as pays,
               round(sum(amount) filter (where approved), 0)      as revenue,
               round(sum(amount) filter (where approved)
                     / nullif(count(*) filter (where approved), 0), 2) as avg_check
        from fc, lastday
        where ts::date > lastday.d - 7
        group by 1
        order by 1""", maxw=20)

    cur.close(); conn.close()
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print("\n" + "=" * 78)
    print(f"Готово. Отчёт: {REPORT}")
    print("Пришлите его файлом в чат — разберу цифры.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
