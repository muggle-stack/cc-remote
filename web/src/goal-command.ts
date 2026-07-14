export type GoalCommand =
  | { kind: "show" }
  | { kind: "clear" }
  | { kind: "set"; objective: string };

/** Parse the shared Claude/Codex /goal argument contract. */
export function parseGoalCommand(args: string): GoalCommand {
  const value = args.trim();
  if (!value) return { kind: "show" };
  if (value.toLowerCase() === "clear") return { kind: "clear" };
  return { kind: "set", objective: value };
}
