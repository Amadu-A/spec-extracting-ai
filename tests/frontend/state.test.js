// tests/frontend/state.test.js
/** Регрессии UI-state: нормализация страниц, ручной просмотр, replay и восстановление. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, applyEvent, exportPayload, parsePages, restoreSnapshot, completed } from '../../app/web/static/js/state.js';
import { invoiceState, applyInvoiceEvent, restoreInvoice } from '../../app/web/static/js/invoice-state.js';

test('Счета: ручной просмотр, переключение файлов, replay и независимая ревизия', () => {
  const state = invoiceState();
  state.jobId = 'test'; state.status = { files: [{ source_file: 'a.pdf', page_count: 5 }, { source_file: 'b.png', page_count: 1 }] };
  assert.equal(applyInvoiceEvent(state, 'page_started', { file_index: 0, pdf_page: 2, revision: 0 }, 1), true);
  state.viewed_pdf_page = 4;
  applyInvoiceEvent(state, 'text_delta', { text: '{', revision: 0 }, 2);
  applyInvoiceEvent(state, 'page_started', { file_index: 0, pdf_page: 2, revision: 0 }, 3);
  assert.equal(state.viewed_pdf_page, 4);
  applyInvoiceEvent(state, 'page_started', { file_index: 1, pdf_page: 1, revision: 0 }, 4);
  assert.equal(state.viewed_file, 1); assert.equal(state.document.filename, 'b.png');
  assert.equal(applyInvoiceEvent(state, 'text_delta', { text: 'duplicate', revision: 0 }, 4), false);
  state.result = { files: [] };
  applyInvoiceEvent(state, 'result_invalidated', { revision: 1 }, 5);
  assert.equal(state.result, null); assert.equal(state.raw, '');
  assert.equal(applyInvoiceEvent(state, 'text_delta', { text: 'stale', revision: 0 }, 6), false);
});

test('Счета: восстановление использует свой snapshot и валидную физическую страницу', () => {
  const state = invoiceState();
  restoreInvoice(state, { status: { job_id: 'invoice', revision: 2, current_file_index: 1, current_pdf_page: 1, files: [{ source_file: 'a.pdf', page_count: 4 }, { source_file: 'b.jpg', page_count: 1 }] }, result: null, raw_text: 'raw', last_event_id: 25 }, { viewed_file: 0, viewed_pdf_page: 3 });
  assert.equal(state.viewed_file, 0); assert.equal(state.viewed_pdf_page, 3); assert.equal(state.processing_file, 1);
  assert.equal(state.last_event_id, 25); assert.equal(state.revision, 2);
});

test('Диапазоны страниц: сортировка, дубликаты и физические границы', () => {
  assert.deepEqual(parsePages(' 3-5, 1, 3 ', 10), [1, 3, 4, 5]);
  for (const value of ['', '0', '4-2', '1,,2', '1-999999', 'a']) assert.throws(() => parsePages(value, 10));
});

test('Ручной просмотр сохраняется до следующего нового page_started VLM', () => {
  const state = initialState();
  applyEvent(state, 'page_started', { pdf_page: 3 }, 1);
  assert.equal(state.viewed_pdf_page, 3);
  state.viewed_pdf_page = 7;
  applyEvent(state, 'text_delta', { text: '{' }, 2);
  applyEvent(state, 'item_validated', { item: { Caption: 'A' } }, 3);
  applyEvent(state, 'page_completed', { pdf_page: 3 }, 4);
  assert.equal(state.viewed_pdf_page, 7);
  applyEvent(state, 'page_started', { pdf_page: 3, attempt: 2 }, 5);
  assert.equal(state.viewed_pdf_page, 7);
  applyEvent(state, 'page_started', { pdf_page: 4 }, 6);
  assert.equal(state.viewed_pdf_page, 4);
  state.viewed_pdf_page = 9;
  assert.equal(applyEvent(state, 'page_started', { pdf_page: 3 }, 1), false);
  assert.equal(state.viewed_pdf_page, 9);
  assert.equal(state.processing_pdf_page, 4);
});

test('Reconnect не дублирует подтверждённые записи', () => {
  const state = initialState();
  const item = { Caption: 'x', Units: 'шт', Quantity: '2,5', UserComment1: 'P', Pages: '1' };
  applyEvent(state, 'item_validated', { item }, 9);
  applyEvent(state, 'item_validated', { item }, 9);
  assert.equal(state.items.length, 1);
  assert.equal(state.last_event_id, 9);
});

test('Snapshot восстанавливает current и ручной viewed независимо', () => {
  const state = initialState();
  restoreSnapshot(state, { status: { job_id: 'j', document_id: 'd', filename: 'file.pdf', document_pages: 10,
    current_pdf_page: 4, revision: 1 }, partial: { items: [] }, raw_text: '{}', last_event_id: 40 }, { viewed_pdf_page: 9 });
  assert.equal(state.viewed_pdf_page, 9);
  assert.equal(state.processing_pdf_page, 4);
  applyEvent(state, 'page_started', { pdf_page: 4 }, 39);
  assert.equal(state.viewed_pdf_page, 9);
  assert.equal(completed('cancelled'), false);
  assert.equal(completed('completed_with_warnings'), true);
  assert.deepEqual(exportPayload(state), { file_name: 'file.pdf', items: [] });
});

test('Подтверждённый JSON использует имя сервера и не меняет исходный поток', () => {
  const state = initialState();
  assert.deepEqual(exportPayload(state), { file_name: '', items: [] });
  state.document = { filename: 'Проект "А".pdf' }; state.raw = '{"rows":[]}';
  applyEvent(state, 'item_validated', { item: { Caption: 'Позиция' } }, 1);
  const value = JSON.parse(JSON.stringify(exportPayload(state)));
  assert.deepEqual(value, { file_name: 'Проект "А".pdf', items: [{ Caption: 'Позиция' }] });
  assert.equal(state.raw, '{"rows":[]}');
  state.document = { filename: 'Новый.pdf' }; state.items = [];
  assert.deepEqual(exportPayload(state), { file_name: 'Новый.pdf', items: [] });
});
