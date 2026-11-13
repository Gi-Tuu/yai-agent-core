import { useEffect, useState } from "react";
import {
  CopilotChat,
  CopilotKitProvider,
  useAgent,
} from "@copilotkit/react-core/v2";
import "@copilotkit/react-core/v2/styles.css";

function EventLog() {
  const { agent } = useAgent();
  const [lines, setLines] = useState([]);

  useEffect(() => {
    if (!agent) return undefined;
    const push = (text) =>
      setLines((current) => [...current.slice(-7), text]);
    const subscription = agent.subscribe({
      onRunStartedEvent() {
        push("RUN_STARTED");
      },
      onTextMessageStartEvent() {
        push("TEXT_MESSAGE_START");
      },
      onTextMessageContentEvent({ textMessageBuffer }) {
        if (textMessageBuffer) push(`TEXT: ${textMessageBuffer.slice(0, 80)}`);
      },
      onTextMessageEndEvent() {
        push("TEXT_MESSAGE_END");
      },
      onToolCallEndEvent({ toolCallName }) {
        push(`TOOL_CALL_END: ${toolCallName}`);
      },
      onRunFinishedEvent() {
        push("RUN_FINISHED");
      },
      onRunErrorEvent({ error }) {
        push(`RUN_ERROR: ${error?.message ?? "error"}`);
      },
    });
    return () => subscription.unsubscribe();
  }, [agent]);

  return (
    <aside className="event-log">
      <h2>AG-UI events</h2>
      <p className="hint">
        由 YAI 事件流映射而来（RUN / TEXT / TOOL / CUSTOM）
      </p>
      {lines.length === 0 ? (
        <p className="empty">发送一条消息后这里会出现事件。</p>
      ) : (
        <ul>
          {lines.map((line, index) => (
            <li key={`${index}-${line}`}>{line}</li>
          ))}
        </ul>
      )}
    </aside>
  );
}

export default function App() {
  return (
    <CopilotKitProvider runtimeUrl="/api/copilotkit">
      <main className="shell">
        <header>
          <span className="badge">YAI × CopilotKit</span>
          <h1>AG-UI 1.0 前端 demo</h1>
          <p>
            浏览器 → CopilotKit Runtime → <code>/api/copilotkit</code> → YAI
            FastAPI (<code>/agent</code>) → SSE
          </p>
        </header>
        <section className="workspace">
          <div className="chat">
            <CopilotChat />
          </div>
          <EventLog />
        </section>
      </main>
    </CopilotKitProvider>
  );
}
