# Аналитика платежей — подключение к БД

База в приватной сети AWS, доступ только через SSH-туннель к bastion-хосту.
Скрипты рассчитаны на запуск **локально** (Mac/Linux), где лежит приватный ключ.

## Требования

- `ssh`, `nc` (есть в macOS из коробки)
- `psql` (`brew install libpq` + добавить в PATH, либо `brew install postgresql`)

## Настройка (один раз)

```bash
cp analytics/.env.example analytics/.env
$EDITOR analytics/.env          # подставить путь к ключу и пароль
chmod 600 analytics/.env
chmod 600 "/path/to/timurAI"    # ssh откажется работать с ключом, доступным всем
```

`analytics/.env` в `.gitignore` — пароль и путь к ключу в репозиторий не попадают.

> Пароль содержит `$` и `-`, поэтому в `.env` он **обязательно** в одинарных кавычках:
> `PGPASSWORD='...'`

## Использование

```bash
./analytics/tunnel.sh up        # поднять туннель (127.0.0.1:54322 -> RDS:5432)
./analytics/tunnel.sh status
./analytics/tunnel.sh down

./analytics/db.sh                                  # интерактивный psql
./analytics/db.sh -c "select now()"                # одиночный запрос
./analytics/db.sh -f sql/00_discover.sql           # выполнить файл
./analytics/db.sh --csv -f sql/x.sql > analytics/out/x.csv   # выгрузка в CSV
```

`db.sh` сам поднимает туннель, если тот ещё не поднят.

## Шаг 1 — разведка схемы

Структура базы пока неизвестна, поэтому первый запуск — разведочный:

```bash
./analytics/db.sh -f sql/00_discover.sql > analytics/out/00_discover.txt
```

Файл покажет: схемы, размеры таблиц, таблицы и колонки с платёжной семантикой,
внешние ключи и enum-статусы. Пришлите его в чат — по нему пишутся конкретные
витрины по платежам (выручка по дням, конверсия, средний чек, возвраты, когорты).

Детали по конкретной таблице:

```bash
./analytics/db.sh -v tbl=public.payments -f sql/01_table_detail.sql
```

## Учётка

`vendo_ai_reader` — read-only. Все скрипты только читают; ничего не пишут и не меняют.

## Структура

```
analytics/
├── .env.example      # шаблон конфига (реальный .env не коммитится)
├── tunnel.sh         # управление SSH-туннелем
├── db.sh             # psql через туннель
├── sql/
│   ├── 00_discover.sql      # разведка схемы
│   └── 01_table_detail.sql  # детали по одной таблице
└── out/              # выгрузки (в .gitignore)
```
