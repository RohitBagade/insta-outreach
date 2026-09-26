// The voice agent's page: a hands-free conversation with the local server, which talks to Groq.
//
//   mic -> voice detection (AnalyserNode RMS against an adaptive noise floor)
//       -> a MediaRecorder recording per turn -> /api/transcribe
//       -> /api/chat, streamed -> sentences -> speechSynthesis, while the rest still streams in
//
// Plain browser JavaScript, no build step. Text only ever enters the page through textContent.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    name: $("name"), model: $("model"), pill: $("pill"), pillText: $("pill-text"),
    banner: $("banner"), bannerTitle: $("banner-title"), bannerText: $("banner-text"),
    orb: $("orb"), status: $("status"), hint: $("hint"),
    start: $("start"), interrupt: $("interrupt"), mute: $("mute"), reset: $("reset"),
    voice: $("voice"), barge: $("barge"), voiceNote: $("voice-note"),
    transcript: $("transcript"), empty: $("empty"),
  };

  // Voice activity detection, on the RMS level of the mic signal (0..1).
  const VAD = {
    minStart: 0.012, // quieter than this is never speech,
    startRatio: 3, //   nor is anything under 3x the room's noise floor
    stopRatio: 0.55, // speech goes on while the level stays above this share of the start level
    silenceMs: 700, // this much quiet ends a turn
    minSpeechMs: 300, // shorter sounds (clicks, coughs) are ignored
    maxTurnMs: 30000, // a turn is sent after this long even without a pause
    segmentMs: 4000, // a recording with no speech in it is replaced after this long,
    quietMs: 300, //   at a quiet moment, so the start of speech is never cut off
    bargeRatio: 2.5, // interrupting him by voice takes a louder voice than a normal turn,
    bargeMs: 250, //   held at least this long
    rearmMs: 350, // after he stops talking, wait this long before listening again
  };
  const MAX_HISTORY = 20;
  const HINT = "Just talk, then pause when you're done.";
  const MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  // What Whisper tends to "hear" in near-silence.
  const HALLUCINATIONS = new Set([
    "thank you", "thank you very much", "thanks", "thanks for watching", "thank you for watching",
    "thanks for listening", "please subscribe", "subscribe", "like and subscribe", "you", "bye", "bye bye",
    "subtitles by the amara org community", "धन्यवाद", "शुक्रिया",
  ]);
  const MALE_VOICE = /(prabhat|madhur|andrew|brian|guy|christopher|eric|roger|steffan|ryan|thomas|william|david|mark|ravi|hemant|manohar|kunal|\bmale\b)/i;
  const PICKER_LANGS = /^(en|hi|mr|gu|bn|pa|ta|te|kn|ml|ur)(-|$)/;
  const synth = window.speechSynthesis || null;
  const store = { // localStorage can be missing or throw (private windows, blocked storage)
    get(key) { try { return window.localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch { /* not remembered */ } },
  };

  let config = null;
  const history = []; // the last MAX_HISTORY {role, content} messages, sent with every chat request
  let state = "off"; // off | listening | thinking | speaking
  let muted = false;
  let mic = null; // {stream, ctx, analyser, samples, mime, stopClock}
  let det = newDetector();
  let seg = null; // the recording in progress: {recorder, started, blob: Promise<Blob|null>}
  let turnSeq = 0; // id of the latest finished turn
  let ignoreUpTo = 0; // turns up to this id were interrupted: their transcripts are dropped
  let lane = Promise.resolve(); // transcripts are applied in the order the turns were spoken
  let chat = null; // AbortController of the reply being fetched
  let epoch = 0; // bumped by Stop and New conversation: anything still in flight is ignored
  let lastLang = "en"; // language Whisper heard last, for choosing a voice
  let micLevel = 0;
  let pulse = 0; // a word boundary while he speaks (the orb bumps)
  let hintTimer = 0;
  let setupIssue = ""; // shown in the corner pill while the mic is off: "Offline", "Setup needed"
  let recheckTimer = 0;

  function newDetector() {
    return { floor: 0.004, inSpeech: false, confirmed: false, since: 0, lastVoice: 0, voiced: 0, peak: 0,
      threshold: 0, loudAt: 0, holdUntil: 0, last: 0 };
  }

  // ------------------------------------------------------------------ setup

  // While the server is down or the key is missing, look again every few seconds: once Rohit has
  // fixed .env and restarted, the page carries on by itself.
  async function loadConfig() {
    clearTimeout(recheckTimer);
    try {
      const res = await fetch("/api/config");
      if (!res.ok) throw new Error(String(res.status));
      config = await res.json();
    } catch {
      showBanner("Can't reach the voice agent.", "Is python -m voice_agent still running? Start it again: this page reconnects by itself.");
      setupIssue = "Offline";
      render();
      recheckTimer = setTimeout(loadConfig, 3000);
      return false;
    }
    document.title = `${config.name} · voice agent`;
    ui.name.textContent = config.name;
    ui.model.textContent = config.model ? `on Groq · ${config.model}` : "on Groq";
    setupIssue = "";
    if (config.problem) {
      showProblem(config.problem);
      recheckTimer = setTimeout(loadConfig, 3000);
    } else if (!ui.banner.hidden) {
      hideBanner(); // whatever it said is fixed now
    }
    ui.start.disabled = Boolean(config.problem);
    render();
    return !config.problem;
  }

  function showProblem(problem) {
    const titles = { no_key: "Groq key needed.", bad_key: "Groq rejected the key.", no_model: "No Groq model available." };
    showBanner(titles[problem.code] || "Not ready.", problem.error);
    setupIssue = "Setup needed";
    render();
  }

  async function start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      showBanner("This browser can't record here.", `Open ${location.origin} in Microsoft Edge or Google Chrome.`);
      return;
    }
    ui.start.disabled = true;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx(); // created during the click, so the browser lets it run
    const ready = await loadConfig(); // fresh greeting, and the key's current state
    ui.start.disabled = true;
    if (!ready) {
      ctx.close().catch(() => {});
      ui.start.disabled = Boolean(config && config.problem);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      ctx.close().catch(() => {});
      ui.start.disabled = false;
      micError(err);
      return;
    }
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const mime = MIME_TYPES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
    mic = { stream, ctx, analyser, samples: new Float32Array(analyser.fftSize), mime, stopClock: startClock(tick) };
    for (const track of stream.getAudioTracks()) track.addEventListener("ended", micLost);
    ui.start.disabled = false;
    hideBanner();
    epoch += 1;
    det = newDetector();
    if (config.greeting && !history.length) {
      addMessage("assistant", config.greeting);
      say(config.greeting);
    } else {
      setState("listening");
    }
  }

  function stop() {
    epoch += 1;
    cancelReply();
    dropSegment();
    if (mic) {
      mic.stopClock();
      for (const track of mic.stream.getTracks()) {
        track.removeEventListener("ended", micLost);
        track.stop();
      }
      mic.ctx.close().catch(() => {});
      mic = null;
    }
    muted = false;
    det = newDetector();
    micLevel = 0;
    setState("off");
  }

  function micLost() {
    stop();
    showBanner("The microphone stopped.", "It may have been unplugged or switched off. Press Start talking to reconnect.");
  }

  function micError(err) {
    const name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      showBanner("Microphone blocked.",
        "Click the lock or microphone icon in the address bar, allow the microphone for this page, then press Start talking again.");
    } else if (name === "NotFoundError" || name === "OverconstrainedError") {
      showBanner("No microphone found.", "Plug in or switch on a microphone, then press Start talking again.");
    } else if (name === "NotReadableError" || name === "AbortError") {
      showBanner("The microphone is busy.", "Another app (a call or a recorder) may be using it. Close it, then press Start talking again.");
    } else {
      showBanner("Couldn't open the microphone.", String((err && err.message) || err));
    }
  }

  // A steady clock from a worker: page timers slow to a crawl when the page is hidden.
  function startClock(fn) {
    try {
      const worker = new Worker("/static/ticker.js");
      worker.onmessage = fn;
      return () => worker.terminate();
    } catch {
      const id = setInterval(fn, 30);
      return () => clearInterval(id);
    }
  }

  // ---------------------------------------------------------- state machine

  // Listening while idle and while thinking (he can keep talking); while speaking only with barge-in on.
  function armed() {
    if (!mic || muted) return false;
    if (state === "listening" || state === "thinking") return true;
    return state === "speaking" && ui.barge.checked;
  }

  function setState(next) {
    state = next;
    if (armed()) {
      ensureSegment();
    } else {
      dropSegment();
      det.inSpeech = false;
      det.confirmed = false;
    }
    render();
  }

  function cancelReply() {
    if (chat) chat.abort();
    chat = null;
    speaker.cancel();
  }

  function interrupt() {
    if (state !== "speaking" && state !== "thinking") return;
    ignoreUpTo = turnSeq;
    cancelReply();
    setState(mic ? "listening" : "off");
  }

  function toggleMute() {
    if (!mic) return;
    muted = !muted;
    for (const track of mic.stream.getAudioTracks()) track.enabled = !muted; // nothing is captured while muted
    setState(state);
  }

  function newConversation() {
    epoch += 1;
    cancelReply();
    history.length = 0;
    lastLang = "en";
    ui.transcript.replaceChildren();
    ui.empty.hidden = false;
    setState(mic ? "listening" : "off");
  }

  // ------------------------------------------------------- voice detection

  function tick() {
    if (!mic) return;
    const now = performance.now();
    const dt = det.last ? Math.min(100, now - det.last) : 0;
    det.last = now;
    mic.analyser.getFloatTimeDomainData(mic.samples);
    let sum = 0;
    for (let i = 0; i < mic.samples.length; i++) sum += mic.samples[i] * mic.samples[i];
    const rms = Math.sqrt(sum / mic.samples.length);
    micLevel = muted ? 0 : rms;
    watchSpeech(now);
    if (armed() && now >= det.holdUntil) detect(rms, now, dt);
  }

  function detect(rms, now, dt) {
    const barge = state === "speaking";
    const start = Math.max(VAD.minStart, det.floor * VAD.startRatio) * (barge ? VAD.bargeRatio : 1);
    const stop = start * VAD.stopRatio;
    if (!det.inSpeech) {
      if (rms >= start) {
        Object.assign(det, { inSpeech: true, confirmed: false, since: now, lastVoice: now, voiced: 0, peak: rms, threshold: start });
        ensureSegment();
        render();
        return;
      }
      det.floor += (rms - det.floor) * (rms < det.floor ? 0.1 : 0.01); // falls fast, rises slowly
      det.floor = Math.min(0.05, Math.max(0.0005, det.floor));
      if (rms >= stop) det.loudAt = now;
      if (seg && now - seg.started > VAD.segmentMs && now - det.loudAt > VAD.quietMs) recycleSegment();
      return;
    }
    if (rms >= stop) {
      det.lastVoice = now;
      det.voiced += dt;
      det.peak = Math.max(det.peak, rms);
    }
    if (!det.confirmed && det.voiced >= (barge ? VAD.bargeMs : VAD.minSpeechMs)) {
      det.confirmed = true;
      heard();
    }
    if (now - det.lastVoice >= VAD.silenceMs || now - det.since >= VAD.maxTurnMs) endTurn();
  }

  // Real speech (not a click): stop talking if he was, and hold any reply until this turn is in.
  function heard() {
    if (state === "speaking" || (state === "thinking" && chat)) {
      cancelReply();
      setState("listening");
    }
  }

  function endTurn() {
    const turn = { confirmed: det.confirmed, peak: det.peak, threshold: det.threshold };
    det.inSpeech = false;
    det.confirmed = false;
    det.loudAt = performance.now();
    const finished = seg;
    seg = null;
    ensureSegment(); // keep listening while this turn is processed
    if (turn.confirmed && finished) submitTurn(closeSegment(finished), turn);
    else closeSegment(finished); // a click or a cough
    render();
  }

  // Recordings: one MediaRecorder at a time, replaced while nobody speaks, kept once someone does.
  function ensureSegment() {
    if (seg || !mic) return;
    let recorder;
    try {
      recorder = new MediaRecorder(mic.stream, mic.mime ? { mimeType: mic.mime } : undefined);
    } catch {
      return;
    }
    const type = recorder.mimeType || mic.mime || "audio/webm";
    const chunks = [];
    const blob = new Promise((resolve) => {
      recorder.ondataavailable = (event) => { if (event.data && event.data.size) chunks.push(event.data); };
      recorder.onstop = () => resolve(new Blob(chunks, { type }));
      recorder.onerror = () => resolve(null);
    });
    recorder.start();
    seg = { recorder, started: performance.now(), blob };
  }

  function closeSegment(segment) {
    if (!segment) return Promise.resolve(null);
    if (segment.recorder.state !== "inactive") segment.recorder.stop();
    return segment.blob;
  }

  function dropSegment() {
    closeSegment(seg);
    seg = null;
  }

  function recycleSegment() {
    const old = seg;
    seg = null;
    ensureSegment(); // the new one starts before the old one stops: no gap
    closeSegment(old);
  }

  // ------------------------------------------------------ turns and replies

  function submitTurn(recording, turn) {
    const id = ++turnSeq;
    const myEpoch = epoch;
    if (state === "listening") setState("thinking");
    const outcome = recording.then(transcribe).then((result) => ({ result }), (problem) => ({ problem }));
    lane = lane.then(() => outcome).then((done) => applyTranscript(id, myEpoch, turn, done))
      .catch((err) => console.error(err)); // a bug in one turn must not stall the next ones
  }

  async function transcribe(blob) {
    if (!blob || !blob.size) throw { code: "empty_audio", error: "The recording was empty." };
    let res;
    try {
      res = await fetch("/api/transcribe", {
        method: "POST", headers: { "Content-Type": blob.type || "audio/webm" }, body: blob,
      });
    } catch {
      throw offline();
    }
    if (!res.ok) throw await problemFrom(res);
    return res.json();
  }

  function applyTranscript(id, myEpoch, turn, done) {
    if (myEpoch !== epoch || id <= ignoreUpTo) return;
    if (done.problem) {
      notify(done.problem);
    } else {
      const text = usable(done.result, turn);
      if (text) {
        if (done.result.language) lastLang = done.result.language;
        addMessage("user", text);
      }
    }
    if (id !== turnSeq || det.confirmed || !mic) return; // he's still talking: that turn gets the answer
    const last = history[history.length - 1];
    if (!done.problem && last && last.role === "user") reply();
    else if (state === "thinking") setState("listening");
  }

  // The transcript, or "" for noise Whisper turned into words.
  function usable(result, turn) {
    const text = String((result && result.text) || "").trim();
    const plain = text.toLowerCase().replace(/[^\p{L}\p{M}\p{N}\s]+/gu, " ").replace(/\s+/g, " ").trim();
    if (!plain || /^[[(].*[\])]$/.test(text)) return ""; // nothing, or "[Music]", "(silence)"
    const faint = turn.peak < turn.threshold * 2.5; // barely louder than what counts as speech
    const doubt = result && result.no_speech_prob;
    const noSpeech = typeof doubt === "number" ? doubt : 0; // Whisper's own doubt that it was speech
    if (HALLUCINATIONS.has(plain) && (faint || noSpeech > 0.5)) return "";
    if (faint && noSpeech > 0.8) return "";
    return text;
  }

  async function reply() {
    cancelReply();
    const myEpoch = epoch;
    const ctl = new AbortController();
    chat = ctl;
    setState("thinking");
    speaker.begin();
    let bubble = null;
    let text = "";
    let problem = null;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history.slice(-MAX_HISTORY) }),
        signal: ctl.signal,
      });
      if (!res.ok) throw await problemFrom(res);
      await readEvents(res, (event) => {
        if (event.type === "delta" && typeof event.text === "string") {
          if (!bubble) {
            bubble = addBubble("agent", "");
            setState("speaking");
          }
          text += event.text;
          bubble.textContent = text;
          follow();
          speaker.push(event.text);
        } else if (event.type === "error") {
          problem = event;
        }
      });
    } catch (err) {
      if (!ctl.signal.aborted) problem = err && err.error ? err : offline();
    }
    if (chat === ctl) chat = null;
    if (myEpoch !== epoch) return;
    if (text) remember("assistant", text); // what was said so far, even if cut short
    if (ctl.signal.aborted) return; // interrupted: that code has already moved on
    if (problem) notify(problem);
    if (bubble) speaker.end();
    else setState(mic ? "listening" : "off");
  }

  // Server-sent events from a fetch body: calls onEvent with each JSON "data:" payload.
  async function readEvents(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      for (let cut = buffer.indexOf("\n\n"); cut >= 0; cut = buffer.indexOf("\n\n")) {
        const data = buffer.slice(0, cut).split("\n")
          .filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
        buffer = buffer.slice(cut + 2);
        let event = null;
        try {
          event = JSON.parse(data);
        } catch {
          continue;
        }
        if (event) onEvent(event);
      }
    }
  }

  async function problemFrom(res) {
    try {
      const body = await res.json();
      if (body && body.error) return body;
    } catch { /* not JSON */ }
    return { code: `http_${res.status}`, error: `The voice agent answered with an error (${res.status}).` };
  }

  function offline() {
    return { code: "offline", error: "Can't reach the voice agent. Is python -m voice_agent still running?" };
  }

  function notify(problem) {
    const code = problem.code || "";
    if (code === "no_key" || code === "bad_key" || code === "no_model") {
      stop();
      showProblem(problem);
      ui.start.disabled = true;
      return;
    }
    const message = problem.error || "Something went wrong.";
    addNote(message, true);
    flashHint(message);
  }

  // ------------------------------------------------------------------ speech

  // Sentences go to speechSynthesis as soon as they are complete, while the reply still streams in.
  const speaker = {
    buffer: "", open: 0, streaming: false, gen: 0, lastEvent: 0,
    begin() {
      this.cancel();
      this.streaming = true;
    },
    push(text) {
      this.buffer += text;
      for (let cut = sentenceEnd(this.buffer); cut > 0; cut = sentenceEnd(this.buffer)) {
        this.utter(this.buffer.slice(0, cut));
        this.buffer = this.buffer.slice(cut);
      }
    },
    end() {
      this.utter(this.buffer);
      this.buffer = "";
      this.streaming = false;
      this.check();
    },
    utter(sentence) {
      const words = speakable(sentence);
      const voice = words ? pickVoice(languageOf(words)) : null;
      if (!voice) return; // no voices in this browser: the text on screen has to do
      const utterance = new SpeechSynthesisUtterance(words);
      utterance.voice = voice;
      utterance.lang = voice.lang;
      const gen = this.gen;
      const current = () => {
        if (gen !== this.gen) return false;
        this.lastEvent = performance.now();
        return true;
      };
      utterance.onstart = current;
      utterance.onboundary = () => { if (current()) pulse = 1; };
      utterance.onend = utterance.onerror = () => {
        if (!current()) return;
        this.open -= 1;
        this.check();
      };
      this.open += 1;
      this.lastEvent = performance.now();
      synth.resume(); // Chrome can leave the queue paused
      synth.speak(utterance);
    },
    check() {
      if (!this.streaming && this.open <= 0 && state === "speaking") doneSpeaking();
    },
    forget() { // ignore whatever the queued utterances report from now on
      this.gen += 1;
      this.open = 0;
    },
    cancel() {
      this.forget();
      this.buffer = "";
      this.streaming = false;
      if (synth) synth.cancel();
    },
  };

  function say(text) {
    setState("speaking");
    speaker.begin();
    speaker.push(text);
    speaker.end();
  }

  function doneSpeaking() {
    det.holdUntil = performance.now() + VAD.rearmMs; // let the room go quiet first
    setState(mic ? "listening" : "off");
  }

  // Browsers sometimes never report the end of an utterance; don't wait for it forever.
  function watchSpeech(now) {
    if (state !== "speaking" || speaker.open <= 0 || !synth) return;
    const silent = !synth.speaking && !synth.pending;
    if ((silent && now - speaker.lastEvent > 2000) || now - speaker.lastEvent > 20000) {
      if (!silent) synth.cancel();
      speaker.forget();
      speaker.check();
    }
  }

  const SENTENCE_END = /[.!?।]+["'”’)\]]*(?=\s)|[।\n]/g;
  const ABBREVIATION = /\b(?:mr|mrs|ms|dr|vs)\.$/i;

  // Index just past the first complete sentence in text, or 0 while it is still incomplete.
  function sentenceEnd(text) {
    SENTENCE_END.lastIndex = 0;
    for (let m = SENTENCE_END.exec(text); m; m = SENTENCE_END.exec(text)) {
      const end = m.index + m[0].length;
      if (!ABBREVIATION.test(text.slice(Math.max(0, end - 5), end))) return end;
    }
    if (text.length > 220) { // a very long sentence: break it at a comma or a space
      const cut = Math.max(text.lastIndexOf(", ", 200), text.lastIndexOf(" ", 200));
      if (cut > 40) return cut + 1;
    }
    return 0;
  }

  // What a speech voice should read: no markdown, links or emoji.
  function speakable(text) {
    const words = text
      .replace(/https?:\/\/\S+/g, " a link ")
      .replace(/[*_#`~>|]+/g, " ")
      .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}]/gu, "")
      .replace(/\s+/g, " ")
      .trim();
    return /[\p{L}\p{N}]/u.test(words) ? words : "";
  }

  function languageOf(text) {
    if (/\p{Script=Devanagari}/u.test(text) && !["hi", "mr", "ne", "sa"].includes(lastLang)) return "hi";
    return lastLang || "en";
  }

  function langOf(voice) {
    return String(voice.lang || "").toLowerCase().replace("_", "-");
  }

  // Natural (Edge's online) voices first, then male ones (he's a "him"), then Indian accents.
  function voiceScore(voice, lang) {
    const vl = langOf(voice);
    if (lang && vl.split("-")[0] !== lang) return -1;
    let score = 0;
    if (/natural/i.test(voice.name)) score += 100;
    if (MALE_VOICE.test(voice.name)) score += 40;
    if (vl.endsWith("-in")) score += 20;
    if (!voice.localService) score += 2;
    return score;
  }

  function pickVoice(lang) {
    if (!synth) return null;
    const voices = synth.getVoices();
    if (!voices.length) return null;
    const chosen = ui.voice.value && voices.find((v) => v.voiceURI === ui.voice.value);
    if (chosen) return chosen;
    for (const code of [lang, "en"]) {
      let best = null;
      let bestScore = -1;
      for (const voice of voices) {
        const score = voiceScore(voice, code);
        if (score > bestScore) {
          best = voice;
          bestScore = score;
        }
      }
      if (best) return best;
    }
    return voices.find((v) => v.default) || voices[0];
  }

  function loadVoices() {
    if (!synth) {
      ui.voiceNote.textContent = "This browser has no speech voices, so his replies are text only.";
      return;
    }
    const voices = synth.getVoices();
    const keep = ui.voice.value || store.get("voice-agent.voice") || "";
    const listed = voices
      .filter((v) => PICKER_LANGS.test(langOf(v)) || v.voiceURI === keep)
      .sort((a, b) => voiceScore(b) - voiceScore(a) || a.name.localeCompare(b.name));
    ui.voice.replaceChildren(new Option("Automatic (best for the language)", ""));
    for (const voice of listed) ui.voice.append(new Option(voice.name.replace(/^Microsoft /, ""), voice.voiceURI));
    ui.voice.value = listed.some((v) => v.voiceURI === keep) ? keep : "";
    if (!voices.length) {
      ui.voiceNote.textContent = "No speech voices found, so his replies are text only. Microsoft Edge has natural voices.";
    } else if (!voices.some((v) => /natural/i.test(v.name))) {
      ui.voiceNote.textContent = "For a much more natural voice, open this page in Microsoft Edge.";
    } else {
      ui.voiceNote.textContent = "";
    }
  }

  // --------------------------------------------------------------------- UI

  function remember(role, content) {
    history.push({ role, content });
    if (history.length > MAX_HISTORY) history.splice(0, history.length - MAX_HISTORY);
  }

  function addMessage(role, text) {
    remember(role, text);
    addBubble(role === "user" ? "user" : "agent", text);
  }

  function addBubble(kind, text) {
    const item = document.createElement("li");
    item.className = `bubble ${kind}`;
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = kind === "user" ? "You" : (config && config.name) || "Agent";
    const body = document.createElement("p");
    body.textContent = text;
    item.append(who, body);
    append(item);
    return body;
  }

  function addNote(text, isError) {
    const item = document.createElement("li");
    item.className = isError ? "note error" : "note";
    item.textContent = text;
    append(item);
  }

  function append(item) {
    const list = ui.transcript;
    list.append(item);
    while (list.children.length > 200) list.firstElementChild.remove();
    ui.empty.hidden = true;
    ui.reset.disabled = false;
    list.scrollTop = list.scrollHeight;
  }

  function follow() { // keep the growing reply in view, unless he scrolled up to read
    const list = ui.transcript;
    if (list.scrollHeight - list.scrollTop - list.clientHeight < 160) list.scrollTop = list.scrollHeight;
  }

  function showBanner(title, text) {
    ui.bannerTitle.textContent = title;
    ui.bannerText.textContent = text;
    ui.banner.hidden = false;
  }

  function hideBanner() {
    ui.banner.hidden = true;
  }

  function flashHint(text) {
    ui.hint.textContent = text;
    ui.hint.classList.add("error");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      ui.hint.classList.remove("error");
      render();
    }, 6000);
  }

  function setPill(kind, text) {
    ui.pill.dataset.kind = kind;
    ui.pillText.textContent = text;
  }

  function render() {
    const hearing = det.inSpeech && armed();
    const name = (config && config.name) || "He";
    let status = config && config.problem ? "Setup needed" : "Press Start talking";
    if (state !== "off") {
      if (hearing) status = "Hearing you…";
      else if (state === "speaking") status = `${name} is talking…`;
      else if (state === "thinking") status = "Thinking…";
      else status = muted ? "Mic muted" : "Listening…";
    }
    ui.status.textContent = status;
    if (!ui.hint.classList.contains("error")) {
      let hint = HINT;
      if (state === "off" && setupIssue) hint = "Once that's fixed, this page carries on by itself.";
      else if (state === "speaking") hint = ui.barge.checked ? "Talk over him, or press Space, to interrupt." : "Press Space or Interrupt to cut in.";
      else if (muted) hint = "Your mic is muted. Unmute it to talk.";
      ui.hint.textContent = hint;
    }
    ui.start.textContent = state === "off" ? "Start talking" : "Stop";
    ui.start.classList.toggle("primary", state === "off");
    // During a session all four buttons stay put, so nothing moves under the pointer mid-reply.
    ui.interrupt.hidden = !mic;
    ui.interrupt.disabled = state !== "speaking" && state !== "thinking";
    ui.mute.hidden = !mic;
    ui.reset.disabled = !ui.transcript.children.length;
    ui.mute.textContent = muted ? "Unmute mic" : "Mute mic";
    ui.mute.setAttribute("aria-pressed", String(muted));
    if (mic) setPill(muted ? "muted" : "live", muted ? "Mic muted" : "Mic on");
    else setPill(setupIssue ? "bad" : "off", setupIssue || "Mic off");
  }

  // ------------------------------------------------------------------- orb

  const PALETTES = {
    off: [[124, 131, 255], [168, 85, 247]],
    listening: [[56, 189, 248], [99, 102, 241]],
    hearing: [[74, 222, 128], [16, 185, 129]],
    thinking: [[251, 191, 36], [236, 72, 153]],
    speaking: [[192, 132, 252], [236, 72, 153]],
    muted: [[148, 163, 184], [100, 116, 139]],
  };
  const orb = { ctx: ui.orb.getContext("2d"), colors: PALETTES.off.map((c) => c.slice()), level: 0,
    gazeX: 0, gazeY: 0, blinkAt: -1000, nextBlink: 1800 };
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)");
  const rgba = ([r, g, b], a) => `rgba(${r | 0}, ${g | 0}, ${b | 0}, ${a})`;

  function drawOrb(now) {
    requestAnimationFrame(drawOrb);
    const canvas = ui.orb;
    const c = orb.ctx;
    const size = Math.round(canvas.clientWidth * Math.min(window.devicePixelRatio || 1, 2));
    if (!c || !size) return;
    if (canvas.width !== size) {
      canvas.width = size;
      canvas.height = size;
    }
    const t = now / 1000;
    const hearing = det.inSpeech && armed();
    const thinking = state === "thinking" && !hearing;
    const look = hearing ? "hearing" : muted && state === "listening" ? "muted" : state;
    const target = PALETTES[look] || PALETTES.off;
    orb.colors = orb.colors.map((rgb, i) => rgb.map((v, k) => v + (target[i][k] - v) * 0.08));

    let energy = 0.08; // how lively he looks, 0..1
    if (state === "listening" || hearing) energy = Math.min(1, Math.sqrt(micLevel) * 2.2);
    else if (state === "speaking") energy = 0.3 + 0.45 * Math.abs(Math.sin(t * 7.1) * Math.sin(t * 2.3 + 1)) + 0.3 * pulse;
    else if (thinking) energy = 0.25 + 0.08 * Math.sin(t * 5);
    pulse *= 0.9;
    orb.level += (energy - orb.level) * (energy > orb.level ? 0.35 : 0.12);

    const still = calm.matches;
    const w = canvas.width;
    const cx = w / 2;
    const cy = w / 2;
    const breathe = still ? 1 : 1 + 0.03 * Math.sin((t * 2 * Math.PI) / 4.5);
    const r0 = w * 0.28 * breathe * (1 + orb.level * 0.14);
    const [a, b] = orb.colors;
    c.clearRect(0, 0, w, w);

    const glow = c.createRadialGradient(cx, cy, r0 * 0.7, cx, cy, w * 0.5); // fades out inside the canvas
    glow.addColorStop(0, rgba(a, 0.3 + orb.level * 0.3));
    glow.addColorStop(1, rgba(b, 0));
    c.fillStyle = glow;
    c.fillRect(0, 0, w, w);

    const ripple = still ? 0 : 0.012 + orb.level * 0.055; // the livelier, the more the edge ripples
    c.beginPath();
    for (let i = 0; i <= 96; i++) {
      const th = (i / 96) * Math.PI * 2;
      const wave = 0.5 * Math.sin(3 * th + t * 1.4) + 0.3 * Math.sin(5 * th - t * 2.1) + 0.2 * Math.sin(2 * th + t * 0.8);
      const r = r0 * (1 + ripple * wave);
      if (i) c.lineTo(cx + r * Math.cos(th), cy + r * Math.sin(th));
      else c.moveTo(cx + r * Math.cos(th), cy + r * Math.sin(th));
    }
    c.closePath();
    const body = c.createLinearGradient(cx - r0, cy - r0, cx + r0, cy + r0);
    body.addColorStop(0, rgba(a, 1));
    body.addColorStop(1, rgba(b, 1));
    c.fillStyle = body;
    c.fill();
    const shine = c.createRadialGradient(cx - r0 * 0.35, cy - r0 * 0.45, 0, cx - r0 * 0.35, cy - r0 * 0.45, r0 * 1.1);
    shine.addColorStop(0, "rgba(255, 255, 255, 0.4)");
    shine.addColorStop(1, "rgba(255, 255, 255, 0)");
    c.fillStyle = shine;
    c.fill();

    if (thinking) { // three dots circling while he thinks
      for (let k = 0; k < 3; k++) {
        const angle = (still ? 0 : t * 2.6) + (k * Math.PI * 2) / 3;
        c.beginPath();
        c.arc(cx + Math.cos(angle) * r0 * 1.32, cy + Math.sin(angle) * r0 * 1.32, r0 * 0.055, 0, Math.PI * 2);
        c.fillStyle = rgba(a, 0.9);
        c.fill();
      }
    }
    drawFace(c, cx, cy, r0, now, hearing, thinking);
  }

  function drawFace(c, cx, cy, r0, now, hearing, thinking) {
    if (now > orb.nextBlink) {
      orb.blinkAt = now;
      orb.nextBlink = now + 2200 + Math.random() * 4000;
    }
    const since = now - orb.blinkAt;
    const open = since < 160 ? Math.abs(1 - since / 80) : 1; // a blink: shut and open again in 160 ms
    orb.gazeX += ((thinking ? 0.35 : 0) - orb.gazeX) * 0.06; // looks up and away while thinking
    orb.gazeY += ((thinking ? -0.3 : 0) - orb.gazeY) * 0.06;
    const sleepy = muted || state === "off" ? 0.55 : 1;
    const eyeH = r0 * 0.17 * Math.max(0.1, open) * sleepy * (hearing ? 1.1 : 1);
    const eyeY = cy - r0 * 0.1 + orb.gazeY * r0 * 0.3;
    c.fillStyle = "rgba(255, 255, 255, 0.95)";
    for (const side of [-1, 1]) {
      c.beginPath();
      c.ellipse(cx + side * r0 * 0.28 + orb.gazeX * r0 * 0.3, eyeY, r0 * 0.1, eyeH, 0, 0, Math.PI * 2);
      c.fill();
    }
    const mx = cx + orb.gazeX * r0 * 0.25;
    const my = cy + r0 * 0.33 + orb.gazeY * r0 * 0.15;
    if (state === "speaking") { // an open smile that opens and closes as he talks
      c.beginPath();
      c.ellipse(mx, my - r0 * 0.02, r0 * 0.19, r0 * (0.05 + 0.13 * orb.level), 0, 0, Math.PI);
      c.closePath();
      c.fill();
    } else {
      c.beginPath();
      c.lineWidth = r0 * 0.05;
      c.lineCap = "round";
      c.strokeStyle = "rgba(255, 255, 255, 0.95)";
      c.moveTo(mx - r0 * 0.2, my);
      c.quadraticCurveTo(mx, my + r0 * (thinking ? 0.03 : 0.16), mx + r0 * 0.2, my);
      c.stroke();
    }
  }

  // ----------------------------------------------------------------- wiring

  ui.start.addEventListener("click", () => (state === "off" ? start() : stop()));
  ui.interrupt.addEventListener("click", interrupt);
  ui.mute.addEventListener("click", toggleMute);
  ui.reset.addEventListener("click", newConversation);
  ui.voice.addEventListener("change", () => {
    store.set("voice-agent.voice", ui.voice.value);
    if (state === "off" || state === "listening") say("Hi Rohit, this is how I sound."); // a sample
  });
  ui.barge.checked = store.get("voice-agent.barge") === "1";
  ui.barge.addEventListener("change", () => {
    store.set("voice-agent.barge", ui.barge.checked ? "1" : "0");
    setState(state);
  });

  // During a session Space only ever interrupts him: it must not also press the focused button
  // (that is usually Stop, right after Start). Enter still presses buttons.
  document.addEventListener("keydown", (event) => {
    if (event.code !== "Space" || !mic || typing(event.target)) return;
    event.preventDefault();
    if (!event.repeat) interrupt();
  });
  document.addEventListener("keyup", (event) => {
    if (event.code === "Space" && mic && !typing(event.target)) event.preventDefault();
  });
  function typing(target) {
    return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName));
  }

  if (synth) synth.addEventListener("voiceschanged", loadVoices);
  loadVoices();
  loadConfig();
  render();
  requestAnimationFrame(drawOrb);
})();
