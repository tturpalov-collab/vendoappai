-- Детали по одной таблице: колонки, индексы, пример строк.
-- Запуск: ./analytics/db.sh -v tbl=public.payments -f sql/01_table_detail.sql

\echo '=== КОЛОНКИ ==='
select ordinal_position as pos, column_name, data_type, is_nullable, column_default
from information_schema.columns
where (table_schema || '.' || table_name) = :'tbl'
order by ordinal_position;

\echo ''
\echo '=== ИНДЕКСЫ ==='
select indexname, indexdef
from pg_indexes
where (schemaname || '.' || tablename) = :'tbl';

\echo ''
\echo '=== ТОЧНОЕ ЧИСЛО СТРОК ==='
\set cnt 'select count(*) as exact_rows from ' :tbl
:cnt ;

\echo ''
\echo '=== ПРИМЕР 10 СТРОК ==='
\set smp 'select * from ' :tbl ' limit 10'
:smp ;
