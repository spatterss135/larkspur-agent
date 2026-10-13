"""Spike checks 1 + 6: a scripted fake model drives create_agent tool calls,
and wrap_tool_call middleware can return a synthetic result without executing.
No API keys needed."""

from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_tool_call
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

executed = []


@tool
def place_hold(date: str) -> str:
    """Place a 48-hour hold on a date."""
    executed.append(date)
    return '{"status": "executed"}'


@tool
def check_availability(date: str) -> str:
    """Check whether a date is open."""
    executed.append(f"check:{date}")
    return '{"status": "executed", "open": true}'


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@dataclass
class Ctx:
    sender_email: str


seen_context = []


@wrap_tool_call
def autonomy(request, handler):
    seen_context.append(request.runtime.context)
    if request.tool_call["name"] == "place_hold":  # pretend level 1: don't execute
        return ToolMessage(
            content='{"status": "pending_approval"}',
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
        )
    return handler(request)  # reads run normally


script = iter(
    [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "check_availability", "args": {"date": "2026-06-13"}, "id": "c1"},
                {"name": "place_hold", "args": {"date": "2026-06-13"}, "id": "c2"},
            ],
        ),
        AIMessage(content="June 13 is open right now. I've asked our coordinator to hold it."),
    ]
)
agent = create_agent(
    ScriptedModel(messages=script),
    [place_hold, check_availability],
    middleware=[autonomy],
    context_schema=Ctx,
)
out = agent.invoke(
    {"messages": [{"role": "user", "content": "Is June 13 open? Please hold it."}]},
    context=Ctx(sender_email="jordan@example.com"),
)

tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
print("executed:", executed)
print("tool results:", [(m.name, m.content) for m in tool_msgs])
print("context seen by middleware:", seen_context[0])
print("final:", out["messages"][-1].content)
assert executed == ["check:2026-06-13"], "place_hold must NOT execute"
assert any("pending_approval" in m.content for m in tool_msgs)
print("CHECK 1 + 6: PASS")
