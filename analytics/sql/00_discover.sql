-- Разведка схемы: что вообще есть в базе и где лежат платежи.
-- Запуск: ./analytics/db.sh -f sql/00_discover.sql > out/00_discover.txt

\echo '=== 1. БАЗА И РОЛЬ ==='
select current_database() as db,
       current_user       as role,
       version()          as pg_version;

\echo ''
\echo '=== 2. СХЕМЫ И КОЛИЧЕСТВО ТАБЛИЦ ==='
select table_schema,
       count(*) as tables
from information_schema.tables
where table_schema not in ('pg_catalog','information_schema')
  and table_type = 'BASE TABLE'
group by 1
order by 2 desc;

\echo ''
\echo '=== 3. ТАБЛИЦЫ: РАЗМЕР И ОЦЕНКА СТРОК (топ-60) ==='
select n.nspname                                        as schema,
       c.relname                                        as table,
       to_char(c.reltuples, 'FM999,999,999')            as est_rows,
       pg_size_pretty(pg_total_relation_size(c.oid))    as size
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where c.relkind = 'r'
  and n.nspname not in ('pg_catalog','information_schema')
order by pg_total_relation_size(c.oid) desc
limit 60;

\echo ''
\echo '=== 4. ТАБЛИЦЫ, ПОХОЖИЕ НА ПЛАТЁЖНЫЕ (по имени) ==='
select table_schema, table_name
from information_schema.tables
where table_schema not in ('pg_catalog','information_schema')
  and table_type = 'BASE TABLE'
  and (table_name ~* 'pay|transact|invoice|charge|order|purchase|subscript|billing|refund|checkout|tariff|price|balance|wallet')
order by 1, 2;

\echo ''
\echo '=== 5. КОЛОНКИ С ДЕНЕЖНОЙ/СТАТУСНОЙ СЕМАНТИКОЙ ==='
select table_schema, table_name, column_name, data_type
from information_schema.columns
where table_schema not in ('pg_catalog','information_schema')
  and (column_name ~* 'amount|sum|total|price|cost|currency|status|paid|payment|refund|provider|method'
       or data_type in ('money','numeric'))
order by table_schema, table_name, ordinal_position;

\echo ''
\echo '=== 6. ВНЕШНИЕ КЛЮЧИ (как связаны сущности) ==='
select tc.table_schema || '.' || tc.table_name       as from_table,
       kcu.column_name                               as from_column,
       ccu.table_schema || '.' || ccu.table_name     as to_table,
       ccu.column_name                               as to_column
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
     on kcu.constraint_name = tc.constraint_name
    and kcu.table_schema    = tc.table_schema
join information_schema.constraint_column_usage ccu
     on ccu.constraint_name = tc.constraint_name
    and ccu.table_schema    = tc.table_schema
where tc.constraint_type = 'FOREIGN KEY'
  and tc.table_schema not in ('pg_catalog','information_schema')
order by 1, 2;

\echo ''
\echo '=== 7. ENUM-ТИПЫ (статусы платежей и т.п.) ==='
select t.typname as enum_type,
       string_agg(e.enumlabel, ' | ' order by e.enumsortorder) as values
from pg_type t
join pg_enum e on e.enumtypid = t.oid
join pg_namespace n on n.oid = t.typnamespace
where n.nspname not in ('pg_catalog','information_schema')
group by 1
order by 1;
