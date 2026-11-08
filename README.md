# Програмне забезпечення технології цифрових двійників

Продовження робіт 1–2 (емулятор 3D-принтера та IoT-канал MQTT; їхній код лежить у `lab2/`).
Формат повідомлень і топіки ті самі: `factory/{printer_id}/telemetry/{metric}`, `factory/{printer_id}/status`.

```
emulator.py ──MQTT──▶ Mosquitto ──▶ pipeline.py ──▶ стан двійника в пам'яті ──▶ підписники
                                     │  валідація, дедуплікація,        (консоль, MQTT twin/state,
                                     │  буфер упорядкування, фільтр      веб-дашборд SSE, InfluxDB)
                                     │  Гампеля, правила синхронізації
                                     └─▶ ресемплінг 1 с + заповнення пропусків ──▶ InfluxDB ──▶ Grafana
```

## Структура

| Шлях | Призначення |
|---|---|
| `lab2/` | код робіт 1–2 без змін: `emulator_publisher.py`, `subscriber_receiver.py` (запуск з `lab2/`: `../.venv/bin/python emulator_publisher.py`) |
| `.venv/` | спільне віртуальне оточення (`requirements.txt`) |
| `docker-compose.yml`, `infra/` | Mosquitto (конфіг на основі роботи 2), InfluxDB 2.9, Grafana 13 з готовими джерелом даних і дашбордом; Eclipse Ditto 3.9.7 (робота 4, `infra/ditto/`) |
| `pipeline.py` | точка входу конвеєра: MQTT → обробка → стан → підписники, метрики |
| `twin/config.py` | каталог метрик (одиниці, фізичні діапазони, пороги зміни, параметри фільтра та заповнення пропусків) |
| `twin/model.py` | **модель стану двійника**: атрибути, похідні показники, індикатори режимів |
| `twin/ingest.py` | валідація, дедуплікація, детектор втрат за `seq_id`, буфер упорядкування з watermark |
| `twin/preprocessing.py` | фільтр Гампеля (викиди), ресемплінг на сітку 1 с, LOCF / інтерполяція / дозаповнення |
| `twin/sync.py` | **правила синхронізації** PERIODIC / EVENT / THRESHOLD |
| `twin/state_store.py` | сховище стану в пам'яті, версії, сповіщення підписників (Observer) |
| `twin/processor.py` | зшивання етапів, таймери (watchdog, періодичне правило, метрики) |
| `twin/storage.py`, `twin/history.py` | запис у InfluxDB (line protocol, батчі, повтори), схема, Flux-запити до історії |
| `twin/web.py`, `web/index.html` | веб-дашборд реального часу (SSE) + історія зі сховища |
| `emulator.py` | емулятор із роботи 2 + сплеск, обрив зв'язку, thermal runaway, інжекція збоїв, команди `factory/{id}/commands` |
| `query_history.py` | CLI-вибірки історичних даних |
| `experiments/` | сценарії вимірювань і побудова таблиць / графіків |
| `ditto/models/` | **робота 4**: моделі двійників — WoT Thing Models (`things/` — типи двійників, `features/` — їхні складові) |
| `twin/ditto_graph.py`, `twin/ditto_mapping.py`, `twin/ditto_client.py` | граф двійників і політика доступу; відповідність стану конвеєра властивостям двійників; клієнт HTTP / WebSocket API Ditto |
| `ditto_setup.py`, `ditto_adapter.py`, `ditto_monitor.py`, `ditto_query.py` | створення графа; адаптер конвеєр ↔ Ditto; реакція на події; запити до графа |
| `web/3d/`, `viz_server.py` | **робота 5**: 3D-візуалізація (Three.js): модель `models/printer.glb` (скрипт Blender `model/build_printer.py`), модулі `js/`, бібліотеки `vendor/`; бекенд застосунку |
| `tests/` | модульні тести етапів конвеєра, інтеграції з Ditto та бекенду 3D |
| `report/` | звіти (`Звіт_ЛР3.docx`, `lab4/Звіт_ЛР4.md` → `build_report.py` → `.docx`), рисунки |
| `results/` | сирі результати вимірювань: метрики щосекунди, підсумки, вибірки затримок, профіль |

## Запуск

Потрібні Docker і Python 3.12. Віртуальне оточення `.venv` спільне для всіх робіт; створити його:

```bash
cd ~/digitaltwinSoftware
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
docker compose up -d
```

