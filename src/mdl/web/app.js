/* ==========================================================================
 * MusicDL — 前端逻辑
 * 原生 JS，无框架、无构建、无外部依赖。
 *
 * 依赖的接口（同源）：
 *   GET  /api/health
 *   GET  /api/search?q=&provider=&limit=
 *   POST /api/resolve            {url}
 *   POST /api/download           {provider,id,quality,title,artists,album,cover_url,duration_ms}
 *   POST /api/download/batch     {quality,tracks[]}
 *   GET  /api/jobs/{job_id}
 *   GET  /api/file?path=<相对路径>
 * ========================================================================== */

(function () {
  'use strict';

  /* ------------------------------ 常量 ------------------------------ */

  var PROVIDERS = [
    { id: 'netease', label: '网易云' },
    { id: 'qq', label: 'QQ音乐' },
    { id: 'kugou', label: '酷狗' },
    { id: 'kuwo', label: '酷我' }
  ];

  var PROVIDER_LABEL = {};
  PROVIDERS.forEach(function (p) { PROVIDER_LABEL[p.id] = p.label; });

  var QUALITIES = [
    { id: '128', label: '128K' },
    { id: '192', label: '192K' },
    { id: '320', label: '320K' },
    { id: 'flac', label: 'FLAC' }
  ];

  var DEFAULT_QUALITY = '320';
  var SEARCH_LIMIT = 20;
  var POLL_INTERVAL = 1000;

  var KIND_LABEL = {
    song: '单曲',
    playlist: '歌单',
    album: '专辑'
  };

  /* ------------------------------ 工具 ------------------------------ */

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svgIcon(paths, extra) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.6');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    if (extra) {
      Object.keys(extra).forEach(function (k) { svg.setAttribute(k, extra[k]); });
    }
    paths.forEach(function (d) {
      var p = document.createElementNS(ns, 'path');
      p.setAttribute('d', d);
      svg.appendChild(p);
    });
    return svg;
  }

  var NOTE_PATHS = [
    'M9 17.4V6.2l9-1.9v11.2',
    'M6.6 20a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8z',
    'M15.6 17.9a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8z'
  ];

  function providerLabel(id) {
    return PROVIDER_LABEL[id] || id || '未知平台';
  }

  function badge(provider) {
    var key = PROVIDER_LABEL[provider] ? provider : 'unknown';
    var b = el('span', 'badge', providerLabel(provider));
    b.setAttribute('data-provider', key);
    return b;
  }

  function durationText(track) {
    if (track && track.duration_text) return track.duration_text;
    var ms = (track && Number(track.duration_ms)) || 0;
    if (!ms || ms <= 0) return '--:--';
    var total = Math.round(ms / 1000);
    var mm = Math.floor(total / 60);
    var ss = total % 60;
    return (mm < 10 ? '0' + mm : mm) + ':' + (ss < 10 ? '0' + ss : ss);
  }

  function artistText(track) {
    if (track && track.artist_text) return track.artist_text;
    if (track && Array.isArray(track.artists) && track.artists.length) {
      return track.artists.filter(Boolean).join(' / ');
    }
    return '未知歌手';
  }

  function fileUrl(path) {
    return '/api/file?path=' + encodeURIComponent(path || '');
  }

  function humanSize(bytes) {
    var n = Number(bytes) || 0;
    if (n <= 0) return '';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  /* ------------------------------ Toast ------------------------------ */

  // 延迟查询，避免脚本在 DOM 之前执行时拿到 null
  function toastBox() { return $('toasts'); }

  function toast(message, kind, ms) {
    var box = toastBox();
    if (!box) return;
    var node = el('div', 'toast', message);
    node.setAttribute('data-kind', kind || 'ok');
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    box.appendChild(node);

    var ttl = typeof ms === 'number' ? ms : (kind === 'error' ? 5200 : 3200);
    var removed = false;

    function dismiss() {
      if (removed) return;
      removed = true;
      node.classList.add('is-out');
      setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 200);
    }

    node.addEventListener('click', dismiss);
    setTimeout(dismiss, ttl);
  }

  /* ------------------------------ HTTP ------------------------------ */

  function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      headers: { 'Accept': 'application/json' },
      cache: 'no-store'
    };
    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (opts.signal) init.signal = opts.signal;

    return fetch(path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var msg = '';
          if (data && data.detail) {
            msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
          } else if (data && data.error) {
            msg = String(data.error);
          } else if (text && text.length < 300) {
            msg = text.trim();
          }
          if (!msg) msg = '请求失败（HTTP ' + res.status + '）';
          var err = new Error(msg);
          err.status = res.status;
          throw err;
        }
        if (data === null) throw new Error('服务器返回了非 JSON 数据');
        return data;
      });
    });
  }

  /* ------------------------------ 状态 ------------------------------ */

  var state = {
    busy: false,
    groups: [],
    resolved: null,
    jobs: {},
    pollers: {},
    seq: 0,
    ctrl: null,
    statusToken: 0
  };

  /* ------------------------------ 顶部平台状态 ------------------------------ */

  function renderProviderSkeleton() {
    var box = $('providers');
    if (!box) return;
    box.innerHTML = '';
    PROVIDERS.forEach(function (p) {
      var pill = el('span', 'pill');
      pill.setAttribute('data-provider', p.id);
      pill.appendChild(el('span', 'dot'));
      pill.appendChild(document.createTextNode(p.label));
      pill.title = p.label + ' · 检测中';
      box.appendChild(pill);
    });
  }

  function applyHealth(health) {
    var box = $('providers');
    if (!box) return;
    var online = {};
    var list = (health && Array.isArray(health.providers)) ? health.providers : [];
    // 契约里 providers 是字符串数组；这里也容忍 {"id":...}/{"provider":...} 形态
    list = list.map(function (item) {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') return String(item.id || item.provider || item.name || '');
      return '';
    }).filter(Boolean);

    list.forEach(function (id) { online[id] = true; });

    box.innerHTML = '';
    PROVIDERS.forEach(function (p) {
      var on = !!online[p.id];
      var pill = el('span', 'pill ' + (on ? 'is-on' : 'is-off'));
      pill.setAttribute('data-provider', p.id);
      pill.appendChild(el('span', 'dot'));
      pill.appendChild(document.createTextNode(p.label));
      pill.title = p.label + (on ? ' · 在线' : ' · 离线');
      box.appendChild(pill);
    });

    // 后端报告了契约外的平台时也展示出来，避免静默丢失信息
    list.forEach(function (id) {
      if (PROVIDER_LABEL[id]) return;
      var pill = el('span', 'pill is-on');
      pill.appendChild(el('span', 'dot'));
      pill.appendChild(document.createTextNode(id));
      box.appendChild(pill);
    });

    var badgeEl = $('versionBadge');
    if (badgeEl) {
      badgeEl.textContent = (health && health.version) ? 'v' + health.version : 'v—';
    }
  }

  function loadHealth() {
    renderProviderSkeleton();
    api('/api/health').then(function (data) {
      applyHealth(data);
    }).catch(function (err) {
      applyHealth({ providers: [] });
      toast('无法连接后端服务：' + err.message, 'error');
    });
  }

  /* ------------------------------ 模式识别 ------------------------------ */

  var RE_URL = /https?:\/\/[^\s"'<>，。；、）】]+/i;
  var RE_NUMERIC_ID = /^\d{5,}$/;

  function extractUrl(value) {
    var m = String(value || '').match(RE_URL);
    return m ? m[0] : '';
  }

  function detectMode(value) {
    var v = String(value || '').trim();
    if (!v) return 'search';
    if (RE_URL.test(v)) return 'resolve';
    if (RE_NUMERIC_ID.test(v)) return 'resolve';
    return 'search';
  }

  function syncModeChip() {
    var input = $('q');
    var chip = $('modeChip');
    var btn = $('searchBtn');
    if (!input || !chip || !btn) return;
    var mode = detectMode(input.value);
    if (mode === 'resolve') {
      chip.hidden = false;
      chip.textContent = '解析';
      btn.textContent = '解析';
    } else {
      chip.hidden = true;
      btn.textContent = '搜索';
    }
  }

  /* ------------------------------ 状态条 / 空状态 ------------------------------ */

  function setStatus(text, isError) {
    var bar = $('statusBar');
    var label = $('statusText');
    if (!bar || !label) return;
    state.statusToken += 1;
    if (!text) {
      bar.hidden = true;
      bar.classList.remove('is-error');
      return;
    }
    bar.hidden = false;
    bar.classList.toggle('is-error', !!isError);
    label.textContent = text;
    var spinner = bar.querySelector('.spinner');
    if (spinner) spinner.style.display = isError ? 'none' : '';
  }

  // 显示一条会在 ms 毫秒后自动消失的状态；期间若出现新状态则不覆盖
  function flashStatus(text, ms) {
    setStatus(text, false);
    var token = state.statusToken;
    window.setTimeout(function () {
      if (state.statusToken === token) setStatus('', false);
    }, ms || 2600);
  }

  function showEmpty(title, text) {
    var empty = $('empty');
    if (!empty) return;
    $('emptyTitle').textContent = title;
    $('emptyText').textContent = text;
    empty.hidden = false;
  }

  function hideEmpty() {
    var empty = $('empty');
    if (empty) empty.hidden = true;
  }

  function clearResults() {
    var box = $('results');
    if (box) box.innerHTML = '';
    state.groups = [];
    state.resolved = null;
  }

  /* ------------------------------ 骨架屏 ------------------------------ */

  function skeletonGroup(provider) {
    var group = el('div', 'group');
    var head = el('div', 'group-head');
    if (provider && provider !== 'unknown') head.appendChild(badge(provider));
    var c = el('span', 'group-count');
    var line = el('span', 'skeleton', '');
    line.style.display = 'inline-block';
    line.style.width = '38px';
    line.style.height = '10px';
    c.appendChild(line);
    head.appendChild(c);
    group.appendChild(head);

    var tracks = el('div', 'tracks');
    for (var i = 0; i < 4; i++) {
      var row = el('div', 'skel-row');
      row.appendChild(el('div', 'skeleton thumb'));
      var lines = el('div', 'skel-lines');
      lines.appendChild(el('div', 'skeleton line-1'));
      lines.appendChild(el('div', 'skeleton line-2'));
      row.appendChild(lines);
      tracks.appendChild(row);
    }
    group.appendChild(tracks);
    return group;
  }

  // provider: 'all' -> 四个平台骨架；具体平台 -> 单组骨架；null -> 一个通用骨架（解析中）
  function renderSkeletons(provider) {
    clearResults();
    hideEmpty();
    var box = $('results');
    if (!box) return;
    var list;
    if (provider === null) list = ['unknown'];
    else if (provider && provider !== 'all') list = [provider];
    else list = PROVIDERS.map(function (p) { return p.id; });
    list.forEach(function (id) {
      box.appendChild(skeletonGroup(id));
    });
  }

  /* ------------------------------ 封面 ------------------------------ */

  function makeCover(url, large) {
    var wrap = el('div', large ? 'resolve-cover' : 'cover');
    var fallback = el('span', 'cover-fallback');
    fallback.appendChild(svgIcon(NOTE_PATHS));

    if (url) {
      var img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('load', function () {
        img.classList.add('is-loaded');
        fallback.style.display = 'none';
      });
      img.addEventListener('error', function () {
        // 图裂了就整个移除，只留占位音符
        if (img.parentNode) img.parentNode.removeChild(img);
        fallback.style.display = '';
      });
      img.src = url;
      wrap.appendChild(img);
    }
    wrap.appendChild(fallback);
    return wrap;
  }

  /* ------------------------------ 曲目行 ------------------------------ */

  function makeQualitySelect(track, index) {
    var sel = document.createElement('select');
    sel.className = 'quality-select';
    sel.setAttribute('aria-label', '音质');
    QUALITIES.forEach(function (q) {
      var opt = document.createElement('option');
      opt.value = q.id;
      opt.textContent = q.label;
      sel.appendChild(opt);
    });
    sel.value = DEFAULT_QUALITY;
    sel.addEventListener('click', function (e) { e.stopPropagation(); });
    return sel;
  }

  function makeDownloadButton(track, qualitySelect, index) {
    var btn = el('button', 'dl-btn', '下载');
    btn.type = 'button';
    btn.addEventListener('click', function () {
      downloadOne(track, qualitySelect.value, btn);
    });
    return btn;
  }

  function makeTrackRow(track, index) {
    var row = el('div', 'track');
    row.setAttribute('data-provider', track.provider || '');

    row.appendChild(el('span', 'track-index', String(index + 1)));
    row.appendChild(makeCover(track.cover_url, false));

    var main = el('div', 'track-main');
    var title = el('div', 'track-title', track.title || '未命名曲目');
    title.title = track.title || '';
    main.appendChild(title);

    var sub = el('div', 'track-sub');
    sub.appendChild(document.createTextNode(artistText(track)));
    if (track.album) {
      sub.appendChild(el('span', 'sep', '·'));
      sub.appendChild(document.createTextNode(track.album));
    }
    sub.title = sub.textContent;
    main.appendChild(sub);
    row.appendChild(main);

    row.appendChild(el('span', 'track-dur', durationText(track)));

    var actions = el('div', 'track-actions');
    var sel = makeQualitySelect(track, index);
    actions.appendChild(sel);
    actions.appendChild(makeDownloadButton(track, sel, index));
    row.appendChild(actions);

    return row;
  }

  /* ------------------------------ 单曲下载 ------------------------------ */

  function setButtonLoading(btn, loading) {
    if (!btn) return;
    if (loading) {
      btn.dataset.orig = btn.dataset.orig || btn.textContent;
      btn.disabled = true;
      btn.classList.add('is-loading');
      btn.classList.remove('is-done');
      btn.textContent = '';
      btn.appendChild(el('span', 'mini-spinner'));
      btn.appendChild(document.createTextNode('下载中'));
    } else {
      btn.disabled = false;
      btn.classList.remove('is-loading');
      btn.textContent = btn.dataset.orig || '下载';
    }
  }

  function setButtonDone(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.classList.remove('is-loading');
    btn.classList.add('is-done');
    btn.textContent = '已下载';
  }

  function downloadOne(track, quality, btn) {
    if (!track || !track.id) {
      toast('这条曲目缺少 id，无法下载', 'error');
      return;
    }
    setButtonLoading(btn, true);

    var payload = {
      provider: track.provider || '',
      id: String(track.id),
      quality: quality || DEFAULT_QUALITY,
      title: track.title || '',
      artists: Array.isArray(track.artists) ? track.artists : [],
      album: track.album || '',
      cover_url: track.cover_url || '',
      duration_ms: Number(track.duration_ms) || 0
    };

    api('/api/download', { method: 'POST', body: payload }).then(function (res) {
      setButtonDone(btn);
      var name = res && res.file ? res.file : (track.title || '文件');
      var size = res && res.size ? ' · ' + humanSize(res.size) : '';
      toast('已下载：' + name + size, 'ok');
      if (res && res.path) {
        fileToast('保存到浏览器：', res.file || name, res.path);
      }
    }).catch(function (err) {
      setButtonLoading(btn, false);
      toast('下载失败：' + err.message, 'error');
    });
  }

  // 下载成功后的一条可点击 toast，点文件名就用 /api/file 拉回浏览器
  function fileToast(prefix, name, path) {
    var box = toastBox();
    if (!box) return;
    var node = el('div', 'toast');
    node.setAttribute('data-kind', 'ok');
    node.appendChild(el('span', '', prefix));
    var link = document.createElement('a');
    link.href = fileUrl(path);
    link.download = name || '';
    link.textContent = name || path;
    link.style.wordBreak = 'break-all';
    link.style.color = 'var(--accent)';
    node.appendChild(link);
    box.appendChild(node);

    var removed = false;
    function dismiss() {
      if (removed) return;
      removed = true;
      node.classList.add('is-out');
      setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 200);
    }
    node.addEventListener('click', dismiss);
    setTimeout(dismiss, 8000);
  }

  /* ------------------------------ 搜索结果渲染 ------------------------------ */

  function groupQuality(provider) {
    // 每组共用一个音质选择：批量下载时用组内的值
    var sel = document.createElement('select');
    sel.className = 'quality-select';
    sel.setAttribute('aria-label', '批量音质');
    QUALITIES.forEach(function (q) {
      var opt = document.createElement('option');
      opt.value = q.id;
      opt.textContent = q.label;
      sel.appendChild(opt);
    });
    sel.value = DEFAULT_QUALITY;
    return sel;
  }

  function renderGroup(group, index) {
    var provider = group.provider || 'unknown';
    var tracks = Array.isArray(group.tracks) ? group.tracks : [];
    var ok = group.ok !== false;

    var wrap = el('div', 'group');
    wrap.style.animationDelay = Math.min(index * 45, 220) + 'ms';

    var head = el('div', 'group-head');
    head.appendChild(badge(provider));

    // 失败的分组不隐藏：明确写出「该平台不可用」+ 原因，用户要知道是谁挂了
    var count = el('span', 'group-count',
      ok ? (tracks.length ? tracks.length + ' 首' : '没有结果') : '该平台不可用');
    head.appendChild(count);

    if (!ok) {
      var msg = group.error || '该平台暂时不可用';
      var errChip = el('span', 'group-error', msg);
      errChip.title = msg;
      head.appendChild(errChip);
    } else if (tracks.length) {
      var actions = el('div', 'group-actions');
      var sel = groupQuality(provider);
      var btn = el('button', 'dl-btn', '全部下载');
      btn.type = 'button';
      btn.addEventListener('click', function () {
        startBatch(tracks, sel.value, btn);
      });
      actions.appendChild(sel);
      actions.appendChild(btn);
      head.appendChild(actions);
    }

    wrap.appendChild(head);

    if (tracks.length) {
      var list = el('div', 'tracks');
      tracks.forEach(function (t, i) {
        list.appendChild(makeTrackRow(t, i));
      });
      wrap.appendChild(list);
    }

    return wrap;
  }

  function renderSearchResult(data) {
    var box = $('results');
    if (!box) return;
    box.innerHTML = '';
    state.resolved = null;

    var groups = (data && Array.isArray(data.groups)) ? data.groups : [];
    state.groups = groups;

    var total = 0;
    var failedGroups = 0;
    groups.forEach(function (g) {
      if (g && g.ok !== false && Array.isArray(g.tracks)) total += g.tracks.length;
      if (g && g.ok === false) failedGroups++;
    });

    if (!groups.length) {
      showEmpty('没有搜到内容', '换个关键词试试，或者只选一个平台搜索。');
      return;
    }

    groups.forEach(function (g, i) {
      box.appendChild(renderGroup(g || {}, i));
    });

    hideEmpty();

    var q = (data && data.query) ? '「' + data.query + '」' : '';
    if (total > 0) {
      var suffix = failedGroups ? '，' + failedGroups + ' 个平台出错' : '';
      flashStatus(q + '共 ' + total + ' 首结果' + suffix, 2600);
    } else {
      setStatus('没有匹配的曲目' + (failedGroups ? '（' + failedGroups + ' 个平台出错）' : ''), true);
    }
  }

  /* ------------------------------ 解析结果渲染 ------------------------------ */

  function renderResolve(data) {
    var box = $('results');
    if (!box) return;
    box.innerHTML = '';
    state.resolved = data;

    var tracks = (data && Array.isArray(data.tracks)) ? data.tracks : [];
    var kind = KIND_LABEL[data && data.kind] || '内容';

    var card = el('div', 'resolve-card');

    var hero = el('div', 'resolve-hero');
    hero.appendChild(makeCover((data && data.cover_url) || '', true));

    var info = el('div', 'resolve-info');

    var kicker = el('div', 'resolve-kicker');
    kicker.appendChild(badge((data && data.provider) || 'unknown'));
    kicker.appendChild(document.createTextNode(kind));
    info.appendChild(kicker);

    var title = el('h2', 'resolve-title', (data && data.title) || '未命名');
    title.title = title.textContent;
    info.appendChild(title);

    var meta = el('div', 'resolve-meta');
    var count = (data && typeof data.count === 'number') ? data.count : tracks.length;
    meta.appendChild(document.createTextNode('共 ' + count + ' 首'));
    if (data && data.source_url) {
      meta.appendChild(el('span', 'sep', '·'));
      var link = document.createElement('a');
      link.href = data.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '查看原页面';
      meta.appendChild(link);
    }
    info.appendChild(meta);

    var actions = el('div', 'resolve-actions');
    var sel = groupQuality((data && data.provider) || 'unknown');
    actions.appendChild(sel);
    var allBtn = el('button', 'btn-lg primary-btn', '全部下载');
    allBtn.type = 'button';
    allBtn.disabled = !tracks.length;
    allBtn.addEventListener('click', function () {
      startBatch(tracks, sel.value, allBtn);
    });
    actions.appendChild(allBtn);
    info.appendChild(actions);

    hero.appendChild(info);
    card.appendChild(hero);

    if (tracks.length) {
      var list = el('div', 'tracks resolve-tracks');
      tracks.forEach(function (t, i) {
        list.appendChild(makeTrackRow(t, i));
      });
      card.appendChild(list);
    }

    box.appendChild(card);
    hideEmpty();

    if (!tracks.length) {
      setStatus('解析成功，但里面没有曲目', true);
    } else {
      flashStatus('已解析' + kind + '：' + ((data && data.title) || '') + '（' + tracks.length + ' 首）', 2600);
    }
  }

  /* ------------------------------ 批量下载 / 任务 ------------------------------ */

  function startBatch(tracks, quality, btn) {
    var list = (tracks || []).filter(function (t) { return t && t.id; });
    if (!list.length) {
      toast('没有可下载的曲目', 'warn');
      return;
    }
    if (list.length > 200) {
      list = list.slice(0, 200);
      toast('单次最多 200 首，已截取前 200 首', 'warn');
    }

    if (btn) {
      btn.dataset.orig = btn.dataset.orig || btn.textContent;
      btn.disabled = true;
      btn.textContent = '提交中…';
    }

    function restoreBtn() {
      if (!btn) return;
      btn.disabled = false;
      btn.textContent = btn.dataset.orig || '全部下载';
    }

    api('/api/download/batch', {
      method: 'POST',
      body: { quality: quality || DEFAULT_QUALITY, tracks: list }
    }).then(function (res) {
      var jobId = res && res.job_id;
      if (!jobId) throw new Error('后端没有返回 job_id');
      restoreBtn();
      toast('已创建任务 ' + jobId + '，共 ' + list.length + ' 首', 'ok');
      trackJob(jobId, list.length);
      openJobsPanel();
    }).catch(function (err) {
      restoreBtn();
      toast('批量下载失败：' + err.message, 'error');
    });
  }

  function trackJob(jobId, expectedTotal) {
    state.seq += 1;
    state.jobs[jobId] = {
      id: jobId,
      state: 'running',
      total: expectedTotal || 0,
      finished: 0,
      ok: 0,
      failed: 0,
      current: '',
      items: [],
      created_at: 0,
      _seq: state.seq
    };
    renderJobs();
    pollJob(jobId);
  }

  function pollJob(jobId) {
    if (state.pollers[jobId]) return;
    var misses = 0;

    var timer = window.setInterval(function () {
      api('/api/jobs/' + encodeURIComponent(jobId)).then(function (data) {
        misses = 0;
        if (!data) return;
        var prev = state.jobs[jobId] || {};
        if (prev._seq !== undefined && data._seq === undefined) data._seq = prev._seq;
        state.jobs[jobId] = data;
        renderJobs();
        if (data.state === 'done' || data.state === 'failed') {
          stopPolling(jobId);
          var ok = Number(data.ok) || 0;
          var failed = Number(data.failed) || 0;
          if (data.state === 'failed') {
            toast('任务 ' + jobId + ' 结束：成功 ' + ok + '，失败 ' + failed, 'error');
          } else if (failed > 0) {
            toast('任务 ' + jobId + ' 完成：成功 ' + ok + '，失败 ' + failed, 'warn');
          } else {
            toast('任务 ' + jobId + ' 全部完成：' + ok + ' 首', 'ok');
          }
        }
      }).catch(function (err) {
        if (err && err.status === 404) {
          stopPolling(jobId);
          var job = state.jobs[jobId];
          if (job) job.state = 'failed';
          renderJobs();
          toast('任务 ' + jobId + ' 在服务端不存在了', 'error');
          return;
        }
        // 网络抖动不终止；连续失败 10 次才放弃，避免后台任务被无声丢弃
        misses += 1;
        if (misses >= 10) {
          stopPolling(jobId);
          var j = state.jobs[jobId];
          if (j && j.state === 'running') {
            j.state = 'failed';
            j.current = '与服务器的连接中断';
            renderJobs();
            toast('任务 ' + jobId + ' 轮询中断，请刷新页面查看结果', 'error');
          }
        }
      });
    }, POLL_INTERVAL);

    state.pollers[jobId] = timer;
  }

  function stopPolling(jobId) {
    var timer = state.pollers[jobId];
    if (timer) {
      window.clearInterval(timer);
      delete state.pollers[jobId];
    }
  }

  function jobStateLabel(s) {
    if (s === 'done') return '已完成';
    if (s === 'failed') return '失败';
    if (s === 'running') return '进行中';
    return '等待中';
  }

  function renderJobs() {
    var box = $('jobsList');
    if (!box) return;

    var ids = Object.keys(state.jobs);
    if (!ids.length) {
      box.innerHTML = '';
      var empty = el('div', 'jobs-empty');
      empty.appendChild(document.createTextNode('还没有下载任务。'));
      empty.appendChild(document.createElement('br'));
      empty.appendChild(document.createTextNode('点「全部下载」后，进度会显示在这里。'));
      box.appendChild(empty);
      return;
    }

    // 新任务在前
    ids.sort(function (a, b) {
      var ja = state.jobs[a], jb = state.jobs[b];
      var sa = Number(ja && ja._seq) || 0;
      var sb = Number(jb && jb._seq) || 0;
      if (sa !== sb) return sb - sa;
      var ca = Number(ja && ja.created_at) || 0;
      var cb = Number(jb && jb.created_at) || 0;
      return cb - ca;
    });

    box.innerHTML = '';
    ids.forEach(function (id) {
      box.appendChild(renderJob(state.jobs[id]));
    });
  }

  function renderJob(job) {
    var card = el('div', 'job');
    card.setAttribute('data-job', job.id);

    var total = Number(job.total) || (Array.isArray(job.items) ? job.items.length : 0);
    var finished = Number(job.finished) || 0;
    var ok = Number(job.ok) || 0;
    var failed = Number(job.failed) || 0;
    var jobState = job.state || 'running';

    var top = el('div', 'job-top');
    top.appendChild(el('span', 'job-id', job.id));
    var st = el('span', 'job-state', jobStateLabel(jobState));
    st.setAttribute('data-state', jobState);
    top.appendChild(st);
    card.appendChild(top);

    var current = job.current || (jobState === 'running' ? '准备中…' : (jobState === 'done' ? '已完成' : '已结束'));
    var cur = el('div', 'job-current', current);
    cur.title = current;
    card.appendChild(cur);

    var pct = total > 0 ? Math.min(100, Math.round((finished / total) * 100)) : (jobState === 'done' ? 100 : 0);
    var progress = el('div', 'progress');
    var fill = el('div', 'progress-fill');
    fill.style.width = pct + '%';
    fill.setAttribute('data-state', jobState);
    progress.appendChild(fill);
    progress.setAttribute('role', 'progressbar');
    progress.setAttribute('aria-valuemin', '0');
    progress.setAttribute('aria-valuemax', String(total || 0));
    progress.setAttribute('aria-valuenow', String(finished));
    card.appendChild(progress);

    var stats = el('div', 'job-stats');
    var sTotal = el('span', '', '');
    sTotal.appendChild(document.createTextNode('共 '));
    sTotal.appendChild(el('b', '', String(total)));
    stats.appendChild(sTotal);

    var sOk = el('span', 'ok', '');
    sOk.appendChild(document.createTextNode('成功 '));
    sOk.appendChild(el('b', '', String(ok)));
    stats.appendChild(sOk);

    var sFail = el('span', 'fail', '');
    sFail.appendChild(document.createTextNode('失败 '));
    sFail.appendChild(el('b', '', String(failed)));
    stats.appendChild(sFail);

    card.appendChild(stats);

    var items = Array.isArray(job.items) ? job.items : [];
    if (items.length) {
      var list = el('div', 'job-items');
      items.forEach(function (it) {
        var itemState = it.state || 'pending';
        var row = el('div', 'job-item');
        row.setAttribute('data-state', itemState);
        row.appendChild(el('span', 'item-dot'));

        var name = el('span', 'item-name', it.title || '未命名');
        name.title = it.title || '';
        row.appendChild(name);

        if (itemState === 'done' && it.path) {
          var link = document.createElement('a');
          link.className = 'item-link';
          link.href = fileUrl(it.path);
          var fname = it.file || (it.path.split(/[\\/]/).pop() || '文件');
          link.textContent = fname;
          link.title = '下载到浏览器：' + fname;
          link.setAttribute('download', fname);
          row.appendChild(link);
        } else if (itemState === 'failed') {
          var e = el('span', 'item-err', it.error || '失败');
          e.title = it.error || '失败';
          row.appendChild(e);
        }

        list.appendChild(row);
      });
      card.appendChild(list);
    }

    return card;
  }

  /* ------------------------------ 任务面板开合 ------------------------------ */

  function openJobsPanel() {
    var panel = $('jobsPanel');
    var backdrop = $('backdrop');
    if (!panel) return;
    panel.classList.add('is-open');
    if (backdrop && window.matchMedia('(max-width: 1080px)').matches) {
      backdrop.hidden = false;
    }
  }

  function closeJobsPanel() {
    var panel = $('jobsPanel');
    var backdrop = $('backdrop');
    if (panel) panel.classList.remove('is-open');
    if (backdrop) backdrop.hidden = true;
  }

  /* ------------------------------ 搜索 / 解析 ------------------------------ */

  function setBusy(busy) {
    state.busy = busy;
    var btn = $('searchBtn');
    if (btn) btn.disabled = busy;
  }

  // 新的搜索/解析请求会中止上一个，避免慢请求把新结果覆盖掉
  function abortInflight() {
    if (state.ctrl) {
      try { state.ctrl.abort(); } catch (e) { /* ignore */ }
      state.ctrl = null;
    }
  }

  function newInflight() {
    abortInflight();
    if (typeof AbortController === 'function') {
      state.ctrl = new AbortController();
      return state.ctrl.signal;
    }
    return undefined;
  }

  function isAbort(err) {
    return !!err && (err.name === 'AbortError' || err.code === 20);
  }

  function runSearch(query, provider) {
    var q = String(query || '').trim();
    if (!q) {
      toast('先输入关键词', 'warn');
      var input = $('q');
      if (input) input.focus();
      return;
    }

    var prov = provider || 'all';
    var signal = newInflight();
    renderSkeletons(prov);
    setBusy(true);
    setStatus('正在搜索「' + q + '」…', false);

    var url = '/api/search?q=' + encodeURIComponent(q) +
      '&provider=' + encodeURIComponent(prov) +
      '&limit=' + SEARCH_LIMIT;

    api(url, { signal: signal }).then(function (data) {
      renderSearchResult(data);
    }).catch(function (err) {
      if (isAbort(err)) return;
      clearResults();
      showEmpty('搜索失败', err.message);
      setStatus('搜索失败：' + err.message, true);
      toast('搜索失败：' + err.message, 'error');
    }).then(function () {
      if (state.ctrl && state.ctrl.signal === signal) setBusy(false);
      else if (!signal) setBusy(false);
    });
  }

  function runResolve(raw) {
    var value = String(raw || '').trim();
    if (!value) return;
    var url = extractUrl(value) || value;
    var signal = newInflight();

    renderSkeletons(null);
    setBusy(true);
    setStatus('正在解析链接…', false);

    api('/api/resolve', { method: 'POST', body: { url: url }, signal: signal }).then(function (data) {
      renderResolve(data);
    }).catch(function (err) {
      if (isAbort(err)) return;
      clearResults();
      showEmpty('解析失败', err.message);
      setStatus('解析失败：' + err.message, true);
      toast('解析失败：' + err.message, 'error');
    }).then(function () {
      if (state.ctrl && state.ctrl.signal === signal) setBusy(false);
      else if (!signal) setBusy(false);
    });
  }

  function submitSearch() {
    var input = $('q');
    if (!input) return;
    var value = input.value.trim();
    if (!value) {
      input.focus();
      toast('先输入关键词或粘贴链接', 'warn');
      return;
    }
    // 上一次请求还在跑时直接发起新的：旧请求会被 abort，不会覆盖新结果
    if (detectMode(value) === 'resolve') {
      runResolve(value);
    } else {
      var sel = $('provider');
      runSearch(value, sel ? sel.value : 'all');
    }
  }

  /* ------------------------------ 事件绑定 ------------------------------ */

  function bindEvents() {
    var form = $('searchForm');
    if (form) {
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        submitSearch();
      });
    }

    var input = $('q');
    if (input) {
      input.addEventListener('input', syncModeChip);
      input.addEventListener('paste', function () {
        // 粘贴后等浏览器把值写进 input 再判断
        window.setTimeout(syncModeChip, 0);
      });
    }

    var toggle = $('jobsToggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var panel = $('jobsPanel');
        if (!panel) return;
        if (panel.classList.contains('is-open')) closeJobsPanel();
        else openJobsPanel();
      });
    }

    var close = $('jobsClose');
    if (close) close.addEventListener('click', closeJobsPanel);

    var backdrop = $('backdrop');
    if (backdrop) backdrop.addEventListener('click', closeJobsPanel);

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        closeJobsPanel();
        return;
      }
      // "/" 聚焦搜索框；在输入控件里不劫持
      if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        var active = document.activeElement;
        var tag = active && active.tagName ? active.tagName.toLowerCase() : '';
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || (active && active.isContentEditable)) {
          return;
        }
        e.preventDefault();
        var box = $('q');
        if (box) {
          box.focus();
          box.select();
        }
      }
    });

    // 窗口变宽后关掉移动端抽屉状态，避免遮罩残留
    window.addEventListener('resize', function () {
      if (window.matchMedia('(min-width: 1081px)').matches) {
        var backdrop = $('backdrop');
        if (backdrop) backdrop.hidden = true;
      }
    });
  }

  /* ------------------------------ 深链接 ------------------------------ */

  // 支持 ?q=<关键词> 与 ?url=<分享链接>，加载后自动执行一次。
  // 执行完立刻用 replaceState 把 query 清掉：刷新不会重复触发，也不会留下脏 URL。
  function consumeDeepLink() {
    var loc = window.location;
    if (!loc || !loc.search) return false;

    var params;
    try {
      params = new URLSearchParams(loc.search);
    } catch (e) {
      return false;
    }

    var rawUrl = (params.get('url') || '').trim();
    var rawQ = (params.get('q') || '').trim();
    if (!rawUrl && !rawQ) return false;

    var input = $('q');
    var provSel = $('provider');
    var provParam = (params.get('provider') || '').trim();

    // 先清 URL，再发请求：即使请求失败也不会在刷新时重放
    try {
      var clean = loc.pathname + (loc.hash || '');
      window.history.replaceState(null, '', clean);
    } catch (e) { /* file:// 或沙箱下可能受限，忽略 */ }

    if (rawUrl) {
      if (input) input.value = rawUrl;
      syncModeChip();
      runResolve(rawUrl);
      return true;
    }

    if (provParam && provSel) {
      var known = false;
      for (var i = 0; i < provSel.options.length; i++) {
        if (provSel.options[i].value === provParam) { known = true; break; }
      }
      if (known) provSel.value = provParam;
    }

    if (input) input.value = rawQ;
    syncModeChip();
    runSearch(rawQ, provSel ? provSel.value : 'all');
    return true;
  }

  /* ------------------------------ 启动 ------------------------------ */

  function init() {
    bindEvents();
    syncModeChip();
    renderJobs();
    loadHealth();

    var input = $('q');
    if (input) {
      input.focus();
    }

    // 深链接放在最后：DOM 与事件都已就绪
    consumeDeepLink();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
