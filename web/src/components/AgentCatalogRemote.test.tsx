import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AgentInfo } from "../api/client";
import { AgentCatalogCard } from "./AgentCatalogCard";

describe("remote Agent disclosure", () => {
  it("shows endpoint, residency, upload and cancellation semantics before selection", () => {
    const agent = {
      id: "remote-fixture",
      name: "remote-fixture",
      display_name: "Remote Fixture",
      source: "config",
      transport: "remote",
      availability: { status: "available" },
      status: "available",
      capabilities: {},
      model_support: { binding: "agent-default" },
      metadata: {
        remote_endpoint: "https://remote.test/api/",
        data_residency: "eu-west",
        uploads_source_files: true,
        cancellation_semantics: "best-effort-unknown",
      },
      spec_hash: `sha256:${"c".repeat(64)}`,
      warnings: [],
    } satisfies AgentInfo;

    render(
      <AgentCatalogCard
        agent={agent}
        index={0}
        inputType="checkbox"
        checked={false}
        onChange={vi.fn()}
      />,
    );

    const disclosure = screen.getByLabelText("remote-fixture remote data disclosure");
    expect(disclosure.textContent).toContain("https://remote.test/api/");
    expect(disclosure.textContent).toContain("eu-west");
    expect(disclosure.textContent).toContain("source upload · enabled");
    expect(disclosure.textContent).toContain("best-effort-unknown");
  });
});
