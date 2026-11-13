import { hasHarmonyPush, enableHarmonyPush, disableHarmonyPush } from "./harmony-push.ts";
import type { RemoteNotificationMode } from "./notification-mode";

export interface PushBindingTarget {
  machineIds: string[];
  mode: RemoteNotificationMode;
}

export type PushBindingState = "off" | "binding" | "remote" | "local";

export interface PushBindingSnapshot {
  state: PushBindingState;
  target: PushBindingTarget | null;
  bound: PushBindingTarget | null;
}

// false preserves the previous binding; null means the adapter revoked it while
// rolling back a failed mutation. Neither result commits the requested target.
export type PushBindingResult = boolean | null;
type PushBindingApply = (target: PushBindingTarget | null) => Promise<PushBindingResult>;
type PushBindingListener = (snapshot: PushBindingSnapshot) => void;

function sameTarget(
  left: PushBindingTarget | null,
  right: PushBindingTarget | null,
): boolean {
  if (!left || !right) return left === right;
  return left.mode === right.mode
    && left.machineIds.length === right.machineIds.length
    && left.machineIds.every((machine, index) => machine === right.machineIds[index]);
}

function copyTarget(target: PushBindingTarget | null): PushBindingTarget | null {
  return target ? { ...target, machineIds: [...target.machineIds] } : null;
}

export class PushBindingCoordinator {
  private readonly apply: PushBindingApply;
  private desired: PushBindingTarget | null = null;
  private bound: PushBindingTarget | null = null;
  private state: PushBindingState = "off";
  private drain: Promise<void> | null = null;
  private refresh = false;
  private listener: PushBindingListener | null = null;

  constructor(apply: PushBindingApply) {
    this.apply = apply;
  }

  subscribe(listener: PushBindingListener): () => void {
    this.listener = listener;
    listener(this.snapshot());
    return () => {
      if (this.listener === listener) this.listener = null;
    };
  }

  snapshot(): PushBindingSnapshot {
    return {
      state: this.state,
      target: copyTarget(this.desired),
      bound: copyTarget(this.bound),
    };
  }

  isRemoteActive(machineId: string): boolean {
    return !!this.desired?.machineIds.includes(machineId)
      && !!this.bound?.machineIds.includes(machineId);
  }

  async setTarget(target: PushBindingTarget | null, refresh = false): Promise<PushBindingSnapshot> {
    this.desired = target ? {
      ...target, machineIds: [...new Set(target.machineIds)].sort(),
    } : null;
    this.refresh ||= refresh;
    if (!this.drain) {
      this.drain = this.run().finally(() => {
        this.drain = null;
      });
    }
    await this.drain;
    return this.snapshot();
  }

  private publish(state: PushBindingState): void {
    this.state = state;
    this.listener?.(this.snapshot());
  }

  private async run(): Promise<void> {
    while (true) {
      const target = copyTarget(this.desired);
      if (sameTarget(target, this.bound) && !this.refresh) {
        this.publish(target ? "remote" : "off");
        return;
      }
      this.refresh = false;
      this.publish("binding");
      let enabled: PushBindingResult = false;
      try {
        enabled = await this.apply(target);
      } catch {
        // A failed adapter must settle through the same path as a rejected
        // binding, including any newer target queued while it was in flight.
      }
      if (enabled) this.bound = target;
      else if (enabled === null) this.bound = null;

      // A newer request arrived while the network mutation was in flight.
      // Continue from the actual binding that just completed; never publish the
      // stale target as the current machine.
      if (!sameTarget(target, this.desired) || this.refresh) continue;
      if (!target) {
        if (enabled) this.bound = null;
        this.publish("off");
      } else if (enabled || target.machineIds.every(machine => this.bound?.machineIds.includes(machine))) {
        // During a same-machine mode update the prior remote subscription still
        // delivers. Treat it as remote to avoid a duplicate local notification.
        this.publish("remote");
      } else {
        this.publish("local");
      }
      return;
    }
  }
}

export async function enableRemotePush(machineIds: string[], mode: RemoteNotificationMode = "generic"): Promise<PushBindingResult> {
  try {
    if (hasHarmonyPush()) return await enableHarmonyPush(machineIds, mode);
    return await (await import("./web-push.ts")).enableRemotePush(machineIds, mode);
  } catch {
    return false;
  }
}

export async function disableRemotePush(): Promise<boolean> {
  try {
    if (hasHarmonyPush()) return await disableHarmonyPush();
    return await (await import("./web-push.ts")).disableRemotePush();
  } catch {
    return false;
  }
}