Якщо запущено стек `dt` з іншого курсу, спершу зупиніть їх (порти 1883 / 8086 / 3000).

Конвеєр (термінал 1):

```bash
.venv/bin/python pipeline.py
```

Емулятори (термінали 2, 3):

```bash
.venv/bin/python emulator.py printer_01
```

```bash
.venv/bin/python emulator.py printer_02 --p-invalid 0.01 --p-duplicate 0.01 --p-jitter 0.1
```

| Інтерфейс | Адреса |
|---|---|
| Веб-дашборд стану двійника (реальний час) | http://localhost:8050 |
| Grafana (історія, метрики конвеєра) | http://localhost:3000 |
| InfluxDB UI (Data Explorer), `admin` / `admin12345` | http://localhost:8086 |

Стежити за станом двійника може будь-який MQTT-клієнт:

```bash
docker exec lab3-mosquitto mosquitto_sub -t 'factory/+/twin/events' -v
```

## Демонстрація на захисті

1. `docker compose up -d`, далі `pipeline.py` і відкритий http://localhost:8050.
2. `emulator.py printer_01`: з'являється двійник. Значення оновлюються, картки підсвічуються кольором правила синхронізації: синій THRESHOLD, помаранчевий EVENT, сірий PERIODIC. Режим роботи змінюється кожні 5 с (EVENT), кожні 5 с фільтр Гампеля відкидає сплеск температури (OUTLIER).
3. Обрив зв'язку:

   ```bash
   .venv/bin/python emulator.py printer_02 --outage 20:15
   ```

   Брокер надсилає LWT, і двійник переходить в OFFLINE. Після відновлення приходить буфер store-and-forward, а в «Історії» (resampled) видно заповнені пропуски.
4. Подія перевищення температури:

   ```bash
   .venv/bin/python emulator.py printer_02 --runaway 10:15
   ```

   Тепловий стан змінюється NORMAL → WARNING → CRITICAL.
5. Сплеск інтенсивності:

   ```bash
   .venv/bin/python emulator.py printer_03 --profile 0:1,10:50,25:1
   ```

   Вгорі дашборда видно зростання потоку й затримки.
6. Історичні дані: панель «Історія» та «Стан на момент часу» в дашборді, Grafana й CLI:

   ```bash
   .venv/bin/python query_history.py state-at printer_01 2m
   ```

   ```bash
   .venv/bin/python query_history.py series printer_01 nozzle_temperature --start 3m --source raw
   ```

   ```bash
   .venv/bin/python query_history.py fill printer_02 --start 15m
   ```

   ```bash
   .venv/bin/python query_history.py events printer_01 --start 10m
   ```

## Вимірювання та тести

```bash
.venv/bin/python -m unittest discover -s tests -v
```

```bash
.venv/bin/python experiments/run_experiments.py
```

```bash
.venv/bin/python experiments/analyze.py
```

Профіль потоків конвеєра під навантаженням (py-spy, режим `--gil`):

```bash
.venv/bin/python experiments/profile_pipeline.py 40
```

`run_experiments.py` триває близько 25 хв. Сценарії: S1 штатний режим, S2 сплески ×25 / ×60, S3 обрив зв'язку на 20 с, навантажувальний тест (×1…×150, із записом у БД і без нього), перебір затримки буфера впорядкування. Сирі результати лежать у `results/<run>/` (метрики щосекунди, підсумок, вибірка затримок, статистика емуляторів). `analyze.py` будує `results/tables.md` і `report/figures/*.png`.

## Робота 4. Платформа цифрових двійників Eclipse Ditto

```
pipeline.py ──MQTT twin/state──▶ ditto_adapter.py ──HTTP PATCH (merge)──▶ Eclipse Ditto ──WebSocket events──▶ ditto_monitor.py
     ▲                                  │ ◀──── WebSocket messages (WoT action) ────┘ ◀── emergencyCooldown ─────┘
emulator.py ◀──MQTT factory/{id}/commands┘
```

Ditto запускається тим самим `docker compose up -d` (перший старт ≈ 1–2 хв). Після цього:

```bash
.venv/bin/python ditto_setup.py
```

Скрипт створює політику та граф: ферма `kpi.dt:farm-01`, принтери `printer_01`, `printer_02`, по 9 двійників на принтер. Ще один принтер: `ditto_setup.py printer_03`.

