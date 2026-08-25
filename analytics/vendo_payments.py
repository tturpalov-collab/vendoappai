#!/usr/bin/env python3
"""
Разведка платежей в базе Vendo — одной командой.

    python3 vendo_payments.py

Что делает сам:
  * поднимает SSH-туннель до RDS через bastion вашим ключом;
  * при необходимости ставит psycopg (в --user, без sudo);
  * обходит базу и ищет, где лежат платежи;
  * собирает отчёт в vendo_payments_report.txt рядом со скриптом.

Только чтение: ни одного INSERT/UPDATE/DELETE. Учётка read-only.
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
PGDATABASE = os.environ.get("PGDATABASE", "postgres")

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "vendo_payments_report.txt")

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
        cur.execute(sql, args or ())
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

# ── шаг 4: разведка ───────────────────────────────────────────────────────────
def list_databases(cur):
    """Все базы на сервере, куда пускают."""
    return [r[0] for r in q(cur, """
        select datname from pg_database
        where not datistemplate and datallowconn order by 1""")]

def count_tables(cur):
    rows = q(cur, """
        select count(*) from information_schema.tables
        where table_schema not in ('pg_catalog','information_schema')
          and table_type = 'BASE TABLE'""")
    return rows[0][0] if rows else 0

def pick_database(driver, password, first_conn, first_db):
    """Ищет базу, в которой реально есть таблицы."""
    cur = first_conn.cursor()
    n = count_tables(cur)
    dbs = list_databases(cur)
    say(f"  базы на сервере: {', '.join(dbs) if dbs else '—'}")
    if n > 0:
        say(f"  в '{first_db}' таблиц: {n} — работаем с ней")
        cur.close()
        return first_conn, first_db
    say(f"  в '{first_db}' таблиц нет — смотрю остальные")
    cur.close(); first_conn.close()

    best, best_n, best_conn = None, 0, None
    for db in dbs:
        if db == first_db:
            continue
        try:
            c = connect(driver, password, db)
        except Exception as e:
            say(f"    {db}: не пускает ({str(e).splitlines()[0][:60]})")
            continue
        c.autocommit = True
        cu = c.cursor()
        k = count_tables(cu)
        cu.close()
        say(f"    {db}: таблиц {k}")
        if k > best_n:
            if best_conn:
                best_conn.close()
            best, best_n, best_conn = db, k, c
        else:
            c.close()
    if not best_conn:
        sys.exit("Ни в одной базе не видно таблиц — возможно, у vendo_ai_reader "
                 "нет прав на нужную схему. Покажите отчёт ассистенту.")
    say(f"  выбрана база: {best} ({best_n} таблиц)")
    return best_conn, best

def explore(cur):
    head("1. БАЗА")
    table(cur, "select current_database() as db, current_user as role, "
               "substring(version() from 'PostgreSQL [0-9.]+') as version")

    say("\n  схемы и число таблиц:")
    table(cur, """
        select table_schema, count(*) as tables
        from information_schema.tables
        where table_schema not in ('pg_catalog','information_schema')
          and table_type = 'BASE TABLE'
        group by 1 order by 2 desc""")

    head("2. ТАБЛИЦЫ: РАЗМЕР И ОЦЕНКА СТРОК (топ-40)")
    table(cur, """
        select n.nspname as schema, c.relname as table,
               c.reltuples::bigint as est_rows,
               pg_size_pretty(pg_total_relation_size(c.oid)) as size
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind = 'r' and n.nspname not in ('pg_catalog','information_schema')
        order by pg_total_relation_size(c.oid) desc limit 40""")

    head("3. ВСЕ ТАБЛИЦЫ")
    all_tables = q(cur, """
        select table_schema, table_name from information_schema.tables
        where table_schema not in ('pg_catalog','information_schema')
          and table_type = 'BASE TABLE' order by 1,2""")
    for sch, tbl in all_tables:
        say(f"  {sch}.{tbl}")
    say(f"  — всего {len(all_tables)}")

    head("4. КОЛОНКИ ВСЕХ ТАБЛИЦ")
    cols = q(cur, """
        select table_schema, table_name, column_name, data_type
        from information_schema.columns
        where table_schema not in ('pg_catalog','information_schema')
        order by table_schema, table_name, ordinal_position""")
    by_table = {}
    for sch, tbl, col, typ in cols:
        by_table.setdefault((sch, tbl), []).append((col, typ))
    for (sch, tbl), cl in by_table.items():
        say(f"  {sch}.{tbl}")
        for col, typ in cl:
            say(f"      {col} : {typ}")

    head("5. ВНЕШНИЕ КЛЮЧИ")
    table(cur, """
        select tc.table_schema||'.'||tc.table_name as from_table, kcu.column_name as from_col,
               ccu.table_schema||'.'||ccu.table_name as to_table, ccu.column_name as to_col
        from information_schema.table_constraints tc
        join information_schema.key_column_usage kcu
             on kcu.constraint_name = tc.constraint_name and kcu.table_schema = tc.table_schema
        join information_schema.constraint_column_usage ccu
             on ccu.constraint_name = tc.constraint_name and ccu.table_schema = tc.table_schema
        where tc.constraint_type = 'FOREIGN KEY'
          and tc.table_schema not in ('pg_catalog','information_schema')
        order by 1,2""")

    head("6. ENUM-ТИПЫ")
    table(cur, """
        select t.typname as enum_type,
               string_agg(e.enumlabel, ' | ' order by e.enumsortorder) as values
        from pg_type t join pg_enum e on e.enumtypid = t.oid
        join pg_namespace n on n.oid = t.typnamespace
        where n.nspname not in ('pg_catalog','information_schema')
        group by 1 order by 1""", maxw=90)

    # кандидаты в платёжные таблицы
    cands = []
    for (sch, tbl), cl in by_table.items():
        names = [c for c, _ in cl]
        score = 0
        if PAY_RE.search(tbl): score += 3
        if any(AMOUNT_RE.search(c) for c in names): score += 2
        if any(STATUS_RE.search(c) for c in names): score += 1
        if any(c.lower() in ("currency", "currency_code") for c in names): score += 2
        if score >= 3:
            cands.append((score, sch, tbl, cl))
    cands.sort(reverse=True, key=lambda x: x[0])
    return cands[:10]

def profile(cur, sch, tbl, cl):
    fq = f'"{sch}"."{tbl}"'
    head(f"ТАБЛИЦА {sch}.{tbl}")
    names = [c for c, _ in cl]
    types = dict(cl)

    rows = q(cur, f"select count(*) from {fq}")
    n = rows[0][0] if rows else 0
    say(f"  строк: {n}")
    if n == 0:
        return

    ts_cols = [c for c in names if TS_RE.search(c)
               and any(k in types[c] for k in ("timestamp", "date"))]
    amt_cols = [c for c in names if AMOUNT_RE.search(c)
                and types[c] in ("numeric", "money", "integer", "bigint",
                                 "double precision", "real")]
    st_cols = [c for c in names if STATUS_RE.search(c)]

    for c in ts_cols[:3]:
        say(f"\n  диапазон по {c}:")
        table(cur, f'select min("{c}") as min, max("{c}") as max from {fq}')

    for c in st_cols[:6]:
        say(f"\n  распределение по {c}:")
        table(cur, f'select "{c}" as value, count(*) as cnt from {fq} '
                   f'group by 1 order by 2 desc limit 15')

    if amt_cols and ts_cols:
        a, t = amt_cols[0], ts_cols[0]
        say(f"\n  сумма {a} по месяцам (последние 18):")
        table(cur, f'''select to_char(date_trunc('month', "{t}"), 'YYYY-MM') as month,
                              count(*) as cnt, round(sum("{a}"::numeric), 2) as total,
                              round(avg("{a}"::numeric), 2) as avg
                       from {fq} where "{t}" is not null
                       group by 1 order by 1 desc limit 18''')

    say("\n  пример строк (3):")
    table(cur, f"select * from {fq} limit 3", maxw=28)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    driver = ensure_psycopg()
    open_tunnel()
    pw = get_password()
    try:
        conn = connect(driver, pw)
    except Exception as e:
        sys.exit(f"Не подключился к базе: {str(e).splitlines()[0]}\nПокажите это ассистенту.")
    conn.autocommit = True

    say(f"Отчёт собран: {datetime.now():%Y-%m-%d %H:%M}")
    head("0. ВЫБОР БАЗЫ")
    conn, dbname = pick_database(driver, pw, conn, PGDATABASE)
    conn.autocommit = True
    cur = conn.cursor()

    cands = explore(cur)

    head("7. КАНДИДАТЫ В ПЛАТЁЖНЫЕ ТАБЛИЦЫ")
    if not cands:
        say("  Ничего похожего по именам не нашлось — смотрите раздел 4 целиком.")
    for score, sch, tbl, _ in cands:
        say(f"  {sch}.{tbl}   (вес {score})")

    for score, sch, tbl, cl in cands:
        profile(cur, sch, tbl, cl)

    cur.close(); conn.close()
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    print("\n" + "=" * 78)
    print(f"Готово. Отчёт: {REPORT}")
    print("Пришлите этот файл ассистенту — дальше аналитика на нём.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано.")
    finally:
        pass
