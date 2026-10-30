// app/web/static/js/api.js
/** Единый HTTP helper: cookie/CSRF, безопасные ошибки и скачивание экспортов. */
let csrf = '';
let expired = () => {};

/** Устанавливает серверный CSRF без хранения cookie в JavaScript. */
export function setCsrf(value) { csrf = value; }
/** Подключает действие при истечении browser session. */
export function onExpired(callback) { expired = callback; }

/** Генерирует ключ повторов и на LAN HTTP, где crypto.randomUUID недоступен. */
export function idempotencyKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
}

/** Копирует текст в HTTPS и в доверенном LAN HTTP через доступный browser fallback. */
export async function copyText(value) {
  if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(value); return; }
  const area = document.createElement('textarea'); area.value = value; area.className = 'visually-hidden';
  document.body.append(area); area.select();
  const copied = document.execCommand('copy'); area.remove();
  if (!copied) throw new Error('Браузер не разрешил копирование. Используйте экспорт JSON.');
}

/** Вызывает публичный API и возвращает JSON, не скрывая машинный код ошибки. */
export async function api(path, options = {}) {
  if (options.method && options.method !== 'GET' && !csrf) setCsrf((await api('/session')).csrf);
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf);
  const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'same-origin' });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(payload.error?.message || `Ошибка HTTP ${response.status}`);
    error.code = payload.error?.code;
    error.reference = payload.error?.reference_id;
    if (response.status === 401 && path !== '/session') await expired();
    throw error;
  }
  return response.json();
}

/** Скачивает реальный защищённый экспорт; ошибок вместо файла не сохраняет. */
export async function download(url, extension) {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error?.message || 'Экспорт недоступен');
  }
  const blob = await response.blob();
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `specification.${extension}`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
