# vendoappai — аналитика платежей

## Что это за задача

Аналитика по платежам из production-базы (PostgreSQL на AWS RDS).
Пользователь хочет, чтобы **данные собирал и анализировал ассистент**, а не
запускал скрипты руками. Не предлагайте ему «вот скрипт, запустите» — поднимайте
туннель, ходите в базу и делайте выводы сами, показывая пользователю результат.

## Доступ к базе

База в приватной сети, доступ только через SSH-туннель к bastion-хосту:

```
ssh -L 54322:vendo-app.cvwxd0jliglu.eu-central-1.rds.amazonaws.com:5432 ec2-user@18.196.28.193 -i <ключ>
```

Пользователь БД — `vendo_ai_reader`, **read-only**. Пароль и путь к ключу лежат
в `analytics/.env` (не в git). Если файла нет — попросите пользователя создать
его из `analytics/.env.example`.

> Важно: это **не работает** из Claude Code на вебе — там исходящий порт 22
> заблокирован сетевой политикой окружения, и `ssh` не установлен. Задача
> рассчитана на локальный запуск (CLI / десктоп-приложение).

## Инструменты (запускает ассистент, не пользователь)

- `./analytics/tunnel.sh up|down|status` — SSH-туннель на 127.0.0.1:54322
- `./analytics/db.sh` — `psql` через туннель (туннель поднимается сам)
  - `./analytics/db.sh -c "select ..."` — одиночный запрос
  - `./analytics/db.sh -f sql/00_discover.sql` — файл
  - `./analytics/db.sh --csv -f sql/x.sql > analytics/out/x.csv` — выгрузка
- `analytics/sql/00_discover.sql` — разведка схемы (таблицы, размеры, платёжные
  таблицы и колонки, внешние ключи, enum-статусы)
- `analytics/sql/01_table_detail.sql` — колонки/индексы/примеры строк:
  `./analytics/db.sh -v tbl=public.payments -f sql/01_table_detail.sql`

## Если нет psql

`psql` на машине может отсутствовать (Homebrew недоступен из-за сетевых
ограничений — `raw.githubusercontent.com` режется). Не заставляйте пользователя
что-то ставить руками: разберитесь сами.

Порядок предпочтений:

1. `psql` — проверить `command -v psql`, а также
   `/Applications/Postgres.app/Contents/Versions/latest/bin/psql`.
2. Python + `psycopg`: `python3 -m pip install --user "psycopg[binary]"`
   (pypi.org обычно доступен). Туннель поднимается тем же `analytics/tunnel.sh`,
   а запросы идут через `psycopg.connect(host="127.0.0.1", port=54322, ...)`,
   параметры берутся из `analytics/.env`.

Во втором случае положите тонкую обёртку в `analytics/query.py`, чтобы
дальнейшие запросы шли одной командой, и обновите этот раздел.

## Порядок работы

1. Схема базы пока не разобрана — начните с `00_discover.sql` и разберитесь,
   где лежат платежи, какие статусы, валюты и связи с пользователями/заказами.
2. Запишите разобранную схему сюда, в раздел «Схема», чтобы следующие сессии
   не повторяли разведку.
3. Дальше — витрины под запросы пользователя: выручка по периодам, средний чек,
   конверсия, возвраты, разрезы по методам оплаты и когортам.
4. Готовые запросы складывайте в `analytics/sql/` с говорящими именами.

## Схема

База `app` на сервере (не `postgres` — та пустая), PostgreSQL 18.3, схема `public`,
16 таблиц с префиксом `vendotek_`. Данные — зеркало внешней системы Vendotek:
почти везде есть `synced_at`, у части таблиц `dirty` / `external`.

**Предметная область: вендинговые автоматы в ОАЭ.** Валюта во всех продажах — `AED`,
организация `vendotek-uae`. Цены в `vendotek_planogram_product.price` — `bigint`,
предположительно в филсах (1 AED = 100 fils), проверять перед использованием.

### Платежи и продажи

