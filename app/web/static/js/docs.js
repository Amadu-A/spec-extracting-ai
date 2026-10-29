// app/web/static/js/docs.js
/** Инициализация локального Swagger без inline scripts и передачи данных внешним CDN. */
window.SwaggerUIBundle({ url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true, persistAuthorization: false, validatorUrl: null });
