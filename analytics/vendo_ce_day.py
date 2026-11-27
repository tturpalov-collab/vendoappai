#!/usr/bin/env python3
"""
Обрывы CE за один день — потранзакционно.

    python3 vendo_ce_day.py                 # последний день с данными
    DAY=2026-11-25 python3 vendo_ce_day.py  # конкретная дата

Каждый обрыв отдельной строкой: время, терминал, точка, организация, сумма,
карта, способ ввода, RRN, длительности. Плюс что происходило на том же
автомате вокруг обрыва — дошёл ли человек до оплаты со второй попытки.
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
REPORT = os.path.join(HERE, "vendo_ce_day_report.txt")

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
DAY = os.environ.get("DAY", "")       # пусто — последний день с данными
WINDOW_MIN = int(os.environ.get("WINDOW_MIN", 10))   # окно вокруг обрыва

MISTAP = ("coalesce(c.pos_entry_mode,'') = 'CONTACTLESS' "
          "and coalesce(c.aid,'') = '' and coalesce(c.pan,'') = ''")

DAY_SQL = f"'{DAY}'::date" if DAY else "(select max(pos_localtime_at)::date from vendotek_payment)"

BASE = f"""
with d as (select {DAY_SQL} as day),
f as (
  select p.id, p.approved,
         p.pos_localtime_at                 as ts,
         coalesce(p.cash_amount, 0) + coalesce(p.cashless_amount, 0) as amount,
         coalesce(p.cash_amount, 0)         as cash,
         u.sn                               as sn,
         coalesce(nullif(u.location_name,''), '—')  as loc,
         v.organization_name                as vend_org,
         coalesce(v.completed, false)       as completed,
         coalesce(v.cancelled, false)       as cancelled,
         nullif(v.product_name, '')         as product,
         coalesce(c.response_code, '')      as code,
         coalesce(c.pos_entry_mode, '')     as entry,
         nullif(c.pan, '')                  as pan,
         nullif(c.application_label, '')    as card,
         nullif(c.issuer, '')               as issuer,
         nullif(c.rrn, '')                  as rrn,
         nullif(c.auth_id, '')              as auth,
         c.transaction_duration_s           as txn_s,
         c.vend_duration_s                  as vend_s,
         ({MISTAP})                         as mistap
  from vendotek_payment p
  join vendotek_vend v on v.id = p.vend_id
  join vendotek_unit u on u.id = v.unit_id
  left join vendotek_payment_cashless c on c.payment_id = p.id
  where p.pos_localtime_at >= {DAY_SQL} - interval '7 day'
    and p.pos_localtime_at <  {DAY_SQL} + interval '2 day'
),
fc as (select * from f where not mistap),
owner as (select sn, (array_agg(vend_org order by ts desc))[1] as org
          from fc group by 1),
