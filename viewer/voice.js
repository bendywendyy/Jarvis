// How long you must stay quiet (ms) before a dictated thought is sent. The recogniser finalises a
// phrase at every short pause, so finals are buffered; each new bit of speech appends and restarts
// this window, and only the pause that outlasts it sends the whole combined sentence.
export const FINISH_MS = 900;

// Voice for the Knowledge Galaxy, using only what the browser ships with:
//   speaking  -> speechSynthesis (prefers a British English voice)
//   listening -> webkitSpeechRecognition (continuous, with interim results)
// Interrupt words ("stop", "wait", ...) skip the buffer and act the moment they're heard.

const STALL_MS = 4000;       // safety net: if the recogniser goes quiet mid-phrase, use what it had
const ECHO_TAIL_MS = 600;    // after the galaxy stops talking, the mic may still be hearing its voice
const INTERRUPT_WORDS = new Set(['stop', 'wait', 'cancel', 'quiet', 'enough', 'pause', 'shush', 'hush']);
const INTERRUPT_PHRASES = new Set(['hold on', 'hang on', 'never mind', 'nevermind', 'shut up', 'be quiet', 'one sec', 'one second']);
const FILLERS = new Set(['ok', 'okay', 'no', 'oh', 'hey', 'please', 'just', 'um', 'uh', 'now', 'right']);

