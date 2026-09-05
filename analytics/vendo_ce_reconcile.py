#!/usr/bin/env python3
"""
Сверка ошибок CE с прошлым снимком базы.

    python3 vendo_ce_reconcile.py

Базовые значения снимка от 26.08.2026 зашиты в скрипт: он сам покажет
было / стало / дельту по месяцам, организациям и терминалам.
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
REPORT = os.path.join(HERE, "vendo_ce_reconcile_report.txt")

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



# ── БАЗОВЫЕ ЗНАЧЕНИЯ: снимок от 26.08.2026, данные по 26 августа ──────────────
SNAPSHOT = "26.08.2026 (данные по 26 августа)"

BASE_MONTH = {
    "2025-10": 39, "2025-11": 13, "2025-12": 109,
    "2026-01": 63, "2026-02": 45, "2026-03": 6251, "2026-04": 10932,
    "2026-05": 1629, "2026-06": 2496, "2026-07": 2061, "2026-08": 576,
}

BASE_AUG_ORG = {
    "upay-general-trading": 401, "modern-vending": 79, "ginco-general-trading": 27,
    "vendotek-uae": 20, "awafi-vending": 15, "alameed": 12, "tazizi": 10,
    "coffepoint": 5, "mania-trading": 5, "fuelup": 1, "soco-br-of-majid-al-futtaim": 1,
}

BASE_AUG_TERM = {
    "221400017033": 176, "221400014242": 43, "221400017027": 38, "221400016790": 29,
    "221400017144": 24, "221400016716": 21, "221400010768": 20, "221400010888": 16,
    "221400016444": 15, "221400010875": 15, "221400016430": 15, "221400016438": 15,
    "221400010886": 13, "221400016436": 10, "221400010764": 9, "221400010887": 8,
    "221400010833": 7, "221400010864": 6, "221400010739": 6, "221400016440": 6,
    "221400016787": 5, "221400016769": 5, "221400017017": 5, "221400010898": 4,
    "221400014005": 4,
}

BASE_TAZIZI_MONTH = {"2026-06": 59, "2026-07": 71, "2026-08": 10}
BASE_LAST_PAYMENT = "2026-08-26"
BASE_PAYMENTS = 535236

MISTAP = ("coalesce(c.pos_entry_mode,'') = 'CONTACTLESS' "
          "and coalesce(c.aid,'') = '' and coalesce(c.pan,'') = ''")

BASE = f"""
with f as (
  select p.id, p.approved, p.pos_localtime_at as ts,
         v.organization_name as org, u.sn,
         coalesce(nullif(u.location_name,''), '—') as loc,
         coalesce(c.response_code,'') as code,
         ({MISTAP}) as mistap
  from vendotek_payment p
  join vendotek_vend v on v.id = p.vend_id
  join vendotek_unit u on u.id = v.unit_id
  left join vendotek_payment_cashless c on c.payment_id = p.id
),
fc as (select * from f where not mistap)
"""

def fetch(cur, sql):
    cur.execute(sql)
    return cur.fetchall()

def delta_line(key, was, now, width=30):
    d = now - was
    if d == 0:
        mark, ds = "  ", "без изменений"
    elif d > 0:
        mark, ds = "▲ ", f"+{d}"
        if was:
            ds += f"  ({d/was*100:+.0f} %)"
    else:
        mark, ds = "▼ ", f"{d}"
        if was:
            ds += f"  ({d/was*100:+.0f} %)"
    return f"  {mark}{str(key):<{width}} было {was:>6}   стало {now:>6}   {ds}"

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

    say(f"Сверка ошибок CE — {datetime.now():%Y-%m-%d %H:%M}")
    say(f"Базовый снимок: {SNAPSHOT}")
    say("Промахи по чужой карте (MiFare) исключены.")

    # ── 0. свежесть данных ────────────────────────────────────────────────────
    head("0. НАСКОЛЬКО ОБНОВИЛАСЬ БАЗА")
    r = fetch(cur, """
        select count(*) as payments,
               min(pos_localtime_at)::date as first_day,
               max(pos_localtime_at)::date as last_day,
               max(synced_at)::date as last_sync
        from vendotek_payment""")[0]
    payments, first_day, last_day, last_sync = r
    dp = payments - BASE_PAYMENTS
    say(f"  платежей в базе:      было {BASE_PAYMENTS:>9,}   стало {payments:>9,}   "
        f"{dp:+,}".replace(",", " "))
    say(f"  последний платёж:     было {BASE_LAST_PAYMENT}   стало {last_day}")
    say(f"  последняя синхронизация: {last_sync}")
    say(f"  диапазон данных: {first_day} … {last_day}")
    if str(last_day) == BASE_LAST_PAYMENT:
        say("  [!] База не обновлялась с прошлого снимка — сверять нечего.")

    # ── 1. CE по месяцам ──────────────────────────────────────────────────────
    head("1. CE ПО МЕСЯЦАМ: БЫЛО / СТАЛО")
    rows = fetch(cur, BASE + """
        select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
               count(*) filter (where not approved and code = 'CE') as ce
        from fc where ts >= '2025-10-01' group by 1 order by 1""")
    now_month = {m: c for m, c in rows}
    for m in sorted(set(BASE_MONTH) | set(now_month)):
        say(delta_line(m, BASE_MONTH.get(m, 0), now_month.get(m, 0), width=10))
    tw, tn = sum(BASE_MONTH.values()), sum(now_month.values())
    say("")
    say(delta_line("ИТОГО с окт. 2025", tw, tn, width=10))

    # ── 2. август целиком ─────────────────────────────────────────────────────
    head("2. АВГУСТ ЗАКРЫЛСЯ: CE ПО ОРГАНИЗАЦИЯМ")
    say("  Базовый снимок держал август по 26-е число; теперь месяц полный.")
    say("")
    rows = fetch(cur, BASE + """
        select org, count(*) filter (where not approved and code = 'CE') as ce
        from fc where ts >= '2026-08-01' and ts < '2026-09-01'
        group by 1 having count(*) filter (where not approved and code = 'CE') > 0
        order by 2 desc""")
    now_org = {o: c for o, c in rows}
    for o in sorted(set(BASE_AUG_ORG) | set(now_org), key=lambda k: -now_org.get(k, 0)):
        say(delta_line(o, BASE_AUG_ORG.get(o, 0), now_org.get(o, 0)))
    say("")
    say(delta_line("ВСЕГО ЗА АВГУСТ", sum(BASE_AUG_ORG.values()), sum(now_org.values())))

    head("3. АВГУСТ: CE ПО ТЕРМИНАЛАМ")
    rows = fetch(cur, BASE + """
        select sn, loc, org, count(*) filter (where not approved and code = 'CE') as ce
        from fc where ts >= '2026-08-01' and ts < '2026-09-01'
        group by 1, 2, 3 having count(*) filter (where not approved and code = 'CE') > 0
        order by 4 desc""")
    now_term = {sn: ce for sn, _, _, ce in rows}
    meta = {sn: (loc, org) for sn, loc, org, _ in rows}
    for sn in sorted(set(BASE_AUG_TERM) | set(now_term), key=lambda k: -now_term.get(k, 0)):
        loc, org = meta.get(sn, ("—", "—"))
        say(delta_line(f"{sn}  {loc[:18]:<18} {org[:20]}",
                       BASE_AUG_TERM.get(sn, 0), now_term.get(sn, 0), width=44))
    new_t = set(now_term) - set(BASE_AUG_TERM)
    if new_t:
        say("")
        say(f"  Новые терминалы с CE, которых не было в снимке: {len(new_t)}")

    # ── 4. сентябрь ───────────────────────────────────────────────────────────
    head("4. СЕНТЯБРЬ — ЧЕГО В ПРОШЛОМ СНИМКЕ НЕ БЫЛО ВОВСЕ")
    table(cur, BASE + """
        select to_char(date_trunc('week', ts), 'YYYY-MM-DD') as week,
               count(*) as attempts,
               count(*) filter (where not approved) as declined,
               count(*) filter (where not approved and code = 'CE') as ce,
               count(*) filter (where not approved and code = '') as no_answer,
               round(100.0 * count(*) filter (where not approved) / count(*), 2) as decline_pct
        from fc where ts >= '2026-09-01' group by 1 order by 1""")

    head("5. СЕНТЯБРЬ: CE ПО ОРГАНИЗАЦИЯМ И ТЕРМИНАЛАМ")
    table(cur, BASE + """
        select org, count(*) as attempts,
               count(*) filter (where not approved and code = 'CE') as ce,
               round(100.0 * count(*) filter (where not approved and code = 'CE')
                     / count(*), 2) as ce_pct
        from fc where ts >= '2026-09-01' group by 1
        having count(*) filter (where not approved and code = 'CE') > 0
        order by 3 desc""")
    say("")
    table(cur, BASE + """
        select sn, loc, org, count(*) as attempts,
               count(*) filter (where not approved and code = 'CE') as ce
        from fc where ts >= '2026-09-01' group by 1, 2, 3
        having count(*) filter (where not approved and code = 'CE') > 0
        order by 5 desc limit 25""")

    # ── 6. tazizi ─────────────────────────────────────────────────────────────
    head("6. TAZIZI: CE ПО МЕСЯЦАМ, БЫЛО / СТАЛО")
    rows = fetch(cur, BASE + """
        select to_char(date_trunc('month', ts), 'YYYY-MM') as month,
               count(*) filter (where not approved and code = 'CE') as ce
        from fc where org = 'tazizi' and ts >= '2026-06-01' group by 1 order by 1""")
    now_tz = {m: c for m, c in rows}
    for m in sorted(set(BASE_TAZIZI_MONTH) | set(now_tz)):
        say(delta_line(m, BASE_TAZIZI_MONTH.get(m, 0), now_tz.get(m, 0), width=10))

    head("7. TAZIZI: НА КАКИХ ТЕРМИНАЛАХ CE, ПО МЕСЯЦАМ")
    table(cur, BASE + """
        select sn, loc,
               count(*) filter (where not approved and code = 'CE'
                    and ts >= '2026-06-01' and ts < '2026-07-01') as ce_jun,
               count(*) filter (where not approved and code = 'CE'
                    and ts >= '2026-07-01' and ts < '2026-08-01') as ce_jul,
               count(*) filter (where not approved and code = 'CE'
                    and ts >= '2026-08-01' and ts < '2026-09-01') as ce_aug,
               count(*) filter (where not approved and code = 'CE'
                    and ts >= '2026-09-01') as ce_sep,
               count(*) filter (where ts >= '2026-06-01') as attempts
        from fc where org = 'tazizi' and ts >= '2026-06-01'
        group by 1, 2
        having count(*) filter (where not approved and code = 'CE') > 0
        order by 5 desc, 4 desc""")

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
