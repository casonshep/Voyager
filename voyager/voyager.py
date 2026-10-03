"""Voyager: the environment, the Jev client, the GPT handle and the curriculum.

The goal/subtask/action machinery lives in ``voyager.tree`` (decomposer,
traversal, builder); ``run_tree.py`` wires it together. This class only owns
process lifecycle and the shared clients.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from .agents import CurriculumAgent
from .env import VoyagerEnv
from .typesafe import JevClient
from .utils import timing


def make_llm(model: str | None = None, timeout: float = 240.0):
    """Build the GPT handle from ``voyager.tree.llm`` (Package B) without depending on its class name."""
    try:
        from voyager.tree import llm as llm_mod  # type: ignore[attr-defined]
    except Exception as exc:
        print(f"\033[33mGPT handle unavailable: voyager.tree.llm import failed ({exc})\033[0m")
        return None
    kwargs: Dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    for factory_name in ("LLM", "OpenAILLM", "GPT", "Client", "make_llm", "create"):
        factory = getattr(llm_mod, factory_name, None)
        if factory is None:
            continue
        for attempt in (dict(kwargs, timeout=timeout), kwargs, {}):
            try:
                handle = factory(**attempt)
            except TypeError:
                continue
            if hasattr(handle, "complete"):
                return handle
    for name in dir(llm_mod):
        obj = getattr(llm_mod, name)
        if isinstance(obj, type) and hasattr(obj, "complete"):
            try:
                return obj(**kwargs)
            except Exception:
                try:
                    return obj()
                except Exception:
                    continue
    print("\033[33mGPT handle unavailable: no factory with complete() in voyager.tree.llm\033[0m")
    return None


class Voyager:
    def __init__(
        self,
        mc_port: int = None,
        azure_login: Dict[str, str] = None,
        server_port: int = 3000,
        bot_username: str = "bot",
        openai_api_key: str = None,
        openai_model: str | None = None,
        env_wait_ticks: int = 5,
        env_request_timeout: int = 600,
        openai_api_request_timeout: int = 240,
        ckpt_dir: str = "ckpt",
        resume: bool = False,
        long_term_goal: str | None = None,
        typesafe_enabled: bool = True,
        typesafe_model: str = "jev-latest",
    ):
        """
        :param mc_port: minecraft in-game port
        :param azure_login: minecraft login config
        :param server_port: mineflayer port
        :param bot_username: the bot's in-game name
        :param openai_api_key: openai api key (also read from the environment)
        :param openai_model: model for the curriculum, decomposer and builder (``OPENAI_MODEL`` otherwise)
        :param env_wait_ticks: ticks the bot waits after connecting
        :param ckpt_dir: checkpoint dir (tree, primitives, curriculum, typesafe logs)
        :param resume: reload completed goals, the current tree and saved primitives
        :param long_term_goal: optional direction shown to the curriculum
        :param typesafe_enabled: run Jev; silently disabled when TYPESAFE_API_KEY is unset
        :param typesafe_model: TypeSafe model alias
        """
        self.ckpt_dir = ckpt_dir
        timing.configure(ckpt_dir)
        self.env = VoyagerEnv(
            mc_port=mc_port,
            azure_login=azure_login,
            server_port=server_port,
            request_timeout=env_request_timeout,
            log_path=f"./logs/{bot_username}" if bot_username != "bot" else "./logs",
            bot_username=bot_username,
            pause_server=False,
        )
        self.env_wait_ticks = env_wait_ticks
        if openai_api_key:
            os.environ["OPENAI_API_KEY"] = openai_api_key
        self.jev = JevClient(enabled=typesafe_enabled, model=typesafe_model, ckpt_dir=ckpt_dir)
        self.llm = make_llm(openai_model or os.environ.get("OPENAI_MODEL"), timeout=openai_api_request_timeout)
        self.curriculum_agent = CurriculumAgent(self.llm, ckpt_dir=ckpt_dir, resume=resume, long_term_goal=long_term_goal)
        self.resume = resume
        self.started = False

    def start(self) -> Any:
        """Connect the bot. A fresh run clears the inventory; a resumed run keeps it."""
        obs = self.env.reset(options={"mode": "soft" if self.resume else "hard", "wait_ticks": self.env_wait_ticks})
        self.started = True
        self.resume = True
        return obs

    def knowledge(self) -> Dict[str, Any]:
        return self.env.knowledge()

    def close(self):
        try:
            self.env.agent_stop()
        except Exception:
            pass
        self.env.close()
