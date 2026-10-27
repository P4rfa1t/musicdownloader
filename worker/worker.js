/**
 * MusicDL Relay —— 给 GitHub Pages 版前端用的无服务器中继。
 *
 * 为什么需要它：网易云 / QQ音乐 / 酷狗 / 酷我的接口都不带 CORS 头，
 * 浏览器直连会被拦；而且下载要落盘，纯前端也做不到。这个 Worker
 * 把四个平台的接口代理出来，加上 CORS，并在服务端完成取直链和下载。
 *
 * 部署：
 *   1. npm i -g wrangler
 *   2. wrangler login
 *   3. wrangler deploy worker.js --name mdl-relay --compatibility-date 2024-11-01
 *   4. 记下 https://mdl-relay.<你的子域>.workers.dev
 *   5. 打开 Pages 站点，点「⚙ 设置」把它填进去
 *
 * 可选：在 Cloudflare 面板给 Worker 加环境变量 MDL_TOKEN，
 * 之后请求必须带 ?token=xxx 或 X-MDL-Token 头，防止别人白嫖你的额度。
 */

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Max-Age": "86400",
};

const NETEASE = "https://music.163.com";
const MUSICU = "https://u.y.qq.com/cgi-bin/musicu.fcg";
const KUGOU_SEARCH = "https://songsearch.kugou.com/song_search_v2";
const KUGOU_PLAY = "https://m.kugou.com/app/i/getSongInfo.php";
const KUGOU_KRC = "https://krcs.kugou.com/search";
const KUGOU_LYRIC = "https://lyrics.kugou.com/download";
const KUWO_SEARCH = "http://search.kuwo.cn/r.s";
const KUWO_PLAY = "http://nmobi.kuwo.cn/mobi.s";

const BR_MAP = { 128000: "128", 192000: "192", 320000: "320", 999000: "flac" };

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: JSON_HEADERS });
    }

    if (env.MDL_TOKEN && !authorized(request, url, env.MDL_TOKEN)) {
      return json({ detail: "token 校验失败" }, 401);
    }

    try {
      const path = url.pathname.replace(/\/+$/, "");

      if (path === "" || path === "/api/health") {
        return json({
          ok: true,
          version: "1.0.0",
          providers: ["netease", "qq", "kugou", "kuwo"],
          relay: "cloudflare-worker",
          download_dir: "（中继模式不落盘，文件直接回传浏览器）",
        });
      }

      if (path === "/api/search") {
        return await handleSearch(url);
      }

      if (path === "/api/resolve") {
        return await handleResolve(request);
      }

      if (path === "/api/download") {
        return await handleDownload(request, url);
      }

      if (path === "/api/lyric") {
        return await handleLyric(url);
      }

      // 中继没有持久任务表，批量交给前端串行降级
      if (path === "/api/download/batch" || path.startsWith("/api/jobs")) {
        return json(
          { detail: "中继模式不支持批量任务，请用单曲接口串行下载" },
          501
        );
      }

      return json({ detail: `未知路径: ${path}` }, 404);
    } catch (err) {
      return json({ detail: `中继异常: ${err && err.message ? err.message : err}` }, 502);
    }
  },
};

