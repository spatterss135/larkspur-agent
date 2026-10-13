"""Spike checks 2, 4, 5: structured output on Sonnet 5.5 / Haiku 5.5 inside create_agent,
the judge schema on GPT-6.1 Sol, prompt caching inside a tool loop, and what the
LangSmith trace contains. Costs a few cents."""

import json
import os

from dotenv import load_dotenv

load_dotenv()
os.environ["LANGSMITH_TRACING"] = "true"
os.environ["LANGSMITH_PROJECT"] = "larkspur-spike"

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.middleware import wrap_tool_call  # noqa: E402
from langchain_anthropic import ChatAnthropic  # noqa: E402
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langchain_core.tracers.context import collect_runs  # noqa: E402
from langchain_core.tracers.langchain import wait_for_all_tracers  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langsmith import Client  # noqa: E402
from pydantic import BaseModel  # noqa: E402

# A long, realistic system prompt so the cacheable prefix clears the provider minimum.
POLICY = (
    "Larkspur Hall policy excerpt. Holds last 48 hours and are never bookings. "
    "Tours run Tuesday to Saturday, 10:00 to 16:00, in 45-minute slots. "
    "Prices come only from the pricing tools. Never state a price you did not receive. "
)
SYSTEM = "You are the inquiry assistant for Larkspur Hall, a wedding and event venue.\n" + (
    POLICY * 90
)


@tool
def search_venue_docs(query: str) -> str:
    """Search the venue's documents."""
    return json.dumps(
        {
            "status": "executed",
            "chunks": [
                {
                    "doc_id": "parking",
                    "section": "On-site parking",
                    "text": "Free on-site parking for 120 cars. Overflow at the church lot.",
                }
            ],
        }
    )


@wrap_tool_call
def tag_status(request, handler):
    return handler(request)  # pass-through; we only want the tool span in the trace


class Source(BaseModel):
    doc_id: str
    section: str


class Reply(BaseModel):
    reply: str
    sources: list[Source]


class Verdict(BaseModel):
    evidence: list[str]
    reasoning: str
    verdict: str  # "yes" | "no"


def run_agent(model_id: str):
    agent = create_agent(
        ChatAnthropic(model=model_id, max_tokens=1024),
        [search_venue_docs],
        system_prompt=SYSTEM,
        middleware=[tag_status, AnthropicPromptCachingMiddleware(ttl="5m")],
        response_format=Reply,
    )
    with collect_runs() as cb:
        out = agent.invoke(
            {"messages": [{"role": "user", "content": "Hi! Is there parking at the venue?"}]},
            config={
                "metadata": {"case_id": "spike-parking", "agent_model": model_id},
                "tags": ["spike"],
                "run_name": f"spike-{model_id}",
            },
        )
    usage = [
        (m.usage_metadata or {}).get("input_token_details", {})
        for m in out["messages"]
        if getattr(m, "usage_metadata", None)
    ]
    root = cb.traced_runs[0].id
    print(f"\n== {model_id}")
    print("structured_response:", out.get("structured_response"))
    print("cache details per model call:", usage)
    return root


roots = [run_agent("claude-sonnet-5-5"), run_agent("claude-haiku-5-5")]

judge = ChatOpenAI(model="gpt-6.1-sol").with_structured_output(Verdict, method="json_schema")
v = judge.invoke(
    "Reply: 'We have free parking for 120 cars.' Evidence: 'Free on-site parking for 120 cars.' "
    "Is every factual claim in the reply supported by the evidence? Answer yes or no."
)
print("\n== judge gpt-6.1-sol:", v)

wait_for_all_tracers()
client = Client()
# Finding: collect_runs()[0] is the first model call, not the agent's root run.
# Find the root through the trace instead (parent_run_id is None).
for rid in roots:
    trace_id = client.read_run(rid).trace_id
    runs = list(client.list_runs(project_name="larkspur-spike", trace_id=trace_id))
    root = next(r for r in runs if r.parent_run_id is None)
    meta = (root.extra or {}).get("metadata", {})
    print(f"\n== trace {trace_id}: root={root.name} tags={root.tags}")
    print("   metadata:", {k: meta.get(k) for k in ("case_id", "agent_model")})
    for r in sorted(runs, key=lambda r: r.dotted_order):
        print("   " + "  " * r.dotted_order.count(".") + f"{r.run_type} {r.name}")
