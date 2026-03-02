"""
Command-line interface for Open WebUI Benchmark.

Provides commands for running benchmarks, managing compute profiles,
and analyzing results.
"""

import asyncio
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from benchmark.core.config import load_config, ConfigLoader
from benchmark.core.runner import BenchmarkRunner
from benchmark.scenarios.channels import ChannelAPIBenchmark, ChannelWebSocketBenchmark
from benchmark.scenarios.chat import ChatAPIBenchmark
from benchmark.scenarios.chat_ui import ChatUIBenchmark
from benchmark.scenarios.channel_ui import ChannelUIBenchmark
from benchmark.auth import (
    ensure_admin_authenticated,
    AuthenticationError,
    ServiceNotReadyError,
)
from benchmark.auth.entrypoint import check_auth_status


console = Console()


def print_banner():
    """Print the benchmark suite banner."""
    banner = """
╔═══════════════════════════════════════════════════════════╗
║          Open WebUI Benchmark Suite v0.1.0                ║
║          Testing performance at scale                      ║
╚═══════════════════════════════════════════════════════════╝
    """
    console.print(banner, style="bold blue")


def list_profiles():
    """List available compute profiles."""
    loader = ConfigLoader()
    profiles = loader.load_compute_profiles()
    
    console.print("\n[bold]Available Compute Profiles:[/bold]\n")
    
    for profile_id, profile in profiles.items():
        console.print(f"  [cyan]{profile_id}[/cyan]")
        console.print(f"    Name: {profile.name}")
        console.print(f"    Description: {profile.description}")
        console.print(f"    Resources: {profile.resources.cpus} CPUs, {profile.resources.memory} RAM")
        console.print()


async def auth_check(target_url: Optional[str] = None):
    """Check authentication configuration and service status."""
    console.print("\n[bold]Authentication Status Check[/bold]\n")
    
    with console.status("Checking configuration and service..."):
        status = await check_auth_status(base_url=target_url)
    
    # Display results
    console.print(f"  Service URL: [cyan]{status['service_url']}[/cyan]")
    
    if status['service_reachable']:
        console.print(f"  Service Status: [green]✓ Reachable[/green]")
    else:
        console.print(f"  Service Status: [red]✗ Not reachable[/red]")
    
    if status['credentials_configured']:
        console.print(f"  Credentials: [green]✓ Configured[/green]")
        console.print(f"  Admin Email: [cyan]{status['admin_email']}[/cyan]")
    else:
        console.print(f"  Credentials: [red]✗ Not configured[/red]")
        console.print("    Set ADMIN_USER_EMAIL and ADMIN_USER_PASSWORD environment variables")
    
    console.print()
    
    # Summary
    if status['service_reachable'] and status['credentials_configured']:
        console.print("[green]Ready to run benchmarks![/green]")
        return True
    else:
        console.print("[yellow]Configuration incomplete. Fix issues above before running benchmarks.[/yellow]")
        return False


async def auth_verify(target_url: Optional[str] = None):
    """Verify authentication by actually signing in."""
    console.print("\n[bold]Authentication Verification[/bold]\n")
    
    try:
        with console.status("Authenticating..."):
            client, auth_result = await ensure_admin_authenticated(
                base_url=target_url,
                wait_for_service=True,
                service_wait_retries=10,
            )
        
        console.print(f"  [green]✓ Authentication successful![/green]")
        console.print(f"  User ID: [cyan]{auth_result.user.id}[/cyan]")
        console.print(f"  Email: [cyan]{auth_result.user.email}[/cyan]")
        console.print(f"  Role: [cyan]{auth_result.user.role}[/cyan]")
        
        if auth_result.is_new_signup:
            console.print(f"  [yellow]Note: Created new admin account (first run)[/yellow]")
        
        # Clean up
        await client.close()
        
        console.print("\n[green]Ready to run benchmarks![/green]")
        return True
        
    except ServiceNotReadyError as e:
        console.print(f"  [red]✗ Service not ready: {e}[/red]")
        return False
    except AuthenticationError as e:
        console.print(f"  [red]✗ Authentication failed: {e}[/red]")
        return False
    except Exception as e:
        console.print(f"  [red]✗ Unexpected error: {e}[/red]")
        return False


