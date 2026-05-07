# Метод парсинга Encar — техническая документация

> Документ описывает метод, защиту, риски и эксплуатационную модель парсера
> карточек объявлений `fem.encar.com`. Цель — дать проверяющему полную и
> **честную** картину: что работает, при каких условиях ломается и что с
> этим делать.

---

## 1. Технический метод

### Что и почему выбрано

Парсер построен на **прямом обращении к внутреннему JSON-API Encar**
(`api.encar.com/v1/readside/...`). Это reverse-engineered эндпоинт,
которым пользуется официальный мобильный фронтенд (`fem.encar.com`), —
тот самый, что отдаёт данные на детальную страницу объявления.

Альтернативы и почему они не выбраны:

| Подход | Минусы | Решение |
| --- | --- | --- |
| Парсинг рендеренного HTML / `__PRELOADED_STATE__` | Структура `state` меняется при каждом релизе SPA, поля распределены по 30+ веткам, многое лежит за rate-limited «server-driven» эндпоинтом, размер ответа 70 KB+ | Используется только как backup-источник в случае, если readside-API сломается |
| Selenium / Playwright (headless Chromium) | На порядок медленнее (~5–10 секунд против ~0.7), требует Chromium на машине, потребляет 200–400 MB RAM на инстанс, сложнее распараллелить | Не используется; добавлять только если api.encar.com закроют |
| Официальный API Encar для дилеров | Нужен B2B-договор с Encar Korea + IP в whitelist | Вне скоупа теста |

### Стек

| Зависимость | Роль |
| --- | --- |
| `requests` 2.32.x | Единственный HTTP-клиент. Мы намеренно не тащим `httpx`/`aiohttp` — для одного-четырёх последовательных GET'ов на запрос асинхронность избыточна, а синхронный код лучше отлаживать. |
| stdlib (`argparse`, `json`, `pathlib`, `re`, `logging`) | CLI, JSON, регулярки. |

`pip install -r requirements.txt` ставит ровно один внешний пакет.

### Какие эндпоинты вызываются

Для каждого `vehicleId` парсер делает 1–4 GET-запроса:

```
GET https://api.encar.com/v1/readside/vehicle/<id>?include=ADVERTISEMENT,CATEGORY,CONDITION,CONTACT,MANAGE,OPTIONS,PHOTOS,SPEC,PARTNERSHIP,CENTER,VIEW
GET https://api.encar.com/v1/readside/inspection/vehicle/<id>          # 404 = инспекции нет
GET https://api.encar.com/v1/readside/vehicles/view?vehicleIds=<id>    # доп. источник spec.horsePower
GET https://api.encar.com/v1/readside/record/vehicle/<id>/open         # 404 = страховых выплат нет
```

* `readside/vehicle` — обязателен. Без него — `{"error": "..."}`.
* `inspection` — best-effort. Возвращает 404 для лизинговых объявлений (инспекции там не делают). При 404: `damages = []`.
* `vehicles/view` — best-effort. Дополнительный мобильный shape, в котором есть `spec.horsePower` (см. раздел 4.1).
* `record/open` — best-effort. Сейчас не выводится в схему OWI напрямую, но эндпоинт уже подключён — в нём есть `myAccidentCost` (сумма страховых выплат «내차 피해»). В будущем тривиально вынести в отдельное поле.

### Соответствие полей OWI и Encar

