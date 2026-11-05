/**
 * Terminal presentation for sfrip.
 *
 * Everything the CLI prints goes through here so the look is defined once and
 * the rip logic stays free of formatting. Colour is opt-out: piping to a file
 * or setting NO_COLOR gives plain text, and a `--quiet` run prints nothing but
 * errors.
 */

let colorEnabled = null;

function useColor() {
  if (colorEnabled === null) {
    if (process.env.NO_COLOR) colorEnabled = false;
    else if (process.env.FORCE_COLOR) colorEnabled = true;
    else colorEnabled = process.stdout.isTTY === true;
  }
  return colorEnabled;
}

const wrap = (open, close) => (s) =>
  useColor() ? `\u001b[${open}m${s}\u001b[${close}m` : String(s);

export const c = {
  bold: wrap(1, 22),
  dim: wrap(2, 22),
  red: wrap(31, 39),
  green: wrap(32, 39),
  yellow: wrap(33, 39),
  blue: wrap(34, 39),
  magenta: wrap(35, 39),
  cyan: wrap(36, 39),
  gray: wrap(90, 39),
};

/** Visible width, ignoring ANSI escapes. */
export function width(s) {
  return String(s).replace(/\u001b\[[0-9;]*m/g, "").length;
}

/** Right-pad to a visible width, so columns line up beside coloured text. */
export function pad(s, n) {
  const w = width(s);
  return w >= n ? String(s) : String(s) + " ".repeat(n - w);
}

/** Human byte size. */
export function bytes(n) {
  if (!Number.isFinite(n) || n < 0) return "?";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

/** Thousands separators, for counts a person reads rather than computes. */
export function num(n) {
  return Number(n).toLocaleString("en-US");
}

/** `mm:ss`, for elapsed time. */
export function duration(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

/**
 * A progress bar that redraws in place.
 *
 * Writes to stderr so a piped stdout carries only the result, and degrades to
 * a single line per update when stderr is not a TTY -- redrawing by carriage
 * return would otherwise pile up in a log file.
 */
export class Progress {
  constructor(label, total, { enabled = true } = {}) {
    this.label = label;
    this.total = total;
    this.done = 0;
    this.start = Date.now();
    this.enabled = enabled && process.stderr.isTTY === true;
    this.closed = false;
    if (this.enabled) this.render();
  }

  tick(n = 1) {
    this.done += n;
    if (this.enabled && !this.closed) this.render();
  }

  setLabel(label) {
    this.label = label;
    if (this.enabled && !this.closed) this.render();
  }

  render() {
    const width = 24;
    const frac = this.total > 0 ? Math.min(1, this.done / this.total) : 0;
    const filled = Math.round(frac * width);
    const bar = c.cyan("█".repeat(filled)) + c.gray("░".repeat(width - filled));
    const pct = String(Math.round(frac * 100)).padStart(3);
    const text = `  ${pad(this.label, 22)} ${bar} ${pct}%  ${c.gray(`${this.done}/${this.total}`)}`;
    process.stderr.write(`\r\u001b[2K${text}`);
  }

  finish() {
    if (this.closed) return;
    this.closed = true;
    if (this.enabled) process.stderr.write("\r\u001b[2K");
  }
}

/**
 * A spinner for stages with no countable total.
 *
 * Decrypting and converting report no useful percentage, and a bar stuck at 0%
 * reads as a hang. A spinner says "working" without implying progress it does
 * not have.
 */
export class Spinner {
  constructor(label, { enabled = true } = {}) {
    this.label = label;
    this.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
    this.i = 0;
    this.enabled = enabled && process.stderr.isTTY === true;
    this.timer = null;
    if (this.enabled) {
      this.timer = setInterval(() => this.render(), 90);
      this.timer.unref?.();
    }
  }

  render() {
    const f = this.frames[this.i++ % this.frames.length];
    process.stderr.write(`\r\u001b[2K  ${c.cyan(f)} ${this.label}`);
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.enabled) process.stderr.write("\r\u001b[2K");
  }
}

/** A titled block, for metadata panels. */
export function heading(text) {
  return `\n${c.bold(text)}\n${c.gray("─".repeat(Math.min(64, Math.max(20, width(text)))))}`;
}

/** `label   value`, aligned. */
export function row(label, value, labelWidth = 14) {
  return `  ${c.gray(pad(label, labelWidth))} ${value}`;
}

/**
 * Split a raw input chunk into individual keys.
 *
 * A terminal delivers one keypress per read, but anything non-interactive -- a
 * paste, a pipe, an automated test -- lands several in one chunk. Comparing a
 * whole chunk against a single key silently ignores every keystroke in it,
 * which is what made the arrows look dead.
 *
 * Arrow keys also have more than one encoding in the wild. Windows consoles
 * emit an extra bracket, `ESC [ [ C` rather than the standard `ESC [ C`, and
 * terminals with modifiers emit `ESC [ 1 ; 5 C`. One probe of a real Windows
 * pty showed the double-bracket form, which no ANSI-only matcher would accept.
 *
 * Pure and exported so the decoding can be tested without a terminal, since
 * driving a pty from a test is unreliable and was how this went unnoticed.
 *
 * @param {string} chunk raw bytes decoded as utf8
 * @returns {string[]} one entry per key, arrows normalised to `ESC [ X`
 */
export function decodeKeys(chunk) {
  const patterns = [
    { re: /^\u001b\[\[([ABCD])/, key: (m) => `\u001b[${m[1]}` },
    { re: /^\u001b\[(?:1;\d+)?([ABCD])/, key: (m) => `\u001b[${m[1]}` },
  ];
  const out = [];
  let rest = String(chunk ?? '');
  while (rest.length) {
    let matched = false;
    for (const p of patterns) {
      const m = p.re.exec(rest);
      if (!m) continue;
      out.push(p.key(m));
      rest = rest.slice(m[0].length);
      matched = true;
      break;
    }
    if (matched) continue;
    out.push(rest[0]);
    rest = rest.slice(1);
  }
  return out;
}

/**
 * One step of the settings bar's state machine.
 *
 * Pure, and separate from the terminal, so the controls can be tested by
 * feeding key sequences rather than by driving a pty -- which is unreliable
 * enough that the bug this fixes went unnoticed. The menu keeps only the
 * drawing and the stdin wiring.
 *
 * @param {{cursor: number, settings: object[], actions: object[], state: object}} view
 * @param {string} key one decoded key
 * @returns {{done?: object|null, redraw?: boolean}} `done` set when the menu
 *          should exit: the chosen state, or null for a cancel
 */
export function applyMenuKey(view, key) {
  const { settings, actions, state } = view;
  const total = settings.length + actions.length;

  if (key === '\u0003' || key === 'q' || key === '\u001b') return { done: null };

  if (key === '\u001b[A') {
    view.cursor = (view.cursor - 1 + total) % total;
    return { redraw: true };
  }

  if (key === '\u001b[B' || key === '\r' || key === '\n') {
    /**
     * Down, or enter.
     *
     * They are the same while walking the settings: enter on a settings row
     * has nothing to open, so it advances, letting one key drive the whole bar.
     *
     * On an action row there is nothing to open either, and that is the signal
     * to act. Testing for the last *index* was the bug -- Convert is not last,
     * Cancel follows it, so enter on Convert fell through to "advance" and
     * moved the highlight onto Cancel instead of running.
     *
     * Down keeps advancing past the settings onto the actions and wraps at the
     * end, so every row is reachable with one key. An earlier version stopped
     * on the first action, which left Cancel unreachable by pressing down.
     */
    const onAction = view.cursor >= settings.length;
    if (key === '\r' || key === '\n') {
      if (onAction) {
        const current = actions[view.cursor - settings.length];
        if (current && current.kind === 'cancel') return { done: null };
        return { done: state };
      }
      view.cursor += 1;
      return { redraw: true };
    }
    // Plain down: advance, wrapping.
    view.cursor = (view.cursor + 1) % total;
    return { redraw: true };
  }

  if (key === '\u001b[C' || key === '\u001b[D') {
    const row = settings[view.cursor];
    if (!row) return {};
    const current = Math.max(0, row.options.findIndex((o) => o.value === state[row.id]));
    const next = key === '\u001b[C'
      ? (current + 1) % row.options.length
      : (current - 1 + row.options.length) % row.options.length;
    state[row.id] = row.options[next].value;
    return { redraw: true };
  }

  // A digit picks that option on the highlighted row, for anyone who would
  // rather type a number than press an arrow.
  const n = Number(key);
  const row = settings[view.cursor];
  if (row && Number.isInteger(n) && n >= 1 && n <= row.options.length) {
    state[row.id] = row.options[n - 1].value;
    return { redraw: true };
  }
  return {};
}

/**
 * A settings bar: rows of options laid side by side, one highlighted.
 *
 * Built for the moment after a model has been inspected. The numbers on the
 * panel above decide these answers, so the controls sit under them, and every
 * option is visible at once rather than hidden behind a submenu someone has to
 * open and close to compare.
 *
 *   up/down     move between rows
 *   left/right  change the highlighted row
 *   enter      when the last row is reached, confirm and run
 *   esc / q     leave without converting
 *
 * The confirm row sits last, so walking down through the settings and pressing
 * enter at the bottom is the whole interaction. Enter on a settings row does
 * what "down" does, since there is nothing to open.
 *
 * `--yes` skips all of this, and so does a piped run: the caller's flags are
 * the answer in both cases.
 *
 * @param {string} title
 * @param {Array<{id: string, label: string,
 *                options?: Array<{value: any, label: string}>,
 *                kind?: 'setting'|'confirm'|'cancel'}>} rows
 * @returns {Promise<Object<string, any>|null>} id -> value, or null to abort
 */
export async function menu(title, rows, { enabled = true } = {}) {
  /**
   * Seed every row from the caller's current value, falling back to its first
   * option.
   *
   * Seeding from the first option unconditionally ignored the command line:
   * `--merge=groups --yes` came back as `off`, because the non-interactive
   * path returns this state as-is. A row's initial value is whatever the run
   * was already going to do, so `--yes` and a piped run honour the flags.
   */
  const state = {};
  for (const r of rows) {
    if (!r.options || !r.options.length) continue;
    const wanted = r.value !== undefined ? r.value : r.initial;
    const known = r.options.some((o) => o.value === wanted);
    state[r.id] = known ? wanted : r.options[0].value;
  }
  if (!enabled || !process.stdin.isTTY || !process.stdout.isTTY) return state;

  // Rows that carry a choice, in display order; the action rows follow.
  const settings = rows.filter((r) => r.options && r.options.length);
  const actions = rows.filter((r) => r.kind === 'confirm' || r.kind === 'cancel');

  const indexOf = (row) => {
    const o = row.options.find((x) => x.value === state[row.id]);
    return Math.max(0, row.options.indexOf(o));
  };

  // Cursor lives in the view so the reducer owns every mutation.
  const view = { cursor: 0, settings, actions, state };

  const frame = () => {
    const out = [];
    if (title) {
      out.push(`  ${c.bold(title)}`);
      out.push('');
    }
    for (const row of settings) {
      const active = settings[view.cursor] === row;
      const arrow = active ? c.cyan('❯') : ' ';
      const chips = row.options.map((o, i) => {
        const label = o.label || String(o.value);
        if (i === indexOf(row)) return active ? c.cyan(label) : label;
        return c.gray(label);
      });
      out.push(`  ${arrow} ${c.gray(pad(row.label, 11))} ${chips.join(c.gray(' · '))}`);
    }
    if (settings.length && actions.length) out.push('');
    for (const row of actions) {
      const idx = settings.length + actions.indexOf(row);
      const active = view.cursor === idx;
      out.push(`  ${active ? c.cyan('❯') : ' '} ${active ? c.cyan(row.label) : row.label}`);
    }
    return out;
  };

  const height = frame().length;
  process.stdout.write(frame().join('\n') + '\n');

  return new Promise((resolve) => {
    const stdin = process.stdin;
    const wasRaw = stdin.isRaw;
    stdin.setRawMode?.(true);
    stdin.resume();
    stdin.setEncoding('utf8');

    const finish = (value) => {
      stdin.removeListener('data', onData);
      stdin.setRawMode?.(wasRaw || false);
      stdin.pause();
      resolve(value);
    };

    const draw = () => {
      process.stdout.write(`\u001b[${height}A\u001b[J`);
      process.stdout.write(frame().join('\n') + '\n');
    };

    // The decision logic lives in `applyMenuKey`; this only draws and wires stdin.
    const onData = (chunk) => {
      for (const key of decodeKeys(chunk)) {
        const step = applyMenuKey(view, key);
        if (step.done !== undefined) {
          process.stdout.write('\n');
          finish(step.done);
          return;
        }
        if (step.redraw) draw();
      }
    };

    stdin.setEncoding('utf8');
    stdin.on('data', onData);
  });
}
