"""Explicit in-memory bindings for lifecycle tests, separate from runtime discovery."""

from types import SimpleNamespace


def make_bridge():
    return SimpleNamespace(
        instruction="Recover from ineffective actions",
        max_steps=10,
        step=0,
        done=False,
        termination=None,
        provider_requests={},
        close=lambda: None,
    )


class FixtureBinding:
    def __init__(self, bridge):
        self.bridge = bridge

    @property
    def observation_id(self):
        return f"observation:{self.bridge.step}"

    def observation(self):
        return {
            "id": self.observation_id,
            "channels": ["text"],
            "status": "observed",
            "content": [{"type": "text", "text": f"position={self.bridge.step}"}],
        }

    def catalog(self):
        return [
            {
                "id": "fixture.advance",
                "version": "v1",
                "readiness": "ready",
                "effect": "physical",
                "requires_current_observation": True,
                "input_schema": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}, "delta": {"const": 1}},
                    "required": ["request_id", "delta"],
                    "additionalProperties": False,
                },
            }
        ]

    def validate_request(self, request):
        if request["arguments"]["request_id"] != request["request_id"]:
            raise ValueError("Native and broker request identifiers differ")

    def execute(self, tool, arguments):
        self.bridge.step += 1
        return {"observations": [self.observation()], "control_steps": 1, "isError": False}
