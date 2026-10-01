import os.path
import time
import warnings
from typing import SupportsFloat, Any, Tuple, Dict

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
        pause_server=True,
    ):
        if not mc_port and not azure_login:
            raise ValueError("Either mc_port or azure_login must be specified")
        if mc_port and azure_login:
            warnings.warn(
                "Both mc_port and mc_login are specified, mc_port will be ignored"
            )
        self.mc_port = mc_port
        self.azure_login = azure_login
        self.server = f"{server_host}:{server_port}"
        self.server_port = server_port
        self.request_timeout = request_timeout
        self.log_path = log_path
        # When False the Minecraft server is never paused between steps, so the
        # bot keeps acting while Python thinks (used by the fast loop).
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
            # if self.mc_instance:
            #     self.mc_instance.check_process()
            #     if not self.mc_instance.is_running:
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

    def step(
        self,
        code: str,
        programs: str = "",
    ) -> Tuple[ObsType, SupportsFloat, bool, bool, Dict[str, Any]]:
        if not self.has_reset:
            raise RuntimeError("Environment has not been reset yet")
        self.check_process()
        self.unpause()
        data = {
            "code": code,
            "programs": programs,
        }
        with timing.timed("env.step"):
            res = requests.post(
                f"{self.server}/step", json=data, timeout=self.request_timeout
            )
        if res.status_code != 200:
            raise RuntimeError("Failed to step Minecraft server")
        returned_data = res.json()
        self.pause()
        return json.loads(returned_data)

    # ------------------------------------------------------------------ #
    # Fast loop: a goal-driven controller that runs inside the mineflayer
    # process. Python posts goals and polls status; the bot never waits
    # for Python between actions.
    # ------------------------------------------------------------------ #

    def fast_goal(self, goal: Dict[str, Any]) -> Dict[str, Any]:
        """Replace the fast loop's current goal and (re)start it."""
        self.check_process()
        self.unpause()
        with timing.timed("env.fast_goal"):
            res = requests.post(f"{self.server}/fast/goal", json=goal, timeout=60)
        if res.status_code != 200:
            raise RuntimeError(f"fast/goal failed: {res.status_code} {res.text[:200]}")
        return res.json()

    def fast_subgoals(self, text: str, target: Dict[str, Any] = None) -> Dict[str, Any]:
        """Candidate subgoals for a high goal, derived in Node from recipes and the world."""
        payload: Dict[str, Any] = {"text": text}
        if target:
            payload["target"] = target
        res = requests.post(f"{self.server}/fast/subgoals", json=payload, timeout=60)
        if res.status_code != 200:
            raise RuntimeError(f"fast/subgoals failed: {res.status_code} {res.text[:200]}")
        return res.json()

    def fast_status(self) -> Dict[str, Any]:
        """Drain the fast loop's triggers and observations since the last poll."""
        res = requests.get(f"{self.server}/fast/status", timeout=60)
        if res.status_code != 200:
            raise RuntimeError(f"fast/status failed: {res.status_code} {res.text[:200]}")
        data = res.json()
        if isinstance(data.get("events"), str):
            data["events"] = json.loads(data["events"])
        return data

    def fast_stop(self) -> Dict[str, Any]:
        """Stop the fast loop; the bot idles until the next goal."""
        if not self.mineflayer.is_running:
            return {}
        res = requests.post(f"{self.server}/fast/stop", timeout=60)
        if res.status_code != 200:
            raise RuntimeError(f"fast/stop failed: {res.status_code} {res.text[:200]}")
        return res.json()

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

        return json.loads(returned_data)

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
            res = requests.post(f"{self.server}/stop")
            if res.status_code == 200:
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
