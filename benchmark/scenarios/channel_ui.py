"""
AI chat benchmark through a specific Open WebUI channel using browser UI.
"""

import asyncio
import time
from typing import Optional, Dict, Any, List

from rich.console import Console

from benchmark.clients.browser_client import BrowserClient
from benchmark.clients.http_client import OpenWebUIClient
from benchmark.core.config import BenchmarkConfig
from benchmark.core.metrics import BenchmarkResult
from benchmark.scenarios.chat_ui import ChatUIBenchmark

console = Console()


class ChannelUIBenchmark(ChatUIBenchmark):
    """Chat UI benchmark variant that sends prompts in a specified channel."""

    name = "Channel UI Concurrency"
    description = "Test concurrent AI chat performance via browser UI in a specific channel"
    version = "1.0.0"

    def __init__(
        self,
        config: BenchmarkConfig,
        admin_client: Optional[OpenWebUIClient] = None,
    ):
        super().__init__(config, admin_client=admin_client)
        self._channel_target = (self.config.chat.channel or "").strip()
        self._resolved_channel_id: Optional[str] = None
        self._resolved_channel_name: Optional[str] = None
        self._default_channel_name: str = "benchmark-testing"
        self._channel_created_for_run: bool = False

        # Keep all requests in the same channel context.
        self.config.chat.start_new_chat_between_requests = False

    async def setup(self) -> None:
        """Set up benchmark: authenticate, resolve channel, then create users/browsers."""
        self._setup_signal_handlers()

        try:
            await self._setup_admin_and_model()
            await self._setup_users_and_browsers()
            await self._resolve_or_create_channel()
        except Exception:
            await self.teardown()
            raise

    async def _resolve_or_create_channel(self) -> None:
        """Always create a fresh group channel for the current benchmark run."""
        base_name = self._channel_target or self._default_channel_name
        target = f"{base_name}-{int(time.time())}"
        test_user_ids = [
            client.user.id
            for client in self._test_clients
            if client.user and getattr(client.user, "id", None)
        ]

        created = await self._create_group_channel(target, test_user_ids)
        chosen = created
        self._channel_created_for_run = True

        self._resolved_channel_id = str(chosen.get("id", "")).strip() or None
        self._resolved_channel_name = (
            str(chosen.get("name", "")).strip()
            or str(chosen.get("title", "")).strip()
            or target
        )

        if self._resolved_channel_id:
            # Verify channel is retrievable after resolve/create.
            await self._admin_client.get_channel(self._resolved_channel_id)

        raw_type = str(chosen.get("type", "")).strip().lower()
        if "group" not in raw_type:
            raise RuntimeError(
                f"Channel '{self._resolved_channel_name}' exists but is not a group channel "
                f"(type='{chosen.get('type', '')}'). "
                "This Open WebUI build may not support API group-channel creation yet."
            )

        action = "Created" if self._channel_created_for_run else "Using existing"
        console.print(
            f"[dim]{action} channel: {self._resolved_channel_name} "
            f"(id={self._resolved_channel_id or 'unknown'}, type={chosen.get('type', 'unknown')})[/dim]"
        )

    async def _create_group_channel(self, target: str, user_ids: List[str]) -> Dict[str, Any]:
        """Create a group channel, trying compatible payload variants."""
        description = "Benchmark test channel for channel-ui benchmark"

        payloads: List[Dict[str, Any]] = [
            {
                "name": target,
                "description": description,
                "access_control": None,
                "type": "group",
                "user_ids": user_ids,
                "group_ids": [],
            },
            {
                "name": target,
                "description": description,
                "access_control": None,
                "channel_type": "group",
                "user_ids": user_ids,
                "group_ids": [],
            },
            {
                "name": target,
                "description": description,
                "access_control": None,
                "type": "GROUP",
                "user_ids": user_ids,
                "group_ids": [],
            },
            {
                "name": target,
                "description": description,
                "access_control": None,
                "type": "group_channel",
                "user_ids": user_ids,
                "group_ids": [],
            },
        ]

        last_error: Optional[Exception] = None

        for payload in payloads:
            try:
                response = await self._admin_client.client.post(
                    "/api/v1/channels/create",
                    json=payload,
                    headers=self._admin_client.headers,
                )
                response.raise_for_status()
                created = response.json()

                created_type = str(created.get("type", "")).strip().lower()
                if "group" in created_type:
                    return created

                # Wrong type created; best-effort cleanup and try next variant.
                created_id = str(created.get("id", "")).strip()
                if created_id:
                    try:
                        await self._admin_client.delete_channel(created_id)
                    except Exception:
                        pass
            except Exception as e:
                last_error = e

        raise RuntimeError(
            f"Failed to create group channel '{target}'. Last error: {last_error}"
        )

    async def _get_users_missing_channel_access(self) -> List[str]:
        """Return benchmark user IDs that cannot access the resolved channel."""
        if not self._resolved_channel_id:
            return []

        missing: List[str] = []
        for client in self._test_clients:
            if not client.user or not client.user.id:
                continue
            try:
                # Check the exact capability used by the benchmark. Some builds
                # deny/lag channel listing/detail APIs while message APIs work.
                await client.get_channel_messages(self._resolved_channel_id, limit=1)
            except Exception:
                missing.append(client.user.id)
        return missing

    async def _add_members_to_group_channel(self, missing_user_ids: List[str]) -> None:
        """Grant benchmark users channel access by safely updating channel ACL."""
        if not self._resolved_channel_id or not missing_user_ids:
            return

        channel_id = self._resolved_channel_id
        try:
            channel = await self._admin_client.get_channel(channel_id)

            access_control: Dict[str, Any] = channel.get("access_control") or {}
            read_acl: Dict[str, Any] = access_control.get("read") or {}
            write_acl: Dict[str, Any] = access_control.get("write") or {}

            read_user_ids = list(read_acl.get("user_ids") or [])
            write_user_ids = list(write_acl.get("user_ids") or [])
            read_group_ids = list(read_acl.get("group_ids") or [])
            write_group_ids = list(write_acl.get("group_ids") or [])

            for user_id in missing_user_ids:
                if user_id not in read_user_ids:
                    read_user_ids.append(user_id)
                if user_id not in write_user_ids:
                    write_user_ids.append(user_id)

            update_payload = {
                "name": channel.get("name") or self._resolved_channel_name or "benchmark-testing",
                "description": channel.get("description"),
                "data": channel.get("data"),
                "meta": channel.get("meta"),
                "access_control": {
                    "read": {
                        "user_ids": read_user_ids,
                        "group_ids": read_group_ids,
                    },
                    "write": {
                        "user_ids": write_user_ids,
                        "group_ids": write_group_ids,
                    },
                },
            }

            response = await self._admin_client.client.post(
                f"/api/v1/channels/{channel_id}/update",
                json=update_payload,
                headers=self._admin_client.headers,
            )
            response.raise_for_status()
            console.print("[dim]Updated channel access_control with benchmark users[/dim]")
        except Exception as e:
            console.print(f"[yellow]Warning: failed to update channel ACL: {e}[/yellow]")
            return

        for _ in range(8):
            still_missing = await self._get_users_missing_channel_access()
            if not still_missing:
                return
            await asyncio.sleep(0.5)


    async def _prepare_client_for_session(self, client: BrowserClient, user_num: int) -> None:
        """Navigate each browser session to the configured channel before sending prompts."""
        target = self._resolved_channel_id or self._resolved_channel_name or self._channel_target
        if not target:
            raise RuntimeError("No channel target resolved")

        navigated = await client.navigate_to_channel(target)
        if not navigated and self._resolved_channel_name and target != self._resolved_channel_name:
            navigated = await client.navigate_to_channel(self._resolved_channel_name)

        if not navigated:
            raise RuntimeError(f"Failed to navigate user {user_num + 1} to channel '{target}'")

    async def run(self) -> BenchmarkResult:
        """Run benchmark and annotate metadata with resolved channel info."""
        result = await super().run()
        result.metadata = result.metadata or {}
        result.metadata.update({
            "channel": self._resolved_channel_name or self._channel_target,
            "channel_id": self._resolved_channel_id,
            "channel_created_for_run": self._channel_created_for_run,
        })
        return result
