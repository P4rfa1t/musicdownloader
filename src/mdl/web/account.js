/* ==========================================================================
   MusicDL — 账号面板
   自包含：自己注入 DOM、自己发请求，不依赖 app.js 的内部实现。

   对外只暴露 window.MDLAccount = { init, open, close, refresh }。
   用法：
     MDLAccount.init({ onPlaylistLoaded: function (title, tracks) { ... } });
   ========================================================================== */

(function () {
  'use strict';

  var DOM_ID = 'acctModal';
  var STORAGE_KEY = 'mdl_api_base'; // 与 app.js 一致，设置里改的地址这里也要认
  var POLL_MS = 1500;
  var DEFAULT_QR_TTL = 300;

  /* --------------------------- 平台元数据 --------------------------- */

  var PROVIDERS = [
    {
      id: 'netease',
      name: '网易云',
      mode: 'qr',
      placeholder: 'MUSIC_U=xxxx; __csrf=xxxx; os=pc',
      hint:
        '扫码登录最省事。也可以在 music.163.com 登录后按 F12 → Application → Cookies，' +
        '把整条 Cookie 复制过来（必须包含 MUSIC_U 这个字段，否则只是游客身份）。'
    },
    {
      id: 'qq',
      name: 'QQ音乐',
      mode: 'cookie',
      placeholder: 'uin=xxxx; qm_keyst=xxxx; qqmusic_key=xxxx',
      hint:
        'QQ 音乐的下载直链（vkey）对匿名请求一律返回空，必须带登录态。' +
        '在 y.qq.com 登录后复制整条 Cookie，重点是 qm_keyst / qqmusic_key 这两个字段。'
    },
    {
      id: 'kugou',
      name: '酷狗',
      mode: 'cookie',
      placeholder: 'KuGoo=xxxx; kg_mid=xxxx; token=xxxx',
      hint:
        '在 kugou.com 登录后复制整条 Cookie（KuGoo、kg_mid、token 是关键字段）。' 
    },
    {
      id: 'kuwo',
      name: '酷我',
      mode: 'cookie',
      placeholder: 'kw_token=xxxx; Hm_lvt_xxxx=xxxx',
      hint:
        '酷我匿名态大多只返回十几秒的试听片段，带登录 Cookie 才可能拿到完整文件。' +
        '在 kuwo.cn 登录后复制整条 Cookie（kw_token 是关键字段）。'
    }
  ];

  function providerMeta(id) {
    for (var i = 0; i < PROVIDERS.length; i++) {
      if (PROVIDERS[i].id === id) return PROVIDERS[i];
    }
    return { id: id, name: id, mode: 'cookie', placeholder: '', hint: '' };
  }

  /* ------------------------------ 状态 ------------------------------ */

  var state = {
    inited: false,
    open: false,
    opts: {},
    accounts: {},      // provider -> account 对象
    errors: {},        // provider -> 字符串
    playlists: {},     // provider -> {status:'idle'|'loading'|'ready'|'error', items:[], note:''}
    qr: null,          // {provider, key, timer, deadline}
    prevFocus: null,
    loadedOnce: false
  };

  /* ------------------------------ 工具 ------------------------------ */

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    var s = value === null || value === undefined ? '' : String(value);
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function normalizeBase(value) {
    var v = String(value || '').trim();
    if (!v) return '';
    if (!/^https?:\/\//i.test(v)) return '';
    return v.replace(/\/+$/, '');
  }

  // 与 app.js 同序：?api= → localStorage → window.MDL_API_BASE → 同源
  function resolveBase() {
    try {
      var m = /[?&]api=([^&#]*)/.exec(window.location.search || '');
      if (m && m[1]) {
        var fromQuery = normalizeBase(decodeURIComponent(m[1]));
        if (fromQuery) return fromQuery;
      }
    } catch (e) { /* 忽略：非法 URL 参数不能拖垮面板 */ }

    try {
      var stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored !== null) {
        var fromStore = normalizeBase(stored);
        if (fromStore) return fromStore;
      }
    } catch (e2) { /* 隐私模式下 localStorage 会抛，忽略 */ }

    try {
      var injected = normalizeBase(window.MDL_API_BASE || '');
      if (injected) return injected;
    } catch (e3) { /* 忽略 */ }

    return '';
  }

  function apiUrl(path) {
    var base = resolveBase();
    var p = String(path || '');
    if (!base) return p;
    return base + (p.charAt(0) === '/' ? p : '/' + p);
  }

  /* ------------------------------ 请求 ------------------------------ */

  // 统一出口：网络错误、HTTP 错误、{ok:false} 全部转成带人话的 Error
  function req(method, path, body) {
    var init = {
      method: method || 'GET',
      headers: { Accept: 'application/json' },
      cache: 'no-store'
    };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    var url = apiUrl(path);

    return fetch(url, init).then(
      function (res) {
        return res.text().then(function (text) {
          var data = null;
          if (text) {
            try {
              data = JSON.parse(text);
            } catch (e) {
              data = null;
            }
          }
          if (!res.ok) {
            var msg = '';
            if (data && data.detail) {
              msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
            } else if (data && data.error) {
              msg = String(data.error);
            } else if (text && text.length < 200) {
              msg = text.trim();
            }
            if (!msg) msg = '请求失败（HTTP ' + res.status + '）';
            var err = new Error(msg);
            err.status = res.status;
            throw err;
          }
          if (data && data.ok === false) {
            throw new Error(data.error ? String(data.error) : '接口返回失败');
          }
          return data;
        });
      },
      // fetch 本身失败（后端没起、被 CORS 拦、断网）
      function (err) {
        var e = new Error(
          '连不上后端：' + (err && err.message ? err.message : '网络错误')
        );
        e.network = true;
        throw e;
      }
    );
  }

  /* ------------------------------ 注入 DOM ------------------------------ */

  function buildCardHtml(meta) {
    var id = meta.id;

    // 四张卡结构必须同构：按钮行永远是 [扫码登录?] [粘贴 Cookie]，
    // textarea 默认收起，点「粘贴 Cookie」才展开（展开后按钮行只剩扫码登录）。
    var qrBtn = meta.mode === 'qr'
      ? '<button class="ghost-btn acct-login-btn" type="button" data-act="qr-start" data-provider="' +
        id + '">扫码登录</button>'
      : '';

    var qrBox = meta.mode === 'qr'
      ? '<div class="acct-qr" id="acct-qr-' + id + '" hidden>' +
          '<div class="acct-qr-frame">' +
            '<img id="acct-qr-img-' + id + '" alt="扫码登录二维码" />' +
          '</div>' +
          '<p class="acct-qr-msg" id="acct-qr-msg-' + id + '">等待扫码</p>' +
        '</div>'
      : '';

    return (
      '<section class="acct-plat" data-provider="' + id + '" id="acct-plat-' + id + '">' +
        '<header class="acct-plat-head">' +
          '<span class="badge" data-provider="' + id + '">' + esc(meta.name) + '</span>' +
          '<span class="acct-state" id="acct-state-' + id + '">检测中…</span>' +
          // 退出按钮由 renderAccount 按需插入/移除，未登录时 DOM 里根本不该有它
          '<div class="acct-plat-actions" id="acct-actions-' + id + '"></div>' +
        '</header>' +
        '<div class="acct-ident" id="acct-ident-' + id + '" hidden>' +
          '<span class="acct-avatar" id="acct-avatar-' + id + '" hidden></span>' +
          '<span class="acct-nick" id="acct-nick-' + id + '"></span>' +
          '<span class="acct-vip" id="acct-vip-' + id + '" hidden>VIP</span>' +
        '</div>' +
        '<div class="acct-err" id="acct-err-' + id + '" hidden></div>' +
        '<div class="acct-ops">' +
          qrBox +
          '<div class="acct-op-row" id="acct-btnrow-' + id + '">' +
            qrBtn +
            '<button class="ghost-btn acct-login-btn" type="button" id="acct-toggle-' + id + '" ' +
              'data-act="cookie-open" data-provider="' + id + '">粘贴 Cookie</button>' +
          '</div>' +
          '<div class="acct-cookie" id="acct-cookie-' + id + '" hidden>' +
            '<textarea class="acct-textarea" id="acct-input-' + id + '" spellcheck="false" ' +
              'placeholder="' + esc(meta.placeholder) + '"></textarea>' +
            '<div class="acct-op-row">' +
              '<button class="primary-btn acct-save-btn" type="button" data-act="cookie-save" ' +
                'data-provider="' + id + '">保存</button>' +
              '<button class="ghost-btn acct-cancel-btn" type="button" data-act="cookie-close" ' +
                'data-provider="' + id + '">取消</button>' +
            '</div>' +
            '<p class="acct-hint">' + esc(meta.hint) + '</p>' +
          '</div>' +
        '</div>' +
        '<div class="acct-pls" id="acct-pls-' + id + '" hidden></div>' +
      '</section>'
    );
  }

  // 退出按钮按需挂载：未登录时就把它从 DOM 里摘掉，
  // 免得键盘 tab 停到一个视觉隐藏、点了没用的按钮上。
  function ensureLogout(pid, show) {
    var slot = $('acct-actions-' + pid);
    if (!slot) return;
    var btn = $('acct-logout-' + pid);

    if (show) {
      if (!btn) {
        slot.insertAdjacentHTML(
          'beforeend',
          '<button class="ghost-btn danger acct-logout" type="button" id="acct-logout-' + pid + '" ' +
            'data-act="logout" data-provider="' + pid + '">退出</button>'
        );
      }
      return;
    }
    if (btn && btn.parentNode) btn.parentNode.removeChild(btn);
  }

  // 展开/收起 Cookie 输入区。展开时顶部按钮行只剩「扫码登录」，
  // 因为保存/取消就在 textarea 正下方。
  function setCookiePanel(pid, open) {
    var box = $('acct-cookie-' + pid);
    var toggle = $('acct-toggle-' + pid);
    var input = $('acct-input-' + pid);
    if (box) box.hidden = !open;
    if (toggle) toggle.hidden = open;
    if (open && input && input.focus) input.focus();
  }

  function injectDom() {
    if (document.getElementById(DOM_ID)) return;

    var cards = '';
    for (var i = 0; i < PROVIDERS.length; i++) {
      cards += buildCardHtml(PROVIDERS[i]);
    }

    var html =
      '<div class="acct-modal" id="' + DOM_ID + '" hidden role="dialog" aria-modal="true" aria-labelledby="acctTitle">' +
        '<div class="acct-card" role="document">' +
          '<div class="acct-head">' +
            '<h2 id="acctTitle">账号</h2>' +
            '<div class="acct-head-actions">' +
              '<button class="ghost-btn" type="button" id="acctRefresh" title="重新拉取登录状态">刷新</button>' +
              '<button class="icon-btn acct-close" type="button" id="acctClose" aria-label="关闭">×</button>' +
            '</div>' +
          '</div>' +
          '<div class="acct-intro">' +
            '登录后可以使用更高码率、账号歌单和需要权限的曲目。Cookie 只存在本机后端，不会上传到别处。' +
          '</div>' +
          '<div class="acct-body" id="acctBody">' + cards + '</div>' +
        '</div>' +
      '</div>';

    var host = document.body;
    if (host.insertAdjacentHTML) {
      host.insertAdjacentHTML('beforeend', html);
    } else {
      var holder = document.createElement('div');
      holder.innerHTML = html;
      while (holder.firstChild) host.appendChild(holder.firstChild);
    }
  }

  /* ------------------------------ 渲染 ------------------------------ */

  function setState(pid, text, kind) {
    var node = $('acct-state-' + pid);
    if (!node) return;
    node.textContent = text;
    node.className = 'acct-state' + (kind ? ' is-' + kind : '');
  }

  function showError(pid, message) {
    state.errors[pid] = message;
    var node = $('acct-err-' + pid);
    if (!node) return;
    node.textContent = message;
    node.hidden = false;
  }

  function clearError(pid) {
    delete state.errors[pid];
    var node = $('acct-err-' + pid);
    if (!node) return;
    node.textContent = '';
    node.hidden = true;
  }

  function renderAccount(account) {
    if (!account || !account.provider) return;
    var pid = account.provider;
    state.accounts[pid] = account;

    var ident = $('acct-ident-' + pid);
    var nick = $('acct-nick-' + pid);
    var avatar = $('acct-avatar-' + pid);
    var vip = $('acct-vip-' + pid);

    if (account.logged_in) {
      var qrP = $('acct-qr-' + pid);
      if (qrP) qrP.hidden = true; // 登录成功提示已经写在状态行，二维码没必要留着
      setState(pid, '已登录', 'ok');
      if (ident) ident.hidden = false;
      if (nick) nick.textContent = account.nickname || '（未返回昵称）';
      if (avatar) {
        if (account.avatar) {
          avatar.style.backgroundImage = 'url("' + String(account.avatar).replace(/"/g, '%22') + '")';
          avatar.hidden = false;
        } else {
          avatar.style.backgroundImage = '';
          avatar.hidden = true;
        }
      }
      if (vip) vip.hidden = !account.vip;
      ensureLogout(pid, true);
    } else if (account.has_cookie) {
      // 存档里有凭据，但没验证出身份：留着「退出」才能把它清掉
      setState(pid, '已保存 Cookie（未验证身份）', 'warn');
      if (ident) ident.hidden = true;
      if (avatar) avatar.hidden = true;
      ensureLogout(pid, true);
    } else {
      setState(pid, '未登录', 'idle');
      if (ident) ident.hidden = true;
      if (avatar) avatar.hidden = true;
      ensureLogout(pid, false);
    }

    // 歌单区：没凭据就整块不渲染（连占位都不要）；
    // 有凭据才按平台能力决定是「不支持」说明还是真去拉。
    var pls = $('acct-pls-' + pid);
    if (pls) {
      if (!account.logged_in && !account.has_cookie) {
        pls.hidden = true;
        pls.innerHTML = '';
        state.playlists[pid] = { status: 'idle', items: [], note: '' };
      } else if (account.playlists_supported === false) {
        pls.hidden = false;
        // 后端的 note 原样显示，不再自己包一层措辞
        pls.innerHTML =
          '<div class="acct-note">' + esc(account.note || '该平台暂不支持读取账号歌单') + '</div>';
        state.playlists[pid] = { status: 'ready', items: [], note: account.note || '' };
      }
    }

    // 有 Cookie 就先试着拉：失败也要显性，比悄悄空着强
    if (account.playlists_supported !== false && (account.logged_in || account.has_cookie)) {
      loadPlaylists(pid);
    }
  }

  function renderAll(accounts) {
    var byId = {};
    for (var i = 0; i < accounts.length; i++) {
      if (accounts[i] && accounts[i].provider) byId[accounts[i].provider] = accounts[i];
    }
    for (var j = 0; j < PROVIDERS.length; j++) {
      var pid = PROVIDERS[j].id;
      if (byId[pid]) {
        renderAccount(byId[pid]);
      } else {
        // 后端没返回这个平台：不要留空白，明确说出来
        setState(pid, '后端未返回该平台', 'warn');
        showError(pid, '后端 /api/accounts 的响应里没有 ' + pid);
      }
    }
  }

  /* ------------------------------ 歌单 ------------------------------ */

  function playlistState(pid) {
    if (!state.playlists[pid]) {
      state.playlists[pid] = { status: 'idle', items: [], note: '' };
    }
    return state.playlists[pid];
  }

  function renderPlaylists(pid, pl) {
    var box = $('acct-pls-' + pid);
    if (!box) return;

    if (pl.status === 'loading') {
      box.hidden = false;
      box.innerHTML = '<div class="acct-note">正在读取歌单…</div>';
      return;
    }
    if (pl.status === 'error') {
      box.hidden = false;
      box.innerHTML = '<div class="acct-note is-err">歌单读取失败：' + esc(pl.error || '未知错误') + '</div>';
      return;
    }
    if (pl.status !== 'ready') {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }

    box.hidden = false;
    if (pl.note && !pl.items.length) {
      box.innerHTML = '<div class="acct-note">' + esc(pl.note) + '</div>';
      return;
    }
    if (!pl.items.length) {
      box.innerHTML = '<div class="acct-note">这个账号下没有可选歌单</div>';
      return;
    }

    var html = '<div class="acct-pls-title">歌单 <span>' + pl.items.length + '</span></div>';
    html += '<div class="acct-pls-list">';
    for (var i = 0; i < pl.items.length; i++) {
      var it = pl.items[i];
      var cover = it.cover
        ? '<span class="acct-pl-cover" style="background-image:url(&quot;' +
          esc(it.cover) + '&quot;)"></span>'
        : '<span class="acct-pl-cover is-empty"></span>';
      html +=
        '<button class="acct-pl" type="button" data-act="open-playlist" data-provider="' + esc(pid) +
          '" data-playlist="' + esc(it.id) + '">' +
          cover +
          '<span class="acct-pl-main">' +
            '<span class="acct-pl-name">' + esc(it.name || '未命名歌单') + '</span>' +
            '<span class="acct-pl-sub">' +
              '<span>' + esc(it.track_count === null || it.track_count === undefined ? '—' : it.track_count) + ' 首</span>' +
              (it.creator ? '<span class="acct-sep">·</span><span>' + esc(it.creator) + '</span>' : '') +
            '</span>' +
          '</span>' +
        '</button>';
    }
    html += '</div>';
    box.innerHTML = html;
  }

  function loadPlaylists(pid) {
    var pl = playlistState(pid);
    if (pl.status === 'loading') return;
    pl.status = 'loading';
    renderPlaylists(pid, pl);

    req('GET', '/api/accounts/' + encodeURIComponent(pid) + '/playlists').then(
      function (data) {
        var box = state.playlists[pid];
        box.status = 'ready';
        box.items = (data && data.playlists) || [];
        box.note = (data && data.note) || '';
        renderPlaylists(pid, box);
      },
      function (err) {
        var box = state.playlists[pid];
        box.status = 'error';
        box.error = err.message;
        renderPlaylists(pid, box);
      }
    );
  }

  /* --------------------------- 自包含的曲目预览 --------------------------- */

  function renderTracksFallback(pid, title, tracks) {
    var box = $('acct-pls-' + pid);
    if (!box) return;
    var html = '<div class="acct-pls-title">' + esc(title || '歌单') +
      ' <span>' + tracks.length + ' 首</span></div>';
    html += '<div class="acct-track-list">';
    for (var i = 0; i < tracks.length; i++) {
      var t = tracks[i] || {};
      html +=
        '<div class="acct-track">' +
          '<span class="acct-track-title">' + esc(t.title || '(无标题)') + '</span>' +
          '<span class="acct-track-sub">' + esc(t.artist_text || '') + '</span>' +
          '<span class="acct-track-dur">' + esc(t.duration_text || '') + '</span>' +
        '</div>';
    }
    html += '</div>';
    box.hidden = false;
    box.innerHTML = html;
  }

  function openPlaylist(pid, playlistId, title) {
    var box = $('acct-pls-' + pid);
    if (box) {
      box.hidden = false;
      box.insertAdjacentHTML('afterbegin', '<div class="acct-note" id="acct-pl-loading-' + esc(pid) + '">正在读取曲目…</div>');
    }

    req('GET', '/api/playlists/' + encodeURIComponent(pid) + '/' + encodeURIComponent(playlistId)).then(
      function (data) {
        var loading = $('acct-pl-loading-' + pid);
        if (loading && loading.parentNode) loading.parentNode.removeChild(loading);

        var tracks = (data && data.tracks) || [];
        var name = (data && data.title) || title || '歌单';

        if (!tracks.length) {
          showError(pid, '《' + name + '》里没有曲目');
          return;
        }
        clearError(pid);

        if (typeof state.opts.onPlaylistLoaded === 'function') {
          try {
            state.opts.onPlaylistLoaded(name, tracks);
          } catch (e) {
            showError(pid, '主界面渲染回调出错：' + (e && e.message ? e.message : e));
          }
        } else {
          // 没有主界面回调时，自己渲染一份预览，别让点击看起来没反应
          renderTracksFallback(pid, name, tracks);
        }
      },
      function (err) {
        var loading = $('acct-pl-loading-' + pid);
        if (loading && loading.parentNode) loading.parentNode.removeChild(loading);
        showError(pid, '读取歌单曲目失败：' + err.message);
      }
    );
  }

  /* ------------------------------ 扫码 ------------------------------ */

  function stopQr() {
    if (!state.qr) return;
    if (state.qr.timer) window.clearTimeout(state.qr.timer);
    state.qr = null;
  }

  function qrMsg(pid, text, kind) {
    var node = $('acct-qr-msg-' + pid);
    if (!node) return;
    node.textContent = text;
    node.className = 'acct-qr-msg' + (kind ? ' is-' + kind : '');
  }

  var QR_TEXT = {
    wait: '等待扫码',
    scanned: '已扫码，请在手机上确认',
    ok: '登录成功',
    expired: '二维码已过期，点「扫码登录」重新获取',
    error: '登录失败'
  };

  function startQr(pid) {
    clearError(pid);
    stopQr();

    var boxEl = $('acct-qr-' + pid);
    var img = $('acct-qr-img-' + pid);
    if (boxEl) boxEl.hidden = false;
    if (img) {
      img.removeAttribute('src');
      img.alt = '正在获取二维码';
    }
    qrMsg(pid, '正在获取二维码…');

    req('POST', '/api/accounts/' + encodeURIComponent(pid) + '/qr').then(
      function (data) {
        var key = data && data.key;
        if (!key) {
          qrMsg(pid, '后端没有返回 key', 'error');
          showError(pid, '扫码接口没有返回 key，无法轮询状态');
          return;
        }
        if (img && data.qr_image) {
          img.src = data.qr_image;
          img.alt = '扫码登录二维码';
        }
        qrMsg(pid, '等待扫码');

        var ttl = Number(data.expires_in) > 0 ? Number(data.expires_in) : DEFAULT_QR_TTL;
        state.qr = {
          provider: pid,
          key: key,
          timer: null,
          deadline: Date.now() + ttl * 1000
        };
        pollQr(pid, key);
      },
      function (err) {
        qrMsg(pid, '获取二维码失败：' + err.message, 'error');
        showError(pid, '扫码登录：' + err.message);
      }
    );
  }

  function pollQr(pid, key) {
    if (!state.qr || state.qr.key !== key) return;

    if (Date.now() > state.qr.deadline) {
      qrMsg(pid, QR_TEXT.expired, 'expired');
      stopQr();
      return;
    }

    req('GET', '/api/accounts/' + encodeURIComponent(pid) + '/qr/' + encodeURIComponent(key)).then(
      function (data) {
        if (!state.qr || state.qr.key !== key) return;
        var status = (data && data.status) || 'wait';
        var message = (data && data.message) || QR_TEXT[status] || '';

        if (status === 'ok') {
          stopQr();
          if (data.account) {
            renderAccount(data.account);
          } else {
            // 后端只说成功、没给账号信息：状态行也要有反馈，别静默
            setState(pid, message || '已登录', 'ok');
            refresh();
          }
          var b = $('acct-qr-' + pid);
          if (b) b.hidden = true;
          return;
        }
        if (status === 'expired' || status === 'error') {
          qrMsg(pid, message || QR_TEXT[status], status === 'error' ? 'error' : 'expired');
          if (status === 'error') showError(pid, '扫码登录：' + (message || '失败'));
          stopQr();
          return;
        }

        qrMsg(pid, message || QR_TEXT.wait, status === 'scanned' ? 'scanned' : '');
        state.qr.timer = window.setTimeout(function () { pollQr(pid, key); }, POLL_MS);
      },
      function (err) {
        if (!state.qr || state.qr.key !== key) return;
        qrMsg(pid, '轮询失败：' + err.message, 'error');
        showError(pid, '扫码状态查询失败：' + err.message);
        // 网络抖动重试一次再放弃
        state.qr.timer = window.setTimeout(function () { pollQr(pid, key); }, POLL_MS * 2);
      }
    );
  }

  /* ------------------------------ 操作 ------------------------------ */

  function saveCookie(pid) {
    var input = $('acct-input-' + pid);
    var value = input ? String(input.value || '').trim() : '';
    var btn = document.querySelector('[data-act="cookie-save"][data-provider="' + pid + '"]');

    if (!value) {
      showError(pid, 'Cookie 是空的，先把浏览器里的整条 Cookie 粘进来');
      if (input) input.focus();
      return;
    }

    clearError(pid);
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    setState(pid, '正在验证…', 'busy');

    req('POST', '/api/accounts/' + encodeURIComponent(pid), { cookie: value }).then(
      function (data) {
        if (btn) { btn.disabled = false; btn.textContent = '保存'; }
        if (input) input.value = '';
        setCookiePanel(pid, false); // 提交完就收起，别让输入区一直占着
        var acct = (data && data.account) || null;
        if (acct) {
          renderAccount(acct);
          if (!acct.logged_in && acct.has_cookie) {
            showError(pid, '已保存，但这个 Cookie 没验证出身份（可能已过期）');
          }
        } else {
          setState(pid, '已保存 Cookie（未验证身份）', 'warn');
        }
      },
      function (err) {
        if (btn) { btn.disabled = false; btn.textContent = '保存'; }
        showError(pid, '保存失败：' + err.message);
        setState(pid, '保存失败', 'error');
      }
    );
  }

  function logout(pid) {
    var btn = $('acct-logout-' + pid);
    if (btn) { btn.disabled = true; btn.textContent = '退出中…'; }
    clearError(pid);

    req('DELETE', '/api/accounts/' + encodeURIComponent(pid)).then(
      function () {
        if (btn) { btn.disabled = false; btn.textContent = '退出'; }
        state.playlists[pid] = { status: 'idle', items: [], note: '' };
        var pls = $('acct-pls-' + pid);
        if (pls) { pls.hidden = true; pls.innerHTML = ''; }
        var qrBox = $('acct-qr-' + pid);
        if (qrBox) qrBox.hidden = true;
        stopQr();
        state.accounts[pid] = {
          provider: pid,
          name: providerMeta(pid).name,
          has_cookie: false,
          logged_in: false,
          nickname: null,
          avatar: null,
          vip: false,
          login_mode: providerMeta(pid).mode,
          playlists_supported: true,
          note: null
        };
        renderAccount(state.accounts[pid]);
      },
      function (err) {
        if (btn) { btn.disabled = false; btn.textContent = '退出'; }
        showError(pid, '退出失败：' + err.message);
      }
    );
  }

  /* ------------------------------ 生命周期 ------------------------------ */

  function refresh() {
    state.loadedOnce = false;
    for (var i = 0; i < PROVIDERS.length; i++) {
      setState(PROVIDERS[i].id, '检测中…', 'busy');
      clearError(PROVIDERS[i].id);
    }

    return req('GET', '/api/accounts').then(
      function (data) {
        state.loadedOnce = true;
        var list = (data && data.accounts) || [];
        if (!list.length) {
          for (var j = 0; j < PROVIDERS.length; j++) {
            setState(PROVIDERS[j].id, '后端没有账号数据', 'warn');
            showError(PROVIDERS[j].id, '/api/accounts 返回了空的 accounts 数组');
          }
          return;
        }
        renderAll(list);
      },
      function (err) {
        var hint = err.network
          ? '账号功能需要后端：' + err.message
          : '读取账号状态失败：' + err.message;
        for (var k = 0; k < PROVIDERS.length; k++) {
          setState(PROVIDERS[k].id, err.network ? '后端未连接' : '读取失败', 'error');
          showError(PROVIDERS[k].id, hint);
        }
      }
    );
  }

  function open() {
    injectDom();
    var modal = $(DOM_ID);
    if (!modal) return;
    state.open = true;
    state.prevFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add('acct-open');
    var closeBtn = $('acctClose');
    if (closeBtn && closeBtn.focus) closeBtn.focus();
    refresh();
  }

  function close() {
    var modal = $(DOM_ID);
    state.open = false;
    stopQr();
    if (modal) modal.hidden = true;
    document.body.classList.remove('acct-open');
    if (state.prevFocus && state.prevFocus.focus) {
      try { state.prevFocus.focus(); } catch (e) { /* 元素可能已移除 */ }
    }
    state.prevFocus = null;
  }

  /* ------------------------------ 事件 ------------------------------ */

  function onClick(event) {
    var target = event.target;

    // 点遮罩关闭：只有点在弹层本身（不是卡片内部）才算
    if (target && target.id === DOM_ID) {
      close();
      return;
    }

    var btn = target && target.closest ? target.closest('[data-act]') : null;
    if (!btn) return;

    var act = btn.getAttribute('data-act');
    var pid = btn.getAttribute('data-provider');

    if (act === 'qr-start') {
      startQr(pid);
    } else if (act === 'cookie-open') {
      setCookiePanel(pid, true);
    } else if (act === 'cookie-close') {
      setCookiePanel(pid, false);
    } else if (act === 'cookie-save') {
      saveCookie(pid);
    } else if (act === 'logout') {
      logout(pid);
    } else if (act === 'open-playlist') {
      openPlaylist(pid, btn.getAttribute('data-playlist'), '');
    }
  }

  function onKeydown(event) {
    if (!state.open) return;
    if (event.key === 'Escape' || event.keyCode === 27) {
      event.preventDefault();
      close();
    }
  }

  function init(opts) {
    state.opts = opts || {};
    injectDom();

    if (state.inited) return;
    state.inited = true;

    var modal = $(DOM_ID);
    if (modal) modal.addEventListener('click', onClick);

    var closeBtn = $('acctClose');
    if (closeBtn) closeBtn.addEventListener('click', close);

    var refreshBtn = $('acctRefresh');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', function () {
        refresh();
      });
    }

    // 顶栏按钮由 index.html 提供，可能还没加，别炸
    var trigger = document.getElementById('accountBtn');
    if (trigger) {
      trigger.addEventListener('click', open);
    }

    document.addEventListener('keydown', onKeydown);

    // 主界面切换后端地址后，账号面板要跟着换
    window.addEventListener('storage', function (e) {
      if (e && e.key === STORAGE_KEY && state.open) refresh();
    });
  }

  window.MDLAccount = {
    init: init,
    open: open,
    close: close,
    refresh: refresh
  };
})();