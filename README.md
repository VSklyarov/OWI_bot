# OWI Encar Parser

Парсер карточек объявлений корейского автомобильного маркетплейса
[Encar.com](https://encar.com) для проекта **ONE WAY IMPORT (OWI)**.

Принимает URL объявления `https://fem.encar.com/cars/detail/<id>?...`
(или legacy `https://www.encar.com/dc/dc_cardetailview.do?carid=<id>`)
и возвращает структурированный JSON с обязательными полями.

## Требования

- **Python 3.10 или выше** (проверено на 3.10/3.11/3.12/3.13)
- `pip`
- Доступ в интернет (парсер ходит к `api.encar.com`)

## Установка

```bash
git clone <repo-url> OWI_bot
cd OWI_bot
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux / Git Bash
source .venv/bin/activate

pip install -r requirements.txt
```

Внешних зависимостей всего одна: [`requests`](https://pypi.org/project/requests/).

## Запуск

### Распарсить один URL

```bash
python main.py "https://fem.encar.com/cars/detail/41453346?listAdvType=share"
```

Парсер печатает JSON в `stdout`:

```json
{
  "price_krw": 103000000,
  "brand": "BMW",
  "model": "X5 (G05) xDrive 50e xLine",
  "engine_cc": 2998,
  "horsepower": 489,
  "fuel_type": "hybrid",
  "drive": "4WD",
  "body_type": "SUV",
  "first_registration": "2025-06",
  "mileage_km": 20798,
  "is_leasing": false,
  "is_sold_or_pledge": false,
  "damages": []
}
```

### Прогнать все три тестовых URL и сохранить результаты

```bash
python main.py --test-all
```

Будут перезаписаны файлы:

- `results/result_41453346.json` — обычное объявление
- `results/result_41443212.json` — объявление с повреждениями (синие/красные метки)
- `results/result_41687040.json` — лизинговое объявление (`리스`)

Прогон занимает 1–2 секунды на быстром интернете.

### Помощь

```bash
python main.py -h
```

## Telegram-бот (для сдачи)

Бот делает ровно одно: принимает ссылку Encar и возвращает JSON-ответ, как `main.py`.

### Быстрый запуск локально (polling)

1) Создай бота у `@BotFather` и получи токен.

2) Запусти:

```bash
# PowerShell
$env:TELEGRAM_BOT_TOKEN="YOUR_TOKEN"
python bot.py
```

Теперь можно написать боту в Telegram и отправить ссылку Encar.

### Деплой

Подойдёт любой VPS/хостинг, где можно держать процесс:

- **VPS**: `screen/tmux` или `systemd`
- **PaaS**: Render/Railway/Fly.io (как long-running worker)

## Формат вывода

| Поле                   | Тип            | Пример                         |
| ---------------------- | -------------- | ------------------------------ |
| `price_krw`            | `int`          | `103000000`                    |
| `brand`                | `str`          | `"BMW"`                        |
| `model`                | `str`          | `"X5 (G05) xDrive 50e xLine"`  |
| `engine_cc`            | `int`          | `2998`                         |
| `horsepower`           | `int \| null`  | `489` (PS, заводская)          |
| `fuel_type`            | `str`          | `gasoline\|diesel\|hybrid\|electric` |
| `drive`                | `str`          | `2WD\|4WD\|FWD\|RWD`           |
| `body_type`            | `str`          | `"SUV"`                        |
| `first_registration`   | `str` (YYYY-MM)| `"2025-06"`                    |
| `mileage_km`           | `int`          | `20798`                        |
| `is_leasing`           | `bool`         | `false`                        |
| `is_sold_or_pledge`    | `bool`         | `false`                        |
| `damages`              | `list[object]` | `[{"part": "...", "type": "paint\|replace"}]` |

> **`horsepower`** строится 4-уровневым резолвером: попытка из API (`spec.horsePower`) →
> компактный `vehicles/view` → OEM engine code (`B58B30S` → 489) → marketing
> trim badge (`xDrive 50e` → 489) с year-aware дисамбигвацией (X5 G05 pre-LCI vs LCI).
> `null` возвращается только для редких трим-комбинаций, не покрытых таблицей.
> Полная стратегия и обоснование значений — в [`docs/method.md`](docs/method.md) §4.1.

При любой ошибке (URL некорректен, объявление удалено, API недоступно)
парсер возвращает:

```json
{ "error": "Описание причины ошибки" }
```

и завершает процесс с кодом `1`. **Стектрейсов в stdout не бывает.**

## Структура проекта

```
OWI_bot/
├── parser/
│   ├── __init__.py
│   ├── encar_parser.py     # ядро: HTTP, нормализация, обработка ошибок
│   ├── engine_specs.py     # таблицы horsepower (engine code + grade name) и drive heuristic
│   └── utils.py            # маппинги fuel/body/parts и парсинг URL
├── results/
│   ├── result_41453346.json
│   ├── result_41443212.json
│   └── result_41687040.json
├── docs/
│   └── method.md           # технический отчёт о методе обхода защиты
├── main.py                 # CLI-точка входа
├── requirements.txt
└── README.md
```

## Метод обхода антипарсинговой защиты (кратко)

* Используется reverse-engineered JSON API `api.encar.com/v1/readside/...`
  — тот же, что вызывает официальный мобильный фронт `fem.encar.com`.
* Никаких Selenium/Playwright, никакой капчи, никакой авторизации.
* Cloudflare / WAF на этих эндпоинтах не наблюдается. Достаточно
  браузероподобного `User-Agent` и `Origin/Referer`.
* Один запрос = до 4 GET'ов: `vehicle` + `inspection` + `vehicles/view` + `record`.
  Полное время — ~0.7–1.2 секунды на объявление.
* Нагрузка: **5–10 RPS на IP — безопасно**. Прокси на единичных запросах не
  требуются; на промышленной нагрузке рекомендуются дата-центровые
  с ротацией.
* `horsepower` и `drive` в API Encar отсутствуют как структурированные
  поля — реконструируются из `inspection.master.detail.motorType` (OEM
  engine code) и `category.gradeName` (marketing trim badge) с year-aware
  дисамбигвацией. На трёх тестовых URL значения совпадают с заводскими
  спецификациями BMW.

Полный технический отчёт со всеми наблюдениями, метриками мониторинга
и оценкой рисков стабильности — в [`docs/method.md`](docs/method.md).

## Лицензия

Test task. Использование — внутри OWI.