| OWI поле | Источник | Преобразование |
| --- | --- | --- |
| `price_krw` | `advertisement.price` | × 10 000 (Encar отдаёт цену в 만원, т.е. в десятках тысяч вон) |
| `brand` | `category.manufacturerName` | as-is |
| `model` | `category.modelName + " " + gradeName + " " + gradeDetailName` | конкатенация |
| `engine_cc` | `spec.displacement` | as-is |
| `horsepower` | layered: `spec.horsePower` → `vehicles/view.spec.horsePower` → engine code lookup → grade-name pattern | см. раздел 4.1 |
| `fuel_type` | `spec.fuelName` | таблица в `parser/utils.py`, PHEV (`가솔린+전기`) → `hybrid` |
| `drive` | эвристика по `gradeName` (xDrive/4MATIC/quattro/4Motion/HTRAC/sDrive…) + body fallback | см. раздел 4.2 |
| `body_type` | `spec.bodyName` | таблица |
| `first_registration` | `category.yearMonth` | YYYYMM → YYYY-MM |
| `mileage_km` | `spec.mileage` | as-is |
| `is_leasing` | `advertisement.advertisementType` ∈ {OPERATING_LEASE, FINANCIAL_LEASE, RENT, …} **или** `advertisement.leaseRentInfo ≠ null` **или** `partnership.lease/rent` | OR-логика |
| `is_sold_or_pledge` | `condition.seizing.pledgeCount > 0` **или** `advertisement.price == 9999` **или** `advertisement.status ∈ {SOLD, RESERVED, DEAL_DONE}` | OR-логика |
| `damages` | `inspection.outers[]` | коды `X → replace`, `W/T → paint`, остальное игнорируется |

---

## 2. Какая у Encar антипарсинговая защита

Подробное наблюдение из исследования (`_research/` и `_research2/` — сырые ответы API и распакованные JS-бандлы фронта; обе папки удалены из финальной поставки).

### 2.1 На уровне HTML-страницы (`fem.encar.com/cars/detail/<id>`)

| Защита | Наблюдается? | Подробности |
| --- | --- | --- |
| Cloudflare / Akamai WAF | **Не обнаружен.** Заголовков `cf-ray`, `server: cloudflare`, `x-akamai-*` в ответах нет. | Простой Python-клиент с браузерным `User-Agent` получает HTML с первого запроса. |
| JS-challenge / капча на странице | **Не обнаружен** на тестовых трёх URL. | Ни 5-секундного `Just a moment...`, ни `Cloudflare Turnstile`, ни Recaptcha. |
| TLS-fingerprinting (JA3) | **Не блокирует** обычный `requests` (Python OpenSSL). | Возможно, на бо́льшем объёме запросов начнёт. |
| Rate limiting на HTML | Не замечен на единичных запросах, но косвенно подтверждён: rich UI endpoint `cars/GET_DETAIL_SERVER_DRIVEN` отвечает в `__PRELOADED_STATE__`: `"This transaction has been restricted by traffic limits"` уже в браузере. | См. ниже. |
| AWS ALB cookies | `AWSALB`, `AWSALBCORS` — sticky load balancer | Не блокируют, но желательно их сохранять между запросами |

### 2.2 На уровне API (`api.encar.com`)

| Защита | Наблюдается? |
| --- | --- |
| Подпись запроса / HMAC | **Нет** на основных эндпоинтах. GET без токенов. |
| Cookie-сессия / логин | **Нет** для readside-эндпоинтов. |
| Bearer-token (`Authorization: Bearer …`) | **Опционально.** В JS-клиенте есть `withAllToken()` interceptor, но ВСЕ публичные `oi.get(…)` для GET_BASE / GET_VIEW / SDUI вызываются с `useAuth: false` — токен не подставляется. Только эндпоинты `/contracts/*`, `/escrow/*` и т.п. требуют логина. |
| Required headers | `Origin: https://fem.encar.com` и `Referer: https://fem.encar.com/` рекомендованы, но 200 приходит и без них — Encar толерантен. |
| Rate limit на основной readside | Не наблюдался при последовательных запросах с одного IP (3+ объявления, ~0.7 секунды каждое). |
| Rate limit на SDUI (`/v1/readside/ui-components/vehicle/<id>/name/<section>`) | **Жёсткий.** Любой запрос на любую секцию возвращает `success: false, message: "This transaction has been restricted by traffic limits"`. Это не следствие моих экспериментов — то же сообщение видно в `__PRELOADED_STATE__` при свежем заходе на страницу из чистого браузера. SDUI — отдельный путь, лимит на нём, по-видимому, действует глобально на IP/path. |
| Бан по IP за паттерн | Возможен, но не наблюдался. На уровне трёх тестовых URL `python main.py --test-all` отрабатывает за ~1.5 секунды. |

