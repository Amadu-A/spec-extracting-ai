// app/web/static/js/state.js
/** Чистая модель UI: ручной просмотр независим от обрабатываемой страницы VLM. */

/** Создаёт пустое состояние без демонстрационных результатов и телеметрии. */
export function initialState() {
  return { jobId: null, document: null, viewed_pdf_page: 1, processing_pdf_page: null,
    last_event_id: 0, revision: 0, items: [], raw: '', view: 'live', status: null };
}

/** Валидирует диапазоны до отправки; backend остаётся источником истины. */
export function parsePages(expression, total) {
  if (!expression.trim()) throw new Error('Укажите страницы PDF');
  const selected = new Set();
  for (const part of expression.split(',')) {
    const match = /^\s*([0-9]{1,6})\s*(?:-\s*([0-9]{1,6}))?\s*$/.exec(part);
    if (!match) throw new Error('Формат страниц: 1-5, 8, 12-16');
    const start = Number(match[1]); const end = Number(match[2] || match[1]);
    if (start < 1 || end < start || end > total) throw new Error(`Допустимы страницы 1–${total}`);
    for (let page = start; page <= end; page++) selected.add(page);
  }
  return [...selected].sort((a, b) => a - b);
}

/** Реагирует на новое событие ровно один раз; replay не отбирает ручной просмотр. */
export function applyEvent(state, type, data, eventId) {
  const id = Number(eventId);
  if (id && id <= state.last_event_id) return false;
  if (id) state.last_event_id = id;
  if (type === 'page_started' && data.pdf_page !== state.processing_pdf_page) {
    state.processing_pdf_page = data.pdf_page;
    state.viewed_pdf_page = data.pdf_page;
  }
  if (type === 'text_delta') state.raw += data.text;
  if (type === 'stream_reset') state.raw = '';
  if (type === 'item_validated') state.items.push(data.item);
  return true;
}

/** Восстанавливает атомарный серверный snapshot, сохраняя допустимый ручной просмотр. */
export function restoreSnapshot(state, snapshot, saved = null) {
  const status = snapshot.status;
  state.jobId = status.job_id;
  state.document = { id: status.document_id, filename: status.filename, page_count: status.document_pages };
  state.status = status;
  state.processing_pdf_page = status.current_pdf_page;
  state.viewed_pdf_page = saved?.viewed_pdf_page || status.current_pdf_page || 1;
  if (state.viewed_pdf_page > status.document_pages || state.viewed_pdf_page < 1) state.viewed_pdf_page = 1;
  state.items = snapshot.partial.items;
  state.raw = snapshot.raw_text;
  state.last_event_id = snapshot.last_event_id;
  state.revision = status.revision;
}

/** Считает готовность экспортов только по терминальному успешному состоянию. */
export function completed(status) { return ['completed', 'completed_with_warnings'].includes(status); }
/** Определяет продолжающееся durable задание независимо от соединения SSE. */
export function running(status) { return ['queued', 'running'].includes(status); }
