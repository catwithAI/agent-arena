import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";

import type { AgentInfo } from "../api/client";
import { AgentCatalogCard } from "./AgentCatalogCard";


function agent(status: AgentInfo["availability"]["status"]): AgentInfo {
  return {
    id: "fixture-agent",
    name: "fixture-agent",
    display_name: "Fixture Agent",
    source: "config",
    transport: "local-cli",
    availability: { status, version: "1.2.3", reason: `reason: ${status}` },
    version: "1.2.3",
    status: status === "available" ? "available" : "not_found",
    detail: `reason: ${status}`,
    cli_path: "/bin/fixture",
    capabilities: {
      single_turn: { state: "verified" },
      resume_send_message: { state: "unsupported" },
      mcp: { state: "declared" },
      structured_events: { state: "verified" },
      token_usage: { state: "unsupported" },
      wire: { state: "declared" },
    },
    model_support: { binding: "agent-default" },
    metadata: { installation_url: "https://example.invalid/install" },
    spec_hash: "sha256:fixture",
    warnings: [],
  };
}

afterEach(cleanup);

describe("AgentCatalogCard", () => {
  it("shows v2 metadata and distinguishes verified from declared capabilities", () => {
    render(
      <AgentCatalogCard
        agent={agent("available")}
        index={0}
        inputType="checkbox"
        checked={false}
        onChange={() => undefined}
      />,
    );

    expect(screen.getByText("Fixture Agent")).toBeInTheDocument();
    expect(screen.getByText(/config · local-cli · v1.2.3/)).toBeInTheDocument();
    expect(screen.getByTitle("single: verified")).toHaveClass("verified");
    expect(screen.getByTitle("MCP: declared")).toHaveClass("declared");
    expect(screen.getByTitle("multi: unsupported")).toHaveClass("unsupported");
  });

  it("is keyboard selectable when available", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentCatalogCard
        agent={agent("available")}
        index={0}
        inputType="checkbox"
        checked={false}
        onChange={onChange}
      />,
    );

    await user.tab();
    await user.keyboard(" ");
    expect(onChange).toHaveBeenCalledOnce();
  });

  for (const status of [
    "not_installed",
    "version_unsupported",
    "missing_auth",
    "missing_dependency",
    "misconfigured",
    "unknown",
  ] as const) {
    it(`disables selection and explains ${status}`, () => {
      render(
        <AgentCatalogCard
          agent={agent(status)}
          index={0}
          inputType="radio"
          checked={false}
          onChange={() => undefined}
        />,
      );

      expect(screen.getByRole("radio", { name: /Fixture Agent/ })).toBeDisabled();
      expect(screen.getByText(`reason: ${status}`)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /installation guide/ })).toHaveAttribute(
        "href",
        "https://example.invalid/install",
      );
    });
  }
});
