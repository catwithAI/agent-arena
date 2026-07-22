import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";

import type { AgentManifestResponse, WireManifest } from "../api/client";
import { CoverageMatrix } from "../pages/RunDetail";


afterEach(cleanup);

const deerflowManifest: NonNullable<AgentManifestResponse["manifest"]> = {
  status: "final",
  agent: { id: "deerflow", display_name: "DeerFlow", version: "2.0.0" },
  model: {
    requested: "provider/model",
    effective: "provider/model",
    effective_status: "confirmed",
  },
  config_summary: { prompt_hash: "sha256:fixture" },
  capabilities: {
    mcp: { state: "unsupported", basis: "embedded lifecycle not validated" },
    wire: { state: "unsupported", basis: "transport interception not validated" },
  },
  coverage: { structured_events: "verified", token_usage: "verified" },
  cleanup: {},
  outcome: {},
  degradations: [],
};

const aggregateWire: WireManifest = {
  status: "complete",
  sources: [
    {
      kind: "native-event",
      instance: "native-event",
      status: "complete",
      capabilities: {
        call_boundary: "aggregate-only",
        trajectory: "stream-events",
        subagent_identity: false,
      },
    },
  ],
};

function level(name: string): HTMLElement {
  const row = screen.getByText(name).closest(".coverage-row");
  if (!row) throw new Error(`coverage row ${name} is missing`);
  const result = row.querySelector<HTMLElement>("[data-coverage]");
  if (!result) throw new Error(`coverage level ${name} is missing`);
  return result;
}

describe("CoverageMatrix", () => {
  it("distinguishes verified, aggregate-only, and unsupported coverage", () => {
    render(<CoverageMatrix manifest={deerflowManifest} wireManifest={aggregateWire} />);

    expect(level("Prompt")).toHaveAttribute("data-coverage", "verified");
    expect(level("Model")).toHaveAttribute("data-coverage", "verified");
    expect(level("MCP")).toHaveAttribute("data-coverage", "unsupported");
    expect(level("Trajectory")).toHaveAttribute("data-coverage", "verified");
    expect(level("Tokens")).toHaveAttribute("data-coverage", "aggregate-only");
    expect(level("Wire")).toHaveAttribute("data-coverage", "aggregate-only");
    expect(screen.getByText(/child identity unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/no logical call boundary/)).toBeInTheDocument();
  });

  it("renders missing evidence as unknown instead of zero", () => {
    render(<CoverageMatrix manifest={null} wireManifest={{ status: "not_available" }} />);

    for (const name of ["Prompt", "Model", "MCP", "Trajectory", "Tokens", "Wire"])
      expect(level(name)).toHaveAttribute("data-coverage", "unknown");
    expect(screen.queryByText(/0 call/)).not.toBeInTheDocument();
  });
});
