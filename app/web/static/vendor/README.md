<!-- app/web/static/vendor/README.md -->
# Локальные Swagger UI assets

`swagger-ui-bundle.js`, `swagger-ui.css` и `LICENSE.swagger-ui` — неизменённые распространяемые файлы пакета `swagger-ui-dist` (Apache-2.0), загруженные из официального npm registry. Их версия зафиксирована в `version.json`. Нативная документация и license upstream сохраняются; русская документация собственных модулей не применяется к содержимому third-party bundles.

Файлы обслуживаются локально для `/docs`, не требуют CDN и не обрабатывают документы напрямую. Обновление выполняется явно с проверкой OpenAPI E2E.