day_rows as (select fc.*, o.org from fc join owner o on o.sn = fc.sn, d
             where fc.ts::date = d.day)
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

    say(f"Обрывы CE потранзакционно — отчёт собран {datetime.now():%Y-%m-%d %H:%M}")
    say(f"День: {DAY if DAY else 'последний день с данными'}. "
        f"Окно вокруг обрыва: ±{WINDOW_MIN} мин.")
    say("Промахи по чужой карте (MiFare) исключены.")

    head("0. ЧТО ЗА ДЕНЬ")
    table(cur, BASE + """
        select (select day from d)                               as day,
               count(*)                                          as pays,
               count(*) filter (where approved)                  as approved,
               count(*) filter (where not approved and code = 'CE')  as ce,
               count(*) filter (where not approved
                     and code in ('', 'TO', '907', '909', '911'))    as other_tech,
               count(distinct sn)                                as terminals,
               min(ts)::time(0)::text || ' — ' || max(ts)::time(0)::text as span
        from day_rows, d""", maxw=24)

    head("1. КАЖДЫЙ ОБРЫВ CE ОТДЕЛЬНОЙ СТРОКОЙ")
    say("  vend_flag — сырое значение vendotek_vend.completed / .cancelled.")
    say("  ВНИМАНИЕ: 'completed' НЕ доказывает, что товар выдан — это состояние")
    say("  записи о выдаче, а не подтверждение, что из автомата что-то вышло.")
    say("  Сверять с поведением покупателя в разделе 3.")
    table(cur, BASE + """
        select to_char(ts, 'HH24:MI:SS')  as time_,
               sn, loc, org,
               round(amount, 2)           as amount,
               card, entry, pan, issuer,
               txn_s, vend_s,
               case when cancelled then 'cancelled'
                    when completed then 'completed' else '—' end as vend_flag
        from day_rows
        where not approved and code = 'CE'
        order by ts""", maxw=22)

    head("2. ТЕХНИЧЕСКИЕ РЕКВИЗИТЫ ОБРЫВОВ")
    say("  rrn и auth пустые — значит хост не ответил и транзакция до банка не дошла.")
    table(cur, BASE + """
        select to_char(ts, 'HH24:MI:SS') as time_, sn,
               id::text                  as payment_id,
               coalesce(rrn, '—')        as rrn,
               coalesce(auth, '—')       as auth_id,
               coalesce(product, '—')    as product,
               round(amount, 2)          as amount
        from day_rows
        where not approved and code = 'CE'
        order by ts""", maxw=38)

    head(f"3. ЧТО БЫЛО НА ТОМ ЖЕ АВТОМАТЕ ВОКРУГ ОБРЫВА (±{WINDOW_MIN} мин)")
    say("  Строка с пометкой >>> — сам обрыв. Если следом идёт approved,")
    say("  человек расплатился со второй попытки; если нет — ушёл.")
    table(cur, BASE + f"""
        , ce as (select sn, ts from day_rows where not approved and code = 'CE')
        select to_char(ce.ts, 'HH24:MI') || ' ' || r.sn          as around,
               case when r.ts = ce.ts then '>>>' else '' end     as mark,
               to_char(r.ts, 'HH24:MI:SS')                       as time_,
               case when r.approved then 'ok' else 'отказ' end   as result,
               nullif(r.code, '')                                as code,
               round(r.amount, 2)                                as amount,
               r.card, r.pan
        from ce
        join day_rows r on r.sn = ce.sn
                       and r.ts between ce.ts - interval '{WINDOW_MIN} minute'
                                    and ce.ts + interval '{WINDOW_MIN} minute'
        order by ce.ts, r.ts""", maxw=22)

    head("4. ИТОГ ПО ТЕРМИНАЛАМ, ЗАТРОНУТЫМ ОБРЫВАМИ")
    table(cur, BASE + """
        , hit as (select distinct sn from day_rows where not approved and code = 'CE')
        select r.sn, max(r.loc) as loc, max(r.org) as org,
               count(*)                                   as pays_day,
               count(*) filter (where r.approved)         as ok,
               count(*) filter (where not r.approved and r.code = 'CE') as ce,
               round(100.0 * count(*) filter (where not r.approved and r.code = 'CE')
                     / nullif(count(*), 0), 1)            as ce_pct,
               round(sum(r.amount) filter (where r.approved), 2) as revenue
        from day_rows r join hit on hit.sn = r.sn
        group by 1
        order by 6 desc""", maxw=26)

    head("5. ТЕ ЖЕ ТЕРМИНАЛЫ ЗА НЕДЕЛЮ — РАЗОВО ИЛИ ХРОНИЧЕСКИ")
    table(cur, BASE + """
        , hit as (select distinct sn from day_rows where not approved and code = 'CE')
        select f.sn, f.ts::date as day,
               count(*)                                   as pays,
               count(*) filter (where not f.approved and f.code = 'CE') as ce
        from fc f join hit on hit.sn = f.sn
        group by 1, 2
        order by 1, 2""", maxw=20)

    cur.close(); conn.close()
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print("\n" + "=" * 78)
    print(f"Готово. Отчёт: {REPORT}")
    print("Пришлите его файлом в чат — разберу каждую транзакцию.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
