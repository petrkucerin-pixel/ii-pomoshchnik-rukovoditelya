// Мост «аватар ↔ Телемост» (TASK_11). Встраивается в страницу Телемоста до её скриптов.
// Телемосту: камера — холст с аватаром (до старта — заставка), микрофон — голос аватара.
// Аватару: «микрофон» — смесь звука остальных участников встречи. Свой голос аватар не слышит.
(() => {
  "use strict";
  const W = 640, H = 360;
  const bridge = window.__bridge = {transcript: [], log: [], state: "init", avatarPCs: new WeakSet()};
  const log = m => { bridge.log.push(new Date().toISOString().slice(11, 19) + " " + m); };

  let ctx = null, meetingDest = null, avatarDest = null, canvas = null, g = null, video = null;
  function audio() {
    if (!ctx) {
      ctx = new AudioContext();
      meetingDest = ctx.createMediaStreamDestination();
      avatarDest = ctx.createMediaStreamDestination();
      // тихий генератор держит дорожки живыми, пока никто не говорит
      const osc = ctx.createOscillator(), mute = ctx.createGain();
      mute.gain.value = 0;
      osc.connect(mute); mute.connect(meetingDest); mute.connect(avatarDest); osc.start();
    }
    if (ctx.state !== "running") ctx.resume().catch(() => {});
    return ctx;
  }
  function screen() {
    if (canvas) return canvas;
    canvas = document.createElement("canvas");
    canvas.width = W; canvas.height = H;
    g = canvas.getContext("2d");
    const draw = () => {
      if (video && video.readyState >= 2 && video.videoWidth) {
        const k = Math.max(W / video.videoWidth, H / video.videoHeight);
        const w = video.videoWidth * k, h = video.videoHeight * k;
        g.drawImage(video, (W - w) / 2, (H - h) / 2, w, h);
      } else {
        const grd = g.createLinearGradient(0, 0, W, H);
        grd.addColorStop(0, "#c9d8f2"); grd.addColorStop(1, "#7f9fdc");
        g.fillStyle = grd; g.fillRect(0, 0, W, H);
        g.fillStyle = "#13407e"; g.font = "600 30px sans-serif"; g.textAlign = "center";
        g.fillText("ИИ-помощник", W / 2, H / 2 - 6);
        g.font = "18px sans-serif";
        g.fillText(bridge.state === "error" ? "аватар недоступен" : "подключается…", W / 2, H / 2 + 26);
      }
    };
    setInterval(draw, 66);       // 15 кадров — для созвона достаточно, процессор сервера свободнее
    draw();
    return canvas;
  }

  // ---- звук встречи: перехватываем входящие дорожки Телемоста ----
  const NativePC = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new NativePC(...args);
    if (bridge.state === "starting") bridge.avatarPCs.add(pc);      // соединения SDK аватара — их звук не слушаем
    pc.addEventListener("track", ev => {
      if (ev.track.kind !== "audio" || bridge.avatarPCs.has(pc)) return;
      const src = audio().createMediaStreamSource(new MediaStream([ev.track]));
      src.connect(meetingDest);
      log("звук участника подключён к аватару");
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = NativePC.prototype;
  Object.setPrototypeOf(window.RTCPeerConnection, NativePC);

  // ---- камера и микрофон для Телемоста и для SDK ----
  const md = navigator.mediaDevices;
  const nativeGUM = md.getUserMedia.bind(md);
  md.getUserMedia = async (c = {}) => {
    audio();
    // после старта аватара «только микрофон» просит SDK аватара: даём ему звук встречи, а не его собственный голос
    if (bridge.started && c.audio && !c.video) {
      log("аватар получил звук встречи");
      return new MediaStream([meetingDest.stream.getAudioTracks()[0].clone()]);
    }
    const tracks = [];
    if (c.video) tracks.push(screen().captureStream(15).getVideoTracks()[0]);
    if (c.audio) tracks.push(avatarDest.stream.getAudioTracks()[0].clone());
    log("Телемост получил камеру/микрофон аватара: " + tracks.map(t => t.kind).join(","));
    return tracks.length ? new MediaStream(tracks) : nativeGUM(c);
  };
  const dev = (kind, id) => ({deviceId: id, groupId: "ii", kind, label: "ИИ-помощник", toJSON() { return this; }});
  md.enumerateDevices = async () => [dev("audioinput", "ii-mic"), dev("videoinput", "ii-cam"), dev("audiooutput", "default")];

  // ---- запуск аватара (вызывает робот после входа во встречу) ----
  bridge.start = async (token) => {
    try {
      audio(); screen();
      bridge.state = "starting";
      bridge.started = true;
      const sdk = window.LiveAvatarSDK;
      const s = bridge.session = new sdk.LiveAvatarSession(token, {autoKeepAlive: true});
      s.on(sdk.SessionEvent.SESSION_STREAM_READY, () => {
        video = document.createElement("video");
        video.muted = true; video.playsInline = true; video.autoplay = true;
        // LiveKit присылает видео только для видимого элемента и в размере элемента:
        // кладём его во весь кадр, но почти прозрачным и без нажатий
        Object.assign(video.style, {position: "fixed", left: "0", top: "0", width: W + "px", height: H + "px",
          opacity: "0.01", pointerEvents: "none", zIndex: "2147483647"});
        document.body.appendChild(video);
        s.attach(video);
        video.play().catch(() => {});
        const a = video.srcObject && video.srcObject.getAudioTracks()[0];
        if (a) audio().createMediaStreamSource(new MediaStream([a])).connect(avatarDest);
        bridge.state = "live";
        log("видео и голос аватара идут в Телемост");
        let checks = 0;
        const probe = setInterval(() => {
          const tr = video.srcObject ? video.srcObject.getTracks().map(t => t.kind + (t.muted ? ":muted" : "") + ":" + t.readyState).join(",") : "нет";
          log("видео аватара: " + video.videoWidth + "x" + video.videoHeight + " готовность " + video.readyState + " дорожки " + tr);
          if (++checks >= 3 || video.videoWidth) clearInterval(probe);
        }, 4000);
      });
      s.on(sdk.SessionEvent.SESSION_DISCONNECTED, r => { bridge.state = "ended"; log("сессия аватара закрыта: " + r); });
      const say = role => e => { if (e && e.text) bridge.transcript.push({role, text: e.text, t: Date.now()}); };
      s.on(sdk.AgentEventsEnum.USER_TRANSCRIPTION, say("user"));
      s.on(sdk.AgentEventsEnum.AVATAR_TRANSCRIPTION, say("avatar"));
      await s.start();
      if (bridge.state === "starting") bridge.state = "connected";
      log("сессия аватара запущена");
    } catch (e) {
      bridge.state = "error";
      log("ошибка запуска аватара: " + (e && e.message || e));
    }
  };
  bridge.stop = async () => { try { if (bridge.session) await bridge.session.stop(); } catch (e) { log("stop: " + e); } };
})();
