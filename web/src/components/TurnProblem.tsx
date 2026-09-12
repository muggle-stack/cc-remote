import { Icon } from "../icons";
import { presentHistoricalTurnProblem } from "../problem-presentation";

export default function TurnProblem({ message, continuing }: {
  message: string;
  continuing: boolean;
}) {
  return <div className="turn-problem" role="status">
    <Icon name="info" size={18} />
    <div>
      <p>{presentHistoricalTurnProblem(message, continuing)}</p>
      {continuing && <p className="turn-problem-continuation">
        后续回复正在处理中，无需重复发送。
      </p>}
    </div>
  </div>;
}
