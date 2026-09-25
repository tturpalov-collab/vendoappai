#!/usr/bin/env python3
"""
Попытки оплаты нашими автоматами картой парка (MiFare) — данные для клиента.

    python3 vendo_maf_mifare.py
    ORG=soco MONTHS=12 python3 vendo_maf_mifare.py

Считает, сколько раз гость приложил к POS неплатёжную карту площадки:
по месяцам, по автоматам, по часам, и сколько из этих людей всё-таки
заплатили банковской картой в следующие минуты. Только чтение.
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
REPORT = os.path.join(HERE, "vendo_maf_mifare_report.txt")

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
ORG = os.environ.get("ORG", "majid-al-futtaim")   # подстрока имени организации
MONTHS = int(os.environ.get("MONTHS", 12))

# Промах по чужой карте (MiFare): бесконтактное прикладывание без платёжного
# приложения и без номера карты. У настоящих оплат entry mode всегда
# 'EMV CONTACTLESS' или 'QR CODE READ'. См. CLAUDE.md.
MISTAP = ("coalesce(c.pos_entry_mode,'') = 'CONTACTLESS' "
          "and coalesce(c.aid,'') = '' and coalesce(c.pan,'') = ''")

BASE = f"""
with own as (
  -- терминал относим к клиенту по преобладающей метке организации:
  -- organization_name гуляет от выдачи к выдаче внутри одного автомата
  select sn from (
    select u.sn, v.organization_name as org,
           row_number() over (partition by u.sn order by count(*) desc) as rn
    from vendotek_vend v
    join vendotek_unit u on u.id = v.unit_id
    group by 1, 2
  ) z
  where rn = 1 and org ilike '%{ORG}%'
),
f as (
  select p.id, p.approved,
         p.pos_localtime_at as ts,
         coalesce(p.cash_amount, 0) + coalesce(p.cashless_amount, 0) as amount,
         u.sn                                        as sn,
         coalesce(nullif(u.location_name, ''), '—')  as loc,
         coalesce(c.response_code, '')               as code,
         coalesce(c.pos_entry_mode, '')              as entry,
         coalesce(c.aid, '')                         as aid,
         coalesce(c.pan, '')                         as pan,
         ({MISTAP})                                  as mistap
  from vendotek_payment p
  join vendotek_vend v on v.id = p.vend_id
  join vendotek_unit u on u.id = v.unit_id
  left join vendotek_payment_cashless c on c.payment_id = p.id
  where u.sn in (select sn from own)
),
lastday as (select max(ts)::date as d from f)
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

    say(f"Попытки оплаты картой площадки — {datetime.now():%Y-%m-%d %H:%M}")
    say(f"Организация: подстрока '{ORG}'. Окно помесячных разрезов: {MONTHS} мес.")
    say("Промах по чужой карте: CONTACTLESS без AID и без PAN.")

    # ── 0. периметр клиента ──────────────────────────────────────────────────
    head("0. ПЕРИМЕТР: АВТОМАТЫ КЛИЕНТА")
    table(cur, BASE + """
        select count(distinct sn)                                  as terminals,
               min(ts)::date                                       as first_day,
               max(ts)::date                                       as last_day,
               count(*) filter (where not mistap)                  as payments,
               count(*) filter (where mistap)                      as card_taps,
               round(sum(amount) filter (where approved), 0)       as revenue_aed
        from f""", maxw=20)

    head("0b. СПИСОК АВТОМАТОВ")
    table(cur, BASE + """
        select sn, loc,
               min(ts)::date                                  as first_sale,
               max(ts)::date                                  as last_sale,
               count(*) filter (where not mistap)             as payments,
               count(*) filter (where mistap)                 as card_taps,
               round(sum(amount) filter (where approved), 0)  as revenue_aed
        from f
        group by 1, 2
        order by 6 desc""", maxw=30)

    # ── 1. подпись карты: доказательство, что это не отказ банка ─────────────
    head("1. ЧТО ИМЕННО ПРИКЛАДЫВАЮТ: РАЗБОР НЕУСПЕШНЫХ ПРИКЛАДЫВАНИЙ")
    say("  У платёжной карты всегда есть AID (платёжное приложение) и PAN (номер).")
    say("  Строки без обоих — это карта, которую POS не может принять в принципе.")
    table(cur, BASE + """
        select entry                                        as pos_entry_mode,
               case when aid = '' then 'нет' else 'есть' end as aid_,
               case when pan = '' then 'нет' else 'есть' end as pan_,
               nullif(code, '')                             as response_code,
               count(*)                                     as events
        from f
        where not approved
        group by 1, 2, 3, 4
        order by 5 desc
        limit 20""", maxw=24)

    # ── 2. прикладывания карты площадки по месяцам ───────────────────────────
    head("2. ПРИКЛАДЫВАНИЯ КАРТЫ ПЛОЩАДКИ ПО МЕСЯЦАМ")
    say("  taps_share — доля таких прикладываний от всех прикладываний карты к POS.")
    table(cur, BASE + f"""
        select to_char(date_trunc('month', ts), 'YYYY-MM')            as month,
               count(*) filter (where mistap)                         as card_taps,
               count(distinct ts::date) filter (where mistap)         as days,
               round(count(*) filter (where mistap)
                     / nullif(count(distinct ts::date)
                              filter (where mistap), 0)::numeric, 1)  as taps_per_day,
               count(distinct sn) filter (where mistap)               as terminals,
               count(*) filter (where not mistap)                     as payments,
               round(100.0 * count(*) filter (where mistap)
                     / nullif(count(*), 0), 1)                        as taps_share_pct,
               round(sum(amount) filter (where approved), 0)          as revenue_aed,
               round(sum(amount) filter (where approved)
                     / nullif(count(*) filter (where approved), 0), 2) as avg_check
        from f, lastday
        where ts >= date_trunc('month', lastday.d) - interval '{MONTHS - 1} month'
        group by 1
        order by 1""", maxw=20)

    # ── 3. заплатил ли человек после промаха ─────────────────────────────────
    head("3. ЧТО ПРОИСХОДИТ ПОСЛЕ ПРОМАХА")
    say("  paid_3min — на том же автомате в следующие 3 минуты прошла успешная оплата:")
    say("  человек достал банковскую карту. Остальные ушли, не купив.")
    table(cur, BASE + f"""
        , ev as (
          select sn, ts, mistap,
                 min(ts) filter (where approved) over (
                   partition by sn order by ts
                   rows between 1 following and unbounded following) as next_ok
          from f
        )
        select to_char(date_trunc('month', ts), 'YYYY-MM')    as month,
               count(*)                                       as card_taps,
               count(*) filter (where next_ok <= ts + interval '3 minute') as paid_3min,
               round(100.0 * count(*) filter (where next_ok <= ts + interval '3 minute')
                     / nullif(count(*), 0), 1)                as recovered_pct,
               count(*) filter (where next_ok is null
                                      or next_ok > ts + interval '3 minute') as walked_away
        from ev, lastday
        where mistap
          and ts >= date_trunc('month', lastday.d) - interval '{MONTHS - 1} month'
        group by 1
        order by 1""", maxw=20)

    # ── 4. по автоматам за последние 90 дней ─────────────────────────────────
    head("4. ПО АВТОМАТАМ ЗА ПОСЛЕДНИЕ 90 ДНЕЙ")
    table(cur, BASE + """
        select sn, loc,
               count(*) filter (where mistap)                        as card_taps,
               count(*) filter (where not mistap)                    as payments,
               round(100.0 * count(*) filter (where mistap)
                     / nullif(count(*), 0), 1)                       as taps_share_pct,
               round(sum(amount) filter (where approved), 0)         as revenue_aed,
               round(sum(amount) filter (where approved)
                     / nullif(count(*) filter (where approved), 0), 2) as avg_check
        from f, lastday
        where ts::date > lastday.d - 90
        group by 1, 2
        order by 3 desc""", maxw=30)

    # ── 5. часы суток ────────────────────────────────────────────────────────
    head("5. ЧАСЫ СУТОК ЗА ПОСЛЕДНИЕ 90 ДНЕЙ")
    table(cur, BASE + """
        select extract(hour from ts)::int              as hour,
               count(*) filter (where mistap)          as card_taps,
               count(*) filter (where not mistap)      as payments,
               round(100.0 * count(*) filter (where mistap)
                     / nullif(count(*), 0), 1)         as taps_share_pct
        from f, lastday
        where ts::date > lastday.d - 90
        group by 1
        order by 1""", maxw=20)

    # ── 6. дни недели ────────────────────────────────────────────────────────
    head("6. ДНИ НЕДЕЛИ ЗА ПОСЛЕДНИЕ 90 ДНЕЙ")
    table(cur, BASE + """
        select extract(isodow from ts)::int || ' ' || to_char(ts, 'Dy') as dow,
               count(distinct ts::date)                   as days,
               count(*) filter (where mistap)              as card_taps,
               round(count(*) filter (where mistap)
                     / nullif(count(distinct ts::date), 0)::numeric, 1) as taps_per_day,
               count(*) filter (where not mistap)          as payments
        from f, lastday
        where ts::date > lastday.d - 90
        group by 1
        order by 1""", maxw=20)

    # ── 7. пиковые дни ───────────────────────────────────────────────────────
    head("7. ПИКОВЫЕ ДНИ ПО ПРИКЛАДЫВАНИЯМ КАРТЫ ПЛОЩАДКИ")
    table(cur, BASE + """
        select ts::date                                   as day,
               to_char(ts, 'Dy')                          as dow,
               count(*) filter (where mistap)              as card_taps,
               count(*) filter (where not mistap)          as payments,
               count(distinct sn) filter (where mistap)    as terminals
        from f
        group by 1, 2
        order by 3 desc
        limit 15""", maxw=20)

    # ── 8. сколько раз подряд пробуют ────────────────────────────────────────
    head("8. СКОЛЬКО ПРИКЛАДЫВАНИЙ ЗА ДЕНЬ НА ОДИН АВТОМАТ")
    table(cur, BASE + """
        , dd as (
          select sn, ts::date as day, count(*) as taps
          from f, lastday
          where mistap and ts::date > lastday.d - 90
          group by 1, 2
        )
        select case when taps = 1 then '1'
                    when taps <= 3 then '2-3'
                    when taps <= 10 then '4-10'
                    when taps <= 30 then '11-30'
                    else '31+' end                  as taps_per_day,
               count(*)                             as terminal_days,
               sum(taps)                            as card_taps
        from dd
        group by 1
        order by min(taps)""", maxw=20)

    # ── 9. сколько человек за этими прикладываниями ──────────────────────────
    head("9. ПРИКЛАДЫВАНИЯ ПРОТИВ ЧИСЛА ГОСТЕЙ")
    say("  Тапы подряд на одном автомате с разрывом меньше 3 минут — это один")
    say("  человек, который пробует ещё раз. guest_tries — оценка числа гостей,")
    say("  и именно её надо брать для расчёта недополученных продаж.")
    table(cur, BASE + f"""
        , t as (
          select sn, ts,
                 case when lag(ts) over (partition by sn order by ts) is null
                        or ts - lag(ts) over (partition by sn order by ts)
                             > interval '3 minute'
                      then 1 else 0 end as new_try
          from f
          where mistap
        )
        select to_char(date_trunc('month', ts), 'YYYY-MM')        as month,
               count(*)                                           as card_taps,
               sum(new_try)                                       as guest_tries,
               round(count(*)::numeric / nullif(sum(new_try), 0), 2) as taps_per_guest
        from t, lastday
        where ts >= date_trunc('month', lastday.d) - interval '{MONTHS - 1} month'
        group by 1
        order by 1""", maxw=20)

    cur.close(); conn.close()
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print("\n" + "=" * 78)
    print(f"Готово. Отчёт: {REPORT}")
    print("Пришлите его файлом в чат — соберу отчёт для клиента.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