def list_benchmarks():
    """List available benchmarks."""
    console.print("\n[bold]Available Benchmarks:[/bold]\n")
    
    benchmarks = [
        ("channels-api", ChannelAPIBenchmark),
        ("channels-ws", ChannelWebSocketBenchmark),
        ("chat-api", ChatAPIBenchmark),
        ("chat-ui", ChatUIBenchmark),
        ("channel-ui", ChannelUIBenchmark),
    ]
    
    for cmd, benchmark_class in benchmarks:
        console.print(f"  [cyan]{cmd}[/cyan]")
        console.print(f"    Name: {benchmark_class.name}")
        console.print(f"    Description: {benchmark_class.description}")
        console.print()


async def run_channel_api_benchmark(
    profile_id: str = "default",
    target_url: Optional[str] = None,
    max_users: Optional[int] = None,
    step_size: Optional[int] = None,
    output_dir: Optional[str] = None,
):
    """Run the channel API concurrency benchmark."""
    # Load config with overrides
    overrides = {}
    if target_url:
        overrides["target_url"] = target_url
    
    config = load_config(profile_id, overrides=overrides)
    
    if max_users:
        config.channels.max_concurrent_users = max_users
    
    if step_size:
        config.channels.user_step_size = step_size
    
    # Auto-adjust step size if larger than max users
    if config.channels.user_step_size > config.channels.max_concurrent_users:
        config.channels.user_step_size = config.channels.max_concurrent_users
    
    # Create runner
    runner = BenchmarkRunner(
        config=config,
        profile_id=profile_id,
        output_dir=Path(output_dir) if output_dir else None,
    )
    
    # Run benchmark
    result = await runner.run_benchmark(ChannelAPIBenchmark)
    runner.display_final_summary()
    
    return result


async def run_channel_ws_benchmark(
    profile_id: str = "default",
    target_url: Optional[str] = None,
    max_users: Optional[int] = None,
    output_dir: Optional[str] = None,
):
    """Run the channel WebSocket benchmark."""
    # Load config with overrides
    overrides = {}
    if target_url:
        overrides["target_url"] = target_url
    
    config = load_config(profile_id, overrides=overrides)
    
    if max_users:
        config.channels.max_concurrent_users = max_users
    
    # Create runner
    runner = BenchmarkRunner(
        config=config,
        profile_id=profile_id,
        output_dir=Path(output_dir) if output_dir else None,
    )
    
    # Run benchmark
    result = await runner.run_benchmark(ChannelWebSocketBenchmark)
    runner.display_final_summary()
    
    return result


async def run_chat_api_benchmark(
    profile_id: str = "default",
    target_url: Optional[str] = None,
    max_users: Optional[int] = None,
    model: Optional[str] = None,
    output_dir: Optional[str] = None,
):
    """Run the chat API concurrency benchmark."""
    # Load config with overrides
    overrides = {}
    if target_url:
        overrides["target_url"] = target_url
    
    config = load_config(profile_id, overrides=overrides)
    
    if max_users:
        config.chat.max_concurrent_users = max_users
    
    if model:
        config.chat.model = model
    
    # Create runner
    runner = BenchmarkRunner(
        config=config,
        profile_id=profile_id,
        output_dir=Path(output_dir) if output_dir else None,
    )
    
    # Run benchmark
    result = await runner.run_benchmark(ChatAPIBenchmark)
    runner.display_final_summary()
    
    return result


async def run_chat_ui_benchmark(
    profile_id: str = "default",
    target_url: Optional[str] = None,
    max_users: Optional[int] = None,
    model: Optional[str] = None,
    requests_per_user: Optional[int] = None,
    headless: bool = True,
    slow_mo: int = 0,
    browser_timeout: Optional[int] = None,
    first_token_timeout: Optional[int] = None,
    completion_timeout: Optional[int] = None,
    capture_ui_success_artifacts: bool = False,
    capture_network_trace_on_error: bool = False,
    capture_network_trace_on_success: bool = False,
    network_trace_max_entries: Optional[int] = None,
    pause_after_login: bool = False,
    auto_scale: bool = False,
    response_threshold: int = 1000,
    step_size: Optional[int] = None,
    output_dir: Optional[str] = None,
):
    """Run the chat UI concurrency benchmark using browser automation."""
    overrides = {}
    if target_url:
        overrides["target_url"] = target_url
    
    config = load_config(profile_id, overrides=overrides)
    
    # Auto-scale mode settings
    config.chat.auto_scale = auto_scale
    config.chat.response_time_threshold_ms = response_threshold
    
    if step_size:
        config.chat.user_step_size = step_size
    
    if auto_scale:
        # In auto-scale mode, max_users becomes the cap (default 200)
        config.chat.max_user_cap = max_users if max_users else 200
    elif max_users:
        config.chat.max_concurrent_users = max_users
    
    if model:
        config.chat.model = model

    if requests_per_user:
        config.chat.requests_per_user = requests_per_user

    # Browser-specific settings
    config.browser.headless = headless
    config.browser.slow_mo = slow_mo
    if browser_timeout:
        config.browser.browser_timeout = browser_timeout
    if first_token_timeout:
        config.browser.first_token_timeout_ms = first_token_timeout
    if completion_timeout:
        config.browser.completion_timeout_ms = completion_timeout
    config.browser.capture_success_artifacts = capture_ui_success_artifacts
    config.browser.capture_network_trace_on_error = capture_network_trace_on_error
    config.browser.capture_network_trace_on_success = capture_network_trace_on_success
    config.browser.pause_after_login = pause_after_login
    if network_trace_max_entries:
        config.browser.network_trace_max_entries = network_trace_max_entries
    
    # Create runner
    runner = BenchmarkRunner(
        config=config,
        profile_id=profile_id,
        output_dir=Path(output_dir) if output_dir else None,
    )
    
    # Run benchmark
    result = await runner.run_benchmark(ChatUIBenchmark)
    runner.display_final_summary()
    
    return result


