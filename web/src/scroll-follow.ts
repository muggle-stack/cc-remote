export interface ScrollMetrics {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
}

export interface ScrollFollowSnapshot {
  followOutput: boolean;
  nearBottom: boolean;
}

export interface BottomMeasurement {
  distance: number;
  atBottom: boolean;
  nearBottom: boolean;
}

export const NEAR_BOTTOM_PX = 80;
export const AT_BOTTOM_PX = 2;

const SCROLL_DIRECTION_EPSILON_PX = 0.5;

export function measureBottom(metrics: ScrollMetrics): BottomMeasurement {
  const distance = Math.max(
    0,
    metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight,
  );
  return {
    distance,
    atBottom: distance <= AT_BOTTOM_PX,
    nearBottom: distance <= NEAR_BOTTOM_PX,
  };
}

export function anchoredScrollTop(
  previousScrollTop: number,
  previousScrollHeight: number,
  nextScrollHeight: number,
): number {
  return Math.max(
    0,
    previousScrollTop + nextScrollHeight - previousScrollHeight,
  );
}

/**
 * Keeps output-follow intent separate from the current geometric position.
 * Being close to the bottom must never re-enable following after the user has
 * asked to read history; only reaching the actual bottom (or an explicit
 * resume) does that.
 */
export class ScrollFollowController {
  private current: ScrollFollowSnapshot = {
    followOutput: true,
    nearBottom: true,
  };

  private lastScrollTop = 0;

  snapshot(): ScrollFollowSnapshot {
    return { ...this.current };
  }

  isFollowing(): boolean {
    return this.current.followOutput;
  }

  reset(metrics: ScrollMetrics): ScrollFollowSnapshot {
    this.lastScrollTop = metrics.scrollTop;
    this.current = {
      followOutput: true,
      nearBottom: measureBottom(metrics).nearBottom,
    };
    return this.snapshot();
  }

  pause(metrics: ScrollMetrics): ScrollFollowSnapshot {
    this.lastScrollTop = metrics.scrollTop;
    this.current = {
      followOutput: false,
      nearBottom: measureBottom(metrics).nearBottom,
    };
    return this.snapshot();
  }

  resume(metrics: ScrollMetrics): ScrollFollowSnapshot {
    this.lastScrollTop = metrics.scrollTop;
    this.current = {
      followOutput: true,
      nearBottom: measureBottom(metrics).nearBottom,
    };
    return this.snapshot();
  }

  /** Record a DOM scrollTop write without treating its later scroll event as intent. */
  recordProgrammaticScroll(metrics: ScrollMetrics): ScrollFollowSnapshot {
    this.lastScrollTop = metrics.scrollTop;
    this.current = {
      ...this.current,
      nearBottom: measureBottom(metrics).nearBottom,
    };
    return this.snapshot();
  }

  /** Content resized. Update geometry, but never infer user intent from layout. */
  observeLayout(metrics: ScrollMetrics): ScrollFollowSnapshot {
    this.current = {
      ...this.current,
      nearBottom: measureBottom(metrics).nearBottom,
    };
    return this.snapshot();
  }

  /** Handle a real viewport movement, including scrollbar and keyboard scrolls. */
  observeScroll(metrics: ScrollMetrics): ScrollFollowSnapshot {
    const movingTowardHistory =
      metrics.scrollTop < this.lastScrollTop - SCROLL_DIRECTION_EPSILON_PX;
    const movingTowardLatest =
      metrics.scrollTop > this.lastScrollTop + SCROLL_DIRECTION_EPSILON_PX;
    const measurement = measureBottom(metrics);

    let followOutput = this.current.followOutput;
    // Layout shrinkage can clamp scrollTop downward while the viewport remains
    // at the real bottom. That is geometry, not an upward reading gesture.
    if (movingTowardHistory && !measurement.atBottom) {
      followOutput = false;
    } else if (!followOutput && movingTowardLatest && measurement.atBottom) {
      followOutput = true;
    }

    this.lastScrollTop = metrics.scrollTop;
    this.current = { followOutput, nearBottom: measurement.nearBottom };
    return this.snapshot();
  }
}

export interface FrameCoalescer {
  schedule: (task: () => void) => void;
  cancel: () => void;
}

/** Collapse arbitrarily many stream/layout updates into one write per frame. */
export function createFrameCoalescer(
  requestFrame: (callback: () => void) => number,
  cancelFrame: (id: number) => void,
): FrameCoalescer {
  let frameId: number | null = null;
  let pendingTask: (() => void) | null = null;

  return {
    schedule(task) {
      pendingTask = task;
      if (frameId != null) return;
      frameId = requestFrame(() => {
        frameId = null;
        const run = pendingTask;
        pendingTask = null;
        run?.();
      });
    },
    cancel() {
      pendingTask = null;
      if (frameId == null) return;
      cancelFrame(frameId);
      frameId = null;
    },
  };
}
