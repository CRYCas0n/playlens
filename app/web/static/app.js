/* ==========================================================================
   Playlens client behaviour.

   Progressive enhancement only. Every page is fully usable with JavaScript off:
   the catalogue filters are a plain form, pagination is links, and the game page
   is server-rendered. This file makes those things nicer, not possible.

   The behaviours and the reasoning behind them come from the design package's
   prototype (design/prototype/assets/app.js); the rules it encodes are normative.
   ========================================================================== */
(function () {
  'use strict';

  /* ---------- theme ---------------------------------------------------- */
  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('pl-theme'); } catch (e) { /* private mode */ }
    if (saved) document.documentElement.setAttribute('data-theme', saved);
    document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
      button.addEventListener('click', function () {
        var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('pl-theme', next); } catch (e) { /* ignore */ }
      });
    });
  }

  /* ---------- rails ----------------------------------------------------- */
  function initRails(root) {
    (root || document).querySelectorAll('[data-rail]').forEach(function (rail) {
      var track = rail.querySelector('.rail__track');
      if (!track) return;
      var controls = rail.querySelector('.rail__controls');
      if (controls) {
        // Arrows exist only when there is something to scroll to. Controls that do
        // nothing teach people to ignore controls.
        var sync = function () { controls.hidden = track.scrollWidth <= track.clientWidth + 4; };
        sync();
        window.addEventListener('resize', sync);
      }
      rail.querySelectorAll('[data-rail-dir]').forEach(function (button) {
        button.addEventListener('click', function () {
          var direction = button.getAttribute('data-rail-dir') === 'prev' ? -1 : 1;
          track.scrollBy({ left: direction * Math.round(track.clientWidth * 0.8), behavior: 'smooth' });
        });
      });
    });
  }

  /* ---------- expandable summaries -------------------------------------- */
  function initClamps(root) {
    (root || document).querySelectorAll('.clamp').forEach(function (clamp) {
      var toggle = clamp.querySelector('.clamp__toggle');
      if (!toggle) return;
      var body = clamp.querySelector('.clamp__body');
      if (body && body.scrollHeight <= 172 && clamp.getAttribute('data-expanded') === 'false') {
        toggle.style.display = 'none';
        return;
      }
      toggle.addEventListener('click', function () {
        var open = clamp.getAttribute('data-expanded') === 'true';
        clamp.setAttribute('data-expanded', String(!open));
        toggle.textContent = open ? (toggle.dataset.more || 'Read full summary')
                                  : (toggle.dataset.less || 'Show less');
        toggle.setAttribute('aria-expanded', String(!open));
      });
    });
  }

  /* ---------- spoiler shield -------------------------------------------- */
  function initSpoilers(root) {
    (root || document).querySelectorAll('.spoiler-shield').forEach(function (shield) {
      var button = shield.querySelector('[data-reveal]');
      if (!button) return;
      button.addEventListener('click', function () { shield.setAttribute('data-revealed', 'true'); });
    });
  }

  /* ---------- anchor nav ------------------------------------------------- */
  function initSubnav() {
    var links = Array.prototype.slice.call(document.querySelectorAll('.subnav__link'));
    if (!links.length) return;
    var targets = links
      .map(function (link) { return document.querySelector(link.getAttribute('href')); })
      .filter(Boolean);
    if (!targets.length) return;

    // The active link is the LAST section whose top has passed the sticky header.
    // An IntersectionObserver is wrong here: several sections are on screen at once
    // and the last observer callback wins, lighting up the bottom section on load.
    function sync() {
      var line = 140, active = targets[0];
      targets.forEach(function (element) {
        if (element.getBoundingClientRect().top <= line) active = element;
      });
      links.forEach(function (link) {
        link.classList.toggle('is-active', link.getAttribute('href') === '#' + active.id);
      });
    }
    window.addEventListener('scroll', sync, { passive: true });
    sync();
  }

  /* ---------- preferred platform ----------------------------------------
     Not a new control in the topbar -- the design's information architecture has
     none. The choice is made where the platforms already are, and it drives the
     catalogue filter and the second verdict sentence (ADR-010, C-12).            */
  function preferredPlatform() {
    try { return localStorage.getItem('pl-platform'); } catch (e) { return null; }
  }

  function initPlatformPreference() {
    document.querySelectorAll('[data-prefer-platform]').forEach(function (button) {
      var slug = button.getAttribute('data-prefer-platform');
      if (preferredPlatform() === slug) button.classList.add('is-active');
      button.addEventListener('click', function () {
        var current = preferredPlatform();
        try {
          if (current === slug) localStorage.removeItem('pl-platform');
          else localStorage.setItem('pl-platform', slug);
        } catch (e) { return; }
        var url = new URL(window.location.href);
        if (current === slug) url.searchParams.delete('platform');
        else url.searchParams.set('platform', slug);
        window.location.href = url.toString();
      });
    });

    // A game page opened without an explicit platform adopts the stored preference.
    var stored = preferredPlatform();
    if (stored && /^\/games\/[^/]+$/.test(window.location.pathname)) {
      var url = new URL(window.location.href);
      if (!url.searchParams.has('platform')) {
        url.searchParams.set('platform', stored);
        window.location.replace(url.toString());
      }
    }
  }

  /* ---------- catalogue: filter in place --------------------------------- */
  function initCatalog() {
    var form = document.getElementById('catalog-form');
    var results = document.getElementById('catalog-results');
    if (!form || !results) return;

    var timer = null;
    var controller = null;

    function apply() {
      var params = new URLSearchParams(new FormData(form));
      // A filter change resets to the first page; keeping an offset would land the
      // reader on an empty page.
      params.delete('offset');
      var query = params.toString();

      if (controller) controller.abort();
      controller = new AbortController();
      results.setAttribute('aria-busy', 'true');

      fetch('/games/fragment?' + query, { signal: controller.signal })
        .then(function (response) { return response.text(); })
        .then(function (html) {
          results.innerHTML = html;
          results.removeAttribute('aria-busy');
          // Filters live in the URL, so a link reproduces the state (US-005).
          window.history.replaceState({}, '', '/games' + (query ? '?' + query : ''));
        })
        .catch(function (error) {
          if (error.name === 'AbortError') return;
          results.removeAttribute('aria-busy');
        });
    }

    form.addEventListener('submit', function (event) { event.preventDefault(); apply(); });
    form.addEventListener('change', apply);
    form.addEventListener('input', function (event) {
      if (event.target.type !== 'search') return;
      window.clearTimeout(timer);
      timer = window.setTimeout(apply, 200);  // debounce, per design/COMPONENTS.md
    });

    var stored = preferredPlatform();
    var untouched = !new URLSearchParams(window.location.search).has('platform');
    if (stored && untouched) {
      var checkbox = form.querySelector('input[name="platform"][value="' + stored + '"]');
      if (checkbox && !checkbox.checked) { checkbox.checked = true; apply(); }
    }
  }

  /* ---------- monitoring: SSE with a polling fallback ---------------------
     SSE is not a single point of failure: if the stream will not open, the page
     falls back to polling and keeps updating without a reload (ADR-013).         */
  function initMonitoring() {
    var root = document.getElementById('monitoring');
    if (!root) return;

    var indicator = document.getElementById('stream-state');
    var feed = document.getElementById('event-feed');
    var opened = false;
    var failures = 0;

    function setState(text, cls) {
      if (!indicator) return;
      indicator.textContent = text;
      indicator.className = 'status ' + cls;
    }

    function refresh() {
      fetch('/api/v1/monitoring/status')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var el = document.getElementById('system-message');
          if (el && data.system) el.textContent = data.system.message;
          document.querySelectorAll('[data-kpi]').forEach(function (node) {
            var path = node.getAttribute('data-kpi').split('.');
            var value = data;
            path.forEach(function (key) { value = value ? value[key] : null; });
            if (value !== null && value !== undefined) node.textContent = value;
          });
        })
        .catch(function () { /* the page keeps its last good values */ });
    }

    function startPolling() {
      setState('Polling', 'status--idle');
      refresh();
      window.setInterval(refresh, 3000);
    }

    if (!window.EventSource) { startPolling(); return; }

    var source = new EventSource('/api/v1/monitoring/stream');
    var giveUp = window.setTimeout(function () {
      if (!opened) { source.close(); startPolling(); }
    }, 5000);

    source.addEventListener('open', function () {
      opened = true;
      failures = 0;
      window.clearTimeout(giveUp);
      setState('Live', 'status--ok');
    });

    source.addEventListener('error', function () {
      failures += 1;
      setState('Reconnecting', 'status--warn');
      if (failures >= 3) { source.close(); startPolling(); }
    });

    ['run.started', 'run.progress', 'run.finished', 'job.finished', 'game.synced',
     'summary.generated', 'summary.rejected', 'run.error'].forEach(function (name) {
      source.addEventListener(name, function (event) {
        refresh();
        if (!feed) return;
        var payload = JSON.parse(event.data);
        var row = document.createElement('div');
        row.className = 'log__row';
        row.textContent = new Date(payload.ts).toLocaleTimeString() + '  ' +
          payload.event + (payload.message ? '  ' + payload.message : '');
        feed.prepend(row);
        while (feed.children.length > 40) feed.removeChild(feed.lastChild);
      });
    });
  }

  /* ---------- run now ---------------------------------------------------- */
  function initRunNow() {
    var button = document.getElementById('run-now');
    if (!button) return;
    button.addEventListener('click', function () {
      var token = window.prompt('Admin token');
      if (!token) return;
      button.setAttribute('aria-busy', 'true');
      fetch('/api/v1/admin/crawl/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token },
        body: '{}'
      })
        .then(function (response) { return response.json().then(function (b) { return [response.status, b]; }); })
        .then(function (pair) {
          var status = pair[0], body = pair[1];
          button.removeAttribute('aria-busy');
          // 409 is an expected answer, not a failure: a run is already in progress.
          window.alert(status === 202
            ? 'Run queued. It starts as soon as a worker is free; nothing is cancelled.'
            : (body.detail || 'Could not start the run.'));
        })
        .catch(function () {
          button.removeAttribute('aria-busy');
          window.alert('Could not reach the server.');
        });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initTheme();
    initRails();
    initClamps();
    initSpoilers();
    initSubnav();
    initPlatformPreference();
    initCatalog();
    initMonitoring();
    initRunNow();
  });
})();