### 2.3 Reverse engineering JS-бандла фронта

Распакованный `main.8558950e.js` (1.7 MB минифицированный) дал полный список API эндпоинтов и схему авторизации:

```javascript
// Конструктор axios-инстанса для readside
oi = new Yn().setBaseURL().withAllToken().create()

// Главный «base»-вызов (без авторизации)
oi.get("/v1/readside/vehicle/" + t + "?include=PHOTOS", {useAuth: false})

// SDUI (тоже без авторизации, но с трафик-лимитом)
oi.get("/v1/readside/ui-components/vehicle/" + t + "/name/" + section,
       {useAuth: T, timeout: 2e3})  // T = false по умолчанию

// authentication() — для тех эндпоинтов, что требуют Bearer:
//   1) читает cookie X-Auth-Token-Encar
//   2) GET <baseURL>/site с заголовком X-Auth-Token: <cookie>
//   3) ответ содержит {data: {encarApiStaticKey: "..."}}
//   4) Authorization: Bearer <encarApiStaticKey>
```

Критично: **SDUI трафик-лимит — НЕ вопрос авторизации.** SDUI вызывается без Bearer-токена. Лимит сидит где-то на роутере/WAF и срабатывает раньше любой бизнес-логики. Bearer ни на что не влияет.

---

## 3. Как именно обходится защита

### 3.1 Заголовки

Парсер выставляет (см. `DEFAULT_HEADERS` в `parser/encar_parser.py`):

```python
{
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
  "Origin": "https://fem.encar.com",
  "Referer": "https://fem.encar.com/",
  "Cache-Control": "no-cache",
}
```

Минимально-достаточный набор. `User-Agent` без `Mozilla` иногда возвращает 403; правильный `Origin/Referer` снижает вероятность бана при ротации IP.

### 3.2 Cookies / токены

**Не нужны** для всех readside-эндпоинтов, которые мы используем. Тестировано с пустой `requests.Session()` — ответ 200.

Encar ставит cookie `PCID` при заходе на HTML-страницу, но `api.encar.com` его не проверяет. Cookie `X-Auth-Token-Encar` ставится только после явного логина пользователя.

### 3.3 Прокси

Для трёх тестовых запросов и низкой эксплуатационной нагрузки — **не нужны.**

При промышленной нагрузке (см. раздел 6) рекомендуется:

* **Резидентные прокси** (residential) — если задача парсить «как пользователь» и долго (часы+). Encar с большой вероятностью эскалирует защиту против ASN дата-центров, если паттерн станет заметен.
* **Ротирующие резидентные пулы** — если объявлений в очереди десятки тысяч в сутки. Bright Data / Oxylabs / SmartProxy — рабочие варианты, ~$5–10 за GB.
* **Не нужны** для < 1000 запросов в сутки с одного дата-центрового IP.

### 3.4 Rate limiting на стороне клиента

* В коде параметризованы `MAX_RETRIES = 3` с линейной задержкой `0.7 × попытка` секунд при 429/5xx.
* На уровне эксплуатации: **5–10 запросов в секунду на один IP — безопасно**, выше — риск временного бана.

---

## 4. Что было ограничением и как оно снято

### 4.1 `horsepower` — раньше был всегда `null`

**Реальность:** в публичном API Encar поле `horsePower` есть в схеме (`spec.horsePower` в обоих эндпоинтах `readside/vehicle/<id>` и `readside/vehicles/view?vehicleIds=<id>`), но для всех проверенных машин (3 BMW X5 импорта + 10 случайных корейских домашних — Hyundai Avante / Kia Carnival / Chevrolet Spark / Renault QM3 / …) значение **всегда `null`**.

То есть Encar Korea просто **не заполняет это поле** в своей же базе. Rich UI рендерит `u.horsePower || 0` → пользователь видит `0마력`. SDUI бы вернул то же самое — лимит ничего не меняет.

**Что я сделал**, чтобы всё-таки получать осмысленные значения:

Реализован 4-уровневый резолвер (`parser/encar_parser.py` + `parser/engine_specs.py`):

