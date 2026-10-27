/* ==========================================================================
 * MusicDL — 播放器
 * 原生 JS，无依赖。挂一个底部播放条，队列就是当前结果列表。
 *
 * 依赖的接口（同源）：
 *   POST /api/play/prepare   {provider,id,quality,title,artists,album,cover_url,duration_ms,extra}
 *        → {token,url,quality,bitrate,size,ext,...}
 *   GET  /api/play/{token}   支持 Range 的同源音频流
 *
 * 对外接口：
 *   MDLPlayer.play(track, queue)   播放并替换队列（queue 为 track 数组）
 *   MDLPlayer.setQueue(list)       只换队列
 *   MDLPlayer.toggle() / next() / prev() / isPlaying()
 *   MDLPlayer.hooks = { apiUrl(n), toast(msg,kind), onDownload(track) }
 * ========================================================================== */

(function () {
  'use strict';

  var STORAGE_VOL = 'mdl_player_volume';
  var DEFAULT_QUALITY = '320';
  var SEEK_STEP = 5;

  var ICONS = {
    prev: '<path d="M18 5v14M6 5l9 7-9 7V5z"/>',
    next: '<path d="M6 5v14M18 5l-9 7 9 7V5z"/>',
    play: '<path d="M8 5.5v13l11-6.5-11-6.5z"/>',
    pause: '<path d="M9 5v14M15 5v14"/>',
    download: '<path d="M12 4v11m0 0l-4-4m4 4l4-4M5 20h14"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>'
  };

  var audio = null;
  var els = {};
  var queue = [];
  var index = -1;
  var current = null;
  var busy = false;
  var seq = 0;

  var hooks = {
    apiUrl: function (p) { return p; },
    toast: function (msg, kind) { console.warn('[player]', kind || 'info', msg); },
    onDownload: null
  };

  /* ------------------------------ 工具 ------------------------------ */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(name, cls) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    if (cls) svg.setAttribute('class', cls);
    svg.innerHTML = ICONS[name] || '';
    return svg;
  }

  function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) return '00:00';
    var total = Math.floor(sec);
    var m = Math.floor(total / 60);
    var s = total % 60;
    if (m >= 60) {
      var h = Math.floor(m / 60);
      return h + ':' + pad(m % 60) + ':' + pad(s);
    }
    return pad(m) + ':' + pad(s);
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function keyOf(track) {
    if (!track) return '';
    return String(track.provider || '') + ':' + String(track.id || '');
  }

  function artistText(track) {
    var list = (track && track.artists) || [];
    var text = list.filter(Boolean).join(' / ');
    return text || '未知歌手';
  }

  function providerLabel(id) {
    var map = { netease: '网易云', qq: 'QQ音乐', kugou: '酷狗', kuwo: '酷我' };
    return map[id] || id || '';
  }

  /* ------------------------------ DOM ------------------------------ */

  function build() {
    if (els.root) return;
    var root = el('div', 'player');
    root.id = 'mdlPlayer';
    root.hidden = true;

    // 左：封面 + 标题
    var now = el('div', 'player-now');
    var cover = el('div', 'player-cover');
    cover.appendChild(icon('play', 'player-cover-icon'));
    var meta = el('div', 'player-meta');
    var title = el('div', 'player-title', '');
    var artist = el('div', 'player-artist', '');
    meta.appendChild(title);
    meta.appendChild(artist);
    now.appendChild(cover);
    now.appendChild(meta);

    // 中：控制
    var controls = el('div', 'player-controls');
    var prevBtn = button('prev', '上一首');
    var playBtn = button('play', '播放 / 暂停', 'player-play');
    var nextBtn = button('next', '下一首');
    controls.appendChild(prevBtn);
    controls.appendChild(playBtn);
    controls.appendChild(nextBtn);

    // 右：进度
    var progress = el('div', 'player-progress');
    var cur = el('span', 'player-time', '00:00');
    var seek = document.createElement('input');
    seek.type = 'range';
    seek.className = 'player-seek';
    seek.min = '0';
    seek.max = '1000';
    seek.value = '0';
    seek.step = '1';
    seek.setAttribute('aria-label', '播放进度');
    var dur = el('span', 'player-time', '00:00');
    progress.appendChild(cur);
    progress.appendChild(seek);
    progress.appendChild(dur);

    // 尾：下载 + 音量
    var extra = el('div', 'player-extra');
    var dlBtn = button('download', '下载这首', 'player-dl');
    var vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'player-vol';
    vol.min = '0';
    vol.max = '100';
    vol.step = '1';
    vol.value = String(readVolume());
    vol.setAttribute('aria-label', '音量');
    extra.appendChild(dlBtn);
    extra.appendChild(icon('download', 'player-vol-icon'));
    extra.appendChild(vol);

    root.appendChild(now);
    root.appendChild(controls);
    root.appendChild(progress);
    root.appendChild(extra);
    document.body.appendChild(root);

    els = {
      root: root,
      cover: cover,
      title: title,
      artist: artist,
      play: playBtn,
      prev: prevBtn,
      next: nextBtn,
      seek: seek,
      cur: cur,
      dur: dur,
      dl: dlBtn,
      vol: vol
    };

    bind();
  }

  function button(name, label, extraCls) {
    var btn = el('button', 'player-btn' + (extraCls ? ' ' + extraCls : ''));
    btn.type = 'button';
    btn.setAttribute('aria-label', label);
    btn.title = label;
    btn.appendChild(icon(name));
    return btn;
  }

  function bind() {
    els.play.addEventListener('click', function () { toggle(); });
    els.prev.addEventListener('click', function () { prev(); });
    els.next.addEventListener('click', function () { next(); });

    els.seek.addEventListener('input', function () {
      if (!audio || !isFinite(audio.duration) || audio.duration <= 0) return;
      var ratio = Number(els.seek.value) / 1000;
      audio.currentTime = ratio * audio.duration;
      renderTime();
    });
    // 拖动时不要被 timeupdate 抢回旧位置
    els.seek.addEventListener('pointerdown', function () { els.seek.dataset.drag = '1'; });
    els.seek.addEventListener('pointerup', function () { delete els.seek.dataset.drag; });
    els.seek.addEventListener('change', function () { delete els.seek.dataset.drag; });

    els.vol.addEventListener('input', function () {
      var v = Number(els.vol.value);
      if (audio) audio.volume = Math.max(0, Math.min(1, v / 100));
      try { localStorage.setItem(STORAGE_VOL, String(v)); } catch (e) { /* 隐身模式 */ }
    });

    els.dl.addEventListener('click', function () {
      if (!current) return;
      if (typeof hooks.onDownload === 'function') hooks.onDownload(current);
      else hooks.toast('当前版本不支持从这里下载', 'warn');
    });

    document.addEventListener('keydown', function (e) {
      if (!els.root || els.root.hidden) return;
      var t = e.target || {};
      var tag = (t.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable) return;
      if (e.key === ' ' || e.code === 'Space') {
        e.preventDefault();
        toggle();
      } else if (e.key === 'ArrowLeft') {
        seekBy(-SEEK_STEP);
      } else if (e.key === 'ArrowRight') {
        seekBy(SEEK_STEP);
      }
    });
  }

  function readVolume() {
    try {
      var raw = localStorage.getItem(STORAGE_VOL);
      if (raw === null) return 100;
      var v = Number(raw);
      return isFinite(v) ? Math.max(0, Math.min(100, v)) : 100;
    } catch (e) {
      return 100;
    }
  }

  function ensureAudio() {
    if (audio) return audio;
    audio = new Audio();
    audio.preload = 'auto';
    audio.volume = readVolume() / 100;

    audio.addEventListener('loadedmetadata', function () {
      if (els.dur) els.dur.textContent = fmtTime(audio.duration);
      renderTime();
    });
    audio.addEventListener('timeupdate', function () { renderTime(); });
    audio.addEventListener('play', function () { setPlayIcon(true); markRows(); });
    audio.addEventListener('pause', function () { setPlayIcon(false); });
    audio.addEventListener('waiting', function () { els.root.classList.add('is-buffering'); });
    audio.addEventListener('playing', function () { els.root.classList.remove('is-buffering'); });
    audio.addEventListener('ended', function () { next(); });
    audio.addEventListener('error', function () {
      // 令牌过期 / 上游 502 都会走到这里；提示后由用户重试
      setBusy(false);
      els.root.classList.remove('is-buffering');
      var code = audio.error ? audio.error.code : 0;
      if (code === 4 || code === 2) {
        hooks.toast('这首播不了（可能是版权或试听限制），换一首试试', 'error');
      }
    });
    return audio;
  }

  function setPlayIcon(playing) {
    if (!els.play) return;
    els.play.innerHTML = '';
    els.play.appendChild(icon(playing ? 'pause' : 'play'));
    els.play.classList.toggle('is-playing', !!playing);
  }

  function setBusy(on) {
    busy = !!on;
    if (!els.play) return;
    els.play.disabled = !!on;
    els.play.classList.toggle('is-loading', !!on);
    if (on) {
      els.play.innerHTML = '';
      els.play.appendChild(el('span', 'player-spinner'));
    } else {
      setPlayIcon(audio ? !audio.paused : false);
    }
  }

  function renderTime() {
    if (!audio || !els.seek) return;
    var d = audio.duration;
    var c = audio.currentTime;
    els.cur.textContent = fmtTime(c);
    if (isFinite(d) && d > 0) {
      els.dur.textContent = fmtTime(d);
      if (!els.seek.dataset.drag) els.seek.value = String(Math.round((c / d) * 1000));
    }
  }

  function paint(track) {
    if (!els.root) return;
    els.root.hidden = false;
    // 给 body 补下边距，否则最后一行结果会被播放条压住
    document.body.classList.add('has-player');
    els.title.textContent = track.title || '未命名曲目';
    els.artist.textContent = artistText(track) +
      (track.provider ? ' · ' + providerLabel(track.provider) : '');
    els.title.title = els.title.textContent;

    els.cover.innerHTML = '';
    if (track.cover_url) {
      var img = document.createElement('img');
      img.alt = '';
      img.referrerPolicy = 'no-referrer';
      img.decoding = 'async';
      img.addEventListener('error', function () {
        if (img.parentNode) img.parentNode.removeChild(img);
        els.cover.appendChild(icon('play', 'player-cover-icon'));
      });
      img.src = track.cover_url;
      els.cover.appendChild(img);
    } else {
      els.cover.appendChild(icon('play', 'player-cover-icon'));
    }
    renderMediaSession(track);
  }

  function renderMediaSession(track) {
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new window.MediaMetadata({
        title: track.title || '',
        artist: artistText(track),
        album: track.album || '',
        artwork: track.cover_url ? [{ src: track.cover_url, sizes: '500x500' }] : []
      });
      navigator.mediaSession.setActionHandler('play', function () { resume(); });
      navigator.mediaSession.setActionHandler('pause', function () { pause(); });
      navigator.mediaSession.setActionHandler('previoustrack', function () { prev(); });
      navigator.mediaSession.setActionHandler('nexttrack', function () { next(); });
    } catch (e) {
      /* 老浏览器不支持就算了 */
    }
  }

  /* ------------------------------ 行高亮 ------------------------------ */

  function markRows() {
    var key = keyOf(current);
    var rows = document.querySelectorAll('[data-play-key]');
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var on = key && row.getAttribute('data-play-key') === key;
      row.classList.toggle('is-playing', !!on && audio && !audio.paused);
      row.classList.toggle('is-current', !!on);
    }
  }

  /* ------------------------------ 播放流程 ------------------------------ */

  function load(track, autoplay) {
    build();
    var mySeq = ++seq;
    current = track;
    paint(track);
    setBusy(true);
    els.root.classList.remove('is-buffering');

    var payload = {
      provider: track.provider || '',
      id: String(track.id || ''),
      quality: track._quality || DEFAULT_QUALITY,
      title: track.title || '',
      artists: Array.isArray(track.artists) ? track.artists : [],
      album: track.album || '',
      cover_url: track.cover_url || '',
      duration_ms: Number(track.duration_ms) || 0,
      extra: track.extra || {}
    };

    postJson('/api/play/prepare', payload).then(function (data) {
      if (mySeq !== seq) return; // 用户已经点了别的歌
      var a = ensureAudio();
      a.src = hooks.apiUrl(data.url || ('/api/play/' + data.token));
      a.load();
      if (autoplay === false) {
        setBusy(false);
        return;
      }
      var p = a.play();
      if (p && typeof p.catch === 'function') {
        p.then(function () { setBusy(false); markRows(); })
          .catch(function (err) {
            setBusy(false);
            markRows();
            if (err && err.name === 'NotAllowedError') {
              hooks.toast('浏览器拦截了自动播放，点一下播放键', 'warn');
            } else {
              hooks.toast('播放失败：' + (err && err.message ? err.message : err), 'error');
            }
          });
      } else {
        setBusy(false);
        markRows();
      }
    }).catch(function (err) {
      if (mySeq !== seq) return;
      setBusy(false);
      hooks.toast('拿不到音频：' + (err && err.message ? err.message : err), 'error');
    });
  }

  function postJson(path, body) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('POST', hooks.apiUrl(path), true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.timeout = 30000;
      xhr.onload = function () {
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300 && data) {
          resolve(data);
          return;
        }
        var msg = (data && (data.detail || data.message)) || ('HTTP ' + xhr.status);
        reject(new Error(msg));
      };
      xhr.onerror = function () { reject(new Error('连接后端失败')); };
      xhr.ontimeout = function () { reject(new Error('请求超时')); };
      xhr.send(JSON.stringify(body));
    });
  }

  function play(track, list) {
    if (!track || !track.id) {
      hooks.toast('这条曲目缺少 id，无法播放', 'error');
      return;
    }
    var arr = Array.isArray(list) ? list : [];
    queue = arr.slice();
    index = findIndex(track);
    if (index < 0) {
      queue.unshift(track);
      index = 0;
    }
    load(track, true);
  }

  function findIndex(track) {
    var key = keyOf(track);
    for (var i = 0; i < queue.length; i++) {
      if (keyOf(queue[i]) === key) return i;
    }
    return -1;
  }

  function toggle() {
    if (!audio || !current) return;
    if (audio.paused) resume();
    else pause();
  }

  function resume() {
    if (!audio || !current) return;
    if (!audio.src) {
      load(current, true);
      return;
    }
    var p = audio.play();
    if (p && typeof p.catch === 'function') {
      p.catch(function (err) {
        // 令牌过期时 src 已失效，重新准备一次
        if (err && err.name === 'NotSupportedError') load(current, true);
      });
    }
  }

  function pause() {
    if (!audio) return;
    audio.pause();
  }

  function step(delta) {
    if (!queue.length) return;
    var next = index + delta;
    if (next < 0 || next >= queue.length) {
      pause();
      index = Math.max(0, Math.min(queue.length - 1, next));
      return;
    }
    index = next;
    load(queue[index], true);
  }

  function next() { step(1); }
  function prev() { step(-1); }

  function seekBy(delta) {
    if (!audio || !isFinite(audio.duration) || audio.duration <= 0) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration, audio.currentTime + delta));
    renderTime();
  }

  function setQueue(list) {
    queue = Array.isArray(list) ? list.slice() : [];
    if (current) index = findIndex(current);
  }

  function isPlaying() {
    return !!(audio && !audio.paused);
  }

  function stop() {
    if (audio) {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
    seq += 1;
    current = null;
    if (els.root) els.root.hidden = true;
    document.body.classList.remove('has-player');
    markRows();
  }

  window.MDLPlayer = {
    hooks: hooks,
    play: play,
    setQueue: setQueue,
    toggle: toggle,
    next: next,
    prev: prev,
    pause: pause,
    resume: resume,
    isPlaying: isPlaying,
    stop: stop,
    current: function () { return current; }
  };
})();