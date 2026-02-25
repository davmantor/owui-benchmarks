"""
AI Chat UI concurrency benchmark with auto-scaling support.

Tests Open WebUI performance with multiple concurrent browser users,
optionally auto-scaling to find the maximum sustainable user count.
"""

import asyncio
import random
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from benchmark.core.base import BaseBenchmark
from benchmark.core.config import BenchmarkConfig
from benchmark.core.metrics import MetricsCollector, BenchmarkResult
from benchmark.clients.http_client import OpenWebUIClient, ClientPool
from benchmark.clients.browser_client import BrowserClient, BrowserPool
from benchmark.auth import ensure_admin_authenticated, AuthenticationError, ServiceNotReadyError


console = Console()


class ChatUIBenchmark(BaseBenchmark):
    """
    Benchmark for testing concurrent AI chat performance via browser UI.
    
    Supports two modes:
    1. Fixed users: Test with a specific number of concurrent users
    2. Auto-scale: Progressively add users until P95 response time exceeds threshold
    """
    
    name = "Chat UI Concurrency"
    description = "Test concurrent AI chat performance via browser UI"
    version = "1.0.0"
    
    def __init__(
        self,
        config: BenchmarkConfig,
        admin_client: Optional[OpenWebUIClient] = None,
    ):
        super().__init__(config)
        
        self._admin_client: Optional[OpenWebUIClient] = admin_client
        self._owns_admin_client: bool = admin_client is None
        self._http_client_pool: Optional[ClientPool] = None
        self._browser_pool: Optional[BrowserPool] = None
        self._test_clients: List[OpenWebUIClient] = []
        self._user_credentials: List[Dict[str, str]] = []
        self._created_user_ids: List[str] = []
        self._cleanup_done: bool = False
        self._interrupted: bool = False
        
        # Store original signal handlers
        self._original_sigint = None
        self._original_sigterm = None
    
    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful cleanup on interrupt."""
        def signal_handler(signum, frame):
            self._interrupted = True
            console.print("\n[yellow]Interrupt received, cleaning up...[/yellow]")
        
        self._original_sigint = signal.signal(signal.SIGINT, signal_handler)
        self._original_sigterm = signal.signal(signal.SIGTERM, signal_handler)
    
    def _restore_signal_handlers(self):
        """Restore original signal handlers."""
        if self._original_sigint:
            signal.signal(signal.SIGINT, self._original_sigint)
        if self._original_sigterm:
            signal.signal(signal.SIGTERM, self._original_sigterm)
    
    async def setup(self) -> None:
        """Set up benchmark: authenticate admin, verify model, create users."""
        self._setup_signal_handlers()
        
        try:
            await self._setup_admin_and_model()
            await self._setup_users_and_browsers()
        except Exception:
            await self.teardown()
            raise
    
    async def _setup_admin_and_model(self) -> None:
        """Authenticate admin and verify model availability."""
        if self._admin_client is None:
            try:
                self._admin_client, _auth_result = await ensure_admin_authenticated(
                    base_url=self.config.target_url,
                    timeout=self.config.request_timeout,
                    wait_for_service=True,
                )
            except ServiceNotReadyError as e:
                raise RuntimeError(f"Open WebUI service not ready: {e}")
            except AuthenticationError as e:
                raise RuntimeError(f"Admin authentication failed: {e}")
        
        chat_config = self.config.chat
        model_available = await self._admin_client.verify_model_available(chat_config.model)
        
        if not model_available:
            available_models = await self._admin_client.get_models()
            model_names = [m.get("id", m.get("name", str(m))) for m in available_models[:10]]
            raise RuntimeError(
                f"Model '{chat_config.model}' not available. "
                f"Available: {', '.join(model_names)}"
            )
        
        made_public = await self._admin_client.make_model_public(chat_config.model)
        if not made_public:
            raise RuntimeError(f"Failed to make model '{chat_config.model}' public")
    
    async def _setup_users_and_browsers(self) -> None:
        """Create test users and initialize browser pool."""
        chat_config = self.config.chat
        
        # Determine user count based on mode
        if chat_config.auto_scale:
            user_count = chat_config.max_user_cap
            console.print(f"[dim]Auto-scale mode: pre-creating up to {user_count} users[/dim]")
        else:
            user_count = chat_config.max_concurrent_users
        
        self._http_client_pool = ClientPool(
            self.config.target_url,
            self.config.request_timeout,
        )
        
        # Create users
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(f"Creating {user_count} users...", total=user_count * 2)
            
            def update_progress(current: int, total: int):
                progress.update(task, completed=current)
            
            self._test_clients = await self._http_client_pool.create_benchmark_users(
                admin_client=self._admin_client,
                count=user_count,
                email_pattern="chat_ui_benchmark_{n}@test.local",
                password="chat_ui_benchmark_pass_123",
                name_pattern="Chat UI Benchmark User {n}",
                progress_callback=update_progress,
            )
        
        # Store user IDs for cleanup
        self._created_user_ids = list(getattr(self._http_client_pool, '_benchmark_user_ids', []))
        
        # Store credentials for browser login
        self._user_credentials = [
            {
                "email": f"chat_ui_benchmark_{i + 1}@test.local",
                "password": "chat_ui_benchmark_pass_123",
            }
            for i in range(user_count)
        ]
        
        console.print(f"[green]✓[/green] Created {len(self._test_clients)} users")
        
        # Initialize browser pool
        browser_config = self.config.browser
        self._browser_pool = BrowserPool(
            base_url=self.config.target_url,
            headless=browser_config.headless,
            slow_mo=browser_config.slow_mo,
            viewport_width=browser_config.viewport_width,
            viewport_height=browser_config.viewport_height,
            timeout=browser_config.browser_timeout,
            use_isolated_browsers=browser_config.use_isolated_browsers,
        )
        
        await self._browser_pool.initialize()
        
        if not chat_config.auto_scale:
            # Fixed mode: login all users now
            await self._login_browsers(user_count)
    
    async def _login_browsers(self, count: int) -> None:
        """Login a specific number of browser sessions."""
        console.print(f"[dim]Logging in {count} browser sessions...[/dim]")
        
        credentials_to_login = self._user_credentials[:count]
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Logging in...", total=count)
            
            def login_progress(current: int, total: int):
                progress.update(task, completed=current)
            
            await self._browser_pool.create_clients(
                credentials=credentials_to_login,
                login=True,
                batch_size=10,
                progress_callback=login_progress,
            )
        
        console.print(f"[green]✓[/green] Launched {len(self._browser_pool.clients)} browser sessions")
    
    async def run(self) -> BenchmarkResult:
        """Execute the benchmark in either fixed or auto-scale mode."""
        chat_config = self.config.chat
        
        if chat_config.auto_scale:
            return await self._run_auto_scale()
        else:
            return await self._run_fixed()
    
    async def _run_fixed(self) -> BenchmarkResult:
        """Run benchmark with fixed number of users."""
        chat_config = self.config.chat
        metrics = MetricsCollector()
        metrics.start()
        
        browser_clients = self._browser_pool.clients
        user_count = len(browser_clients)
        requests_per_user = chat_config.requests_per_user
        total_requests = user_count * requests_per_user
        
        console.print(f"\n[bold cyan]Running chat UI benchmark[/bold cyan]")
        console.print(f"[dim]Model: {chat_config.model}[/dim]")
        console.print(f"[dim]Concurrent users: {user_count}[/dim]")
        console.print(f"[dim]Requests per user: {requests_per_user}[/dim]")
        console.print(f"[dim]Total requests: {total_requests}[/dim]\n")
        
        await self._execute_requests(browser_clients, metrics, requests_per_user)
        
        metrics.stop()
        
        return metrics.get_result(
            benchmark_name=self.name,
            concurrent_users=user_count,
            metadata={
                "model": chat_config.model,
                "mode": "fixed",
                "requests_per_user": requests_per_user,
                "headless": self.config.browser.headless,
            },
        )
    
    async def _run_auto_scale(self) -> BenchmarkResult:
        """Run auto-scaling benchmark to find max sustainable users."""
        chat_config = self.config.chat
        threshold_ms = chat_config.response_time_threshold_ms
        max_cap = chat_config.max_user_cap
        step_size = chat_config.user_step_size
        requests_per_user = chat_config.requests_per_user
        
        console.print(f"\n[bold cyan]Running auto-scale chat UI benchmark[/bold cyan]")
        console.print(f"[dim]Model: {chat_config.model}[/dim]")
        console.print(f"[dim]P95 threshold: {threshold_ms}ms[/dim]")
        console.print(f"[dim]Max user cap: {max_cap}[/dim]")
        console.print(f"[dim]Initial step size: {step_size}[/dim]")
        console.print(f"[dim]Requests per user per level: {requests_per_user}[/dim]\n")
        
        level_results: List[Dict[str, Any]] = []
        current_users = 0
        max_sustainable_users = 0
        current_step = step_size
        final_result: Optional[BenchmarkResult] = None
        last_result: Optional[BenchmarkResult] = None
        
        while current_users < max_cap and not self._interrupted:
            next_users = min(current_users + current_step, max_cap)
            
            # Login additional browsers if needed
            logged_in = len(self._browser_pool.clients)
            if next_users > logged_in:
                additional = next_users - logged_in
                console.print(f"[dim]Adding {additional} browser sessions...[/dim]")
                
                additional_creds = self._user_credentials[logged_in:next_users]
                await self._browser_pool.create_clients(
                    credentials=additional_creds,
                    login=True,
                    batch_size=10,
                )
            
            current_users = next_users
            browser_clients = self._browser_pool.clients[:current_users]
            
            console.print(f"\n[bold]Testing with {current_users} users...[/bold]")
            
            metrics = MetricsCollector()
            metrics.start()
            
            await self._execute_requests(browser_clients, metrics, requests_per_user)
            
            metrics.stop()
            result = metrics.get_result(
                benchmark_name=f"{self.name} (Level {current_users})",
                concurrent_users=current_users,
            )
            last_result = result  # Always track the most recent result
            
            p95 = result.p95_response_time_ms
            error_rate = result.error_rate_percent
            
            under_threshold = p95 <= threshold_ms and error_rate < 5.0
            level_results.append({
                "users": current_users,
                "p95_ms": p95,
                "avg_ms": result.avg_response_time_ms,
                "error_rate": error_rate,
                "under_threshold": under_threshold,
            })
            
            threshold_pct = (p95 / threshold_ms) * 100
            color = "green" if threshold_pct < 80 else "yellow" if under_threshold else "red"
            console.print(f"  Users: {current_users} | P95: {p95:.0f}ms ({threshold_pct:.0f}% of threshold) | Errors: {error_rate:.1f}%", style=color)
            
            if under_threshold:
                max_sustainable_users = current_users
                final_result = result
                
                # Adaptive step sizing
                if p95 < threshold_ms * 0.5:
                    current_step = min(current_step * 2, 20)
                elif p95 > threshold_ms * 0.8:
                    current_step = max(current_step // 2, 2)
            else:
                console.print(f"\n[yellow]Threshold exceeded at {current_users} users[/yellow]")
                break
            
            if current_users >= max_cap:
                console.print(f"\n[green]Reached max user cap ({max_cap})[/green]")
                break
        
        self._display_auto_scale_summary(level_results, threshold_ms, max_sustainable_users)
        
        # Use the best passing result, or fallback to last tested result
        if final_result is None:
            final_result = last_result
        
        final_result.metadata = final_result.metadata or {}
        final_result.metadata.update({
            "mode": "auto_scale",
            "model": chat_config.model,
            "max_sustainable_users": max_sustainable_users,
            "threshold_ms": threshold_ms,
            "levels_tested": len(level_results),
            "level_results": level_results,
        })
        
        return final_result
    
    def _display_auto_scale_summary(
        self,
        level_results: List[Dict[str, Any]],
        threshold_ms: int,
        max_sustainable: int,
    ) -> None:
        """Display auto-scale results summary table."""
        console.print("\n")
        
        table = Table(title="Auto-Scale Results")
        table.add_column("Users", justify="right")
        table.add_column("P95 (ms)", justify="right")
        table.add_column("Avg (ms)", justify="right")
        table.add_column("% of Threshold", justify="right")
        table.add_column("Errors", justify="right")
        
        for level in level_results:
            threshold_pct = (level["p95_ms"] / threshold_ms) * 100
            if threshold_pct < 80:
                style = "green"
            elif level["under_threshold"]:
                style = "yellow"
            else:
                style = "red"
            table.add_row(
                str(level["users"]),
                f"{level['p95_ms']:.0f}",
                f"{level['avg_ms']:.0f}",
                f"[{style}]{threshold_pct:.0f}%[/{style}]",
                f"{level['error_rate']:.1f}%",
            )
        
        console.print(table)
        console.print(f"\n[bold]P95 Threshold: {threshold_ms}ms[/bold]")
        console.print(f"[bold green]Maximum Sustainable Users: {max_sustainable}[/bold green]\n")
    
    async def _execute_requests(
        self,
        browser_clients: List[BrowserClient],
        metrics: MetricsCollector,
        requests_per_user: int,
    ) -> None:
        """Execute chat requests across all browser clients."""
        chat_config = self.config.chat
        total_requests = len(browser_clients) * requests_per_user
        completed = 0
        errors = 0
        
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        )
        progress.start()
        task = progress.add_task("Running...", total=total_requests)
        
        def update_status():
            progress.update(task, completed=completed + errors,
                          description=f"Done: {completed} | Errors: {errors}")

        async def capture_failure_debug(client: BrowserClient, user_num: int, req_num: int, error_msg: str):
            """Print immediate debug info and optionally capture artifacts on UI failures."""
            try:
                current_url = client.page.url
            except Exception:
                current_url = "<unavailable>"

            console.print(
                f"[red]UI request failed[/red] "
                f"(user={user_num}, request={req_num}) "
                f"url={current_url} error={error_msg}"
            )

            if not self.config.browser.screenshot_on_error:
                return

            try:
                debug_dir = Path(self.config.output.results_dir) / "chat_ui_debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                base = f"u{user_num}_r{req_num}_{ts}"

                screenshot_path = debug_dir / f"{base}.png"
                await client.take_screenshot(str(screenshot_path))
                console.print(f"[dim]Saved screenshot: {screenshot_path}[/dim]")

                try:
                    html = await client.page.content()
                    html_path = debug_dir / f"{base}.html"
                    html_path.write_text(html, encoding="utf-8")
                    console.print(f"[dim]Saved page HTML: {html_path}[/dim]")
                except Exception as html_err:
                    console.print(f"[yellow]Debug HTML capture failed: {html_err}[/yellow]")
            except Exception as debug_err:
                console.print(f"[yellow]Debug artifact capture failed: {debug_err}[/yellow]")
        
        async def run_user_session(client: BrowserClient, user_num: int):
            nonlocal completed, errors
            
            for req_num in range(requests_per_user):
                if self._interrupted:
                    break
                
                user_prompt = random.choice(chat_config.prompt_pool)
                
                try:
                    console.print(
                        f"[dim]User {user_num + 1}: request {req_num + 1}/{requests_per_user} starting[/dim]"
                    )

                    if req_num > 0:
                        started_new_chat = await client.start_new_chat()
                        if not started_new_chat:
                            console.print(
                                f"[yellow]User {user_num + 1}: could not click New Chat; continuing in current chat[/yellow]"
                            )
                        else:
                            console.print(
                                f"[dim]User {user_num + 1}: opened new chat[/dim]"
                            )
                    
                    result = await client.send_message_and_wait(
                        message=user_prompt,
                        timeout_ms=self.config.browser.browser_timeout,
                        first_token_timeout_ms=self.config.browser.first_token_timeout_ms,
                        completion_timeout_ms=self.config.browser.completion_timeout_ms,
                    )
                    
                    if result.success:
                        metrics.record_streaming_timing(
                            operation="chat_completion_ui",
                            duration_ms=result.total_duration_ms,
                            ttft_ms=result.ttft_ms,
                            tokens_generated=result.tokens_rendered,
                            success=True,
                            metadata={"user": user_num, "request": req_num},
                        )
                        completed += 1
                        console.print(
                            f"[dim]User {user_num + 1}: request {req_num + 1} complete "
                            f"({result.total_duration_ms:.0f}ms, ttft={result.ttft_ms:.0f}ms)[/dim]"
                        )
                    else:
                        await capture_failure_debug(client, user_num, req_num, result.error or "Unknown error")
                        metrics.record_streaming_timing(
                            operation="chat_completion_ui",
                            duration_ms=result.total_duration_ms,
                            ttft_ms=0,
                            tokens_generated=0,
                            success=False,
                            error=result.error,
                            metadata={"user": user_num, "request": req_num},
                        )
                        errors += 1
                    
                    update_status()
                    
                except Exception as e:
                    await capture_failure_debug(client, user_num, req_num, str(e))
                    metrics.record_streaming_timing(
                        operation="chat_completion_ui",
                        duration_ms=0,
                        ttft_ms=0,
                        tokens_generated=0,
                        success=False,
                        error=str(e),
                        metadata={"user": user_num, "request": req_num},
                    )
                    errors += 1
                    update_status()
                
                await asyncio.sleep(0.5)
        
        tasks = [
            run_user_session(client, i)
            for i, client in enumerate(browser_clients)
        ]
        
        await asyncio.gather(*tasks, return_exceptions=True)
        
        progress.update(task, description="Complete!", completed=total_requests)
        progress.stop()
    
    async def teardown(self) -> None:
        """Clean up all resources including deleting created users."""
        if self._cleanup_done:
            return
        
        self._restore_signal_handlers()
        
        # Close browser pool
        if self._browser_pool:
            try:
                await self._browser_pool.close_all()
            except Exception as e:
                console.print(f"[yellow]Warning: Error closing browsers: {e}[/yellow]")
        
        # Close HTTP clients
        if self._http_client_pool:
            try:
                await self._http_client_pool.close_all()
            except Exception as e:
                console.print(f"[yellow]Warning: Error closing HTTP clients: {e}[/yellow]")
        
        # Delete created users
        if self._created_user_ids and self._admin_client:
            console.print(f"[dim]Cleaning up {len(self._created_user_ids)} test users...[/dim]")
            
            deleted = 0
            failed = 0
            
            for user_id in self._created_user_ids:
                try:
                    await self._admin_client.admin_delete_user(user_id)
                    deleted += 1
                except Exception:
                    failed += 1
            
            if failed > 0:
                console.print(f"[yellow]Warning: Failed to delete {failed} users[/yellow]")
            else:
                console.print(f"[green]✓[/green] Deleted {deleted} test users")
        
        # Close admin client
        if self._admin_client and self._owns_admin_client:
            try:
                await self._admin_client.close()
            except Exception:
                pass
        
        self._cleanup_done = True