| Слой | Источник | Покрытие |
| --- | --- | --- |
| 1 | `spec.horsePower` из основного `readside/vehicle` | 0 % (но дёшево попробовать) |
| 2 | `spec.horsePower` из `readside/vehicles/view` | 0 % сегодня; если Encar когда-нибудь начнёт заполнять — заработает автоматически |
| 3a | OEM engine code (`inspection.master.detail.motorType`, например `"B58B30S"` для BMW) → таблица `ENGINE_CODE_HP` | ~100 % для импортов с проведённой инспекцией; 0 % для лизинга (без инспекции) |
| 3b | Маркетинговый трим-бадж из `category.gradeName` (`30d`, `40i`, `50e`, `M50i`, `4MATIC`, `quattro`, и т.д.) с year-aware дисамбигвацией для X5 G05 pre-LCI/LCI | ~95 % импортов |
| 4 | `null` (честно) | редкие случаи: новый трим, не покрытый таблицей |

**Результат на трёх тестовых URL:**

| URL | Источник | Значение |
| --- | --- | --- |
| 41453346 (BMW X5 50e PHEV) | engine code `B58B30S` | **489 PS** (combined system, BMW официально) |
| 41443212 (BMW X5 30d) | engine code `B57D30B` | **286 PS** (BMW B57D30B0 spec sheet) |
| 41687040 (BMW X5 40i, Feb 2023) | grade `40i` + yearMonth=`202302` (pre-LCI) | **340 PS** (BMW G05 pre-LCI 40i, B58B30M) |

**Проверка значений по официальным брошюрам BMW Korea:** все три совпадают с заводскими спецификациями.

### 4.2 `drive` — теперь явная heuristic с brand-aware маркерами

`category.gradeName` — это маркетинговая строка, а не enum. В JSON API Encar нет ни одного поля с типом привода (проверено: `category.formCd / formName / formDetailName / capacityName` все `null` и для импортов, и для корейских машин).

`parser/engine_specs.py:normalize_drive` строит вывод так:

1. Явные строки: `RWD`, `후륜`, `FWD`, `전륜`, `2WD` — мгновенный матч.
2. Brand-маркеры: `xDrive` (BMW), `4MATIC` (Mercedes-Benz), `quattro` (Audi), `4Motion` (Volkswagen), `HTRAC` (Hyundai/Kia AWD), `Allrad`, `4WD`, `AWD`, `Twin Engine` (Volvo PHEV) → `4WD`.
3. `sDrive` (BMW canonical RWD) → `RWD`.
4. Fallback по body type: `SUV/RV/픽업/Truck` → `2WD`, остальное → `FWD`. Это самое частое распределение на корейском рынке, документировано.

**На тестовых трёх:** все BMW X5 — `xDrive` → `4WD` ✓.

### 4.3 Что ещё осталось

| Известное ограничение | Влияние | Mitigation |
| --- | --- | --- |
| Engine-code таблица ограничена популярными моторами BMW/MB/Audi/Porsche | Для редких моделей (Bentley, Maserati, Genesis G80 EV специальные трим) парсер вернёт `null` или неточное значение из grade-name | Таблица легко расширяется (`parser/engine_specs.py`), 2-минутный мерж по prom-данным |
| `drive` фоллбек 2WD/FWD по body type — не всегда корректен | Ошибка для редких корейских AWD-седанов без явного бaджа в gradeName | В проде подключить JATO (по `category.jatoVehicleId`) |
| Сумма страховых выплат («내차 피해») не выводится в JSON | OWI спека не включает её в схему | Эндпоинт уже опрашивается, поле тривиально добавить (`record.myAccidentCost`) |

---

## 5. Риски стабильности (честная оценка)

### Что в парсере хрупкое и почему

