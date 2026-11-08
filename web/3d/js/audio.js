// Multisensory alerts: sound (Web Audio API), voice (Web Speech API), vibration (mobile) and a flashing frame.
// Browsers allow sound only after a user gesture, so the alerts are enabled by the "Звук" button.
import { WARN, CRIT } from './status.js';

export class Alerts {
  constructor() {
    this.ctx = null;
    this.enabled = false;
    this.voice = true;
    this.critical = new Map();       // key -> text of an active, not yet acknowledged critical alarm
    this.repeat = null;
    this.onChange = () => {};
  }

  enable(on) {
    this.enabled = on;
    if (on && !this.ctx) this.ctx = new AudioContext();
    if (on) this.ctx.resume();
    this._schedule();
  }

  _tone(freq, start, dur, gain = 0.18, type = 'sine') {
    const o = this.ctx.createOscillator(), g = this.ctx.createGain();
    o.type = type;
    o.frequency.value = freq;
    const t = this.ctx.currentTime + start;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(gain, t + 0.01);
    g.gain.setValueAtTime(gain, t + dur - 0.03);
    g.gain.linearRampToValueAtTime(0, t + dur);
    o.connect(g).connect(this.ctx.destination);
    o.start(t);
    o.stop(t + dur);
  }

  // earcons: the pitch pattern itself tells the level, without looking at the screen
  _warnSound() { this._tone(660, 0, 0.14); this._tone(660, 0.22, 0.14); }
  _critSound() { for (let i = 0; i < 3; i++) { this._tone(880, i * 0.5, 0.22, 0.22, 'square'); this._tone(620, i * 0.5 + 0.25, 0.22, 0.22, 'square'); } }
  _okSound() { this._tone(523, 0, 0.12); this._tone(784, 0.14, 0.2); }

  _say(text) {
    if (!this.voice || !('speechSynthesis' in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'uk-UA';
    u.rate = 1.05;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  // level of an element changed: key = printer/node
  transition(key, from, to, text) {
    if (to === CRIT) this.critical.set(key, text);
    else this.critical.delete(key);
    if (this.enabled) {
      if (to === CRIT) { this._critSound(); this._say(text); navigator.vibrate?.([300, 120, 300, 120, 300]); }
      else if (to === WARN && from < WARN) { this._warnSound(); this._say(text); navigator.vibrate?.(150); }
      else if (to < WARN && from >= WARN) this._okSound();
    }
    this._schedule();
    this.onChange();
  }

  acknowledge() {
    this.critical.clear();
    this._schedule();
    this.onChange();
  }

  _schedule() {                    // an unacknowledged critical alarm repeats every 6 s
    clearInterval(this.repeat);
    this.repeat = this.enabled && this.critical.size ? setInterval(() => this._critSound(), 6000) : null;
  }
}
