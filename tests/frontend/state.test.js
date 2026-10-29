// tests/frontend/state.test.js
/** Регрессии UI-state: нормализация страниц, ручной просмотр, replay и восстановление. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { initialState, applyEvent, parsePages, restoreSnapshot, completed } from '../../app/web/static/js/state.js';

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
});