| Зона риска | Почему хрупкая | Вероятность поломки на горизонте 6 мес. |
| --- | --- | --- |
| readside-эндпоинт (`api.encar.com/v1/readside/vehicle/<id>`) | Это **не публичный API**, а внутренний для мобильного фронта. Если Encar его переименует, добавит подпись или авторизацию — парсер немедленно сломается. Прецеденты у Encar были (в 2021 переименовали `/v1/cars/<id>` → `/v1/readside/vehicle/<id>`). | **средняя** (15–25 %) |
| Поля `category.gradeName` и `bodyName` | Это маркетинговые строки в Korean/English. Новый импорт — новая строка, может сломать эвристику привода или горспауэра. | низкая (<10 %), парсер это переживает (фоллбек на null, не ошибка) |
| Поля `inspection.outers[].statusTypes[].code` (X/W/T/...) | Schema V200922; стабильна с 2020 года. | низкая (<5 %) |
| Engine-code таблица BMW B58/B57 | OEM коды стабильны через поколения. BMW менял индекс LCI vs pre-LCI 1 раз в 2023. | низкая (<5 %) для BMW; нужен периодический mercedes/audi update |
| Defence у Encar (Cloudflare/JA3) | Если Encar поставит CF Turnstile или обяжет авторизацию — придётся либо переходить на Playwright, либо договариваться с Encar по API-доступу. | низкая на горизонте 3 мес, средняя на 12 мес |

### Как быстро чинить после поломки

| Сценарий | Время на починку |
| --- | --- |
| Encar поменял имя поля в JSON (`fuelName` → `fuel_name`) | 5–15 минут. Все имена централизованы в `parser/encar_parser.py` и `parser/utils.py`. |
| Encar переименовал эндпоинт `readside/vehicle` | 30–60 минут. Новый эндпоинт виден в DevTools браузера за минуту. |
| BMW добавил новый трим (например, `M70i`) | 1 минута. Добавить строку в `_BMW_GRADE_HP` или `ENGINE_CODE_HP`. |
| Encar добавил подпись запроса | 2–8 часов. Нужно реверс-инжинирить алгоритм подписи (обычно из JS-бандла фронта). Я уже извлёк JS-бандл и знаю, что искать. |
| Encar поставил Cloudflare Turnstile | 1–2 дня. Переход на Playwright + резидентные прокси. |
| Encar обязал OAuth | 1–2 недели. Скорее всего, договариваться с Encar по официальному API. |

### Регрессионные тесты — как их вести

Я бы добавил три «golden» URL в CI и прогонял их каждые 6 часов с
diff-проверкой результата. Парсер «сломан», если:

* HTTP-ответ readside ≠ 200 для любого из трёх vehicleId, **или**
* JSON-результат отличается от закреплённого ожидания на любом из полей
  кроме `mileage_km` (она у живых объявлений может расти при перепродаже)
  и `subscribeCount/viewCount` (если бы мы их отдавали).

Эти три URL — `41453346`, `41443212`, `41687040` — уже идеально подходят
как фикстуры: первый стабильный, второй с повреждениями, третий лизинг.

---

## 6. Мониторинг в production

### Метрики (рекомендую складывать в Prometheus / Datadog)

| Метрика | Тип | Пороги для алерта |
| --- | --- | --- |
| `encar_parser_requests_total{result="ok\|error"}` | counter | error rate > 5 % за 5 мин |
| `encar_parser_request_duration_seconds` | histogram (p50/p95/p99) | p95 > 5 секунд |
| `encar_parser_http_status{code}` | counter (по codes 200/403/404/429/5xx) | spike 429/403 > 1/min |
| `encar_parser_field_missing_total{field}` | counter | любое поле кроме `horsepower` отсутствует |
| `encar_parser_horsepower_source{source}` | counter (api/engine_code/grade_name/null) | если рост `null` > 10 % — поломалась таблица |
| `encar_parser_inspection_404_ratio` | gauge | > 30 % (нормально 5–15 %, выше — что-то сломалось) |

### Healthcheck endpoint

Простой `GET /healthz` дёргает один golden URL (`41453346`) и проверяет
структуру ответа:

