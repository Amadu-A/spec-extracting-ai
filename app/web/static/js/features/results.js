// app/web/static/js/features/results.js
/** Безопасная подсветка JSON, реальная телеметрия и журнал фактических этапов. */
import { completed, running } from '../state.js';
import { copyText } from '../api.js';

const names = { queued: 'В очереди', running: 'Анализ документа', completed: 'Анализ завершён',
  completed_with_warnings: 'Завершено с предупреждениями', failed: 'Ошибка анализа', cancelled: 'Анализ отменён' };

/** Создаёт безопасные text/spans; HTML из PDF/модели никогда не интерпретируется. */
function highlight(line) {
  const fragment = document.createDocumentFragment();
  const pattern = /("(?:\\.|[^"\\])*"\s*:?)|\b(true|false|null|\d+(?:\.\d+)?)\b|[{}\[\],]/g;
  let start = 0;
  for (const match of line.matchAll(pattern)) {
    fragment.append(document.createTextNode(line.slice(start, match.index)));
    const span = document.createElement('span'); span.textContent = match[0];
    span.className = match[0].startsWith('"') ? (match[0].endsWith(':') ? 'code__key' : 'code__string') : (match[2] ? 'code__number' : 'code__punctuation');
    fragment.append(span); start = match.index + match[0].length;
  }
  fragment.append(document.createTextNode(line.slice(start))); return fragment;
}

/** Подключает общий result-state к DOM без собственных алгоритмов извлечения. */
export function setupResults(state, notify) {
  const code = document.querySelector('[data-code]');
  const scroll = document.querySelector('[data-code-scroll]');
  const menu = document.querySelector('[data-menu]');
  let previousText = null;
  document.querySelector('[data-menu-toggle]').addEventListener('click', event => {
    menu.hidden = !menu.hidden; event.currentTarget.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.querySelector('[data-copy]').addEventListener('click', async () => {
    try { await copyText(JSON.stringify({ items: state.items }, null, 2)); notify('Подтверждённый JSON скопирован'); }
    catch (failure) { notify(failure.message); }
    menu.hidden = true;
  });
  document.querySelector('[data-toggle-view]').addEventListener('click', event => {
    state.view = state.view === 'live' ? 'validated' : 'live';
    event.currentTarget.textContent = state.view === 'live' ? 'Показать подтверждённый JSON' : 'Показать поток VLM';
    menu.hidden = true; render();
  });
  document.querySelector('[data-journal-toggle]').addEventListener('click', event => {
    const journal = document.querySelector('[data-journal]'); journal.hidden = !journal.hidden;
    event.currentTarget.textContent = journal.hidden ? '⌄  Развернуть' : '⌃  Свернуть'; event.currentTarget.setAttribute('aria-expanded', String(!journal.hidden));
  });
  /** Рисует изменившийся текст с сохранением ручного scroll при чтении старых строк. */
  function render() {
    const text = state.view === 'validated' ? JSON.stringify({ items: state.items }, null, 2) : state.raw;
    if (previousText !== text) {
      const atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 50;
      const fragment = document.createDocumentFragment();
      const visible = text.slice(-160000);
      if (!visible) {
        const placeholder = document.createElement('span'); placeholder.className = 'code__placeholder';
        placeholder.textContent = 'Ответ shared-vlm появится после запуска анализа.'; fragment.append(placeholder);
      } else for (const line of visible.split('\n')) {
        const element = document.createElement('span'); element.className = 'code__line'; element.append(highlight(line)); fragment.append(element);
      }
      code.replaceChildren(fragment); previousText = text;
      if (atBottom) scroll.scrollTop = scroll.scrollHeight;
    }
    const status = state.status;
    document.querySelector('[data-item-count]').textContent = state.items.length;
    document.querySelector('[data-current-page]').textContent = state.processing_pdf_page || '—';
    document.querySelector('[data-progress]').textContent = `${status?.progress || 0}%`;
    document.querySelector('[data-progress-bar]').value = status?.progress || 0;
    document.querySelector('[data-warning-count]').textContent = status?.warning_count || 0;
    document.querySelector('[data-job-status]').textContent = `●  ${names[status?.status] || 'Ожидание документа'}`;
    document.querySelector('[data-processing-label]').textContent = state.processing_pdf_page ? `VLM: лист ${state.processing_pdf_page}` : 'Нет обработки';
    document.querySelector('[data-stream-state]').textContent = state.view === 'validated' ? 'Подтверждённый JSON' : (running(status?.status) ? '●  Стриминг ответа VLM' : 'Поток ответа VLM');
    document.querySelectorAll('[data-export]').forEach(button => { button.disabled = !completed(status?.status); });
    document.querySelector('[data-cancel]').hidden = !running(status?.status);
    document.querySelector('[data-retry]').disabled = !status || running(status.status) || !status.selected_pdf_pages.includes(state.viewed_pdf_page);
    if (status) {
      document.querySelector('[data-selection-label]').textContent = `Просмотр: ${state.viewed_pdf_page} · Выбрано: ${status.total_pages} · Готово: ${status.processed_pages}`;
    } else {
      document.querySelector('[data-selection-label]').textContent = state.document ? `Просмотр: ${state.viewed_pdf_page} · Укажите страницы анализа` : 'Страницы ещё не выбраны';
    }
    renderJournal(status?.pages || []);
  }
  /** Показывает только измеренные этапы, включая прерванные попытки worker. */
  function renderJournal(pages) {
    const journal = document.querySelector('[data-journal]');
    const stages = pages.flatMap(page => page.stages.map(stage => ({ ...stage, page: page.pdf_page })));
    if (!stages.length) {
      const empty = document.createElement('li'); empty.className = 'journal__empty';
      empty.textContent = 'Фактические этапы появятся при обработке документа.';
      journal.replaceChildren(empty); return;
    }
    const atBottom = journal.scrollHeight - journal.scrollTop - journal.clientHeight < 50;
    const fragment = document.createDocumentFragment();
    for (const stage of stages.slice(-100)) {
      const row = document.createElement('li'); row.className = 'journal__row';
      const mark = document.createElement('span'); mark.className = `journal__status journal__status--${stage.state}`; mark.textContent = stage.state === 'completed' ? '●' : (stage.state === 'running' ? '◉' : '⚠');
      const name = document.createElement('span'); name.textContent = `${stage.name} · ${stage.page}`;
      const time = document.createElement('time'); time.className = 'journal__time'; time.dateTime = stage.started_at; time.textContent = new Date(stage.started_at).toLocaleTimeString('ru-RU');
      const status = document.createElement('span'); status.className = `journal__state journal__state--${stage.state}`; status.textContent = { completed: 'Завершено', running: 'В процессе', failed: 'Ошибка', interrupted: 'Прервано' }[stage.state] || stage.state;
      const duration = document.createElement('span'); duration.className = 'journal__duration'; duration.textContent = stage.duration_ms == null ? '—' : `${(stage.duration_ms / 1000).toFixed(1)} с`;
      row.append(mark, name, time, status, duration); fragment.append(row);
    }
    journal.replaceChildren(fragment); if (atBottom) journal.scrollTop = journal.scrollHeight;
  }
  return { render };
}
