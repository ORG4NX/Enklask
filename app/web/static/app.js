/* Enklask — shared UI behaviour: accessible modals, toast auto-dismiss,
   topic filter URL sync + active-filter chips. */

/* ---- Modals: focus restore, Tab trap, Escape ---- */
var _uiModalOpener = null;

function uiOpenModal(el) {
  if (!el) return;
  _uiModalOpener = document.activeElement;
  el.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  var target = el.querySelector('[autofocus]') ||
    el.querySelector('input, select, textarea, button, a[href]');
  if (target) target.focus();
}

function uiCloseModal(el) {
  if (!el) return;
  el.classList.add('hidden');
  document.body.style.overflow = '';
  if (_uiModalOpener && document.contains(_uiModalOpener)) _uiModalOpener.focus();
  _uiModalOpener = null;
}

document.addEventListener('keydown', function (e) {
  var open = document.querySelector('[role="dialog"]:not(.hidden)');
  if (!open) return;
  if (e.key === 'Escape') { uiCloseModal(open); return; }
  if (e.key !== 'Tab') return;
  var f = open.querySelectorAll(
    'a[href], button:not([disabled]), input:not([type="hidden"]), select, textarea');
  f = Array.prototype.filter.call(f, function (n) { return n.offsetParent !== null; });
  if (!f.length) return;
  var first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

/* ---- Toast auto-dismiss (swapped in by htmx, out-of-band) ---- */
var _toastTimer = null;
document.body.addEventListener('htmx:afterSwap', function () {
  var t = document.querySelector('#toast [data-auto-dismiss]');
  if (!t) return;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function () { var h = document.getElementById('toast'); if (h) h.innerHTML = ''; },
    parseInt(t.getAttribute('data-auto-dismiss'), 10) || 5000);
});
document.body.addEventListener('htmx:afterRequest', function (e) {
  // An "Annuler" click inside the toast dismisses it.
  if (e.detail.elt && e.detail.elt.closest && e.detail.elt.closest('#toast')) {
    setTimeout(function () { var h = document.getElementById('toast'); if (h) h.innerHTML = ''; }, 0);
  }
});

/* ---- Topic filters: keep URL in sync, show chips, reset ---- */
(function () {
  var bar = document.getElementById('filters');
  var chips = document.getElementById('active-filters');
  if (!bar || !chips) return;

  var SKIP = { topic_id: 1, sort: 1 };

  var toggle = document.getElementById('filters-toggle');
  var countEl = document.getElementById('filters-count');
  if (toggle) toggle.addEventListener('click', function () {
    var collapsed = bar.classList.toggle('filters-collapsed');
    toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  });

  function fields() { return bar.querySelectorAll('input[name], select[name]'); }

  function labelFor(el) {
    var v = el.tagName === 'SELECT' ? el.options[el.selectedIndex].text : el.value;
    var l = el.getAttribute('data-label') || el.name;
    if (el.name === 'keyword') return '« ' + v + ' »';
    if (el.name === 'price_min') return 'Prix ≥ ' + v + ' €';
    if (el.name === 'price_max') return 'Prix ≤ ' + v + ' €';
    if (el.name === 'distance_max') return '≤ ' + v + ' km';
    return l + ' : ' + v;
  }

  function sync() {
    var params = new URLSearchParams();
    chips.innerHTML = '';
    var count = 0;
    fields().forEach(function (el) {
      if (el.name === 'topic_id' || !el.value) return;
      if (el.type === 'hidden' && el.name === 'tag') { params.append('tag', el.value); return; }
      if (el.name === 'sort' && el.value === 'recent') return;
      params.append(el.name, el.value);
      if (SKIP[el.name]) return;
      count++;
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-brand-light text-brand text-xs font-semibold hover:bg-brand/20 transition';
      b.setAttribute('aria-label', 'Retirer le filtre ' + labelFor(el));
      b.textContent = labelFor(el) + ' ✕';
      b.addEventListener('click', function () { el.value = ''; el.dispatchEvent(new Event('change', { bubbles: true })); });
      chips.appendChild(b);
    });
    if (count) {
      var r = document.createElement('button');
      r.type = 'button';
      r.className = 'text-xs font-medium text-gray-500 underline hover:text-gray-800 px-1';
      r.textContent = 'Réinitialiser les filtres';
      r.addEventListener('click', function () {
        fields().forEach(function (el) {
          if (el.type === 'hidden' || el.name === 'sort') return;
          el.value = '';
        });
        bar.querySelector('[name="keyword"]').dispatchEvent(new Event('change', { bubbles: true }));
      });
      chips.appendChild(r);
    }
    if (countEl) countEl.textContent = count ? '(' + count + ')' : '';
    var qs = params.toString();
    history.replaceState(null, '', bar.getAttribute('data-base-url') + (qs ? '?' + qs : ''));
  }

  bar.addEventListener('change', function () { setTimeout(sync, 0); });
  bar.addEventListener('keyup', function () { setTimeout(sync, 0); });
  sync();
})();
