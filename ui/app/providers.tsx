"use client";

import { ReactNode, useMemo } from "react";
import { CopilotKit } from "@copilotkit/react-core";
import { HttpAgent } from "@ag-ui/client";

const AGENT_URL =
  process.env.NEXT_PUBLIC_AGENT_URL || "http://127.0.0.1:8000/agui";

export default function Providers({ children }: { children: ReactNode }) {
  class PatchedHttpAgent extends HttpAgent {
    connect(input: any) {
      return this.run(input);
    }
  }

  const agent = useMemo(() => new PatchedHttpAgent({ url: AGENT_URL }), []);

  return (
    <CopilotKit
      runtimeUrl={AGENT_URL}
      agents__unsafe_dev_only={{ default: agent }}
    >
      {children}
    </CopilotKit>
  );
}
