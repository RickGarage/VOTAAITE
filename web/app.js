(function () {
  "use strict";

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  async function fetchConfig() {
    const res = await fetch("api/config", { cache: "no-store" });
    return res.json();
  }

  async function measureClockOffset() {
    let best = { rtt: Infinity, offset: 0 };
    for (let i = 0; i < 5; i++) {
      const t0 = Date.now();
      let data;
      try {
        const res = await fetch("api/sync", { cache: "no-store" });
        data = await res.json();
      } catch (err) {
        break;
      }
      const t1 = Date.now();
      const rtt = t1 - t0;
      const offset = data.t - (t0 + rtt / 2);
      if (rtt < best.rtt) best = { rtt: rtt, offset: offset };
      await sleep(120);
    }
    return best.offset;
  }

  function SyncPlayer(media, url, options) {
    this.media = media;
    this.url = url;
    this.options = options || {};
    this.hls = null;
    this.offset = 0;
    this.activeFrag = null;
    this.autoBufferMs = this.options.bufferMs || 6000;
    this.userOffsetMs = this.options.userOffsetMs || 0;
    this.timer = null;
    this.onStatus = this.options.onStatus || function () {};
    this.native = false;
  }

  SyncPlayer.prototype.bufferMs = function () {
    return clamp(this.autoBufferMs + this.userOffsetMs, 500, 30000);
  };

  SyncPlayer.prototype.serverNow = function () {
    return Date.now() + this.offset;
  };

  SyncPlayer.prototype.details = function () {
    if (!this.hls || !this.hls.levels || !this.hls.levels.length) return null;
    for (let i = 0; i < this.hls.levels.length; i++) {
      const level = this.hls.levels[i];
      if (level.details && level.details.fragments && level.details.fragments.length) {
        return level.details;
      }
    }
    return null;
  };

  SyncPlayer.prototype.currentContentEpoch = function () {
    const frag = this.activeFrag;
    if (!frag || frag.programDateTime == null || frag.start == null) return null;
    return frag.programDateTime + (this.media.currentTime - frag.start) * 1000;
  };

  SyncPlayer.prototype.liveEdgeEpoch = function () {
    const details = this.details();
    if (!details || !details.fragments.length) return null;
    const last = details.fragments[details.fragments.length - 1];
    if (last.programDateTime == null) return null;
    return last.programDateTime + last.duration * 1000;
  };

  SyncPlayer.prototype.latencySeconds = function () {
    if (this.hls && typeof this.hls.latency === "number" && isFinite(this.hls.latency)) {
      return Math.max(0, this.hls.latency);
    }
    const seekable = this.media.seekable;
    if (this.native && seekable && seekable.length) {
      return Math.max(0, seekable.end(seekable.length - 1) - this.media.currentTime);
    }
    const details = this.details();
    if (details && details.fragments.length) {
      const last = details.fragments[details.fragments.length - 1];
      return Math.max(0, last.start + last.duration - this.media.currentTime);
    }
    return null;
  };

  SyncPlayer.prototype.targetContent = function () {
    const edge = this.liveEdgeEpoch();
    if (edge != null) return edge - this.bufferMs();
    return this.serverNow() - this.bufferMs();
  };

  SyncPlayer.prototype.seekToContent = function (targetEpoch) {
    const details = this.details();
    if (!details) return false;
    const frags = details.fragments;
    for (let i = frags.length - 1; i >= 0; i--) {
      const frag = frags[i];
      if (frag.programDateTime == null) continue;
      const end = frag.programDateTime + frag.duration * 1000;
      if (targetEpoch >= frag.programDateTime && targetEpoch < end) {
        this.media.currentTime = frag.start + (targetEpoch - frag.programDateTime) / 1000;
        return true;
      }
    }
    return false;
  };

  SyncPlayer.prototype.seekToTarget = function () {
    this.seekToContent(this.targetContent());
  };

  SyncPlayer.prototype.tick = function () {
    if (this.media.paused || this.native) return;
    const target = this.targetContent();
    const current = this.currentContentEpoch();
    if (current == null) return;
    const drift = current - target;
    if (Math.abs(drift) > 1500) {
      this.seekToContent(target);
      this.media.playbackRate = 1;
    } else if (Math.abs(drift) > 120) {
      this.media.playbackRate = clamp(1 - drift / 4000, 0.92, 1.08);
    } else {
      this.media.playbackRate = 1;
    }
    const edge = this.liveEdgeEpoch();
    this.onStatus({
      drift: drift,
      latency: edge != null ? edge - current : this.serverNow() - current,
      bufferMs: this.bufferMs(),
      offsetMs: this.userOffsetMs,
      autoBufferMs: this.autoBufferMs,
      rate: this.media.playbackRate,
    });
  };

  SyncPlayer.prototype.resync = async function () {
    this.offset = await measureClockOffset();
    this.seekToTarget();
  };

  SyncPlayer.prototype.setUserOffset = function (ms) {
    this.userOffsetMs = clamp(ms, -20000, 20000);
  };

  SyncPlayer.prototype.nudge = function (deltaMs) {
    this.setUserOffset(this.userOffsetMs + deltaMs);
    this.seekToTarget();
  };

  SyncPlayer.prototype.setAutoBuffer = function (ms) {
    this.autoBufferMs = clamp(ms, 1000, 30000);
  };

  SyncPlayer.prototype.useNative = function () {
    this.native = true;
    this.media.src = this.url;
    this.media.play().catch(function () {});
  };

  SyncPlayer.prototype.fallbackToNative = function (reason) {
    if (this.native) {
      this.onStatus({ error: reason });
      return;
    }
    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }
    if (this.media.canPlayType("application/vnd.apple.mpegurl")) {
      this.onStatus({ native: true, reason: reason });
      this.useNative();
    } else {
      this.onStatus({ error: reason });
    }
  };

  SyncPlayer.prototype.load = async function () {
    this.offset = await measureClockOffset();
    const self = this;
    const canNative = this.media.canPlayType("application/vnd.apple.mpegurl");
    if (this.options.forceNative && canNative) {
      this.useNative();
    } else if (window.Hls && window.Hls.isSupported()) {
      const syncCount = Math.max(1, Math.round(this.autoBufferMs / 1000 / (this.options.hlsTime || 2)));
      this.hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: false,
        liveSyncDurationCount: syncCount,
        liveMaxLatencyDurationCount: syncCount + 8,
        backBufferLength: 30,
      });
      this.hls.on(window.Hls.Events.FRAG_CHANGED, function (event, data) {
        self.activeFrag = data.frag;
      });
      this.hls.on(window.Hls.Events.ERROR, function (event, data) {
        if (!data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          self.hls.startLoad();
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          self.hls.recoverMediaError();
        } else {
          self.fallbackToNative(data.details || "fatal error");
        }
      });
      this.hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
        self.resync();
      });
      this.hls.loadSource(this.url);
      this.hls.attachMedia(this.media);
    } else if (canNative) {
      this.useNative();
    } else {
      this.onStatus({ error: "HLS is not supported in this browser." });
      return;
    }

    this.timer = setInterval(function () { self.tick(); }, 400);

    const startup = async function () {
      try {
        await self.media.play();
      } catch (err) {
        self.onStatus({ needsGesture: true });
      }
    };
    this.hls ? this.hls.on(window.Hls.Events.MANIFEST_PARSED, startup) : startup();
  };

  SyncPlayer.prototype.destroy = function () {
    if (this.timer) clearInterval(this.timer);
    if (this.hls) this.hls.destroy();
  };

  window.VOTAAITE = {
    fetchConfig: fetchConfig,
    SyncPlayer: SyncPlayer,
    sleep: sleep,
    clamp: clamp,
  };
})();
