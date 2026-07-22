// Compare submit pages: request-body assembly for same-model / multi-model.
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { MemoryRouter } from "react-router-dom";

const { createRunMock } = vi.hoisted(() => ({
  createRunMock: vi.fn(async () => ({
    run_id: "r1", task_id: "t", env_name: "e", agents: [], attempts: [],
  })),
}));

vi.mock("../api/client", async (orig) => {
  const actual = await orig<typeof import("../api/client")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      envs: vi.fn(async () => [{
        name: "order-desk", skill_id: "lane/order-desk", description: "",
        category: "tool-use", test_focus: "", pass_threshold: null, dimensions: [],
        tool_count: 2, task_count: 1, available: true,
        agent_modalities: ["image"], prerequisite_warnings: [],
      }]),
      envTasks: vi.fn(async () => [{
        id: "task_1", env_name: "order-desk", prompt: "p",
        context: {}, constraints: {}, timeout_seconds: 600,
      }]),
      agents: vi.fn(async () => [
        { name: "claude-code", status: "available" as const },
        { name: "codex", status: "available" as const },
      ]),
      modelProviders: vi.fn(async () => ({ providers: ["openrouter"], suggested: [] })),
      openrouterModels: vi.fn(async () => ({
        models: [
          {
            id: "z-ai/glm-5.2", name: "GLM", context_length: 200000,
            input_modalities: ["text"], output_modalities: ["text"],
          },
        ],
        error: null,
      })),
      createRun: createRunMock,
    },
  };
});

import { SameModelSubmit } from "./SameModelSubmit";
import { MultiModelSubmit } from "./MultiModelSubmit";
import { ApiRequestError } from "../api/client";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SameModelSubmit", () => {
  it("submits compare_mode=same-model with a provider-prefixed shared model", async () => {
    render(<MemoryRouter><SameModelSubmit /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/通信采集档/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/模型 ID/), { target: { value: "z-ai/glm-5.2" } });
    fireEvent.click(screen.getByRole("radio", { name: /^full$/ }));
    const submit = screen.getByRole("button", { name: /运行同模型对比/ });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() => expect(createRunMock).toHaveBeenCalled());
    const body = (createRunMock.mock.calls[0] as unknown[])[0] as Record<string, unknown>;
    expect(body.compare_mode).toBe("same-model");
    expect(body.agents).toEqual(["claude-code", "codex"]);
    expect(body.model).toBe("openrouter/z-ai/glm-5.2");
    expect(body.capture_policy).toBe("full");
    expect(body.execution).toBe("serial");
  });

  it("warns when the env needs a modality the model lacks", async () => {
    render(<MemoryRouter><SameModelSubmit /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/通信采集档/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/模型 ID/), { target: { value: "z-ai/glm-5.2" } });
    // env requires image input; the picked model is text-only.
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/image/));
  });

  it("renders structured compatibility rejection with the specific agent", async () => {
    createRunMock.mockRejectedValueOnce(
      new ApiRequestError("POST /api/runs -> 400", 400, {
        detail: {
          code: "agent_compatibility_mismatch",
          reports: [
            {
              agent_id: "codex",
              issues: [
                { code: "agent_model_unsupported", message: "requested model is unsupported" },
              ],
            },
          ],
        },
      }),
    );
    render(<MemoryRouter><SameModelSubmit /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/通信采集档/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/模型 ID/), { target: { value: "shared-model" } });
    const submit = screen.getByRole("button", { name: /运行同模型对比/ });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    await waitFor(() =>
      expect(screen.getByText(/Compatibility check failed/)).toHaveTextContent(
        /codex: requested model is unsupported/,
      ),
    );
  });
});

describe("MultiModelSubmit", () => {
  it("submits compare_mode=multi-model with one agent and N model refs", async () => {
    render(<MemoryRouter><MultiModelSubmit /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/参赛模型/)).toBeInTheDocument());

    const custom = screen.getByLabelText(/手动输入模型 ID/);
    fireEvent.change(custom, { target: { value: "m-one" } });
    fireEvent.keyDown(custom, { key: "Enter" });
    fireEvent.change(custom, { target: { value: "m-two" } });
    fireEvent.keyDown(custom, { key: "Enter" });

    fireEvent.click(screen.getByRole("button", { name: /运行多模型对比/ }));

    await waitFor(() => expect(createRunMock).toHaveBeenCalled());
    const body = (createRunMock.mock.calls[0] as unknown[])[0] as Record<string, unknown>;
    expect(body.compare_mode).toBe("multi-model");
    expect(body.agents).toEqual(["claude-code"]);
    expect(body.models).toEqual(["openrouter/m-one", "openrouter/m-two"]);
  });

  it("blocks submit with fewer than 2 models", async () => {
    render(<MemoryRouter><MultiModelSubmit /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText(/参赛模型/)).toBeInTheDocument());

    const custom = screen.getByLabelText(/手动输入模型 ID/);
    fireEvent.change(custom, { target: { value: "only-one" } });
    fireEvent.keyDown(custom, { key: "Enter" });

    const btn = screen.getByRole("button", { name: /运行多模型对比/ }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(createRunMock).not.toHaveBeenCalled();
  });
});
