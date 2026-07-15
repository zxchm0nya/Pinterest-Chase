(function () {
  'use strict';

  const state = {
    serverUrl: localStorage.getItem('pc_serverUrl') || location.origin,
    id: localStorage.getItem('pc_id') || '',
    nickname: localStorage.getItem('pc_nickname') || '',
    lobbyId: localStorage.getItem('pc_lobbyId') || '',
    lastStartTime: Number(localStorage.getItem('pc_lastStartTime') || 0),
  };
  
  function persist() {
    localStorage.setItem('pc_serverUrl', state.serverUrl);
    localStorage.setItem('pc_id', state.id);
    localStorage.setItem('pc_nickname', state.nickname);
    localStorage.setItem('pc_lobbyId', state.lobbyId);
    localStorage.setItem('pc_lastStartTime', state.lastStartTime);
  }

  async function api(method, pathName, body) {
    const url = state.serverUrl.replace(/\/$/, '') + pathName;
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    return r.json();
  }

  const app = document.getElementById('app');
  let pollTimer = null;

  function render() {
    if (!state.id) {
      app.innerHTML = `
        <div class="card">
          <label>Адрес сервера</label>
          <input id="f-server" value="${state.serverUrl}">
          <label>Твой ник</label>
          <input id="f-nick" placeholder="Ник" value="${state.nickname}">
          <button id="b-register">Подключиться</button>
        </div>
      `;
      document.getElementById('b-register').onclick = async () => {
        state.serverUrl = document.getElementById('f-server').value.trim();
        const nick = document.getElementById('f-nick').value.trim();
        try {
          const r = await api('POST', '/register', { nickname: nick });
          state.id = r.id; state.nickname = r.nickname; persist(); render();
        } catch (e) { alert('Не удалось подключиться: ' + e); }
      };
      return;
    }

    if (!state.lobbyId) {
      app.innerHTML = `
        <div class="card">
          <div class="muted">Ты подключён как <b>${state.nickname}</b></div>
          <button id="b-create">Создать лобби</button>
          <div class="row">
            <input id="f-code" placeholder="Код лобби">
            <button id="b-join" class="secondary">Войти</button>
          </div>
        </div>
      `;
      document.getElementById('b-create').onclick = async () => {
        const r = await api('POST', '/lobby/create', { id: state.id, nickname: state.nickname });
        state.lobbyId = r.lobbyId; persist(); startPolling();
      };
      document.getElementById('b-join').onclick = async () => {
        const code = document.getElementById('f-code').value.trim().toUpperCase();
        const r = await api('POST', '/lobby/join', { lobbyId: code, id: state.id, nickname: state.nickname });
        if (r.error) { alert(r.error); return; }
        state.lobbyId = r.lobbyId; persist(); startPolling();
      };
      return;
    }

    startPolling();
  }

  function startPolling() {
    render_lobby();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(render_lobby, 1500);
  }

  async function render_lobby() {
    if (!state.lobbyId) return;
    const r = await api('GET', `/lobby/state?lobbyId=${state.lobbyId}`);
    if (r.error) {
      clearInterval(pollTimer);
      state.lobbyId = ''; persist(); app.innerHTML = ''; render();
      return;
    }
    const isHost = r.hostId === state.id;

    if (r.status === 'lobby') {
      app.innerHTML = `
        <div class="card">
          <div class="muted">Лобби: <span class="badge">${r.lobbyId}</span></div>
          <ul>${r.members.map(m => `<li>${m.nickname}</li>`).join('')}</ul>
          ${isHost ? `<button id="b-start">Старт раунда</button>` : ''}
          <button id="b-leave" class="secondary">Покинуть</button>
        </div>
      `;
      if (isHost) document.getElementById('b-start').onclick = async () => {
        await api('POST', '/lobby/start', { lobbyId: state.lobbyId, hostId: state.id });
      };
      document.getElementById('b-leave').onclick = () => {
        clearInterval(pollTimer); state.lobbyId = ''; persist(); render();
      };
      return;
    }

    if (r.status === 'running') {
      const elapsed = Math.floor((Date.now() - r.startTime) / 1000);
      const me = r.members.find(m => m.nickname === state.nickname);
      const searchUrl = 'https://ru.pinterest.com/search/pins/?q=' + encodeURIComponent(r.to);

      app.innerHTML = `
        <div class="card">
          <h3>Цель: <b>${r.to}</b></h3>
          <p>Время: ${elapsed}с</p>
          <button id="b-open" style="background:#e60023; color:white; padding:10px; border:none; border-radius:5px; cursor:pointer;">
            📌 Открыть Pinterest в новой вкладке
          </button>
          ${(!me || !me.finished) ? `<button id="b-win">Я нашёл</button>` : `<p>Ты финишировал!</p>`}
        </div>
      `;

      document.getElementById('b-open').onclick = () => window.open(searchUrl, '_blank');
      if (document.getElementById('b-win')) {
        document.getElementById('b-win').onclick = async () => {
          await api('POST', '/lobby/win', { lobbyId: state.lobbyId, id: state.id });
        };
      }
    }
  }

  render();
})();