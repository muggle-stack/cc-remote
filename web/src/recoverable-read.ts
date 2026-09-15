type Schedule<Timer> = (callback: () => void, delayMs: number) => Timer;
type Cancel<Timer> = (timer: Timer) => void;

/** A small, bounded repair cycle for a non-authoritative history/detail read.
 *
 * A growing transcript can remain unstable across one 250 ms retry. Three
 * exponentially-spaced attempts cover an ordinary flush without turning a
 * broken source into a polling loop. A later explicit read starts a fresh
 * cycle after this one is exhausted.
 */
export class RecoverableReadCoordinator<Timer = number> {
  private readonly state = new Map<string, {
    attempts: number;
    timer?: Timer;
  }>();
  private readonly schedule: Schedule<Timer>;
  private readonly cancel: Cancel<Timer>;
  private readonly delayMs: number;
  private readonly maxAttempts: number;
  private readonly backoff: number;

  constructor(
    schedule: Schedule<Timer>,
    cancel: Cancel<Timer>,
    delayMs = 250,
    maxAttempts = 3,
    backoff = 4,
  ) {
    this.schedule = schedule;
    this.cancel = cancel;
    this.delayMs = delayMs;
    this.maxAttempts = Math.max(1, Math.floor(maxAttempts));
    this.backoff = Math.max(1, backoff);
  }

  retry(key: string, read: () => void, delayMs = this.delayMs): boolean {
    const state = this.state.get(key) ?? { attempts: 0 };
    if (state.timer !== undefined) return false;
    // Callers using a long custom watchdog intentionally ask for one probe,
    // not the ordinary short flush-repair sequence.
    const maxAttempts = delayMs === this.delayMs ? this.maxAttempts : 1;
    if (state.attempts >= maxAttempts) {
      this.state.delete(key);
      return false;
    }
    this.state.set(key, state);
    const attemptDelay = delayMs === this.delayMs
      ? delayMs * this.backoff ** state.attempts
      : delayMs;
    state.timer = this.schedule(() => {
      if (this.state.get(key) !== state) return;
      state.timer = undefined;
      state.attempts += 1;
      read();
    }, attemptDelay);
    return true;
  }

  complete(key: string): void {
    const timer = this.state.get(key)?.timer;
    if (timer !== undefined) this.cancel(timer);
    this.state.delete(key);
  }

  clear(): void {
    for (const key of this.state.keys()) this.complete(key);
  }
}
