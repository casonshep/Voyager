"""Python side of the bot process.

``VoyagerEnv`` launches the mineflayer process, connects the bot (``/start``,
``/reset``, ``/stop``, ``/pause``) and exposes the agent-loop surface defined
in PLAN.md section 7: ``knowledge``, ``agent_subtask``, ``agent_status``,
``agent_verify``, ``agent_primitive`` and ``agent_stop``.

The Minecraft server is never paused by the tree runner (``pause_server`` is
False there); the bot keeps acting between Python polls.
"""

import os.path
import time
import warnings
from typing import Any, Dict, Tuple

import requests
import json

import gymnasium as gym
from gymnasium.core import ObsType

import voyager.utils as U
from voyager.utils import timing

from .minecraft_launcher import MinecraftInstance
from .process_monitor import SubprocessMonitor


class VoyagerEnv(gym.Env):
    def __init__(
        self,
        mc_port=None,
        azure_login=None,
        server_host="http://127.0.0.1",
        server_port=3000,
        request_timeout=600,
        log_path="./logs",
        pause_server=False,
        bot_username="bot",
    ):
        if not mc_port and not azure_login:
            raise ValueError("Either mc_port or azure_login must be specified")
        if mc_port and azure_login:
            warnings.warn(
                "Both mc_port and mc_login are specified, mc_port will be ignored"
            )
        self.mc_port = mc_port
        self.azure_login = azure_login
        # The in-game name; several bot processes on one server need distinct names.
        self.bot_username = bot_username
        self.server = f"{server_host}:{server_port}"
        self.server_port = server_port
        self.request_timeout = request_timeout
        self.log_path = log_path
        # When False the Minecraft server is never paused, so the bot keeps
        # acting while Python thinks (the tree runner relies on this).
        self.pause_server = pause_server
        self.mineflayer = self.get_mineflayer_process(server_port)
        if azure_login:
            self.mc_instance = self.get_mc_instance()
        else:
            self.mc_instance = None
        self.has_reset = False
        self.reset_options = None
        self.connected = False
        self.server_paused = False

    # ------------------------------------------------------------------ #
    # process lifecycle
    # ------------------------------------------------------------------ #

    def get_mineflayer_process(self, server_port):
        U.f_mkdir(self.log_path, "mineflayer")
        file_path = os.path.abspath(os.path.dirname(__file__))
        return SubprocessMonitor(
            commands=[
                "node",
                U.f_join(file_path, "mineflayer/index.js"),
                str(server_port),
            ],
            name="mineflayer",
            ready_match=r"Server started on port (\d+)",
            log_path=U.f_join(self.log_path, "mineflayer"),
        )

    def get_mc_instance(self):
        print("Creating Minecraft server")
        U.f_mkdir(self.log_path, "minecraft")
        return MinecraftInstance(
            **self.azure_login,
            mineflayer=self.mineflayer,
            log_path=U.f_join(self.log_path, "minecraft"),
        )

    def check_process(self):
        if self.mc_instance and not self.mc_instance.is_running:
            print("Starting Minecraft server")
            self.mc_instance.run()
            self.mc_port = self.mc_instance.port
            self.reset_options["port"] = self.mc_instance.port
            print(f"Server started on port {self.reset_options['port']}")
        retry = 0
        while not self.mineflayer.is_running:
            print("Mineflayer process has exited, restarting")
            self.mineflayer.run()
            if not self.mineflayer.is_running:
                if retry > 3:
                    raise RuntimeError("Mineflayer process failed to start")
                else:
                    retry += 1
                    continue
            print(self.mineflayer.ready_line)
            res = requests.post(
                f"{self.server}/start",
                json=self.reset_options,
                timeout=self.request_timeout,
            )
            if res.status_code != 200:
                self.mineflayer.stop()
                raise RuntimeError(
                    f"Minecraft server reply with code {res.status_code}"
                )
            return res.json()

    # ------------------------------------------------------------------ #
    # agent loop surface (PLAN.md section 7)
    # ------------------------------------------------------------------ #

    def _post(self, route: str, payload: Dict[str, Any] | None, timeout: int = 60) -> Dict[str, Any]:
        res = requests.post(f"{self.server}{route}", json=payload if payload is not None else {}, timeout=timeout)
        if res.status_code != 200:
            raise RuntimeError(f"{route} failed: {res.status_code} {res.text[:200]}")
        return res.json()

    def _get(self, route: str, timeout: int = 60) -> Dict[str, Any]:
        res = requests.get(f"{self.server}{route}", timeout=timeout)
        if res.status_code != 200:
            raise RuntimeError(f"{route} failed: {res.status_code} {res.text[:200]}")
        return res.json()

    def knowledge(self) -> Dict[str, Any]:
        """``GET /knowledge``: recipes, harvest tools and the mcKnowledge tables for the bot's version."""
        self.check_process()
        with timing.timed("env.knowledge"):
            return self._get("/knowledge", timeout=120)

    def agent_subtask(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /agent/subtask``: hand the loop one subtask to work on.

        ``node`` carries ``id, description, verify, allowed_blocks, budget_actions, kind``.
        Posting the same ``id`` again is a retry of that subtask.
        """
        self.check_process()
        self.unpause()
        with timing.timed("env.agent_subtask"):
            return self._post("/agent/subtask", node)

    def agent_status(self) -> Dict[str, Any]:
        """``GET /agent/status``: snapshot, drained triggers, recent actions, primitives, menu, stats."""
        return self._get("/agent/status")

    def agent_verify(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /agent/verify``: evaluate one VerifySpec against the live world; ``{ok, detail}``."""
        return self._post("/agent/verify", {"spec": spec})

    def agent_primitive(self, name: str, source: str, purpose: str | None = None) -> Dict[str, Any]:
        """``POST /agent/primitive``: register a generated action; ``{ok, error}``.
        ``purpose`` is the subtask it was learned for; Jev sees it on the menu line."""
        body = {"name": name, "source": source}
        if purpose:
            body["purpose"] = purpose
        return self._post("/agent/primitive", body, timeout=30)

    def agent_primitive_remove(self, name: str) -> Dict[str, Any]:
        """``POST /agent/primitive`` with ``remove``: drop a generated action from the menu."""
        return self._post("/agent/primitive", {"name": name, "remove": True}, timeout=30)

    def agent_stop(self) -> Dict[str, Any]:
        """``POST /agent/stop``: the loop idles and raises no triggers until the next subtask."""
        if not self.mineflayer.is_running:
            return {}
        return self._post("/agent/stop", None)

    # ------------------------------------------------------------------ #
    # reset / close / pause
    # ------------------------------------------------------------------ #

    def render(self):
        raise NotImplementedError("render is not implemented")

    def reset(
        self,
        *,
        seed=None,
        options=None,
    ) -> Tuple[ObsType, Dict[str, Any]]:
        if options is None:
            options = {}

        if options.get("inventory", {}) and options.get("mode", "hard") != "hard":
            raise RuntimeError("inventory can only be set when options is hard")

        self.reset_options = {
            "port": self.mc_port,
            "username": self.bot_username,
            "reset": options.get("mode", "hard"),
            "inventory": options.get("inventory", {}),
            "equipment": options.get("equipment", []),
            "spread": options.get("spread", False),
            "waitTicks": options.get("wait_ticks", 5),
            "position": options.get("position", None),
        }

        self.unpause()
        returned_data = None
        with timing.timed("env.reset", mode=self.reset_options["reset"]):
            if self.reset_options["reset"] == "soft":
                returned_data = self.soft_reset()
            if returned_data is None:
                self.mineflayer.stop()
                time.sleep(1)  # wait for mineflayer to exit
                returned_data = self.check_process()
        self.has_reset = True
        self.connected = True
        # All the reset in step will be soft
        self.reset_options["reset"] = "soft"
        self.pause()

        return json.loads(returned_data) if isinstance(returned_data, str) else returned_data

    def soft_reset(self):
        """Reset the running bot in place instead of restarting mineflayer.

        Returns the observation JSON string, or None when no bot is connected or
        the in-process reset failed, in which case the caller falls back to a
        full process restart.
        """
        if not (self.connected and self.mineflayer.is_running):
            return None
        try:
            res = requests.post(
                f"{self.server}/reset",
                json=self.reset_options,
                timeout=self.request_timeout,
            )
        except requests.RequestException as e:
            print(f"In-process reset failed ({e}); restarting mineflayer")
            return None
        if res.status_code != 200:
            print(
                f"In-process reset returned {res.status_code}; restarting mineflayer"
            )
            return None
        return res.json()

    def close(self):
        self.unpause()
        if self.connected:
            try:
                res = requests.post(f"{self.server}/stop", timeout=30)
                if res.status_code == 200:
                    self.connected = False
            except requests.RequestException:
                self.connected = False
        if self.mc_instance:
            self.mc_instance.stop()
        self.mineflayer.stop()
        return not self.connected

    def pause(self):
        if not self.pause_server:
            return False
        if self.mineflayer.is_running and not self.server_paused:
            res = requests.post(f"{self.server}/pause")
            if res.status_code == 200:
                self.server_paused = True
        return self.server_paused

    def unpause(self):
        if self.mineflayer.is_running and self.server_paused:
            res = requests.post(f"{self.server}/pause")
            if res.status_code == 200:
                self.server_paused = False
            else:
                print(res.json())
        return self.server_paused
