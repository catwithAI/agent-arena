import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AgentCatalogCard } from "./AgentCatalogCard";
import type { AgentInfo } from "../api/client";

describe("ACP catalog disclosure", () => {
  it("shows the registry pin, distribution and data boundary", () => {
    const agent = {
      id: "acp:alpha@1.2.3",
      name: "acp:alpha@1.2.3",
      display_name: "Alpha",
      source: "config",
      transport: "acp",
      availability: { status: "available" },
      status: "available",
      capabilities: {},
      model_support: { binding: "agent-default" },
      metadata: {
        registry_sha256: `sha256:${"a".repeat(64)}`,
        distribution: { npx: { package: "@example/alpha@1.2.3" } },
        data_boundary: "local ACP subprocess; agent-specific network behavior applies",
      },
      spec_hash: `sha256:${"b".repeat(64)}`,
      warnings: [],
    } satisfies AgentInfo;
    const onChange = vi.fn();

    render(
      <AgentCatalogCard
        agent={agent}
        index={0}
        inputType="checkbox"
        checked={false}
        onChange={onChange}
      />,
    );

    expect(screen.getByText(/registry · aaaaaaaaaaaa · npx/)).not.toBeNull();
    expect(screen.getByText(/agent-specific network behavior/)).not.toBeNull();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onChange).toHaveBeenCalled();
  });
});
