/**
 * 纯前端直连模式（direct mode）。
 *
 * 没有后端也能搜索和下载：靠 JSONP 绕开 CORS 拿接口数据，
 * 再直接请求音频 CDN（这些 CDN 返回 `Access-Control-Allow-Origin: *`），
 * 落地成 Blob 触发浏览器保存。
 *
 * 各平台的实际支持情况（2026-10 实测）：
 *   QQ    搜索 ✅ JSONP   直链 ⚠️ vkey 需登录 Cookie    CDN ✅ ACAO:*
 *   酷狗  搜索 ✅ JSONP   直链 ✅ JSONP                 CDN ✅ ACAO:*
 *   酷我  搜索 ✅ JSONP   直链 ✅ 纯文本                 CDN ❌ 无 CORS（用跳转降级）
 *   网易云 搜索 ❌ 无 JSONP 直链 ❌                      CDN ✅ ACAO:*
 *
 * 网易云既不给 JSONP 也没有 CORS，纯前端拿不到它的接口，只能提示用户
 * 走「配置后端」。这不是偷懒，是浏览器同源策略的硬边界。
 */

(function (global) {
  'use strict';

  var JSONP_TIMEOUT = 12000;
  var seq = 0;

  // ---------------------------------------------------------------- JSONP

  // 所有 JSONP 请求串行化。
  // 原因：QQ 忽略 callback 参数、固定回调到全局 callback()，多个请求并发
  // 会互相覆盖同一个全局函数，先发的那个永远等不到回调（实测直接超时）。
  // 串行后同一时刻只有一个在飞，名字冲突不可能发生。
  var queue = Promise.resolve();

  function jsonp(url, options) {
    var task = function () {
      return rawJsonp(url, options);
    };
    var p = queue.then(task, task);
    queue = p.then(
      function () {},
      function () {}
    );
    return p;
  }

  /**
   * 注入 <script> 拉 JSONP。兼容三种返回形态：
   *   1. cb({...})                                        —— 标准
   *   2. callback({...})                                  —— QQ 用固定名
   *   3. try{var jsondata={...}; cb(jsondata)}catch(e){}  —— 酷我
   */
  function rawJsonp(url, options) {
    var name = '__mdl_cb_' + ++seq;

    return new Promise(function (resolve, reject) {
      var script = document.createElement('script');
      var timer = null;
      var done = false;

      function cleanup() {
        if (done) return;
        done = true;
        if (timer) clearTimeout(timer);
        if (script.parentNode) script.parentNode.removeChild(script);
        try {
          delete global[name];
          if (global.callback === onCallback) delete global.callback;
        } catch (e) {
          global[name] = undefined;
        }
      }

      function finish(value) {
        cleanup();
        resolve(value);
      }

      function fail(message) {
        cleanup();
        reject(new Error(message));
      }

      function pick(value) {
        if (value && typeof value === 'object') return finish(value);
        // 酷我：cb(jsondata) 里 jsondata 是变量名，真实数据在 window 上
        var loose = global.jsondata;
        if (loose && typeof loose === 'object') return finish(loose);
        fail('JSONP 回调拿到空数据');
      }

      function onCallback(value) {
        pick(value);
      }

      // 自己的名字（酷狗、酷我认这个）+ 固定 callback（QQ 只用这个）
      global[name] = onCallback;
      global.callback = onCallback;

      script.onerror = function () {
        fail('JSONP 请求失败（网络不通或被浏览器拦截）');
      };

      timer = setTimeout(function () {
        fail('JSONP 超时（' + JSONP_TIMEOUT / 1000 + 's）');
      }, JSONP_TIMEOUT);

      // 调用方可能自带 callback=xxx，必须剥掉再换成自己的名字，
      // 否则服务端按第一个 callback 参数返回，回调函数根本不存在。
      var cleaned = String(url).replace(/([?&])callback=[^&]*/gi, '$1').replace(/[?&]$/, '');
      var sep2 = cleaned.indexOf('?') >= 0 ? '&' : '?';
      script.src = cleaned + sep2 + 'callback=' + name + '&_=' + Date.now();
      document.head.appendChild(script);

      // 兜底：酷我脚本是同步调用的，万一回调被抢走，1.5s 后直接读全局变量
      setTimeout(function () {
        if (done) return;
        if (global.jsondata && typeof global.jsondata === 'object') finish(global.jsondata);
      }, 1500);
    });
  }

  // ---------------------------------------------------------------- 工具

  function clean(text) {
    return String(text || '')
      .replace(/<[^>]*>/g, '')
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .trim();
  }

  function mmss(ms) {
    if (!ms || ms <= 0) return '--:--';
    var total = Math.round(ms / 1000);
    var m = Math.floor(total / 60);
    var s = total % 60;
    return (m < 10 ? '0' + m : m) + ':' + (s < 10 ? '0' + s : s);
  }

  function safeName(name) {
    return (
      String(name || 'unknown')
        .replace(/[<>:"/\\|?*\x00-\x1f]/g, '_')
        .replace(/\s+/g, ' ')
        .trim()
        .slice(0, 120) || 'unknown'
    );
  }

  function parseLoose(text) {
    var raw = String(text || '').trim();
    if (!raw) return {};
    try {
      return JSON.parse(raw);
    } catch (e) {
      /* 酷我单引号 JSON */
    }
    try {
      return JSON.parse(raw.replace(/'/g, '"'));
    } catch (e) {
      return {};
    }
  }

  function plainFetch(url, init) {
    return fetch(url, init).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.text();
    });
  }

  // ---------------------------------------------------------------- 搜索

  function searchQQ(keyword, limit) {
    var url =
      'https://c.y.qq.com/soso/fcgi-bin/client_search_cp?p=1&n=' +
      limit +
      '&w=' +
      encodeURIComponent(keyword) +
      '&format=jsonp&callback=cb&new_json=1';
    return jsonp(url).then(function (data) {
      var list = ((((data || {}).data || {}).song || {}).list) || [];
      return list.map(function (s) {
        var albumMid = s.albummid || '';
        var ms = (Number(s.interval) || 0) * 1000;
        return {
          provider: 'qq',
          id: s.songmid,
          title: clean(s.songname),
          artists: (s.singer || []).map(function (x) {
            return clean(x.name);
          }),
          artist_text: (s.singer || [])
            .map(function (x) {
              return clean(x.name);
            })
            .join(' / '),
          album: clean(s.albumname || ''),
          duration_ms: ms,
          duration_text: mmss(ms),
          cover_url: albumMid
            ? 'https://y.gtimg.cn/music/photo_new/T002R300x300M000' + albumMid + '.jpg'
            : '',
          source_url: 'https://y.qq.com/n/ryqq/songDetail/' + s.songmid,
          extra: { album_mid: albumMid, direct: true }
        };
      });
    });
  }

  function searchKugou(keyword, limit) {
    var url =
      'https://songsearch.kugou.com/song_search_v2?keyword=' +
      encodeURIComponent(keyword) +
      '&page=1&pagesize=' +
      limit +
      '&platform=WebFilter&tag=em&filter=2&iscorrection=1&privilege_filter=0&callback=cb';
    return jsonp(url).then(function (data) {
      var list = (((data || {}).data || {}).lists) || [];
      return list.map(function (it) {
        var ms = (Number(it.Duration) || 0) * 1000;
        var cover = String(it.Image || it.AlbumImage || '').replace('{size}', '480');
        return {
          provider: 'kugou',
          id: String(it.FileHash || '').toLowerCase(),
          title: clean(it.SongName),
          artists: (it.Singers || [])
            .map(function (s) {
              return clean(s.name);
            })
            .filter(Boolean),
          artist_text:
            (it.Singers || [])
              .map(function (s) {
                return clean(s.name);
              })
              .filter(Boolean)
              .join(' / ') || clean(it.SingerName),
          album: clean(it.AlbumName),
          duration_ms: ms,
          duration_text: mmss(ms),
          cover_url: cover,
          source_url: 'https://www.kugou.com/song/#hash=' + String(it.FileHash || '').toLowerCase(),
          extra: {
            direct: true,
            hashes: {
              '128': String(it.FileHash || '').toLowerCase(),
              '320': String(it.HQFileHash || '').toLowerCase(),
              flac: String(it.SQFileHash || '').toLowerCase()
            }
          }
        };
      });
    });
  }

  function searchKuwo(keyword, limit) {
    // 必须用 https：Pages 是 https，加载 http 脚本会被当混合内容拦掉
    var url =
      'https://search.kuwo.cn/r.s?all=' +
      encodeURIComponent(keyword) +
      '&ft=music&itemset=web_2013&client=kt&pn=0&rn=' +
      limit +
      '&rformat=json&encoding=utf8&ver=mbox&vipver=1' +
      '&show_copyright_off=1&pcmp4=1&isbang=1&plt=web&sms_ver=1&module=search&callback=cb';
    return jsonp(url).then(function (data) {
      var list = (data || {}).abslist || [];
      return list.map(function (it) {
        var rid = String(it.DC_TARGETID || '');
        var artist = clean(it.ARTIST) || clean(it.AARTIST);
        var short = String(it.web_albumpic_short || '').trim();
        var ms = (Number(it.DURATION) || 0) * 1000;
        return {
          provider: 'kuwo',
          id: rid,
          title: clean(it.NAME || it.SONGNAME),
          artists: artist ? [artist] : [],
          artist_text: artist || '未知歌手',
          album: clean(it.ALBUM),
          duration_ms: ms,
          duration_text: mmss(ms),
          cover_url: short
            ? 'https://img1.kuwo.cn/star/albumcover/' + short.replace(/^\d+\//, '500/')
            : '',
          source_url: 'https://www.kuwo.cn/play_detail/' + rid,
          extra: { direct: true }
        };
      });
    });
  }

  // 网易云：无 JSONP、无 CORS，纯前端拿不到
  function searchNetease() {
    return Promise.reject(
      new Error('网易云接口不允许跨域也没有 JSONP，纯前端拿不到数据（换 QQ / 酷狗 / 酷我，或配置后端）')
    );
  }

  var SEARCHERS = {
    netease: searchNetease,
    qq: searchQQ,
    kugou: searchKugou,
    kuwo: searchKuwo
  };

  function search(keyword, provider, limit) {
    var names = provider === 'all' ? ['qq', 'kugou', 'kuwo'] : [provider];
    return Promise.all(
      names.map(function (name) {
        var fn = SEARCHERS[name];
        if (!fn) {
          return Promise.resolve({
            provider: name,
            ok: false,
            error: '纯前端模式不支持该平台',
            tracks: []
          });
        }
        return fn(keyword, limit)
          .then(function (tracks) {
            return { provider: name, ok: true, error: '', tracks: tracks };
          })
          .catch(function (err) {
            return {
              provider: name,
              ok: false,
              error: String((err && err.message) || err),
              tracks: []
            };
          });
      })
    ).then(function (groups) {
      return { query: keyword, groups: groups, mode: 'direct' };
    });
  }

  // ---------------------------------------------------------------- 直链

  /** 酷狗：playInfo 支持 JSONP，返回完整 mp3 直链 */
  function kugouStream(track, quality) {
    var hashes = (track.extra || {}).hashes || {};
    var order = ['flac', '320', '128'];
    var start = order.indexOf(quality);
    if (start < 0) start = 1;
    order = order.slice(start);

    function attempt(i) {
      if (i >= order.length) {
        return Promise.reject(new Error('酷狗未返回可下载直链（付费或已下架）'));
      }
      var q = order[i];
      var hash = hashes[q] || (q === '128' ? track.id : '');
      if (!hash) return attempt(i + 1);
      var url =
        'https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&format=jsonp&callback=cb&hash=' + hash;
      return jsonp(url)
        .then(function (data) {
          if (!data || !data.url) return attempt(i + 1);
          var br = Number(data.bitRate) || 128;
          return {
            url: data.url,
            ext: data.extName || 'mp3',
            bitrate: br * 1000,
            quality: br <= 128 ? '128' : br <= 320 ? '320' : 'flac'
          };
        })
        .catch(function () {
          return attempt(i + 1);
        });
    }

    return attempt(0);
  }

  /** 酷我：直链是纯文本，用 fetch 会跨域，改用隐藏 iframe/script 方式拿不到；
   *  这里退回「跳转页由用户手动另存」，所以直接抛出让上层降级。 */
  function kuwoStream(track) {
    return Promise.reject(
      new Error('酷我音频 CDN 不允许跨域，纯前端无法直接保存（配置后端可解决）')
    );
  }

  /** QQ：vkey 要登录 Cookie，匿名拿不到 purl */
  function qqStream(track, quality) {
    var payload = {
      comm: { ct: '24', cv: '0' },
      req_0: {
        module: 'music.vkey.GetVkey',
        method: 'UrlGetVkey',
        param: {
          guid: '10000',
          songmid: [track.id],
          songtype: [0],
          uin: '0',
          loginflag: 1,
          platform: '20',
          filename: ['M500' + track.id + '.mp3']
        }
      }
    };
    var url =
      'https://u.y.qq.com/cgi-bin/musicu.fcg?format=jsonp&callback=cb&data=' +
      encodeURIComponent(JSON.stringify(payload));
    return jsonp(url).then(function (data) {
      var payloadData = ((data || {}).req_0 || {}).data || {};
      var info = (payloadData.midurlinfo || [])[0] || {};
      if (!info.purl) {
        throw new Error('QQ 音乐匿名拿不到 vkey（需要登录 Cookie，或配置后端）');
      }
      var sip = (payloadData.sip || [''])[0];
      var full = info.purl.charAt(0) === '/'
        ? sip.replace(/\/+$/, '') + info.purl
        : info.purl;
      return { url: full, ext: 'mp3', bitrate: 128000, quality: '128' };
    });
  }

  var STREAMERS = {
    kugou: kugouStream,
    qq: qqStream,
    kuwo: kuwoStream,
    netease: function () {
      return Promise.reject(new Error('网易云直链接口不允许跨域，纯前端拿不到'));
    }
  };

  function resolveStream(track, quality) {
    var fn = STREAMERS[track.provider];
    if (!fn) return Promise.reject(new Error('不支持的平台：' + track.provider));
    return fn(track, quality || '320');
  }

  // ---------------------------------------------------------------- 下载

  /**
   * 取直链 -> fetch 成 Blob -> a[download] 保存。
   * 音频 CDN 给了 ACAO:*，所以这一步是允许的。
   */
  function download(track, quality, onProgress) {
    return resolveStream(track, quality)
      .then(function (stream) {
        return fetch(stream.url, { mode: 'cors', credentials: 'omit' })
          .then(function (res) {
            if (!res.ok) throw new Error('音频 CDN 返回 ' + res.status);
            var total = Number(res.headers.get('Content-Length') || 0);
            if (!res.body || typeof res.body.getReader !== 'function') {
              return res.blob().then(function (blob) {
                if (onProgress) onProgress(blob.size, blob.size);
                return { blob: blob, total: blob.size, stream: stream };
              });
            }
            // 流式读取以便报进度
            var reader = res.body.getReader();
            var chunks = [];
            var loaded = 0;
            function pump() {
              return reader.read().then(function (r) {
                if (r.done) {
                  return {
                    blob: new Blob(chunks, { type: 'audio/mpeg' }),
                    total: loaded,
                    stream: stream
                  };
                }
                chunks.push(r.value);
                loaded += r.value.length;
                if (onProgress) onProgress(loaded, total);
                return pump();
              });
            }
            return pump();
          });
      })
      .then(function (result) {
        var expected =
          track.duration_ms > 0
            ? (track.duration_ms / 1000) * (result.stream.bitrate / 8)
            : 0;
        if (expected > 0 && result.total > 0 && result.total < expected * 0.35) {
          // 试听片段不落盘，直接报错，别让用户以为下到了
          throw new Error(
            '只拿到试听片段（' +
              Math.round(result.total / 1024) +
              ' KB，完整约 ' +
              Math.round(expected / 1048576) +
              ' MB），该曲目需要会员'
          );
        }
        var filename =
          safeName((track.artist_text || '未知歌手') + ' - ' + (track.title || 'unknown')) +
          '.' +
          (result.stream.ext || 'mp3');
        saveBlob(result.blob, filename);
        return {
          ok: true,
          file: filename,
          size: result.total,
          quality: result.stream.quality,
          provider: track.provider,
          title: track.title
        };
      });
  }

  function saveBlob(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    // 立刻 revoke 会让部分浏览器来不及取数据
    setTimeout(function () {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 4000);
  }

  // ---------------------------------------------------------------- 解析

  function detectProvider(link) {
    if (/music\.163\.com|163cn\.tv|y\.music\.163\.com/.test(link)) return 'netease';
    if (/y\.qq\.com|qq\.com/.test(link)) return 'qq';
    if (/kugou\.com|kgmusic\.cn/.test(link)) return 'kugou';
    if (/kuwo\.cn/.test(link)) return 'kuwo';
    return '';
  }

  /**
   * 纯前端解析：只支持「按 id 回查」，歌单/专辑展开需要额外接口，
   * 目前 QQ 和酷我都不支持公开列表查询，所以统一按单曲处理。
   */
  function resolve(url) {
    var link = String(url || '').trim();
    var m = link.match(/https?:\/\/[^\s，,。；;”"'）)]+/);
    if (m) link = m[0];
    var provider = detectProvider(link);
    if (!provider) return Promise.reject(new Error('无法识别的链接：' + link.slice(0, 120)));
    if (provider === 'netease') {
      return Promise.reject(
        new Error('网易云接口不允许跨域，纯前端解析不了这个链接（配置后端或用其他平台）')
      );
    }
    if (provider === 'qq') {
      var mid = (link.match(/([0-9A-Za-z]{14})/) || [])[1];
      if (!mid) return Promise.reject(new Error('无法识别 QQ 音乐 songmid'));
      // QQ 没有公开的单曲详情 JSONP，用一个宽松搜索兜底
      return searchQQ(mid, 1).then(function (tracks) {
        if (!tracks.length) throw new Error('QQ 未返回该曲目');
        return {
          title: tracks[0].title,
          provider: 'qq',
          kind: 'song',
          cover_url: tracks[0].cover_url,
          source_url: link,
          count: 1,
          tracks: tracks,
          mode: 'direct'
        };
      });
    }
    if (provider === 'kugou') {
      var hash = (link.match(/([0-9A-Fa-f]{32})/) || [])[1];
      if (!hash) return Promise.reject(new Error('无法识别酷狗 hash'));
      return jsonp(
        'https://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&format=jsonp&callback=cb&hash=' +
          hash.toLowerCase()
      ).then(function (data) {
        if (!data || !data.songName) throw new Error('酷狗未返回该曲目');
        var extra = data.extra || {};
        var ms = Number(data.timeLength) || 0;
        var t = {
          provider: 'kugou',
          id: hash.toLowerCase(),
          title: clean(data.songName),
          artists: [clean(data.author_name || data.singerName)].filter(Boolean),
          artist_text: clean(data.author_name || data.singerName) || '未知歌手',
          album: clean(data.album_name),
          duration_ms: ms,
          duration_text: mmss(ms),
          cover_url: String(data.imgUrl || '').replace('{size}', '480'),
          source_url: link,
          extra: {
            direct: true,
            hashes: {
              '128': String(extra['128hash'] || data.hash || hash).toLowerCase(),
              '320': String(extra['320hash'] || '').toLowerCase(),
              flac: String(extra.sqhash || '').toLowerCase()
            }
          }
        };
        return {
          title: t.title,
          provider: 'kugou',
          kind: 'song',
          cover_url: t.cover_url,
          source_url: link,
          count: 1,
          tracks: [t],
          mode: 'direct'
        };
      });
    }
    if (provider === 'kuwo') {
      var rid = (link.match(/play_detail\/(\d+)|musicId=(\d+)|[?&]rid=(?:MUSIC_)?(\d+)/) || [])
        .slice(1)
        .filter(Boolean)[0];
      if (!rid) return Promise.reject(new Error('无法识别酷我 rid'));
      return Promise.reject(
        new Error('酷我纯前端只能搜索，单曲解析请用后端（音频 CDN 也不允许跨域）')
      );
    }
    return Promise.reject(new Error('不支持的链接'));
  }

  // ---------------------------------------------------------------- 能力声明

  var CAPS = {
    netease: { search: false, download: false, reason: '无 JSONP、无 CORS' },
    qq: { search: true, download: false, reason: 'vkey 需登录 Cookie' },
    kugou: { search: true, download: true, reason: '' },
    kuwo: { search: true, download: false, reason: 'CDN 无 CORS' }
  };

  global.MDLDirect = {
    search: search,
    download: download,
    resolve: resolve,
    resolveStream: resolveStream,
    capabilities: CAPS,
    supports: function (provider) {
      var c = CAPS[provider];
      return !!(c && c.search);
    },
    canDownload: function (provider) {
      var c = CAPS[provider];
      return !!(c && c.download);
    },
    jsonp: jsonp,
    _internals: {
      clean: clean,
      mmss: mmss,
      safeName: safeName,
      parseLoose: parseLoose,
      detectProvider: detectProvider
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);