async def run_channel_ui_benchmark(
    profile_id: str = "default",
    target_url: Optional[str] = None,
    max_users: Optional[int] = None,
    model: Optional[str] = None,
    channel: Optional[str] = None,
    requests_per_user: Optional[int] = None,
    headless: bool = True,
    slow_mo: int = 0,
    browser_timeout: Optional[int] = None,
    first_token_timeout: Optional[int] = None,
    completion_timeout: Optional[int] = None,
    capture_ui_success_artifacts: bool = False,
    capture_network_trace_on_error: bool = False,
    capture_network_trace_on_success: bool = False,
    network_trace_max_entries: Optional[int] = None,
    pause_after_login: bool = False,
    auto_scale: bool = False,
    response_threshold: int = 1000,
    step_size: Optional[int] = None,
    output_dir: Optional[str] = None,
):
    """Run the channel UI concurrency benchmark using browser automation."""
    overrides = {}
    if target_url:
        overrides["target_url"] = target_url

    config = load_config(profile_id, overrides=overrides)

    # Auto-scale mode settings
    config.chat.auto_scale = auto_scale
    config.chat.response_time_threshold_ms = response_threshold

    if step_size:
        config.chat.user_step_size = step_size

    if auto_scale:
        # In auto-scale mode, max_users becomes the cap (default 200)
        config.chat.max_user_cap = max_users if max_users else 200
    elif max_users:
        config.chat.max_concurrent_users = max_users

    if model:
        config.chat.model = model

    if requests_per_user:
        config.chat.requests_per_user = requests_per_user

    if channel:
        config.chat.channel = channel

    # Keep each user in the same channel thread for all requests.
    config.chat.start_new_chat_between_requests = False

    # Browser-specific settings
    config.browser.headless = headless
    config.browser.slow_mo = slow_mo
    if browser_timeout:
        config.browser.browser_timeout = browser_timeout
    if first_token_timeout:
        config.browser.first_token_timeout_ms = first_token_timeout
    if completion_timeout:
        config.browser.completion_timeout_ms = completion_timeout
    config.browser.capture_success_artifacts = capture_ui_success_artifacts
    config.browser.capture_network_trace_on_error = capture_network_trace_on_error
    config.browser.capture_network_trace_on_success = capture_network_trace_on_success
    config.browser.pause_after_login = pause_after_login
    if network_trace_max_entries:
        config.browser.network_trace_max_entries = network_trace_max_entries

    # Create runner
    runner = BenchmarkRunner(
        config=config,
        profile_id=profile_id,
        output_dir=Path(output_dir) if output_dir else None,
    )

    # Run benchmark
    result = await runner.run_benchmark(ChannelUIBenchmark)
    runner.display_final_summary()

    return result


async def run_all_benchmarks(
    profile_id: str = "default",
    target_url: Optional[str] = None,
    output_dir: Optional[str] = None,
):
    """Run all available benchmarks."""
    # Load config with overrides
    overrides = {}
    if target_url:
        overrides["target_url"] = target_url
    
    config = load_config(profile_id, overrides=overrides)
    
    # Create runner and register all benchmarks
    runner = BenchmarkRunner(
        config=config,
        profile_id=profile_id,
        output_dir=Path(output_dir) if output_dir else None,
    )
    
    runner.register_benchmark(ChannelAPIBenchmark)
    
    # Run all benchmarks
    results = await runner.run_all()
    runner.display_final_summary()
    
    return results