const normalise = (t) => String(t).toLowerCase().replace(/[^a-z' ]+/g, ' ').replace(/\s+/g, ' ').trim();

/** True when the whole phrase is an interrupt ("stop", "wait", "okay stop", "hold on"...). */
export function isInterrupt(text) {
  const words = normalise(text).split(' ').filter(Boolean);
  if (!words.length || words.length > 4) return false;
  const core = words.filter((w) => !FILLERS.has(w));
  if (!core.length) return false;
  return INTERRUPT_PHRASES.has(core.join(' ')) || core.every((w) => INTERRUPT_WORDS.has(w));
}

/** "stop what's the churn rate" -> "what's the churn rate" */
function stripInterrupt(text) {
  const words = String(text).trim().split(/\s+/);
  let i = 0;
  while (i < words.length && i < 4) {
    const w = normalise(words[i]), two = normalise(words.slice(i, i + 2).join(' '));
    if (INTERRUPT_PHRASES.has(two)) i += 2;
    else if (INTERRUPT_WORDS.has(w) || FILLERS.has(w)) i += 1;
    else break;
  }
  return words.slice(i).join(' ').replace(/^[\s,.;:!?-]+/, '');
}

/** Make answer text read naturally aloud. */
function speakable(text) {
  return String(text)
    .replace(/[*_`#>|]|\[\[|\]\]/g, ' ')
    .replace(/\$(\d[\d,.]*)\s?([kKmM])\b/g, (_, n, u) => `${n} ${/k/i.test(u) ? 'thousand' : 'million'} dollars`)
    .replace(/\/\s?(lb|pound)s?\b/gi, ' per pound')
    .replace(/\/\s?kg\b/gi, ' per kilo')
    .replace(/\/\s?(wk|week)\b/gi, ' per week')
    .replace(/\/\s?(mo|month)\b/gi, ' per month')
    .replace(/\/\s?(hr|hour)\b/gi, ' per hour')
    .replace(/\/\s?(yr|year)\b/gi, ' per year')
    .replace(/(\d)\s?[–—]\s?(\d)/g, '$1 to $2')
    .replace(/~\s?/g, 'about ')
    .replace(/→/g, ' to ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** One utterance per sentence: Chrome cuts long utterances off, and short ones stop faster. */
function sentences(text) {
  const parts = text.split(/(?<=[.!?…])\s+/).map((s) => s.trim()).filter(Boolean);
  return parts.length ? parts : [text];
}

export function createVoice({ micButton, statusEl, input, onThought, onInterrupt }) {
  const synth = window.speechSynthesis || null;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  const log = (...a) => console.info('[voice]', ...a);

  // ------------------------------------------------------------------ status line
  const st = { unlocked: false, speaking: false, thinking: false, mic: false, starting: false, hearing: false, finishing: false, flash: null };
  const whoEl = statusEl.querySelector('.vwho'), textEl = statusEl.querySelector('.vtext'), bar = statusEl.querySelector('.vbar');
  statusEl.style.setProperty('--finish-ms', `${FINISH_MS}ms`);
  let flashTimer = 0;

  function flash(text, ms = 1800, kind = 'stopped') {
    st.flash = { text, kind };
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { st.flash = null; render(); }, ms);
    render();
  }

  function render() {
    const lines = {
      speaking: ['Galaxy', st.mic ? 'Speaking · say “stop” to interrupt' : 'Speaking · press Esc to stop'],
      hearing: ['You', 'Hearing you…'],
      finishing: ['You', 'Got it · sending when you pause'],
      thinking: ['Galaxy', 'Thinking…'],
      starting: ['', 'Starting microphone…'],
      listening: ['You', 'Listening · your turn'],
      locked: ['', 'Voice replies start after your first click'],
      idle: ['', !synth ? 'Voice replies aren’t supported in this browser'
        : Recognition ? 'Voice ready · click the mic to talk' : 'Voice replies on · dictation needs Chrome, Edge or Safari'],
    };
    let state = st.speaking ? 'speaking' : st.hearing ? 'hearing' : st.finishing ? 'finishing' : st.thinking ? 'thinking'
      : st.mic ? (st.starting ? 'starting' : 'listening') : synth && !st.unlocked ? 'locked' : 'idle';
    let [who, text] = lines[state];
    if (st.flash && ['listening', 'starting', 'idle', 'locked'].includes(state)) {
      state = st.flash.kind; who = st.flash.kind === 'error' ? '' : 'You'; text = st.flash.text;
    }
    statusEl.dataset.state = state;
    whoEl.textContent = who;
    whoEl.hidden = !who;
    textEl.textContent = text;
    statusEl.title = voice ? `Speaking voice: ${voice.name} (${voice.lang})` : '';
    if (micButton) {
      micButton.setAttribute('aria-pressed', String(st.mic));
      micButton.title = !Recognition ? 'Dictation needs Chrome, Edge or Safari'
        : st.mic ? 'Stop listening (sends anything you just said)' : 'Talk to your notes';
    }
  }

  function restartBar() {
    bar.style.animation = 'none';
    void bar.offsetWidth;                                  // reflow so the fill restarts from zero
    bar.style.animation = '';
  }

  // ------------------------------------------------------------------ speaking
  let voice = null, voicesReady = false, pendingLine = null;
  let speakToken = 0, speakStartedAt = 0, echoUntil = 0, spokenWords = new Set();
  const keepAlive = new Set();                             // Chrome loses events of garbage-collected utterances

  function chooseVoice() {
    const all = synth ? synth.getVoices() || [] : [];
    if (!all.length) return;
    voicesReady = true;
    const english = all.filter((v) => /^en(\b|[-_])/i.test(v.lang));
    const british = english.filter((v) => /^en[-_]GB$/i.test(v.lang) || /\b(UK|United Kingdom|British)\b/i.test(v.name));
    const score = (v) => (/natural|neural|premium|enhanced/i.test(v.name) ? 4 : 0) + (/^google/i.test(v.name) ? 2 : 0) + (v.localService ? 1 : 0);
    const best = (list) => list.slice().sort((a, b) => score(b) - score(a))[0];
    voice = best(british) || english.find((v) => v.default) || best(english) || all.find((v) => v.default) || all[0];
    log(`speaking voice: ${voice.name} (${voice.lang})${british.length ? '' : ', no British English voice installed'}`);
    render();
  }

  function whenVoicesReady(cb) {
    if (voicesReady || !synth) { cb(); return; }
    let done = false;
    const go = () => { if (!done) { done = true; chooseVoice(); cb(); } };
    synth.addEventListener?.('voiceschanged', go, { once: true });
    setTimeout(go, 1200);                                  // Safari may never fire voiceschanged
  }

  // Browsers refuse to speak until the page has had a click or key press. Unlock inside that very
  // first gesture (a silent utterance: Chrome needs the activation, Safari/iOS need speak() called
  // from within the gesture), then release whatever line was waiting, such as the greeting.
  function unlock() {
    if (st.unlocked || !synth) return;
    const ua = navigator.userActivation;
    if (ua && !ua.isActive && !ua.hasBeenActive) return;   // e.g. Esc doesn't count as a gesture
    st.unlocked = true;
    const primer = new SpeechSynthesisUtterance(' ');
    primer.volume = 0;
    synth.speak(primer);
    log('sound unlocked by your first interaction');
    render();
    if (pendingLine) { const line = pendingLine; pendingLine = null; whenVoicesReady(() => say(line)); }
  }
  ['pointerdown', 'keydown', 'touchend'].forEach((ev) => addEventListener(ev, unlock, { capture: true }));

  function setSpeaking(on) {
    if (st.speaking === on) return;
    st.speaking = on;
    if (on) speakStartedAt = performance.now();
    else echoUntil = performance.now() + ECHO_TAIL_MS;
    render();
  }

  function say(text) {
    if (!synth || !text) return;
    if (!st.unlocked && navigator.userActivation?.hasBeenActive) st.unlocked = true;
    if (!st.unlocked) { pendingLine = text; render(); return; }  // never try before the first click
    const token = ++speakToken;
    const chunks = sentences(speakable(text));
    spokenWords = new Set(normalise(text).split(' '));
    setSpeaking(true);
    const go = () => {
      if (token !== speakToken) return;
      chunks.forEach((chunk, i) => {
        const u = new SpeechSynthesisUtterance(chunk);
        if (voice) u.voice = voice;
        u.lang = voice ? voice.lang : 'en-GB';
        const done = (e) => {
          keepAlive.delete(u);
          if (token !== speakToken) return;
          const failed = e && e.error && e.error !== 'interrupted' && e.error !== 'canceled';
          if (failed) log(`speech error: ${e.error}`);
          if (e && e.error === 'not-allowed') { st.unlocked = false; pendingLine = text; }   // retry after the next click
          if (failed || i === chunks.length - 1) setSpeaking(false);
        };
        u.onend = done;
        u.onerror = done;
        keepAlive.add(u);
        synth.speak(u);
      });
    };
    // Chrome sometimes swallows a speak() issued straight after cancel(), so leave it a beat.
    if (synth.speaking || synth.pending) { synth.cancel(); setTimeout(go, 100); } else go();
  }

  function stopSpeaking() {
    if (!synth) return;
    pendingLine = null;
    speakToken++;
    if (synth.speaking || synth.pending) synth.cancel();
    setSpeaking(false);
  }

  // Watchdog: some engines never fire onend (Chrome after long utterances, hidden tabs).
  setInterval(() => {
    if (st.speaking && synth && !synth.speaking && !synth.pending && performance.now() - speakStartedAt > 2500) setSpeaking(false);
  }, 500);

  if (synth) {
    chooseVoice();
    if (synth.addEventListener) synth.addEventListener('voiceschanged', chooseVoice);
    else synth.onvoiceschanged = chooseVoice;
  }

  // ------------------------------------------------------------------ listening
  let rec = null, live = false, results = new Map();
  let buffer = [], interim = '', interimIdx = -1, lastLive = '';
  let finishTimer = 0, stallTimer = 0, pendingInterrupt = false;
  let sessionStart = 0, sessionHeard = false, quickEnds = 0, netFails = 0;

  function showLive() {
    if (!input) return;
    const text = [...buffer, interim].filter(Boolean).join(' ');
    if (text) input.value = lastLive = text;
    else if (lastLive && input.value === lastLive) input.value = lastLive = '';
  }

  function armFinish() {
    clearTimeout(finishTimer);
    clearTimeout(stallTimer);
    st.finishing = true;
    restartBar();
    finishTimer = setTimeout(() => dispatch(`you paused for ${FINISH_MS}ms`), FINISH_MS);
  }

  function dispatch(why) {
    clearTimeout(finishTimer);
    clearTimeout(stallTimer);
    finishTimer = 0;
    st.finishing = false;
    const text = buffer.join(' ').replace(/\s+/g, ' ').trim();
    buffer = [];
    lastLive = '';
    render();
    if (!text) return;
    const thought = text[0].toUpperCase() + text.slice(1);
    log(`${why}, sending: "${thought}"`);
    onThought(thought);
  }

  function stalled() {
    if (!interim) return;
    log(`recogniser went quiet mid-phrase, using "${interim}"`);
    const s = results.get(interimIdx);
    if (s) s.done = true;
    buffer.push(interim);
    interim = '';
    st.hearing = false;
    dispatch('recogniser stalled');
  }

  function interrupt(heard) {
    stopSpeaking();
    clearTimeout(finishTimer);                             // hold the buffer until the final result confirms
    clearTimeout(stallTimer);
    st.finishing = false;
    onInterrupt?.();
    flash(`Stopped · heard “${normalise(heard)}”`);
    log(`interrupt "${heard}": bypassed the buffer`);
  }

  // Was this word in what we were just saying? Then it's probably the mic hearing our own voice.
  const saidByUs = (text) => normalise(text).split(' ').every((w) => spokenWords.has(w));

  function onResult(e) {
    sessionHeard = true;
    netFails = 0;
    const now = performance.now();
    const interimParts = [];
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const res = e.results[i];
      const text = ((res[0] && res[0].transcript) || '').trim();
      let s = results.get(i);
      if (!s) { s = { echo: st.speaking || now < echoUntil, fired: false, done: false }; results.set(i, s); }
      if (s.done || !text) { if (res.isFinal) s.done = true; continue; }

      const stopWord = isInterrupt(text) && !(s.echo && saidByUs(text));
      if (stopWord && !s.fired) { s.fired = true; pendingInterrupt = true; interrupt(text); }   // instant: interim is enough

      if (res.isFinal) {
        s.done = true;
        if (s.fired) {                                     // interrupt confirmed: drop the half-said thought
          pendingInterrupt = false;
          buffer = [];
          const rest = stopWord ? '' : stripInterrupt(text);   // "stop, what's the churn rate?"
          if (rest) { buffer.push(rest); armFinish(); } else st.finishing = false;
          continue;
        }
        if (s.echo) { log(`ignored while speaking (our own voice): "${text}"`); continue; }
        buffer.push(text);
        log(`final: "${text}", buffered; sending in ${FINISH_MS}ms unless you keep talking`);
        armFinish();                                       // more speech later appends and restarts this
      } else if (!s.echo && !stopWord) {
        interimParts.push(text);
        interimIdx = i;
      }
    }
    interim = interimParts.join(' ');
    if (interim) {                                         // you're still talking: hold the finish window open
      clearTimeout(finishTimer);
      st.finishing = false;
      st.hearing = true;
      clearTimeout(stallTimer);
      stallTimer = setTimeout(stalled, STALL_MS);
    } else st.hearing = false;
    showLive();
    render();
  }

  function makeRecognizer() {
    const r = new Recognition();
    r.continuous = true;                                   // keep going across pauses; we decide when you're done
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.lang = /^en(-|$)/i.test(navigator.language || '') ? navigator.language : 'en-GB';
    r.onstart = () => { live = true; st.starting = false; render(); };
    r.onresult = onResult;
    r.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return;
      log(`recognition error: ${e.error}`);
      if (e.error === 'network' && ++netFails < 3) return;
      const messages = {
        'not-allowed': 'Microphone blocked. Allow it for this site in the address bar, then click the mic again.',
        'service-not-allowed': 'Dictation is turned off in this browser or system (on a Mac, Safari needs Siri or Dictation enabled).',
        'audio-capture': 'No microphone found.',
        network: 'Dictation needs an internet connection: the browser’s built-in recogniser runs online.',
        'language-not-supported': `Dictation doesn’t support ${r.lang} here.`,
      };
      st.mic = false;                                      // fatal; onend follows and won't restart
      flash(messages[e.error] || `Dictation stopped (${e.error}).`, 8000, 'error');
    };
    r.onend = () => {
      live = false;
      st.starting = false;
      st.hearing = false;
      if (pendingInterrupt) buffer = [];
      else if (interim) buffer.push(interim);
      interim = '';
      pendingInterrupt = false;
      results = new Map();
      if (!st.mic) { dispatch('mic switched off'); showLive(); render(); return; }   // send what you said now
      if (buffer.length && !finishTimer) armFinish();
      // Browsers end sessions on their own after silence; keep listening until the mic is switched off.
      const quick = performance.now() - sessionStart < 1000 && !sessionHeard;
      quickEnds = quick ? quickEnds + 1 : 0;
      if (quickEnds >= 5) {
        st.mic = false;
        flash('The browser’s speech recogniser keeps stopping. Click the mic to try again.', 8000, 'error');
        return;
      }
      setTimeout(() => { if (st.mic && !live) startRecognition(); }, 200);
      render();
    };
    return r;
  }

  function startRecognition() {
    rec = rec || makeRecognizer();
    try {
      rec.start();
      st.starting = true;
      sessionStart = performance.now();
      sessionHeard = false;
    } catch { /* already running */ }
    render();
  }

  function toggleMic() {
    if (!Recognition) return;
    if (st.mic) {
      st.mic = false;
      log('mic off');
      if (live) rec.stop();                                // pending audio is finalised, then onend sends it
      else dispatch('mic switched off');
      render();
      return;
    }
    st.mic = true;
    quickEnds = 0;
    netFails = 0;
    log('mic on');
    startRecognition();
  }

  if (micButton) {
    micButton.disabled = !Recognition;
    micButton.addEventListener('click', toggleMic);
  }
  render();

  return {
    say,
    stopSpeaking,
    greet: (line) => say(line),                             // held until the first click
    setThinking(on) { st.thinking = on; render(); },
    isSpeaking: () => st.speaking,
    discardPending() {                                     // you typed and sent a question yourself
      clearTimeout(finishTimer);
      clearTimeout(stallTimer);
      buffer = [];
      interim = '';
      lastLive = '';
      st.finishing = false;
      render();
    },
  };
}
