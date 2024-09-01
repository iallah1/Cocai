#!/usr/bin/env python
import logging
import sys

import chainlit as cl
import phoenix as px
from llama_index.core import Settings
from llama_index.core.agent import AgentRunner, ReActAgent
from llama_index.core.callbacks import CallbackManager, LlamaDebugHandler
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.core.storage.chat_store import SimpleChatStore
from llama_index.core.tools import FunctionTool
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.llms.ollama import Ollama
from phoenix.trace.llama_index import OpenInferenceTraceCallbackHandler

# If Python’s builtin readline module is previously loaded, elaborate line editing and history features will be available.
# https://rich.readthedocs.io/en/stable/console.html#input
from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install

from tools import ToolForSuggestingChoices, roll_a_dice, roll_a_skill

console = Console()

# https://rich.readthedocs.io/en/latest/logging.html#handle-exceptions
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, console=console)],
    # This function does nothing if the root logger already has handlers configured,
    # unless the keyword argument force is set to True.
    # https://docs.python.org/3/library/logging.html#logging.basicConfig
    force=True,
)
logger = logging.getLogger(__name__)

# https://rich.readthedocs.io/en/stable/traceback.html#traceback-handler
logger.debug("Installing rich traceback handler.")
old_traceback_handler = install(show_locals=True, console=console)
logger.debug(
    f"The global traceback handler has been swapped from {old_traceback_handler} to {sys.excepthook}."
)

# "Phoenix can display in real time the traces automatically collected from your LlamaIndex application."
# https://docs.llamaindex.ai/en/stable/module_guides/observability/observability.html
px.launch_app()

# https://docs.llamaindex.ai/en/stable/module_guides/storing/chat_stores/#simplechatstore
try:
    chat_store = SimpleChatStore.from_persist_path(persist_path="chat_store.json")
except Exception as e:
    logger.warning(f"Failed to load chat store from file: {e}, using a new one.")
    chat_store = SimpleChatStore()
chat_memory = ChatMemoryBuffer.from_defaults(
    chat_store=chat_store,
    chat_store_key="user1",
)

# ============= Beginning of the code block for wiring on to models. =============


# This LLM is used by the Agent itself.
Settings.llm = Ollama(
    # https://ollama.com/library/qwen2:7b-instruct
    model="qwen2:7b",
    request_timeout=60,  # secs
    # Uncomment the following line to use the LLM server running on my gaming PC.
    # base_url="http://10.147.20.237:11434",
    streaming=True,
    temperature=0.01,
    additional_kwargs={
        "stop": [
            "<|im_start|>",
            "<|im_end|>",
            "Observation:",
        ],
        "seed": 42,
    },
)

Settings.embed_model = OllamaEmbedding(
    # https://ollama.com/library/nomic-embed-text
    model_name="nomic-embed-text",
    # Uncomment the following line to use the LLM server running on my gaming PC.
    # base_url="http://10.147.20.237:11434",
)

# ============= End of the code block for wiring on to models. =============


def create_callback_manager(should_use_chainlit: bool = True) -> CallbackManager:
    # Phoenix can display in real time the traces automatically collected from your LlamaIndex application.
    # The one-click way is as follows:
    # ```
    # llama_index.core.set_global_handler("arize_phoenix")
    # from llama_index.callbacks.arize_phoenix import (
    #     arize_phoenix_callback_handler,
    # )
    # ```
    # But I prefer to do it manually, so that I can put all callback handlers in one place.
    debug_logger = logging.getLogger("debug")
    debug_logger.setLevel(logging.DEBUG)
    callback_handlers = [
        LlamaDebugHandler(logger=debug_logger),
        OpenInferenceTraceCallbackHandler(),
    ]
    if should_use_chainlit:
        callback_handlers.append(cl.LlamaIndexCallbackHandler())
    return CallbackManager(callback_handlers)


def create_agent(
    should_use_chainlit: bool,
    max_action_steps: int = 5,
) -> AgentRunner:
    # Needed for "Retrieved the following sources" to show up on Chainlit.
    Settings.callback_manager = create_callback_manager(should_use_chainlit)
    all_tools = [
        FunctionTool.from_defaults(
            ToolForSuggestingChoices().suggest_choices,
            return_direct=True,
        ),
        FunctionTool.from_defaults(
            roll_a_dice,
        ),
        FunctionTool.from_defaults(
            roll_a_skill,
        ),
    ]
    agent = ReActAgent.from_tools(
        tools=all_tools,
        verbose=True,
        # x2: An observation step also takes as an iteration.
        # +1: The final output reasoning step needs to take a spot.
        memory=chat_memory,
    )
    # Override the default system prompt for ReAct chats.
    with open("prompts/system_prompt.md") as f:
        MY_SYSTEM_PROMPT = f.read()
    my_system_prompt = MY_SYSTEM_PROMPT.replace(
        # TODO: Use `PromptTemplate.partial_format`. Today, it's not working.
        "{allowance}",
        str(max_action_steps),
    )
    from llama_index.core import PromptTemplate

    system_prompt = PromptTemplate(my_system_prompt)
    agent.update_prompts({"agent_worker:system_prompt": system_prompt})
    return agent


@cl.on_chat_start
async def factory():
    cl.user_session.set("agent", create_agent(should_use_chainlit=True))


@cl.on_chat_end
async def cleanup():
    logger.warning("Interrupted. Persisting chat storage.")
    chat_store.persist(persist_path="chat_store.json")


@cl.on_message
async def main(message: cl.Message):
    """
    ChainLit provides a web GUI for this application.

    See https://docs.chainlit.io/integrations/llama-index.

    Usage:

    ```shell
    chainlit run main.py -w
    ```
    """
    agent: AgentRunner = cl.user_session.get("agent")
    # The Chainlit doc recommends using `await cl.make_async(agent.chat)(message.content)` instead:
    # > The make_async function takes a synchronous function (for instance a LangChain agent) and returns an
    # > asynchronous function that will run the original function in a separate thread. This is useful to run
    # > long running synchronous tasks without blocking the event loop.
    # (https://docs.chainlit.io/api-reference/make-async#make-async)
    # But I think we can just use the async function directly.
    # TODO: streaming seems broken. Why?
    response = await agent.achat(message.content)
    response_message = cl.Message(content="")
    response_message.content = response.response
    await response_message.send()


if __name__ == "__main__":
    # This block
    agent = create_agent(should_use_chainlit=False)
    try:
        agent.chat_repl()
    except KeyboardInterrupt:
        logger.warning("Interrupted. Persisting chat storage.")
        chat_store.persist(persist_path="chat_store.json")