| Таблица | Строк | Роль |
|---|---|---|
| `vendotek_payment` | 535 224 | **Факт-таблица платежей.** `cash_amount`, `cashless_amount`, `approved`, `name` (SALE), `pos_localtime_at` |
| `vendotek_payment_cashless` | 467 044 | Детализация безнала: `amount`, `pan` (маскированный), `application_label`, `pos_entry_mode`, `response_code`, `rrn`, `auth_id`, `issuer`, `transaction_duration_s`, `vend_duration_s` |
| `vendotek_payment_cash` | 68 180 | Детализация наличных: `amount`, `type` = CASH |
| `vendotek_vend` | 531 075 | Выдача товара: `unit_id`, `product_id`, `product_name`, `completed`, `cancelled`, `currency`, `terminal_id`, `organization_name` |
| `vendotek_vend_planogram_product_link` | 385 268 | Связь продажи с товарами |
| `vendotek_vend_fiscal` | мало | Фискализация: `qr`, `address`, `place` |

### Справочники и телеметрия

| Таблица | Строк | Роль |
|---|---|---|
| `vendotek_unit` | 1 008 | Автоматы: `sn`, `tid`, `location_name`, `address`, `city`, `region`, `country`, `tz` |
| `vendotek_org` | 27 | Организации, `distributor_id` |
| `vendotek_planogram_product` | 54 935 | Товары: `name`, `price`, `vat`, `code`, `gtin` |
| `vendotek_planogram`, `..._planogram_product_link` | мало | Планограммы автоматов |
| `vendotek_module`, `..._module_detail`, `..._module_link` | 6 435 / 17 878 / 3 432 | Телеметрия модулей автоматов |
| `databasechangelog`, `databasechangeloglock` | 15 / — | Служебные (Liquibase), в аналитике не участвуют |

### Связи

**Внешних ключей в базе нет** — связи только логические, по `uuid`:

```
vendotek_payment.vend_id           -> vendotek_vend.id
vendotek_payment_cash.payment_id   -> vendotek_payment.id
vendotek_payment_cashless.payment_id -> vendotek_payment.id
vendotek_vend.unit_id              -> vendotek_unit.id
vendotek_unit.owned_by_org_id      -> vendotek_org.id
vendotek_vend_planogram_product_link.vend_id -> vendotek_vend.id
```

Проверка сходимости: 68 180 (cash) + 467 044 (cashless) = 535 224 = ровно число
платежей. То есть у каждого платежа ровно одна строка детализации.

### Ловушки — читать перед любым запросом

1. **`synced_at` — это НЕ дата операции**, а момент выгрузки из внешней системы
   (вся выгрузка приходится на 19–25 августа 2026). Агрегировать по нему нельзя:
   получится вся выручка в одном месяце. Реальное время операции —
   `vendotek_payment.pos_localtime_at` (`timestamp without time zone`, локальное
   время терминала; смещение в `pos_localtime_offset_s`). Диапазон данных:
   **2025-05-06 … 2026-08-25**.
2. **Сумма платежа = `cash_amount + cashless_amount`.** По отдельности каждое поле
   заполнено только для своего типа оплаты, у остальных ноль — средний чек по одной
   колонке занижен в разы.
3. **Статусов-enum нет.** Успех платежа — булев `approved`; у безнала дополнительно
   `response_code` (`000` = успех). Отмена продажи — `vendotek_vend.cancelled`.
4. В ранних данных (май–сентябрь 2025) много транзакций на 0.01–0.04 AED — похоже на
   тестовые прогоны. Для бизнес-метрик их стоит отфильтровывать.

## Правила

- Учётка read-only: только `SELECT`. Ничего не писать и не менять в базе.
- Секреты (`.env`, ключи) и выгрузки из `analytics/out/` — в `.gitignore`,
  в репозиторий не коммитить.
- На больших таблицах ставить `LIMIT` при разведке; для агрегатов сначала
  проверять объём через оценку строк.
