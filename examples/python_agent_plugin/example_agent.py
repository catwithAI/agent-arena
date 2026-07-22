from backend.agents.python_plugin import PythonAgentContext, PythonAgentOutput


class ExampleAgent:
    async def run(self, context: PythonAgentContext) -> PythonAgentOutput:
        artifact = context.artifact_path("answer.txt")
        artifact.write_text(f"Processed: {context.prompt}\n", encoding="utf-8")
        return PythonAgentOutput(
            final_text="Created answer.txt",
            events=({"type": "artifact_created", "path": "answer.txt"},),
            artifacts=("answer.txt",),
            effective_model=context.model,
        )
