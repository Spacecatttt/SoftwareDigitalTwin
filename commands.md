# Lab 1 and 2

## Mosquitto
https://www.datacamp.com/tutorial/mosquitto-docker

https://www.cedalo.com/blog/mosquitto-docker-configuration-ultimate-guide

for i in {1..10}; do
  mosquitto_pub -h localhost -i mosq_pub1 -t "Test" -m "Message with ID: $i" -d
  sleep 1
done

mosquitto_sub -i mosq_sub1 -t "Test"

## Json format

```json
{
  "sensor_id": "feed_rate_01",
  "value": 50.05,
  "unit": "mm/s",
  "timestamp": "2026-10-19T22:05:00.000Z"
}
```

Field Descriptions
* sensor_id (string): Unique identifier of the specific sensor unit on the 3D printer.
* value (float): Current measured metric value (rounded to two decimal places).
* unit (string): Measurement unit for the metric (mm/s for extruder feed rate, A for current, Celsius for temperature, % for percentages).
* timestamp (string): ISO 8601 formatted UTC timestamp indicating the exact moment the telemetry data was generated.

## MQTT topic architecture

```
factory/{PRINTER_ID}/telemetry/{metric_name}
```

Level Breakdown:
* factory (or object): Root namespace defining the organization, enterprise, or general category of objects.
* {PRINTER_ID} (or {id}): Unique identifier of the specific physical device (e.g., printer_01). Isolates data per device.
* telemetry: Data type or channel category (e.g., telemetry, status, commands, config).
* {metric_name} (or {name}): Specific parameter or sensor reporting the value (e.g., nozzle_temperature, extruder_feed_rate).

# Lab 3

Показ
```
.venv/bin/python pipeline.py
.venv/bin/python emulator.py printer_01
```

За запущених конвеєра й емулятора сам каталог атрибутів віддається як JSON:
```
curl -s localhost:8050/api/model | python3 -m json.tool | head -40
```

А поточний стан так:
```
curl -s localhost:8050/api/state | python3 -m json.tool
```


## Блоки схеми та файли

| Блок схеми | Файл і місце в коді |
|---|---|
| Емулятор 3D-принтера | emulator.py (лаба 3), оригінал у lab2/emulator_publisher.py |
| MQTT-брокер Mosquitto | docker-compose.yml + infra/mosquitto/config/mosquitto.conf |
| Зворотний канал стану (MQTT) | MqttRepublisher у pipeline.py:141 |
| 1. Приймання | підписка й on_message у pipeline.py; черга inbox і submit() у twin/processor.py:59 |
| 2. Валідація | parse_message, Deduplicator у twin/ingest.py |
| 3. Впорядкування | EventClock, ReorderBuffer у twin/ingest.py:158 |
| 4. Очищення | LossDetector у twin/ingest.py:137; HampelFilter у twin/preprocessing.py |
| 5. Робочий стан | DeviceModel у twin/model.py:132; пороги й вікна в twin/config.py |
| 6. Синхронізація | SyncPolicy у twin/sync.py |
| 7. Ресемплінг на сітку 1 с | Resampler, StreamResampler у twin/preprocessing.py |
| 8. Сховище стану в пам'яті | TwinStore, DeviceTwin у twin/state_store.py |
| Підписник: консоль | ConsoleNotifier у pipeline.py:110 |
| Підписник: веб-дашборд | twin/web.py (сервер, SSE) + web/index.html (сторінка) |
| Підписник: записувач історії | _on_commit у twin/processor.py → InfluxWriter у twin/storage.py |
| InfluxDB | docker-compose.yml; бакети й задача пониження частоти в ensure_schema() у twin/storage.py |
| Grafana | infra/grafana/ (джерело даних і дашборд) |
| Історичні запити | twin/history.py + CLI query_history.py |

Деякі файли не мають власного блоку на схемі, але зв'язують усе разом:

* *pipeline.py* — точка входу: зчитує параметри командного рядка, підключає MQTT, реєструє підписників і запускає головний цикл.
* *twin/processor.py* — «диригент». Він проводить замір через етапи 2→3→4→5→6/7 (handle_raw → process). Тут же живуть таймери tick(): watchdog, PERIODIC закриття інтервалів сітки, метрики.
* *twin/metrics.py* рахує затримки й пропускну здатність (п.7).
* *experiments/* запускає сценарії й будує графіки
* *tests/* містить модульні тести.


Запустити обрив зв'язку:
```
python emulator.py printer_02 --outage 20:8
```

Сплеск:
```
python emulator.py printer_03 --profile 0:1,10:50,25:1
```

На дашборді :8050 відкрити «Історія», джерело resampled. Точки розфарбовані за fill.

Статистика заповнення з БД:
```
python query_history.py fill printer_02 --start 10m
```

Викиди видно наживо: у журналі конвеєра кожні 5 с з'являється рядок OUTLIER ... raw 245 rejected.


Подія перегріву:
```
python emulator.py printer_02 --runaway 10:15
```
У журналі видно EVENT-коміти thermal_state NORMAL → WARNING → CRITICAL.
Подія зв'язності: зупинити емулятор через Ctrl+C. Брокер надішле LWT, і буде EVENT connectivity → OFFLINE.


Потік сповіщень (push, SSE), той самий, що отримує браузер:
```
curl -N localhost:8050/api/stream
```

Підписник через брокер:
```
docker exec lab3-mosquitto mosquitto_sub -t 'factory/+/twin/#' -v
```


## Модель даних InfluxDB (термінологія, яку варто знати)

| Поняття | Аналог у реляційній БД | Приклад |
|---|---|---|
| bucket | база даних + термін зберігання | digital_twin (30 днів) |
| measurement | таблиця | telemetry_raw |
| tag | індексована колонка (рядок) | printer_id=printer_01 |
| field | неіндексована колонка (значення) | value=210.4 |
| timestamp | первинний ключ (разом із тегами) | мікросекунди |
| series | — | унікальна комбінація measurement + теги |

## Tests

Окремий сценарій запускається так (S1 ):
```
python experiments/run_experiments.py normal
```

Інші: burst, outage, load, reorder. Перед кожним прогоном скрипт скидає сесію брокера й чекає, поки конвеєр дообробить чергу. Так прогони не змішуються.

## Сценарій демонстрації на захисті (≈ 10 хв)

Підготовка: в одному терміналі docker compose up -d, у другому python pipeline.py. Відкрити дашборд http://localhost:8050.

| # | Дія | Що показати й сказати |
|---|---|---|
| 1 | python emulator.py printer_01 | з'явився двійник; картки оновлюються й підсвічуються кольором правила (п.4); кожні 5 с у журналі OUTLIER (п.3) |
| 2 | python emulator.py printer_02 --runaway 10:15 | NORMAL → WARNING → CRITICAL (EVENT), потім назад із гістерезисом (п.1) |
| 3 | Ctrl+C на printer_02 | LWT → OFFLINE (S3 в мініатюрі) |
| 4 | python emulator.py printer_02 --outage 15:10 | обрив і відновлення; в «Історії» (resampled) кольорами видно locf, missing і корекції |
| 5 | python emulator.py printer_03 --profile 0:1,10:30,20:1 | вгорі дашборда зростання потоку й затримки, потім відновлення (S2) |
| 6 | історія: CLI python query_history.py state-at printer_01 2m, InfluxDB UI :8086, Grafana :3000 | «стан двійника 2 хвилини тому» (п.6) |
## Демонстрація: журнал, MQTT-події, історичні запити

Усі команди виконуються з кореня проєкту при активованому `.venv` (`source .venv/bin/activate`).

### 1. Журнал конвеєра під час перегрівання

Термінал 1 — конвеєр (журнал іде в консоль і в `logs/pipeline.log`):
```bash
python pipeline.py
```

Термінал 2 — нормальний принтер для фону:
```bash
python emulator.py printer_01
```

Термінал 3 — перегрівання printer_02 (починається на 10-й секунді, триває 15 с):
```bash
python emulator.py printer_02 --runaway 10:15
```

У журналі конвеєра з'являться рядки:
```
[WARNING] [TWIN printer_02 v139 EVENT] thermal_state: NORMAL -> WARNING (nozzle 216.4 C (setpoint 210))
[WARNING] [TWIN printer_02 v161 EVENT] thermal_state: WARNING -> CRITICAL (nozzle 226.0 C (setpoint 210))
```
Раз на секунду пишеться рядок `[METRICS]`: потік, затримка, черга, збої, коміти P/E/T.

Лише події та метрики з файлу журналу:
```bash
grep -E "EVENT|METRICS" logs/pipeline.log | tail -20
```

### 2. Ті самі події через MQTT (зовнішній підписник)

Термінал 4, запускати до перегрівання:
```bash
docker exec lab3-mosquitto mosquitto_sub -t 'factory/+/twin/events' -v
```

Останній знімок стану (retained, приходить одразу):
```bash
docker exec lab3-mosquitto mosquitto_sub -t 'factory/printer_02/twin/state' -C 1
```

### 3. Стан двійника на момент часу

Відносний час, тобто 1 хвилину тому (зручно одразу після перегрівання):
```bash
python query_history.py state-at printer_02 1m
```

Абсолютний час, UTC:
```bash
python query_history.py state-at printer_02 2026-10-31T09:37:52Z
```

Журнал конвеєра показує місцевий час (UTC+1). Для абсолютного запиту відніміть 1 год, наприклад 10:37:52 у журналі = `09:37:52Z`.

### 4. Сирі значення з ознакою викиду

Останні 2 хвилини; рядки з `outlier=True` — сплески, замість яких у стан пішло останнє прийняте значення (`filtered`):
```bash
python query_history.py series printer_02 nozzle_temperature --source raw --start 2m
```

Точний інтервал:
```bash
python query_history.py series printer_02 nozzle_temperature --source raw --start 2026-10-31T09:37:44Z --stop 2026-10-31T09:37:52Z
```

### 5. Сітка 1 с під час обриву зв'язку

Обрив printer_02 на 15-й секунді, на 10 с:
```bash
python emulator.py printer_02 --outage 15:10
```

Подача (QoS 0): під час обриву `missing`, після відновлення знову `measured`:
```bash
python query_history.py series printer_02 extruder_feed_rate --start 1m
```

Температура (QoS 1): `locf` під час обриву, потім корекції реальними даними з буфера пристрою:
```bash
python query_history.py series printer_02 nozzle_temperature --start 1m
```

Статистика способів заповнення:
```bash
python query_history.py fill printer_02 --start 5m
```

### 6. Інші запити

Події за 10 хв:
```bash
python query_history.py events printer_02 --start 10m
```

Агрегати сирої телеметрії за метриками:
```bash
python query_history.py stats printer_02 --start 15m
```

Довільний Flux-запит:
```bash
python query_history.py flux 'from(bucket: "digital_twin") |> range(start: -1m) |> filter(fn: (r) => r._measurement == "twin_events")'
```

# Lab 4 — Eclipse Ditto

## Запуск

Ditto стартує разом з усім стеком (перший старт ≈ 1–2 хв):
```bash
docker compose up -d
```

Політика та граф двійників (ферма + printer_01..03, ідемпотентно):
```bash
python ditto_setup.py printer_01 printer_02 printer_03
```

Термінали:
```bash
python pipeline.py
```
```bash
python ditto_adapter.py
```
```bash
python ditto_monitor.py
```
```bash
python emulator.py printer_01
```

Explorer UI (`ditto` / `ditto`): http://localhost:8180/ ; моделі: http://localhost:8180/models/things/hotend-1.0.0.tm.jsonld

## Демонстрація на захисті

| # | Дія | Що показати |
|---|---|---|
| 1 | Explorer UI, клік `kpi.dt:printer_01.hotend` → feature `heater` | значення оновлюються за надходження телеметрії (адаптер) |
| 2 | `python ditto_query.py tree` | граф: ферма → принтери → 8 підсистем, зв'язки |
| 3 | `python ditto_query.py search 'gt(features/heater/properties/temperature,212)' --fields features/heater/properties/temperature` | пошук за значенням властивості |
| 4 | `python ditto_query.py relations kpi.dt:printer_01.psu`, `impact kpi.dt:printer_01.psu` | обхід зв'язків, аналіз впливу |
| 5 | `python emulator.py printer_02 --runaway 15:60` | монітор: WARNING → CRITICAL → дія emergencyCooldown → емулятор охолоджується → NORMAL |
| 6 | `python ditto_query.py td kpi.dt:printer_01.hotend --feature heater` | WoT Thing Description, згенерований з моделі |
| 7 | `python ditto_query.py history kpi.dt:printer_02 --ago 2m --fields features/alarms` | стан двійника 2 хв тому і хто його змінив |

## Модель і політика як контракт

Неправильний тип → 400 `wot:payload.validation.error`:
```bash
curl -s -u ditto:ditto -X PATCH -H 'Content-Type: application/merge-patch+json' localhost:8180/api/2/things/kpi.dt:printer_01.hotend -d '{"features":{"heater":{"properties":{"temperature":"hot"}}}}'
```

Немодельований атрибут → 400:
```bash
curl -s -u ditto:ditto -X PATCH -H 'Content-Type: application/merge-patch+json' localhost:8180/api/2/things/kpi.dt:printer_01.hotend -d '{"attributes":{"color":"red"}}'
```

Адаптеру політика не дозволяє міняти атрибути → 403:
```bash
curl -s -u adapter:adapter -X PATCH -H 'Content-Type: application/merge-patch+json' localhost:8180/api/2/things/kpi.dt:printer_01.hotend -d '{"attributes":{"name":"x"}}'
```

Створити двійник без моделі не можна (entity-creation) → 403 `entity.notcreatable`:
```bash
curl -s -u ditto:ditto -X PUT -H 'Content-Type: application/json' localhost:8180/api/2/things/kpi.dt:test -d '{"policyId":"kpi.dt:twin-policy"}'
```

## Події без коду (SSE)

```bash
curl -N -u monitor:monitor -H 'Accept: text/event-stream' 'http://localhost:8180/api/2/things?namespaces=kpi.dt&fields=thingId,features/heater/properties/thermalState'
```

## Ditto ↔ Azure Digital Twins (термінологія)

| Ditto | Azure Digital Twins (DTDL) |
|---|---|
| Thing | digital twin |
| WoT Thing Model (`definition`) | Interface (DTMI) |
| attributes / feature properties | Property |
| feature (`tm:submodel`) | Component |
| `attributes/relations` + search | Relationship + query `JOIN` / `MATCH` |
| message to inbox (WoT action) | Command |
| events (WS / SSE / connections) | event routes (Event Grid / Event Hubs / Service Bus) |
| Policy | Azure RBAC |