function authorized(request, url, token) {
  const q = url.searchParams.get("token");
  const h = request.headers.get("X-MDL-Token");
  return q === token || h === token;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

// ------------------------------------------------------------------ 搜索

async function handleSearch(url) {
  const q = (url.searchParams.get("q") || "").trim();
  const provider = url.searchParams.get("provider") || "all";
  const limit = Math.min(Number(url.searchParams.get("limit") || 20), 50);
  if (!q) return json({ detail: "缺少 q 参数" }, 400);

  const names =
    provider === "all" ? ["netease", "qq", "kugou", "kuwo"] : [provider];

  const groups = await Promise.all(
    names.map(async (name) => {
      try {
        const tracks = await searchOne(name, q, limit);
        return { provider: name, ok: true, error: "", tracks };
      } catch (err) {
        return {
          provider: name,
          ok: false,
          error: String((err && err.message) || err),
          tracks: [],
        };
      }
    })
  );

  return json({ query: q, groups });
}

async function searchOne(name, q, limit) {
  switch (name) {
    case "netease":
      return searchNetease(q, limit);
    case "qq":
      return searchQQ(q, limit);
    case "kugou":
      return searchKugou(q, limit);
    case "kuwo":
      return searchKuwo(q, limit);
    default:
      throw new Error(`不支持的平台: ${name}`);
  }
}

async function searchNetease(q, limit) {
  const api = `${NETEASE}/api/search/get/web?s=${encodeURIComponent(
    q
  )}&type=1&limit=${limit}&offset=0`;
  const data = await fetchJson(api, {
    headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" },
  });
  const songs = (data.result && data.result.songs) || [];
  const tracks = songs.map((s) => ({
    provider: "netease",
    id: String(s.id),
    title: clean(s.name),
    artists: (s.artists || []).map((a) => clean(a.name)),
    artist_text: (s.artists || []).map((a) => clean(a.name)).join(" / "),
    album: clean((s.album && s.album.name) || ""),
    duration_ms: Number(s.duration) || 0,
    duration_text: mmss(Number(s.duration) || 0),
    cover_url: (s.album && s.album.picUrl) || "",
    source_url: `${NETEASE}/song?id=${s.id}`,
    extra: {},
  }));
  // 搜索接口不给封面，补一次批量详情
  if (tracks.length) {
    try {
      const ids = tracks.map((t) => ({ id: Number(t.id) }));
      const detail = await fetchJson(
        `${NETEASE}/api/v3/song/detail?c=${encodeURIComponent(JSON.stringify(ids))}`,
        { headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" } }
      );
      const byId = {};
      (detail.songs || []).forEach((s) => {
        byId[String(s.id)] = s;
      });
      tracks.forEach((t) => {
        const s = byId[t.id];
        if (!s) return;
        const al = s.al || {};
        t.cover_url = t.cover_url || al.picUrl || "";
        t.album = t.album || clean(al.name || "");
        if (!t.duration_ms) {
          t.duration_ms = Number(s.dt) || 0;
          t.duration_text = mmss(t.duration_ms);
        }
      });
    } catch (e) {
      /* 封面补全失败不影响搜索 */
    }
  }
  return tracks;
}

async function searchQQ(q, limit) {
  const payload = {
    comm: { ct: "19", cv: "1859", uin: "0" },
    req: {
      module: "music.search.SearchCgiService",
      method: "DoSearchForQQMusicDesktop",
      param: {
        query: q,
        num_per_page: limit,
        page_num: 1,
        search_type: 0,
        grp: 1,
      },
    },
  };
  const data = await fetchJson(MUSICU, {
    method: "POST",
    headers: {
      "User-Agent": UA,
      Referer: "https://y.qq.com/",
      Origin: "https://y.qq.com",
      "Content-Type": "application/json;charset=UTF-8",
    },
    body: JSON.stringify(payload),
  });
  const body = (((data.req || {}).data || {}).body || {}).song || {};
  const list = body.list || [];
  return list.map((s) => {
    const albumMid = (s.album && s.album.mid) || "";
    return {
      provider: "qq",
      id: s.mid,
      title: clean(s.name || s.title || ""),
      artists: (s.singer || []).map((x) => clean(x.name)),
      artist_text: (s.singer || []).map((x) => clean(x.name)).join(" / "),
      album: clean((s.album && (s.album.name || s.album.title)) || ""),
      duration_ms: (Number(s.interval) || 0) * 1000,
      duration_text: mmss((Number(s.interval) || 0) * 1000),
      cover_url: albumMid
        ? `https://y.gtimg.cn/music/photo_new/T002R300x300M000${albumMid}.jpg`
        : "",
      source_url: `https://y.qq.com/n/ryqq/songDetail/${s.mid}`,
      extra: { album_mid: albumMid },
    };
  });
}

async function searchKugou(q, limit) {
  const api =
    `${KUGOU_SEARCH}?keyword=${encodeURIComponent(q)}&page=1&pagesize=${limit}` +
    `&platform=WebFilter&tag=em&filter=2&iscorrection=1&privilege_filter=0`;
  const data = await fetchJson(api, {
    headers: { "User-Agent": UA, Referer: "https://www.kugou.com/" },
  });
  const list = ((data.data || {}).lists) || [];
  return list.map((it) => {
    const hashes = {
      128: (it.FileHash || "").toLowerCase(),
      320: (it.HQFileHash || "").toLowerCase(),
      flac: (it.SQFileHash || "").toLowerCase(),
    };
    let cover = it.Image || it.AlbumImage || "";
    cover = cover.replace("{size}", "480");
    return {
      provider: "kugou",
      id: (it.FileHash || "").toLowerCase(),
      title: clean(it.SongName || ""),
      artists: (it.Singers || []).map((s) => clean(s.name)).filter(Boolean),
      artist_text:
        (it.Singers || [])
          .map((s) => clean(s.name))
          .filter(Boolean)
          .join(" / ") || clean(it.SingerName || ""),
      album: clean(it.AlbumName || ""),
      duration_ms: (Number(it.Duration) || 0) * 1000,
      duration_text: mmss((Number(it.Duration) || 0) * 1000),
      cover_url: cover,
      source_url: `https://www.kugou.com/song/#hash=${hashes[128]}`,
      extra: { hashes },
    };
  });
}

async function searchKuwo(q, limit) {
  const api =
    `${KUWO_SEARCH}?all=${encodeURIComponent(q)}&ft=music&itemset=web_2013&client=kt` +
    `&pn=0&rn=${limit}&rformat=json&encoding=utf8&ver=mbox&vipver=1` +
    `&show_copyright_off=1&pcmp4=1&isbang=1&plt=web&sms_ver=1&module=search`;
  const text = await fetchText(api, {
    headers: { "User-Agent": UA, Referer: "http://www.kuwo.cn/" },
  });
  const data = parseLooseJson(text);
  const list = data.abslist || [];
  return list.map((it) => {
    const rid = String(it.DC_TARGETID || String(it.MUSICRID || "").replace("MUSIC_", ""));
    const artist = clean(it.ARTIST || "") || clean(it.AARTIST || "");
    const short = (it.web_albumpic_short || "").trim();
    const cover = short
      ? `https://img1.kuwo.cn/star/albumcover/${short.replace(/^\d+\//, "500/")}`
      : "";
    return {
      provider: "kuwo",
      id: rid,
      title: clean(it.NAME || it.SONGNAME || ""),
      artists: artist ? [artist] : [],
      artist_text: artist || "未知歌手",
      album: clean(it.ALBUM || ""),
      duration_ms: (Number(it.DURATION) || 0) * 1000,
      duration_text: mmss((Number(it.DURATION) || 0) * 1000),
      cover_url: cover,
      source_url: `https://www.kuwo.cn/play_detail/${rid}`,
      extra: {},
    };
  });
}

// ------------------------------------------------------------------ 解析

async function handleResolve(request) {
  const body = await request.json().catch(() => ({}));
  const raw = String(body.url || "").trim();
  if (!raw) return json({ detail: "缺少 url" }, 400);

  const m = raw.match(/https?:\/\/[^\s，,。；;”"'）)]+/);
  const link = m ? m[0] : raw;
  const provider = detectProvider(link);
  if (!provider) return json({ detail: `无法识别的链接: ${link.slice(0, 120)}` }, 400);

  if (provider === "netease") {
    const pl = link.match(/playlist\?id=(\d+)|toplist\?id=(\d+)/);
    if (pl) {
      const id = pl[1] || pl[2];
      const data = await fetchJson(
        `${NETEASE}/api/v6/playlist/detail?id=${id}&n=1000`,
        { headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" } }
      );
      const playlist = data.playlist || {};
      const tracks = (playlist.tracks || []).map((t) => neteaseTrack(t));
      if (!tracks.length) return json({ detail: "歌单为空或不存在" }, 400);
      return json({
        title: playlist.name || `歌单 ${id}`,
        provider: "netease",
        kind: "playlist",
        cover_url: playlist.coverImgUrl || "",
        source_url: link,
        count: tracks.length,
        tracks,
      });
    }
    const alb = link.match(/album\?id=(\d+)/);
    if (alb) {
      const data = await fetchJson(`${NETEASE}/api/album/${alb[1]}`, {
        headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" },
      });
      const album = data.album || {};
      const tracks = (album.songs || []).map((s) => ({
        provider: "netease",
        id: String(s.id),
        title: clean(s.name || ""),
        artists: (s.artists || []).map((a) => clean(a.name)),
        artist_text: (s.artists || []).map((a) => clean(a.name)).join(" / "),
        album: clean(album.name || ""),
        duration_ms: Number(s.duration) || 0,
        duration_text: mmss(Number(s.duration) || 0),
        cover_url: album.picUrl || "",
        source_url: `${NETEASE}/song?id=${s.id}`,
        extra: {},
      }));
      if (!tracks.length) return json({ detail: "专辑为空或不存在" }, 400);
      return json({
        title: album.name || `专辑 ${alb[1]}`,
        provider: "netease",
        kind: "album",
        cover_url: album.picUrl || "",
        source_url: link,
        count: tracks.length,
        tracks,
      });
    }
    const sid = link.match(/song\?id=(\d+)|[?&]id=(\d+)|^(\d{3,})$/);
    const id = sid ? sid[1] || sid[2] || sid[3] : "";
    if (!id) return json({ detail: "无法识别网易云曲目 id" }, 400);
    const data = await fetchJson(`${NETEASE}/api/song/detail/?ids=[${id}]`, {
      headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" },
    });
    const s = (data.songs || [])[0];
    if (!s) return json({ detail: "曲目不存在" }, 400);
    const t = neteaseTrack(s);
    return json({
      title: t.title,
      provider: "netease",
      kind: "song",
      cover_url: t.cover_url,
      source_url: link,
      count: 1,
      tracks: [t],
    });
  }

  if (provider === "qq") {
    const mid = (link.match(/songDetail\/([0-9A-Za-z]+)|songmid=([0-9A-Za-z]+)/) || [])
      .slice(1)
      .filter(Boolean)[0];
    if (!mid) return json({ detail: "无法识别 QQ 音乐 songmid" }, 400);
    const data = await fetchJson(MUSICU, {
      method: "POST",
      headers: {
        "User-Agent": UA,
        Referer: "https://y.qq.com/",
        Origin: "https://y.qq.com",
        "Content-Type": "application/json;charset=UTF-8",
      },
      body: JSON.stringify({
        comm: { ct: "24", cv: "0" },
        req_1: {
          module: "music.pf_song_detail_svr",
          method: "get_song_detail",
          param: { song_mid: mid },
        },
      }),
    });
    const info = (((data.req_1 || {}).data || {}).track_info) || {};
    if (!info.mid) return json({ detail: "曲目不存在" }, 400);
    const albumMid = (info.album && info.album.mid) || "";
    const t = {
      provider: "qq",
      id: info.mid,
      title: clean(info.name || info.title || ""),
      artists: (info.singer || []).map((x) => clean(x.name)),
      artist_text: (info.singer || []).map((x) => clean(x.name)).join(" / "),
      album: clean((info.album && info.album.name) || ""),
      duration_ms: (Number(info.interval) || 0) * 1000,
      duration_text: mmss((Number(info.interval) || 0) * 1000),
      cover_url: albumMid
        ? `https://y.gtimg.cn/music/photo_new/T002R300x300M000${albumMid}.jpg`
        : "",
      source_url: link,
      extra: { album_mid: albumMid, file: info.file || {} },
    };
    return json({
      title: t.title,
      provider: "qq",
      kind: "song",
      cover_url: t.cover_url,
      source_url: link,
      count: 1,
      tracks: [t],
    });
  }

  if (provider === "kugou") {
    const hash = (link.match(/([0-9A-Fa-f]{32})/) || [])[1];
    if (!hash) return json({ detail: "无法识别酷狗 hash" }, 400);
    const t = await kugouTrack(hash);
    return json({
      title: t.title,
      provider: "kugou",
      kind: "song",
      cover_url: t.cover_url,
      source_url: link,
      count: 1,
      tracks: [t],
    });
  }

  if (provider === "kuwo") {
    const rid = (link.match(/play_detail\/(\d+)|musicId=(\d+)|[?&]rid=(?:MUSIC_)?(\d+)|^(\d{4,})$/g) || [])
      .map((x) => (x.match(/\d+/) || [])[0])
      .filter(Boolean)[0];
    if (!rid) return json({ detail: "无法识别酷我 rid" }, 400);
    const t = {
      provider: "kuwo",
      id: rid,
      title: `kuwo-${rid}`,
      artists: [],
      artist_text: "未知歌手",
      album: "",
      duration_ms: 0,
      duration_text: "--:--",
      cover_url: "",
      source_url: link,
      extra: {},
    };
    return json({
      title: t.title,
      provider: "kuwo",
      kind: "song",
      cover_url: "",
      source_url: link,
      count: 1,
      tracks: [t],
    });
  }

  return json({ detail: "不支持的链接" }, 400);
}

function neteaseTrack(s) {
  const al = s.al || s.album || {};
  const ar = s.ar || s.artists || [];
  const ms = Number(s.dt || s.duration || 0);
  return {
    provider: "netease",
    id: String(s.id),
    title: clean(s.name || ""),
    artists: ar.map((a) => clean(a.name)),
    artist_text: ar.map((a) => clean(a.name)).join(" / "),
    album: clean(al.name || ""),
    duration_ms: ms,
    duration_text: mmss(ms),
    cover_url: al.picUrl || "",
    source_url: `${NETEASE}/song?id=${s.id}`,
    extra: {},
  };
}

async function kugouTrack(hash) {
  const data = await fetchJson(
    `${KUGOU_PLAY}?cmd=playInfo&hash=${hash}`,
    { headers: { "User-Agent": UA, Referer: "https://m.kugou.com/" } }
  );
  const extra = data.extra || {};
  const hashes = {
    128: (extra["128hash"] || data.hash || "").toLowerCase(),
    320: (extra["320hash"] || "").toLowerCase(),
    flac: (extra.sqhash || "").toLowerCase(),
  };
  return {
    provider: "kugou",
    id: hash.toLowerCase(),
    title: clean(data.songName || ""),
    artists: [clean(data.author_name || data.singerName || "")].filter(Boolean),
    artist_text: clean(data.author_name || data.singerName || "") || "未知歌手",
    album: clean(data.album_name || ""),
    duration_ms: Number(data.timeLength) || 0,
    duration_text: mmss(Number(data.timeLength) || 0),
    cover_url: (data.imgUrl || "").replace("{size}", "480"),
    source_url: `https://www.kugou.com/song/#hash=${hash.toLowerCase()}`,
    extra: { hashes },
  };
}

// ------------------------------------------------------------------ 下载

/**
 * 中继模式下载：服务端取直链后，把音频流原样回传浏览器。
 * 前端用 blob 接收并触发保存，所以中继不落盘、不占存储。
 */
async function handleDownload(request, url) {
  const body = await request.json().catch(() => ({}));
  const provider = String(body.provider || "");
  const id = String(body.id || "");
  const quality = String(body.quality || "320");
  if (!provider || !id) return json({ detail: "缺少 provider 或 id" }, 400);

  const title = String(body.title || id);
  const artistText = Array.isArray(body.artists)
    ? body.artists.filter(Boolean).join(" / ")
    : String(body.artist_text || "未知歌手");
  const durationMs = Number(body.duration_ms) || 0;

  let stream;
  try {
    stream = await resolveStream(provider, id, quality, body);
  } catch (err) {
    return json({ detail: String((err && err.message) || err) }, 502);
  }

  const audio = await fetch(stream.url, {
    headers: { "User-Agent": UA, Referer: stream.referer || "" },
  });
  if (!audio.ok) {
    return json({ detail: `音频 CDN 返回 ${audio.status}` }, 502);
  }

  const length = Number(audio.headers.get("Content-Length") || 0);
  const expected = durationMs > 0 ? (durationMs / 1000) * (stream.bitrate / 8) : 0;
  if (expected > 0 && length > 0 && length < expected * 0.35) {
    return json(
      {
        detail: `只拿到试听片段（${Math.round(
          length / 1024
        )} KB，完整约 ${Math.round(expected / 1048576)} MB），该曲目需要会员或登录 Cookie`,
      },
      502
    );
  }

  const filename = safeName(`${artistText} - ${title}`) + "." + stream.ext;
  const headers = new Headers(JSON_HEADERS);
  headers.set(
    "Content-Type",
    stream.ext === "mp3" ? "audio/mpeg" : "application/octet-stream"
  );
  headers.set("Content-Disposition", `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`);
  headers.set("X-MDL-Filename", encodeURIComponent(filename));
  headers.set("X-MDL-Quality", stream.quality);
  headers.set("X-MDL-Size", String(length));
  if (length) headers.set("Content-Length", String(length));

  return new Response(audio.body, { status: 200, headers });
}

async function resolveStream(provider, id, quality, body) {
  const order = ["flac", "320", "128"];
  const start = order.indexOf(quality) >= 0 ? order.indexOf(quality) : 1;

  if (provider === "netease") {
    for (const q of order.slice(start)) {
      const br = { flac: 999000, 320: 320000, 128: 128000 }[q];
      const data = await fetchJson(
        `${NETEASE}/api/song/enhance/player/url?ids=[${id}]&br=${br}`,
        { headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" } }
      );
      const item = (data.data || [])[0];
      if (!item || !item.url || item.freeTrialInfo) continue;
      return {
        url: item.url,
        ext: item.type || "mp3",
        bitrate: Number(item.br) || br,
        quality: BR_MAP[Number(item.br)] || q,
        referer: `${NETEASE}/`,
      };
    }
    throw new Error("网易云未返回可下载直链（会员曲目或版权受限）");
  }

  if (provider === "qq") {
    for (const q of order.slice(start)) {
      const prefix = { flac: "F000", 320: "M800", 128: "M500" }[q];
      const ext = q === "flac" ? "flac" : "m4a";
      const data = await fetchJson(MUSICU, {
        method: "POST",
        headers: {
          "User-Agent": UA,
          Referer: "https://y.qq.com/",
          Origin: "https://y.qq.com",
          "Content-Type": "application/json;charset=UTF-8",
        },
        body: JSON.stringify({
          comm: { ct: "24", cv: "0" },
          req_0: {
            module: "music.vkey.GetVkey",
            method: "UrlGetVkey",
            param: {
              guid: "10000",
              songmid: [id],
              songtype: [q === "flac" ? 2 : 0],
              uin: "0",
              loginflag: 1,
              platform: "20",
              filename: [`${prefix}${id}.${ext}`],
            },
          },
        }),
      });
      const payload = (data.req_0 || {}).data || {};
      const info = (payload.midurlinfo || [])[0];
      if (!info || !info.purl) continue;
      const sip = (payload.sip || [""])[0];
      const full = info.purl.startsWith("http")
        ? info.purl
        : info.purl.startsWith("/")
        ? sip.replace(/\/+$/, "") + info.purl
        : `https://dl.stream.qqmusic.qq.com/${info.purl.replace(/^\/+/, "")}`;
      return { url: full, ext, bitrate: { flac: 999000, 320: 320000, 128: 128000 }[q], quality: q, referer: "https://y.qq.com/" };
    }
    throw new Error("QQ 音乐未返回 vkey，匿名请求拿不到直链（需要登录 Cookie）");
  }

  if (provider === "kugou") {
    const hashes = (body.extra && body.extra.hashes) || {};
    for (const q of order) {
      const hash = hashes[q] || (q === "128" ? id : "");
      if (!hash) continue;
      const data = await fetchJson(`${KUGOU_PLAY}?cmd=playInfo&hash=${hash}`, {
        headers: { "User-Agent": UA, Referer: "https://m.kugou.com/" },
      });
      if (!data.url) continue;
      const br = Number(data.bitRate) || 128;
      return {
        url: data.url,
        ext: data.extName || "mp3",
        bitrate: br * 1000,
        quality: br <= 128 ? "128" : br <= 320 ? "320" : "flac",
        referer: "https://m.kugou.com/",
      };
    }
    throw new Error("酷狗未返回可下载直链（付费或已下架）");
  }

  if (provider === "kuwo") {
    for (const q of ["320", "128"]) {
      const br = { 320: "320kmp3", 128: "128kmp3" }[q];
      const text = await fetchText(
        `${KUWO_PLAY}?f=web&type=convert_url2&br=${br}&sig=0&format=mp3&rid=${id}`,
        { headers: { "User-Agent": UA, Referer: "http://www.kuwo.cn/" } }
      );
      const m = text.match(/url=(http[^\s\r\n]+)/);
      if (!m) continue;
      return {
        url: m[1].trim(),
        ext: "mp3",
        bitrate: q === "320" ? 320000 : 128000,
        quality: q,
        referer: "http://www.kuwo.cn/",
      };
    }
    throw new Error("酷我未返回可用直链");
  }

  throw new Error(`不支持的平台: ${provider}`);
}

// ------------------------------------------------------------------ 歌词

async function handleLyric(url) {
  const provider = url.searchParams.get("provider") || "";
  const id = url.searchParams.get("id") || "";
  if (!provider || !id) return json({ detail: "缺少 provider 或 id" }, 400);

  try {
    let text = "";
    if (provider === "netease") {
      const data = await fetchJson(
        `${NETEASE}/api/song/lyric?id=${id}&lv=1&kv=1&tv=-1`,
        { headers: { "User-Agent": UA, Referer: `${NETEASE}/`, Cookie: "os=pc; appver=2.9.7" } }
      );
      text = ((data.lrc || {}).lyric) || "";
    } else if (provider === "kugou") {
      const found = await fetchJson(
        `${KUGOU_KRC}?ver=1&man=yes&client=mobi&keyword=&duration=0&hash=${id}`,
        { headers: { "User-Agent": UA, Referer: "https://www.kugou.com/" } }
      );
      const cand = (found.candidates || [])[0];
      if (cand) {
        const data = await fetchJson(
          `${KUGOU_LYRIC}?ver=1&client=pc&id=${cand.id}&accesskey=${cand.accesskey}&fmt=lrc&charset=utf8`,
          { headers: { "User-Agent": UA, Referer: "https://www.kugou.com/" } }
        );
        text = atob(data.content || "");
      }
    } else if (provider === "qq") {
      const data = await fetchJson(MUSICU, {
        method: "POST",
        headers: {
          "User-Agent": UA,
          Referer: "https://y.qq.com/",
          Origin: "https://y.qq.com",
          "Content-Type": "application/json;charset=UTF-8",
        },
        body: JSON.stringify({
          comm: { ct: "24", cv: "0" },
          req_2: {
            module: "music.musichallSong.PlayLyricInfo",
            method: "GetPlayLyricInfo",
            param: { songMID: id, songID: 0 },
          },
        }),
      });
      const payload = (data.req_2 || {}).data || {};
      text = payload.lyric ? atob(payload.lyric) : "";
    }
    return json({ lyric: text });
  } catch (err) {
    return json({ lyric: "", detail: String((err && err.message) || err) }, 200);
  }
}

// ------------------------------------------------------------------ 工具

async function fetchJson(target, init) {
  const text = await fetchText(target, init);
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new Error(`响应不是 JSON: ${text.slice(0, 80)}`);
  }
}

async function fetchText(target, init) {
  const res = await fetch(target, init);
  if (!res.ok) throw new Error(`上游 HTTP ${res.status}`);
  return res.text();
}

function clean(s) {
  return String(s || "")
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .trim();
}

function mmss(ms) {
  if (!ms || ms <= 0) return "--:--";
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function safeName(name) {
  return String(name || "unknown")
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120) || "unknown";
}

function parseLooseJson(text) {
  const raw = String(text || "").trim();
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch (e) {
    /* 酷我返回单引号 JSON */
  }
  const fixed = raw.replace(/'/g, '"');
  try {
    return JSON.parse(fixed);
  } catch (e) {
    return {};
  }
}

function detectProvider(link) {
  if (/music\.163\.com|163cn\.tv|y\.music\.163\.com/.test(link)) return "netease";
  if (/y\.qq\.com|qq\.com/.test(link)) return "qq";
  if (/kugou\.com|kgmusic\.cn/.test(link)) return "kugou";
  if (/kuwo\.cn/.test(link)) return "kuwo";
  return "";
}
