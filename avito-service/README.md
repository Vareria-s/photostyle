# ФотоСтайл → Avito

Сервис подготовлен на реальном YML Tilda: 29 659 673 байта, 3626 offers, 3204 родительских товара. Текущий результат: 2656 черновиков, 548 исключённых родителей, 422 исключённых варианта. Все категории ещё не подтверждены в Avito; production выключен. Preview не означает приёмку площадкой.

Python 3.12+ и стандартная библиотека выполняют конвертацию в GitHub Actions. Отдельный Cloudflare Pages проект `photostyle-avito` выдаёт готовые файлы. Существующие PDF проект `photostyle-documents.pages.dev`, исходники и workflows не изменяются. Relay PDF не имеет диска/scheduled handler и не подходит для разбора каталога 29 MB при каждом запросе; новый relay также не разбирает XML.

Cloudflare Pages развёрнут 27.11.2026: [photostyle-avito.pages.dev](https://photostyle-avito.pages.dev). Панель Cloudflare подтвердила успешный deployment; сетевую доступность snapshot после загрузки GitHub исходников и первого workflow проверяет агент развёртывания. `/health` и `/status.json` показывают время последнего успешного snapshot, blockers и stale; `/preview.xml`, `/report.json`, `/reasons.csv`, `/text-changes.csv` доступны как диагностика. `/feed.xml` возвращает 503 до активации, при возрасте snapshot больше 4 часов, отсутствии status/feed или ошибке GitHub. `/state.json` закрыт. GET и HEAD принимают одинаковые решения; HEAD не возвращает тело.

Workflow `.github/workflows/photostyle-avito.yml` запускается каждый час на 17-й минуте UTC (`17 * * * *`), вручную в GitHub → Actions → PhotoStyle Avito hourly snapshot → Run workflow и при изменениях только своих исходников в main. В Екатеринбурге это также 17-я минута каждого часа. Cron работает с default branch: файл нужно разместить там. Для другого интервала агент меняет только cron; минимальный интервал GitHub — 5 минут. Это не SLA: GitHub допускает задержки/пропуски при нагрузке и отключает расписание публичного репозитория после 60 дней неактивности. Проверять Actions и повторно включать отключённый workflow в его меню. [Официальные правила расписаний](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [включение workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).

Стандартные GitHub-hosted runners бесплатны для публичного репозитория; переход в private меняет доступные лимиты. [GitHub runners](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job). Cloudflare Pages Functions используют общую квоту Workers: Free — 100 000 запросов/сутки на аккаунт, включая PDF; лимит CPU Free 10 ms на запрос. HTTP слой читает только маленькие JSON и передаёт каталог потоком без XML/JSON-декодирования каталога или state. [Pages Functions pricing](https://developers.cloudflare.com/pages/functions/pricing/), [Workers limits](https://developers.cloudflare.com/workers/platform/limits/). Платные функции, intermediary подписки, PAT, сторонние токены не нужны. Runner получает только встроенный краткоживущий GITHUB_TOKEN с contents:write. Отдельные Actions artifacts не сохраняются: диагностические файлы остаются в ветке результатов, исключая накопление оплачиваемого artifact storage. Размер истории ветки следует наблюдать в GitHub.

Конвертер принимает только новый, ещё не существующий путь `--out`: даже существующая пустая папка отвергается. Полный snapshot сначала записывается в отдельную временную папку и появляется по целевому пути одним переименованием каталога. Предыдущий snapshot задаётся отдельным `--state` и никогда не используется как `--out`. Ошибки скачивания/проверки/защитного порога не меняют последний исправный snapshot. Workflow последовательно коммитит только свои результаты в `avito-feed`: snapshot commit, затем маленький `current.json` с его SHA; оба коммита публикуются одним fast-forward обновлением ветки. Pointer может быть кэширован до 60 секунд; status/feed всегда читаются по одному immutable SHA, поэтому гонки двух версий нет. Не используется unauthenticated GitHub API на HTTP пути (лимит 60/h). API runner авторизован, и только подтверждённый 404 ветки при доступном репозитории означает первый запуск. Сеть/403/5xx не сбрасывают state. Non-fast-forward останавливает запуск без force; повторный запуск заново читает актуальный state.

Статус последнего успеха не показывает последнюю неудачную попытку: ошибки и stack-free сообщение доступны в красном run Actions; владелец включает уведомления о failed workflows в GitHub. При ошибке preview остаётся последним исправным, а production закрывается через 4 часа. Порог меняется переменной Cloudflare Pages `STALE_HOURS` с повторным развёртыванием; по умолчанию 4. Каждый upstream запрос ограничен 45 секундами, JSON — 256 KiB, файлы — 100 MiB. При неизвестной длине поток обрывается на лимите: получатель получит ошибку загрузки, а не полный XML. До начала передачи oversized Content-Length даёт 503.

Локальная проверка:

```powershell
python -m unittest discover -s avito-service/tests -v
node --test avito-service/tests/worker.test.mjs
python avito-service/tools/check_publication.py
python avito-service/tools/package.py
```

`photostyle-avito-pages.zip` содержит `_worker.js`, `_routes.json`, `index.html` в корне для Pages Direct Upload; `photostyle-avito-source.zip` содержит исходники, один workflow, инструкции и локальные отчёты. Сборка детерминирована, без зависимостей и внешних записей. Большие state/preview не размещать в main: их создаёт workflow в avito-feed. [Операционная инструкция и миграция](docs/runbook.md).


Вариант товара выбирается один раз: для нового product UID берётся минимальный offer UID; последующие расчёты сохраняют `state.items[productUID].variant_id`. Появление другого варианта с меньшим UID не переключает объявление. Агент может осознанно сменить вариант через `product_overrides[productUID].variant_id` в config.json, предварительно проверив цену, текст, фото и идентичность объявления. Если сохранённый вариант пропал или стал недоступен, другой вариант автоматически не подставляется: причина отражается в отчёте, а ранее опубликованное объявление попадает в подтверждённую процедуру снятия. Если явно заданного варианта нет, возникает blocker `configured_variant_missing`.

Пример локального расчёта в PowerShell (целевая папка не создаётся заранее):

```powershell
$outPath = Join-Path $env:TEMP ('photostyle-avito-' + [guid]::NewGuid().ToString('N'))
python avito-service/converter.py --config avito-service/config.json --source-url https://photostyle.ru/tstore/yml/a8b941bea8a7ee192c94ad5b5a0059eb.yml --state path/to/previous/state.json --out $outPath
```

Для первого preview `--state` может указывать на отсутствующий файл; для существующего клиента перед production обязателен импорт полного опубликованного state из инструкции миграции. Каждый повторный расчёт получает другой отсутствующий `--out`.