def main():
    """Main entry point for CLI."""
    import argparse
    
    print_banner()
    
    parser = argparse.ArgumentParser(
        description="Open WebUI Benchmark Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # List profiles command
    list_profiles_parser = subparsers.add_parser(
        "profiles",
        help="List available compute profiles",
    )
    
    # List benchmarks command
    list_benchmarks_parser = subparsers.add_parser(
        "list",
        help="List available benchmarks",
    )
    
    # Auth command with subcommands
    auth_parser = subparsers.add_parser(
        "auth",
        help="Authentication commands",
    )
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", help="Auth subcommands")
    
    # Auth check subcommand
    auth_check_parser = auth_subparsers.add_parser(
        "check",
        help="Check auth configuration and service status (no actual auth)",
    )
    auth_check_parser.add_argument(
        "-u", "--url",
        help="Target Open WebUI URL (default: from config)",
    )
    
    # Auth verify subcommand
    auth_verify_parser = auth_subparsers.add_parser(
        "verify",
        help="Verify authentication by signing in",
    )
    auth_verify_parser.add_argument(
        "-u", "--url",
        help="Target Open WebUI URL (default: from config)",
    )
    
    # Run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run benchmarks",
    )
    run_parser.add_argument(
        "benchmark",
        nargs="?",
        default="chat-ui",
        choices=["all", "channels-api", "channels-ws", "chat-api", "chat-ui", "channel-ui"],
        help="Benchmark to run (default: chat-ui)",
    )
    run_parser.add_argument(
        "-p", "--profile",
        default="default",
        help="Compute profile to use (default: default)",
    )
    run_parser.add_argument(
        "-u", "--url",
        help="Target Open WebUI URL (default: from config)",
    )
    run_parser.add_argument(
        "-m", "--max-users",
        type=int,
        help="Maximum concurrent users to test",
    )
    run_parser.add_argument(
        "-s", "--step-size",
        type=int,
        help="User increment step size (default: 10)",
    )
    run_parser.add_argument(
        "--model",
        help="Model to use for chat benchmarks (default: gpt-4o-mini)",
    )
    run_parser.add_argument(
        "--requests-per-user",
        type=int,
        help="Number of chat requests to send per user/session (chat benchmarks)",
    )
    run_parser.add_argument(
        "--channel",
        help="Target channel name/id/slug for channel-ui benchmark (default: benchmark-testing, auto-created if missing)",
    )
    run_parser.add_argument(
        "-o", "--output",
        help="Output directory for results",
    )
    
    # Browser-specific options for chat-ui
    run_parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    run_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser with visible window (for debugging)",
    )
    run_parser.add_argument(
        "--slow-mo",
        type=int,
        default=0,
        help="Slow down browser operations by ms (for debugging)",
    )
    run_parser.add_argument(
        "--browser-timeout",
        type=int,
        help="UI chat response timeout in ms for chat-ui benchmark",
    )
    run_parser.add_argument(
        "--first-token-timeout",
        type=int,
        help="UI chat timeout in ms waiting for the first assistant token (chat-ui)",
    )
    run_parser.add_argument(
        "--completion-timeout",
        type=int,
        help="UI chat timeout in ms after first token while waiting for completion (chat-ui)",
    )
    run_parser.add_argument(
        "--capture-ui-success-artifacts",
        action="store_true",
        help="Save screenshot + HTML for each successful chat-ui request (debugging)",
    )
    run_parser.add_argument(
        "--capture-network-trace-on-error",
        action="store_true",
        help="Save browser request/response trace JSON for failed chat-ui requests",
    )
    run_parser.add_argument(
        "--capture-network-trace-on-success",
        action="store_true",
        help="Save browser request/response trace JSON for successful chat-ui requests",
    )
    run_parser.add_argument(
        "--network-trace-max-entries",
        type=int,
        help="Max recorded browser network events kept per session (ring buffer)",
    )
    run_parser.add_argument(
        "--pause-after-login",
        action="store_true",
        help="UI benchmarks: pause after browser login so you can manually inspect before requests",
    )
    
    # Auto-scaling options for chat-ui (auto-scale is default unless --max-users is specified)
    run_parser.add_argument(
        "--response-threshold",
        type=int,
        default=1000,
        help="P95 response time threshold in ms for auto-scaling (default: 1000)",
    )
    
    args = parser.parse_args()
    
    if args.command == "profiles":
        list_profiles()
    elif args.command == "list":
        list_benchmarks()
    elif args.command == "auth":
        if args.auth_command == "check":
            success = asyncio.run(auth_check(target_url=args.url))
            sys.exit(0 if success else 1)
        elif args.auth_command == "verify":
            success = asyncio.run(auth_verify(target_url=args.url))
            sys.exit(0 if success else 1)
        else:
            auth_parser.print_help()
    elif args.command == "run":
        try:
            # Determine headless mode (--headed overrides --headless)
            headless = not getattr(args, 'headed', False)
            
            if args.benchmark == "all":
                asyncio.run(run_all_benchmarks(
                    profile_id=args.profile,
                    target_url=args.url,
                    output_dir=args.output,
                ))
            elif args.benchmark == "channels-api":
                asyncio.run(run_channel_api_benchmark(
                    profile_id=args.profile,
                    target_url=args.url,
                    max_users=args.max_users,
                    step_size=args.step_size,
                    output_dir=args.output,
                ))
            elif args.benchmark == "channels-ws":
                asyncio.run(run_channel_ws_benchmark(
                    profile_id=args.profile,
                    target_url=args.url,
                    max_users=args.max_users,
                    output_dir=args.output,
                ))
            elif args.benchmark == "chat-api":
                asyncio.run(run_chat_api_benchmark(
                    profile_id=args.profile,
                    target_url=args.url,
                    max_users=args.max_users,
                    model=args.model,
                    output_dir=args.output,
                ))
            elif args.benchmark == "chat-ui":
                # Auto-scale by default, fixed mode if --max-users is specified
                auto_scale = args.max_users is None
                asyncio.run(run_chat_ui_benchmark(
                    profile_id=args.profile,
                    target_url=args.url,
                    max_users=args.max_users,
                    model=args.model,
                    requests_per_user=getattr(args, 'requests_per_user', None),
                    headless=headless,
                    slow_mo=getattr(args, 'slow_mo', 0),
                    browser_timeout=getattr(args, 'browser_timeout', None),
                    first_token_timeout=getattr(args, 'first_token_timeout', None),
                    completion_timeout=getattr(args, 'completion_timeout', None),
                    capture_ui_success_artifacts=getattr(args, 'capture_ui_success_artifacts', False),
                    capture_network_trace_on_error=getattr(args, 'capture_network_trace_on_error', False),
                    capture_network_trace_on_success=getattr(args, 'capture_network_trace_on_success', False),
                    network_trace_max_entries=getattr(args, 'network_trace_max_entries', None),
                    pause_after_login=getattr(args, 'pause_after_login', False),
                    auto_scale=auto_scale,
                    response_threshold=getattr(args, 'response_threshold', 1000),
                    step_size=args.step_size,
                    output_dir=args.output,
                ))
            elif args.benchmark == "channel-ui":
                # Auto-scale by default, fixed mode if --max-users is specified
                auto_scale = args.max_users is None
                asyncio.run(run_channel_ui_benchmark(
                    profile_id=args.profile,
                    target_url=args.url,
                    max_users=args.max_users,
                    model=args.model,
                    channel=getattr(args, 'channel', None),
                    requests_per_user=getattr(args, 'requests_per_user', None),
                    headless=headless,
                    slow_mo=getattr(args, 'slow_mo', 0),
                    browser_timeout=getattr(args, 'browser_timeout', None),
                    first_token_timeout=getattr(args, 'first_token_timeout', None),
                    completion_timeout=getattr(args, 'completion_timeout', None),
                    capture_ui_success_artifacts=getattr(args, 'capture_ui_success_artifacts', False),
                    capture_network_trace_on_error=getattr(args, 'capture_network_trace_on_error', False),
                    capture_network_trace_on_success=getattr(args, 'capture_network_trace_on_success', False),
                    network_trace_max_entries=getattr(args, 'network_trace_max_entries', None),
                    pause_after_login=getattr(args, 'pause_after_login', False),
                    auto_scale=auto_scale,
                    response_threshold=getattr(args, 'response_threshold', 1000),
                    step_size=args.step_size,
                    output_dir=args.output,
                ))
            else:
                console.print(f"[red]Unknown benchmark: {args.benchmark}[/red]")
                sys.exit(1)
        except KeyboardInterrupt:
            console.print("\n[yellow]Benchmark interrupted[/yellow]")
            sys.exit(1)
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