| Компонент | Термінал |
|---|---|
| конвеєр роботи 3 | `.venv/bin/python pipeline.py` |
| адаптер конвеєр ↔ Ditto | `.venv/bin/python ditto_adapter.py` |
| монітор подій (реакція) | `.venv/bin/python ditto_monitor.py` |
| емулятори | `.venv/bin/python emulator.py printer_01`, `... printer_02 --runaway 15:60` |

| Інтерфейс | Адреса |
|---|---|
| Ditto Explorer UI (`ditto` / `ditto`) | http://localhost:8180/ |
| Ditto HTTP API | http://localhost:8180/api/2/things/kpi.dt:printer_01 |
| WoT Thing Models | http://localhost:8180/models/things/hotend-1.0.0.tm.jsonld |

Користувачі Ditto (пароль = ім'я): `ditto` — власник, `adapter` — може писати лише features, `monitor` — лише тривоги, зведення ферми та дію `emergencyCooldown`.

Запити до графа:

```bash
.venv/bin/python ditto_query.py tree
```

```bash
.venv/bin/python ditto_query.py search 'gt(features/heater/properties/temperature,212)' --fields features/heater/properties/temperature
```

```bash
.venv/bin/python ditto_query.py impact kpi.dt:printer_01.psu
```

Інші: `state`, `count`, `parts`, `relations`, `td` (WoT Thing Description), `history` (стан на момент часу), див. `ditto_query.py -h`.

Ditto не дозволяє створити двійник нікому, крім `ditto`, поза простором імен `kpi.dt` та без моделі з `http://wot-models/models/things/` (`entity-creation` у `docker-compose.yml`). Адаптер оновлює лише наявні двійники (`If-Match: *`).

## Робота 5. 3D-візуалізація стану (Three.js / WebGL)

```
Ditto ──WebSocket (viewer, READ)──▶ viz_server.py ──SSE──▶ браузер: TwinStore → правила стану → 3D-сцена, панель, звук
InfluxDB ──Flux──────────────────▶ viz_server.py ──HTTP──▶ графіки часових рядів
```

Окрім конвеєра, адаптера й емуляторів (робота 4), потрібен бекенд 3D-застосунку:

```bash
.venv/bin/python viz_server.py
```

Відкрити http://localhost:8060/3d/. Монітор подій (`ditto_monitor.py`) вмикає реакцію на аварію: тривога на рамі принтера та команда охолодження.

| Дія | Як |
|---|---|
| обертання / масштаб / зсув | ліва кнопка / колесо / права кнопка миші |
| показники елемента | клік по елементу (спливна панель), Esc — закрити |
| фокус на елементі | подвійний клік або F; камера супроводжує рухому каретку |
| режим кольорів | «Режим: стан» (кольори за рівнем) / «Режим: матеріали» (реалістичні, світіння сопла) |
| звук і голос | «Звук» (браузер дозволяє звук лише після дії користувача); «Підтвердити тривогу» зупиняє повтор сирени |
| VR | кнопка WebXR унизу (активна лише з гарнітурою) |

Параметри URL: `?printer=printer_02` — обраний принтер, `?lod=0` / `?shadows=0` — без LOD / тіней, `?bench=1` — тест продуктивності на синтетичних оновленнях.

Аварійна ситуація для демонстрації:

```bash
.venv/bin/python emulator.py printer_02 --runaway 15:60
```

Перебудувати 3D-модель (Blender 4.x):

```bash
blender --background --factory-startup --python web/3d/model/build_printer.py -- web/3d/models/printer.glb
```

Тест продуктивності (Chrome через DevTools) і графік:

```bash
.venv/bin/python experiments/viz_bench.py run
```

```bash
.venv/bin/python experiments/viz_bench.py plot
```

## Примітка про годинник WSL2

Системний годинник у WSL2 періодично «стрибає» на секунди вперед і назад: виміряно +1.7 с, −3.5 с і +1.2 с за 40 с. Причина — конфлікт синхронізації часу Hyper-V і `systemd-timesyncd`. Тому:

- конвеєр використовує для таймерів і вимірювання затримки `CLOCK_MONOTONIC`;
- логіка часу події спирається на власні мітки потоку (watermark на потік), а не на годинник сервера;
- емулятор ставить мітки «UTC на старті + monotonic» і додає діагностичне поле `t_mono` для точного вимірювання затримки на одному хості (`--wall-clock` повертає поведінку роботи 2).