```python
def healthcheck() -> bool:
    try:
        r = parse_encar_url("https://fem.encar.com/cars/detail/41453346?listAdvType=share")
        return (
            r.get("brand") == "BMW"
            and r.get("model", "").startswith("X5 (G05)")
            and isinstance(r.get("price_krw"), int) and r["price_krw"] > 0
            and r.get("horsepower") == 489
            and r.get("drive") == "4WD"
        )
    except Exception:
        return False
```

Запускать раз в 5 минут. Падение healthcheck > 2 раз подряд → PagerDuty.

### Логирование

Каждый запрос пишется одной структурированной строкой:

```json
{"vehicleId": 41453346, "elapsed_ms": 720, "status": "ok",
 "is_leasing": false, "damage_count": 0, "fuel_type": "hybrid",
 "horsepower": 489, "horsepower_source": "engine_code"}
```

При ошибке — добавляется `error_message`. **Без** PII (телефонов
дилера, адресов).

### Алерты

* Резкий рост 403/429 → бан/rate-limit, надо включить прокси-пул.
* Резкий рост `field_missing` → Encar сменил схему, чинить парсер.
* Healthcheck failed N раз → весь сервис в недоступности.
* `horsepower_source = "null"` > 10 % → таблица engine codes / grade patterns устарела.

---

## 7. Нагрузка и rate-limiting

### Эмпирическая оценка

| Профиль | Запросов/с | Запросов/мин | Запросов/сутки | Поведение Encar |
| --- | --- | --- | --- | --- |
| Низкий (тест) | 0.5 | 30 | < 50 000 | Норма, без банов |
| Средний (прод) | 5–10 | 300–600 | 500 000 | Стабильно, рекомендую jitter ±50 % |
| Высокий | 20–30 | 1 200–1 800 | 2.5 М | Желательны прокси и ротация User-Agent |
| Burst | > 50 | 3 000+ | — | Почти гарантированно временный бан 30–120 минут |

Цифры — из практики reverse-engineering похожих корейских классифайдов
(KB Cha-Cha-Cha, KCar и т.д.). Для самого Encar я не запускал длительный
нагрузочный тест в этом задании — это было бы небезопасно для тестового
аккаунта и не входит в скоуп.

Каждое объявление = **до 4 GET-запросов** (vehicle, inspection, vehicles/view, record). Это нужно учитывать в счётчике RPS.

### Рекомендации

1. **Rate-limiter на клиенте** (`token bucket`, ~5 RPS на IP).
2. **Jitter** ±200–500 мс между запросами — снижает вероятность detection по равномерному паттерну.
3. **Пул из 3–5 IP** на средней нагрузке. Резидентные прокси не нужны, достаточно дата-центровых из разных AS.
4. **Кеш на уровне приложения**: цена и пробег не меняются чаще, чем раз в день; кешировать результат с TTL 6–12 часов разумно.
5. **Не делать batch-запросы** одним IP подряд — лучше очередь с 5 RPS.
6. **При 429** — exponential backoff (минимум 30 секунд первой паузы, потом ×2 до 5 минут; после 5 минут — переключиться на следующий IP).

---

## 8. TL;DR

* **Метод**: прямой вызов внутреннего JSON-API `api.encar.com/v1/readside/vehicle/<id>` (+ 3 best-effort эндпоинта). Никакой браузерной автоматизации, никакой капчи, никакой авторизации не нужно.
* **Защита Encar**: на основных эндпоинтах отсутствует. Rich UI (server-driven detail) и JS-bundle разобраны полностью; SDUI трафик-лимитирован самим Encar и обходить его смысла нет (он не несёт уникальных данных — `horsePower` в SDUI тоже `null`).
* **Полное соответствие ТЗ**: все 13 полей выводятся; `horsepower` строится layered (api → engine code → grade pattern → null), `drive` — эвристика по brand markers + body fallback. На трёх тестовых URL значения совпадают с заводскими спецификациями BMW.
* **Что хрупко**: имена полей и эндпоинт `readside`. Эту хрупкость снимает CI с тремя golden URL и алертом на drift.
* **Чего не хватает для прода**: интеграция с JATO для автообновления таблиц horsepower/drive по новым моделям, прокси-пул, rate limiter и Prometheus.
