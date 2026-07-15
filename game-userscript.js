// ==UserScript==
// @name         Pinterest Chase — от предмета к предмету
// @namespace    pinterest-chase
// @version      1.0
// @description  Мультиплеерная игра поверх Pinterest: дойди от стартового слова до целевого через картинки
// @match        *://*.pinterest.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      localhost
// @connect      127.0.0.1
// @connect      *
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';

  const state = {
    serverUrl: GM_getValue('serverUrl', 'http://localhost:8787'),
    id: GM_getValue('id', ''),
    nickname: GM_getValue('nickname', ''),
    lobbyId: GM_getValue('lobbyId', ''),
    lastStartTime: GM_getValue('lastStartTime', 0),
    finishedThisRound: GM_getValue('finishedThisRound', false),
    targetFrom: GM_getValue('targetFrom', ''),
    targetTo: GM_getValue('targetTo', ''),
  };
  function persist() {
    GM_setValue('serverUrl', state.serverUrl);
    GM_setValue('id', state.id);
    GM_setValue('nickname', state.nickname);
    GM_setValue('lobbyId', state.lobbyId);
    GM_setValue('lastStartTime', state.lastStartTime);
    GM_setValue('finishedThisRound', state.finishedThisRound);
    GM_setValue('targetFrom', state.targetFrom);
    GM_setValue('targetTo', state.targetTo);
  }

  function decodeBase64Json(value) {
    const binary = atob(value);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  function applyRoundConfigFromUrl() {
    const match = location.hash.match(/(?:^|[#&])pc=([^&]+)/);
    if (!match) return;
    try {
      const config = decodeBase64Json(decodeURIComponent(match[1]));
      state.serverUrl = config.serverUrl || state.serverUrl;
      state.id = config.id || state.id;
      state.nickname = config.nickname || state.nickname;
      state.lobbyId = config.lobbyId || state.lobbyId;
      state.targetFrom = config.from || state.targetFrom;
      state.targetTo = config.to || state.targetTo;
      if (config.startTime && state.lastStartTime !== config.startTime) {
        state.lastStartTime = config.startTime;
        state.finishedThisRound = false;
      }
      persist();
    } catch (e) {
      console.warn('Pinterest Chase: не удалось прочитать параметры раунда', e);
    }
  }

  applyRoundConfigFromUrl();

  function api(method, pathName, body) {
    return new Promise((resolve, reject) => {
      const url = state.serverUrl.replace(/\/$/, '') + pathName;
      GM_xmlhttpRequest({
        method, url,
        headers: { 'Content-Type': 'application/json' },
        data: body ? JSON.stringify(body) : undefined,
        onload: (r) => {
          try { resolve(JSON.parse(r.responseText)); }
          catch (e) { reject(e); }
        },
        onerror: reject,
      });
    });
  }

  async function leaveLobby() {
    const lobbyId = state.lobbyId;
    state.lobbyId = '';
    state.targetFrom = '';
    state.targetTo = '';
    state.finishedThisRound = false;
    persist();
    render();
    if (lobbyId) {
      await api('POST', '/lobby/leave', { lobbyId, id: state.id }).catch(() => {});
    }
  }

  function normalizeText(value) {
    return String(value || '')
      .toLowerCase()
      .replace(/ё/g, 'е')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function collectPinterestText() {
    const parts = [document.title, location.href];
    const nodes = document.querySelectorAll('a, article, h1, h2, h3, img, [aria-label], [title], [alt]');
    for (const node of nodes) {
      if (node.closest && node.closest('#pc-panel')) continue;
      const text = [
        node.getAttribute && node.getAttribute('aria-label'),
        node.getAttribute && node.getAttribute('title'),
        node.getAttribute && node.getAttribute('alt'),
        node.innerText,
        node.textContent,
      ].filter(Boolean).join(' ');
      if (text) parts.push(text);
      if (parts.join(' ').length > 120000) break;
    }
    return normalizeText(parts.join(' '));
  }

  async function finishRoundAutomatically(reason) {
    if (!state.id || !state.lobbyId || state.finishedThisRound) return;
    state.finishedThisRound = true;
    persist();
    try {
      await api('POST', '/lobby/win', { lobbyId: state.lobbyId, id: state.id });
      window.parent.postMessage({
        type: 'pc-target-found',
        id: state.id,
        lobbyId: state.lobbyId,
        reason,
      }, '*');
      render();
    } catch (e) {
      state.finishedThisRound = false;
      persist();
    }
  }

  function checkTargetOnPage() {
    const target = normalizeText(state.targetTo);
    if (!target || state.finishedThisRound) return;
    const haystack = collectPinterestText();
    if (haystack.includes(target)) {
      finishRoundAutomatically('target-text');
    }
  }

  let autoCheckTimer = null;
  function scheduleAutoCheck() {
    if (autoCheckTimer) clearTimeout(autoCheckTimer);
    autoCheckTimer = setTimeout(checkTargetOnPage, 500);
  }

  const observer = new MutationObserver(scheduleAutoCheck);
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
  window.addEventListener('hashchange', () => {
    applyRoundConfigFromUrl();
    scheduleAutoCheck();
  });
  setInterval(checkTargetOnPage, 2000);
  scheduleAutoCheck();

  // ---------- UI ----------
  const css = `
  #pc-panel { position:fixed; bottom:16px; right:16px; z-index:999999; width:280px;
    background:#111; color:#fff; border-radius:14px; box-shadow:0 6px 24px rgba(0,0,0,.4);
    font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; overflow:hidden; }
  #pc-header { background:#E60023; padding:8px 12px; display:flex; justify-content:space-between; align-items:center; cursor:pointer; }
  #pc-header b { font-size:13px; }
  #pc-body { padding:10px 12px; max-height:70vh; overflow-y:auto; }
  #pc-panel input { width:100%; box-sizing:border-box; padding:6px 8px; margin:3px 0 6px;
    border-radius:8px; border:1px solid #444; background:#1c1c1c; color:#fff; }
  #pc-panel button { width:100%; padding:7px; margin:3px 0; border:none; border-radius:8px;
    background:#E60023; color:#fff; font-weight:600; cursor:pointer; }
  #pc-panel button.secondary { background:#333; }
  #pc-panel .row { display:flex; gap:6px; }
  #pc-panel .row > * { flex:1; }
  #pc-panel .muted { color:#999; font-size:11px; margin:2px 0 8px; }
  #pc-panel .target { background:#1c1c1c; border-radius:8px; padding:8px; margin:6px 0; text-align:center; }
  #pc-panel .target b { font-size:16px; }
  #pc-timer { font-size:22px; text-align:center; margin:4px 0; font-weight:700; }
  #pc-panel ul { list-style:none; padding:0; margin:4px 0; }
  #pc-panel li { display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px solid #222; }
  `;
  const styleEl = document.createElement('style');
  styleEl.textContent = css;
  document.documentElement.appendChild(styleEl);

  const panel = document.createElement('div');
  panel.id = 'pc-panel';
  panel.innerHTML = `
    <div id="pc-header"><b>🎯 Pinterest Chase</b><span id="pc-toggle">▾</span></div>
    <div id="pc-body"></div>
  `;
  document.documentElement.appendChild(panel);
  const body = panel.querySelector('#pc-body');
  panel.querySelector('#pc-header').addEventListener('click', () => {
    body.style.display = body.style.display === 'none' ? 'block' : 'none';
  });

  function render() {
    if (!state.id) {
      body.innerHTML = `
        <div class="muted">Адрес сервера (для друзей по локальной сети — IP хоста):</div>
        <input id="f-server" value="${state.serverUrl}">
        <div class="muted">Твой ник:</div>
        <input id="f-nick" placeholder="Ник" value="${state.nickname}">
        <button id="b-register">Подключиться</button>
      `;
      body.querySelector('#b-register').onclick = async () => {
        state.serverUrl = body.querySelector('#f-server').value.trim();
        const nick = body.querySelector('#f-nick').value.trim();
        try {
          const r = await api('POST', '/register', { nickname: nick });
          state.id = r.id; state.nickname = r.nickname; persist(); render();
        } catch (e) { alert('Не удалось подключиться к серверу: ' + e); }
      };
      return;
    }

    if (!state.lobbyId) {
      body.innerHTML = `
        <div class="muted">Ты: <b>${state.nickname}</b></div>
        <button id="b-create">Создать лобби</button>
        <div class="row">
          <input id="f-code" placeholder="Код лобби">
          <button id="b-join" class="secondary" style="flex:0 0 70px">Войти</button>
        </div>
      `;
      body.querySelector('#b-create').onclick = async () => {
        const r = await api('POST', '/lobby/create', { id: state.id, nickname: state.nickname });
        state.lobbyId = r.lobbyId; persist(); render();
      };
      body.querySelector('#b-join').onclick = async () => {
        const code = body.querySelector('#f-code').value.trim().toUpperCase();
        const r = await api('POST', '/lobby/join', { lobbyId: code, id: state.id, nickname: state.nickname });
        if (r.error) { alert(r.error); return; }
        state.lobbyId = r.lobbyId; persist(); render();
      };
      return;
    }

    body.innerHTML = `<div class="muted">Загрузка лобби...</div>`;
    refreshLobby();
  }

  async function refreshLobby() {
    const r = await api('GET', `/lobby/state?lobbyId=${state.lobbyId}`);
    if (r.error) {
      state.lobbyId = ''; persist(); render(); return;
    }
    const isHost = r.hostId === state.id;
    let html = `<div class="muted">Лобби: <b>${r.lobbyId}</b> ${isHost ? '(ты хост)' : ''}</div>`;

    if (r.status === 'lobby') {
      state.finishedThisRound = false; persist();
      html += `
        <div class="row">
          <input id="f-invite" placeholder="Ник друга">
          <button id="b-invite" class="secondary" style="flex:0 0 70px">Позвать</button>
        </div>
        <ul>${r.members.map(m => `<li><span>${m.nickname}</span></li>`).join('')}</ul>
        ${isHost ? `<button id="b-start">Старт</button>` : `<div class="muted">Ждём, когда хост нажмёт «Старт»</div>`}
        <button id="b-leave" class="secondary">Покинуть лобби</button>
      `;
      body.innerHTML = html;
      body.querySelector('#b-invite').onclick = async () => {
        const nick = body.querySelector('#f-invite').value.trim();
        const rr = await api('POST', '/lobby/invite', { targetNickname: nick, lobbyId: state.lobbyId, fromNickname: state.nickname });
        if (rr.error) alert(rr.error); else alert('Приглашение отправлено');
      };
      if (isHost) body.querySelector('#b-start').onclick = async () => {
        await api('POST', '/lobby/start', { lobbyId: state.lobbyId, hostId: state.id });
      };
      body.querySelector('#b-leave').onclick = leaveLobby;
    }

    if (r.status === 'running') {
      state.targetFrom = r.from;
      state.targetTo = r.to;
      // навигация на страницу поиска, если раунд новый
      if (state.lastStartTime !== r.startTime) {
        state.lastStartTime = r.startTime;
        state.finishedThisRound = false;
        persist();
        location.href = 'https://www.pinterest.com/search/pins/?q=' + encodeURIComponent(r.from);
        return;
      }
      const elapsed = Math.floor((Date.now() - r.startTime) / 1000);
      const me = r.members.find(m => m.nickname === state.nickname);
      const finished = r.members.filter(m => m.finished).sort((a, b) => a.time - b.time);
      const allFinished = r.members.length > 0 && finished.length === r.members.length;
      const winner = finished[0];
      const ordered = [...finished, ...r.members.filter(m => !m.finished)];
      const winnerBanner = allFinished
        ? `<div class="target">🏆 Победил <b>${winner.nickname}</b> — ${Math.floor(winner.time/1000)}с</div>`
        : (winner ? `<div class="muted">Пока быстрее всех: <b>${winner.nickname}</b> (${Math.floor(winner.time/1000)}с)</div>` : '');
      html += `
        <div class="target">Ищи от: <b>${r.from}</b><br>до: <b>${r.to}</b></div>
        <div id="pc-timer">${elapsed}с</div>
        ${(!me || !me.finished) ? `<button id="b-win">✅ Я нашёл!</button>` : `<div class="muted">Ты финишировал за ${Math.floor(me.time/1000)}с. Ждём остальных...</div>`}
        <button id="b-leave" class="secondary">Покинуть лобби</button>
        ${winnerBanner}
        <ul>${ordered.map((m,i) => `<li><span>${m.finished ? (i===0?'🏆 ':(i+1)+'. ') : ''}${m.nickname}</span><span>${m.finished ? Math.floor(m.time/1000)+'с' : '...'}</span></li>`).join('')}</ul>
        ${isHost ? `<button id="b-reset" class="secondary">Новый раунд</button>` : ''}
      `;
      body.innerHTML = html;
      persist();
      scheduleAutoCheck();
      if (body.querySelector('#b-win')) {
        body.querySelector('#b-win').onclick = async () => {
          await api('POST', '/lobby/win', { lobbyId: state.lobbyId, id: state.id });
        };
      }
      body.querySelector('#b-leave').onclick = leaveLobby;
      if (isHost && body.querySelector('#b-reset')) {
        body.querySelector('#b-reset').onclick = async () => {
          await api('POST', '/lobby/reset', { lobbyId: state.lobbyId, hostId: state.id });
        };
      }
    }
  }

  render();
  setInterval(() => { if (state.id && state.lobbyId) refreshLobby(); }, 1500);
})();
