import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

vi.mock("../api/client", async (orig) => {
  const actual = await orig<typeof import("../api/client")>();
  const env = (name: string, category: string) => ({
    name,
    skill_id: `lane/${name}`,
    description: `${name} description`,
    category,
    test_focus: `${name} focus`,
    pass_threshold: 60,
    dimensions: [{ name: "quality", weight: 100, description: "Quality" }],
    tool_count: 0,
    task_count: 1,
  });
  return {
    ...actual,
    api: {
      ...actual.api,
      envs: vi.fn(async () => [
        env("spreadsheet", "office-productivity"),
        env("optimizer", "coding"),
        env("legacy", "old-one-off-category"),
      ]),
    },
  };
});

import { Scenarios } from "./Scenarios";

afterEach(() => cleanup());

describe("Scenarios capability groups", () => {
  it("renders stable categories in taxonomy order and preserves legacy metadata", async () => {
    render(<Scenarios />);

    const office = await screen.findByRole("heading", { name: "办公与内容生产" });
    const coding = screen.getByRole("heading", { name: "编程与算法" });
    const fallback = screen.getByRole("heading", { name: "未分类" });

    expect(office.compareDocumentPosition(coding) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(coding.compareDocumentPosition(fallback) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(office.closest("section")!).getByText("spreadsheet")).toBeInTheDocument();
    expect(within(coding.closest("section")!).getByText("optimizer")).toBeInTheDocument();
    expect(within(fallback.closest("section")!).getByText("legacy")).toBeInTheDocument();
  });
});
