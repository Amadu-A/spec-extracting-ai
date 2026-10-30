<!-- reports/deployment-repair.md -->
# Исправление развёртывания — 30.10.2026

Исправления подготовлены локально. Серверное развёртывание и настоящий RabbitMQ bootstrap пока не подтверждены: SSH `main@192.168.55.3` с существующей аутентификацией завершился `Permission denied (publickey,password)`. Пользователь выполняет commit/push и приведённые ниже серверные команды. Секреты сервера не запрашивались и не читались; shared stack и PDRD не изменялись.

## Исходное состояние и правила

- Локальный HEAD и проверенная актуальная GitHub main: `86eca26a7a5859b5536d3b31150fd869c8fee37e`. До работы незакоммиченных изменений не было. Серверный HEAD через SSH прочитать не удалось.
- Правила shared main проверены начиная с `docs/LLM_CONTEXT.md`, включая инфраструктурные инструкции и `docs/services.yaml`; HEAD `b0d955acaeb9e54c24a4b44c787883808d467330`.
- Рабочий [PDRD scripts/up.sh](https://github.com/Amadu-A/PDRD-validation/blob/8fd5adcdf1db9088f182f69bee7e7df1ef9ca2b1/scripts/up.sh) изучен и сопоставлен с существующим bootstrap. Сохранены создание отсутствующих user/vhost и проверка пароля. PDRD меняет пароль при несовпадении и повторно назначает права; здесь эти действия ограничены требованиями владельца: пароль не меняется, корректные права повторно не записываются, общий stack не запускается и не перезапускается. Текущая реализация не переписана с нуля.

## Причина PermissionError и исправление

Linux `COPY` переносит mode исходного файла; Git не хранит различие 600/644. После `umask 077` новые исходники могут стать 600, каталоги — 700. При COPY с владельцем root штатный UID 10001 не может прочитать `/app/app/operations.py`. Обычные тесты в checkout под владельцем файла этого не обнаруживают. Свойства COPY описаны в [Dockerfile reference](https://docs.docker.com/reference/dockerfile/#copy).

Dockerfile теперь явно устанавливает `/app` и каталоги кода/prompt/миграций в 755, файлы в 644; владельцем кода остаётся root. Собственные данные `/app/data` создаются с владельцем UID/GID 10001 и mode 700. Установка зависимостей выполняется с umask 022. Сохранены USER app, read_only, cap_drop=ALL, no-new-privileges и существующие API, PostgreSQL, очередь и VLM.

Проверено на настоящих Linux-образах, собранных из одного контекста с mode 600/700:

| Вариант | Фактический результат |
|---|---|
| Dockerfile исходного HEAD | Сборка успешна; `import app.operations` от штатного USER падает: `PermissionError: [Errno 13] Permission denied: '/app/app/operations.py'` |
| Исправленный Dockerfile | UID 10001; прочитаны все ресурсы app/prompts/migrations и 38 Python-модулей; импорт operations успешен |
| Исправленный образ, `python -m app.operations config` | Exit 0, `Settings/secrets: OK`, без PermissionError, с искусственными секретами |

Runtime-проверки выполнялись с read-only filesystem, network=none, cap_drop=ALL и no-new-privileges; USER не переопределялся. Временные собственные контейнеры и теги удалены. Доступ к реальным AMQP/VLM для этого теста не имитируется и не заявляется.

## Bootstrap и диагностика

`up.sh` после сборки проверяет импорт/config под UID 10001 ещё до команд RabbitMQ. Settings разрешает автоматическую подготовку только `specification`/`specification` и `specification.extractions`. Пароль поступает из Pydantic через захваченный stdout и stdin существующего контейнера; он отсутствует в argv, пользовательском журнале и исключениях. Сохраняются временный приватный файл RAM, cleanup, timeout и flock. Служебные теги и права в чужих vhost вызывают отказ без их изменения.

Добавлена строка `RabbitMQ bootstrap: user=specification vhost=specification mutations=true|false`. Значение true означает, что bootstrap создал отсутствующий ресурс или исправил права в своём vhost. Корректный повторный запуск показывает false; повторная сборка собственного приложения остаётся штатной частью up.sh.

После healthchecks API/worker выполняется `broker-check --require-consumer`. Новый процесс использует настоящий RabbitPublisher приложения: AMQP аутентификация, совместимое durable-объявление рабочей очереди и passive declare с проверкой consumer_count. Отсутствие подписки worker через 15 секунд — ошибка запуска. Диагностика не извлекает и не публикует сообщения. Ошибки CLI/Pydantic и библиотечные логи скрывают значения секретов. Успех этой команды на сервере пока не получен агентом.

## Резервный registry

Если локальный `postgres:16-alpine` уже есть, сетевой pull не требуется. Иначе up.sh пробует Docker Hub; после отказа/timeout — `mirror.gcr.io/library/postgres:16-alpine`, затем назначает локальный официальный тег. Каждому pull дан предел 180 секунд. Ошибка двух источников останавливает запуск до мутаций RabbitMQ. Не меняются Docker daemon, Compose, proxy или настройки shared инфраструктуры. Python/Astral остаётся закреплённым образом GHCR: зеркало предназначено для образов Docker Hub.

Доступность OCI manifest зеркала проверена: `sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab`, linux/amd64 `16.15-alpine3.24`. Автоматическое переключение при искусственном отказе Hub и остановка при отказе обоих источников проверены функционально с Docker double; это не серверный сетевой прогон. Назначение кеша описано в [официальной документации mirror.gcr.io](https://docs.cloud.google.com/artifact-registry/docs/pull-cached-dockerhub-images).

## Тесты окончательного кода

| Проверка | Результат |
|---|---|
| Windows `ops/check-quality.ps1 -Browser -Docker` | 151 passed, 2 skipped, 4 deselected; 157,58 с; coverage 82% |
| Ruff check/format | Успех; 59 Python-файлов отформатированы |
| JavaScript | 4 passed, Node из закреплённого Playwright |
| Chromium E2E | 2 passed в составе Windows gate |
| Настоящая Docker-регрессия | passed в составе gate; stdin tar.gz с правами 600/700; UID 10001/import/config |
| Linux pytest, существующий validation-образ, network=none | 150 passed, 7 deselected; 98,94 с; coverage 81% |
| Git diff --check | Успех; index не изменялся |
| Реальный серверный AMQP/API/worker | Не проверен: нет успешной SSH-аутентификации |

Windows skips — два POSIX-теста, успешно выполненные на Linux (GNU timeout и Node без системного node). Четыре live-проверки не включались; Linux дополнительно исключал два browser-теста и Docker-сборку, уже выполненные отдельно. Все четыре локальных PDF/JSON прошли существующие проверки baseline-контракта; извлечение моделью повторно не выполнялось, новые OCR-метрики не заявляются. Предыдущая матрица остаётся в `reports/quality.md`.

Новые/дополненные регрессии: отказ образа до bootstrap; создание и повтор без мутаций; отсутствие изменений shared_admin, contract-diff и pdrd_validation в изолированном state; отказ без смены неверного пароля; запрет чужих namespace/очереди; кеш/зеркало/двойной отказ registry; отсутствие consumer и его позднее появление; скрытие секретных текстов исключений и Pydantic input_value. Тесты bootstrap используют double, Docker-регрессия — настоящую сборку. Внутренние подробные успешные прогоны сохранены в игнорируемые `permission-*.log`; пользовательские scripts логи не скрывают.

## Команды оператора

Windows, Docker Desktop запущен:

```powershell
.\ops\check-quality.ps1 -Browser -Docker
uv run --frozen pytest tests/test_docker_image.py --docker -s
git diff --check
git add -- Dockerfile README.md app/operations.py docs/operations.md ops/check-quality.ps1 pyproject.toml scripts/test.sh scripts/up.sh tests/conftest.py tests/test_bootstrap_settings.py tests/test_deployment.py tests/test_docker_image.py tests/test_operations.py .github/workflows/quality.yml reports/files.md reports/implementation-report.md reports/deployment-repair.md
git diff --cached --stat
git commit -m "Исправить права образа и проверки RabbitMQ при развёртывании"
git push origin main
```

Сервер, после push:

```bash
cd ~/projects/spec-extracting-ai
git status --short
git remote set-url origin git@github.com:Amadu-A/spec-extracting-ai.git
git pull --ff-only origin main
git rev-parse HEAD
uv run --frozen python -m app.operations normalize-env
bash scripts/test.sh --browser --docker
bash scripts/up.sh
docker compose --env-file .env.example --env-file .env exec -T api id
docker compose --env-file .env.example --env-file .env exec -T api python -m app.operations config
docker compose --env-file .env.example --env-file .env exec -T api python -m app.operations broker-check --require-consumer
curl --fail --show-error http://127.0.0.1:8083/api/v1/health/ready
docker exec --user rabbitmq shared-rabbitmq-1 rabbitmqctl list_user_permissions specification
docker exec --user rabbitmq shared-rabbitmq-1 rabbitmqctl list_queues -p specification name durable consumers messages
bash scripts/up.sh
docker compose --env-file .env.example --env-file .env ps
docker compose --env-file .env.example --env-file .env logs --tail=80 api worker
```

`normalize-env` сохраняет действующие секреты. В `.env` остаются только DATABASE_PASSWORD, RABBITMQ_PASSWORD, UI_PASSWORD и API_KEYS; несекретные параметры — в `.env.example`. Ручная подготовка RabbitMQ и ввод пароля не нужны. Не заменяйте RABBITMQ_PASSWORD случайным новым значением для уже существующего пользователя: скрипт ожидаемо остановится при несовпадении, не меняя пароль.

Ожидания: UID 10001; config OK; readiness HTTP 200; права specification только в vhost specification; durable=true, consumers>0; при втором up.sh mutations=false; API и worker healthy. Наличие пользователя/vhost само по себе не считается доказательством работающего AMQP.

Ручная проверка с другого компьютера: открыть `http://192.168.55.3:8083`, войти своим UI_PASSWORD, загрузить PDF и запустить выбранные листы. PDF слева следует обрабатываемой странице, справа поступают реальные токены, задание завершается, JSON и Excel содержат одинаковые строки. Проверить `/docs` и создание/статус задания с собственным API-ключом. Конкретный запуск задания подтверждает весь путь API → очередь → worker → VLM; один healthcheck этого не доказывает.

## Файлы

Изменены Dockerfile, app/operations.py, scripts/up.sh, оба quality script, pyproject.toml, CI, tests/conftest.py, tests/test_bootstrap_settings.py, tests/test_deployment.py, README и operations. Добавлены tests/test_docker_image.py и tests/test_operations.py; обновлены reports/files.md, reports/implementation-report.md и этот отчёт. Полные тексты всех 17 файлов записаны в локальный игнорируемый `reports/live/deployment-changes-full.txt`; реальные .env/examples/outputs туда не входят. Commit/push агентом не выполнялись.
