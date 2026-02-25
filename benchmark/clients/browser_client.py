"""
Browser client for UI-based benchmarking using Playwright.
"""

import asyncio
import time
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright


@dataclass
class BrowserChatResult:
    """Result from a browser-based chat interaction."""
    content: str
    ttft_ms: float  # Time to first token appearing in UI
    total_duration_ms: float  # Time until streaming complete
    tokens_rendered: int  # Approximate tokens based on content length
    success: bool
    error: Optional[str] = None
    
    @property
    def tokens_per_second(self) -> float:
        """Calculate tokens per second based on render time."""
        if self.total_duration_ms > 0 and self.tokens_rendered > 0:
            return self.tokens_rendered / (self.total_duration_ms / 1000)
        return 0.0


class BrowserClient:
    """Async browser client for Open WebUI interactions."""
    
    # CSS Selectors for Open WebUI - may need updates as UI evolves
    SELECTORS = {
        "email_input": 'input[type="email"]',
        "password_input": 'input[type="password"]',
        "login_button": 'button[type="submit"]',
        "chat_input": '[contenteditable="true"]',
        "send_button": 'button[type="submit"]:has(svg), button[aria-label*="Send"]',
        "new_chat_button": 'button:has-text("New Chat"), a[href="/"]',
        "model_selector": 'button[aria-label*="Model"], .model-selector, [data-testid="model-selector"]',
        "model_option": 'div[role="option"], button[role="menuitem"]',
        # Exclude the composer wrapper (`#message-input-container`), which also matches the prefix.
        "message_container": '[id^="message-"]:not(#message-input-container)',
        "response_prose": '.prose',
        "response_content_container": '#response-content-container',
        "assistant_message": '.chat-assistant',
        "streaming_indicator": '.typing-indicator, [class*="loading"], [class*="streaming"]',
    }
    
    def __init__(
        self,
        base_url: str,
        headless: bool = True,
        slow_mo: int = 0,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        timeout: float = 30000,
    ):
        self.base_url = base_url.rstrip('/')
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout = timeout
        
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._is_logged_in: bool = False
    
    async def launch(self) -> None:
        """Launch the browser and create a new context."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
        )
        self._context = await self._browser.new_context(
            viewport={"width": self.viewport_width, "height": self.viewport_height},
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self.timeout)
    
    async def close(self) -> None:
        """Close the browser and clean up resources."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._page = None
        self._is_logged_in = False
    
    @property
    def page(self) -> Page:
        """Get the current page, raising if not initialized."""
        if self._page is None:
            raise RuntimeError("Browser not launched. Call launch() first.")
        return self._page
    
    @property
    def is_logged_in(self) -> bool:
        """Check if the client is logged in."""
        return self._is_logged_in
    
    async def login(self, email: str, password: str, max_retries: int = 5) -> bool:
        """Log in to Open WebUI with retry and exponential backoff."""
        last_error = None
        
        for attempt in range(max_retries):
            try:
                return await self._attempt_login(email, password)
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    backoff = 2 ** (attempt + 1)
                    await asyncio.sleep(backoff)
        
        raise RuntimeError(f"Login failed after {max_retries} attempts: {last_error}")
    
    async def _attempt_login(self, email: str, password: str) -> bool:
        """Single login attempt."""
        try:
            await self.page.goto(f"{self.base_url}/auth", wait_until="domcontentloaded", timeout=60000)
            
            await self.page.wait_for_selector(
                self.SELECTORS["email_input"],
                state="visible",
                timeout=60000,
            )
            await self.page.wait_for_timeout(500)
            
            await self.page.fill(self.SELECTORS["email_input"], email)
            await self.page.fill(self.SELECTORS["password_input"], password)
            await self.page.click(self.SELECTORS["login_button"])
            
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            
            # Wait for redirect away from /auth
            for _ in range(10):
                current_url = self.page.url
                if "/auth" not in current_url:
                    # Look for chat input as indicator of successful login
                    try:
                        await self.page.wait_for_selector(
                            self.SELECTORS["chat_input"],
                            state="visible",
                            timeout=5000,
                        )
                    except Exception:
                        pass  # Chat input not found but we're past auth
                    self._is_logged_in = True
                    return True
                await self.page.wait_for_timeout(500)
            
            raise RuntimeError("Login did not redirect from auth page")
                
        except Exception as e:
            self._is_logged_in = False
            raise RuntimeError(f"Login failed: {e}")
    
    async def select_model(self, model_id: str) -> bool:
        """Select a model in the UI. Returns False if selection fails."""
        try:
            model_selector = await self.page.wait_for_selector(
                self.SELECTORS["model_selector"],
                state="visible",
                timeout=5000,
            )
            if model_selector:
                await model_selector.click()
                await self.page.wait_for_timeout(500)
                
                model_option = self.page.get_by_text(model_id, exact=False)
                if await model_option.count() > 0:
                    await model_option.first.click()
                    return True
                    
            return False
        except Exception:
            return False
    
    async def start_new_chat(self) -> bool:
        """Start a new chat session."""
        try:
            new_chat_btn = await self.page.wait_for_selector(
                self.SELECTORS["new_chat_button"],
                state="visible",
                timeout=5000,
            )
            if new_chat_btn:
                await new_chat_btn.click()
                # Give the app a moment to navigate/reset the chat composer.
                await self.page.wait_for_timeout(800)
                return True
            return False
        except Exception:
            return False
    
    async def send_message_and_wait(
        self,
        message: str,
        timeout_ms: float = 60000,
        first_token_timeout_ms: Optional[float] = None,
        completion_timeout_ms: Optional[float] = None,
    ) -> BrowserChatResult:
        """Send a message and wait for streaming response. Returns timing metrics."""
        start_time = time.time()
        first_token_time: Optional[float] = None
        poll_interval_s = 0.1
        max_stable_checks = 30  # ~3s of stable content before considering response complete
        first_token_timeout_ms = first_token_timeout_ms or timeout_ms
        completion_timeout_ms = completion_timeout_ms or timeout_ms
        
        try:
            chat_input = await self.page.wait_for_selector(
                self.SELECTORS["chat_input"],
                state="visible",
                timeout=10000,
            )
            
            if not chat_input:
                return BrowserChatResult(
                    content="",
                    ttft_ms=0,
                    total_duration_ms=0,
                    tokens_rendered=0,
                    success=False,
                    error="Chat input not found",
                )
            
            # contenteditable requires click + type instead of fill
            await chat_input.click()
            await chat_input.press("Control+a")
            await self.page.keyboard.type(message)
            
            initial_messages = await self.page.query_selector_all(self.SELECTORS["message_container"])
            initial_count = len(initial_messages)
            
            await self.page.keyboard.press("Enter")
            send_time = time.time()
            
            # Wait for user message + assistant response
            target_message_count = initial_count + 2
            
            content = ""
            last_content_length = 0
            stable_count = 0
            timed_out = True
            saw_assistant_message = False
            saw_streaming_ui = False
            
            timeout_stage = "unknown"

            while True:
                now = time.time()
                elapsed_since_send_ms = (now - send_time) * 1000
                if first_token_time is None:
                    if elapsed_since_send_ms >= first_token_timeout_ms:
                        timeout_stage = "first_token"
                        break
                else:
                    elapsed_since_first_token_ms = (now - first_token_time) * 1000
                    if elapsed_since_first_token_ms >= completion_timeout_ms:
                        timeout_stage = "completion"
                        break

                current_messages = await self.page.query_selector_all(self.SELECTORS["message_container"])

                response_message = None

                # Primary heuristic: expect one new user row + one new assistant row.
                if len(current_messages) >= target_message_count:
                    response_message = current_messages[-1]

                # Fallback heuristic: locate the most recent row that contains assistant markup.
                if response_message is None and current_messages:
                    for msg in reversed(current_messages):
                        try:
                            assistant_node = await msg.query_selector(self.SELECTORS["assistant_message"])
                            if assistant_node:
                                response_message = msg
                                break
                        except Exception:
                            continue

                if response_message is not None:
                    saw_assistant_message = True
                    
                    # Re-resolve the response content element each poll because Open WebUI
                    # may replace/re-render the assistant subtree during streaming.
                    response_element = await response_message.query_selector(self.SELECTORS["response_prose"])
                    if not response_element:
                        response_element = await response_message.query_selector(
                            self.SELECTORS["response_content_container"]
                        )
                    if not response_element:
                        response_element = await response_message.query_selector(
                            self.SELECTORS["assistant_message"]
                        )
                    if not response_element:
                        # Try fallback selectors
                        alternatives = [
                            "div.prose",
                            "div.markdown",
                            "div[class*='content']",
                            "div[class*='markdown']",
                            "div[class*='response']",
                            "pre",
                            "p",
                        ]
                        for selector in alternatives:
                            response_element = await response_message.query_selector(selector)
                            if response_element:
                                test_content = await response_element.inner_text()
                                if test_content and len(test_content.strip()) > 1:
                                    break
                                else:
                                    response_element = None

                    if not response_element:
                        # Try any div with meaningful content
                        all_divs = await response_message.query_selector_all("div")
                        for div in all_divs:
                            test_content = await div.inner_text()
                            if test_content and len(test_content.strip()) > 10:
                                response_element = div
                                break

                    if not response_element:
                        response_element = response_message

                    if response_element:
                        try:
                            indicator = await response_message.query_selector(self.SELECTORS["streaming_indicator"])
                            if indicator and await indicator.is_visible():
                                saw_streaming_ui = True
                        except Exception:
                            pass

                        current_content = await response_element.inner_text() or ""
                        current_content_stripped = current_content.strip()

                        if first_token_time is None and len(current_content_stripped) > 0:
                            first_token_time = time.time()
                        
                        content = current_content
                        
                        # Check if streaming is complete (content stable for ~3s)
                        if (
                            len(content) == last_content_length
                            and len(current_content_stripped) > 0
                        ):
                            stable_count += 1
                            if stable_count >= max_stable_checks:
                                # Prefer a stronger completion signal when available:
                                # if a loading/streaming indicator is visible, keep waiting.
                                try:
                                    indicator = await self.page.query_selector(self.SELECTORS["streaming_indicator"])
                                    if indicator and await indicator.is_visible():
                                        stable_count = 0
                                    else:
                                        timed_out = False
                                        break
                                except Exception:
                                    # If indicator detection is unreliable, fall back to stable-content heuristic.
                                    timed_out = False
                                    break
                        else:
                            stable_count = 0
                            last_content_length = len(content)
                
                await asyncio.sleep(poll_interval_s)
            
            end_time = time.time()
            
            total_duration_ms = (end_time - send_time) * 1000
            ttft_ms = (first_token_time - send_time) * 1000 if first_token_time else total_duration_ms
            tokens_rendered = len(content) // 4 if content else 0  # ~4 chars per token

            content_stripped = content.strip()
            if not content_stripped:
                if timed_out and saw_assistant_message:
                    timeout_reason = "Timed out waiting for first assistant token"
                    if saw_streaming_ui:
                        timeout_reason += " (assistant was still streaming/thinking)"
                    return BrowserChatResult(
                        content=content,
                        ttft_ms=ttft_ms,
                        total_duration_ms=total_duration_ms,
                        tokens_rendered=0,
                        success=False,
                        error=timeout_reason,
                    )
                return BrowserChatResult(
                    content=content,
                    ttft_ms=ttft_ms,
                    total_duration_ms=total_duration_ms,
                    tokens_rendered=0,
                    success=False,
                    error=(
                        "No assistant response content detected "
                        f"(assistant_seen={saw_assistant_message}, timed_out={timed_out})"
                    ),
                )

            if timed_out:
                timeout_reason = "Timed out before response completion (partial response possible)"
                if "timeout_stage" in locals() and timeout_stage == "completion":
                    timeout_reason = "Timed out waiting for response completion after first token"
                return BrowserChatResult(
                    content=content,
                    ttft_ms=ttft_ms,
                    total_duration_ms=total_duration_ms,
                    tokens_rendered=tokens_rendered,
                    success=False,
                    error=timeout_reason,
                )
            
            return BrowserChatResult(
                content=content,
                ttft_ms=ttft_ms,
                total_duration_ms=total_duration_ms,
                tokens_rendered=tokens_rendered,
                success=True,
            )
            
        except Exception as e:
            end_time = time.time()
            return BrowserChatResult(
                content="",
                ttft_ms=0,
                total_duration_ms=(end_time - start_time) * 1000,
                tokens_rendered=0,
                success=False,
                error=str(e),
            )
    
    async def take_screenshot(self, path: str) -> None:
        """Take a screenshot of the current page."""
        await self.page.screenshot(path=path)


class BrowserPool:
    """
    Pool of browser clients for concurrent benchmarks.
    
    Uses shared browser contexts by default (lighter weight) or
    isolated browser instances for full separation.
    """
    
    def __init__(
        self,
        base_url: str,
        headless: bool = True,
        slow_mo: int = 0,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        timeout: float = 30000,
        use_isolated_browsers: bool = False,
    ):
        self.base_url = base_url
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout = timeout
        self.use_isolated_browsers = use_isolated_browsers
        
        self._playwright: Optional[Playwright] = None
        self._shared_browser: Optional[Browser] = None
        self._clients: List[BrowserClient] = []
        self._user_credentials: List[Dict[str, str]] = []
    
    async def initialize(self) -> None:
        """Start Playwright and launch shared browser if using contexts."""
        self._playwright = await async_playwright().start()
        
        if not self.use_isolated_browsers:
            self._shared_browser = await self._playwright.chromium.launch(
                headless=self.headless,
                slow_mo=self.slow_mo,
            )
    
    async def create_clients(
        self,
        credentials: List[Dict[str, str]],
        login: bool = True,
        batch_size: int = 10,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[BrowserClient]:
        """Create and optionally login browser clients for each credential set."""
        self._user_credentials = credentials
        self._clients = []        
        async def create_context() -> BrowserClient:
            """Create a browser client with a new context."""
            if self.use_isolated_browsers:
                client = BrowserClient(
                    base_url=self.base_url,
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    viewport_width=self.viewport_width,
                    viewport_height=self.viewport_height,
                    timeout=self.timeout,
                )
                await client.launch()
            else:
                client = BrowserClient(
                    base_url=self.base_url,
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    viewport_width=self.viewport_width,
                    viewport_height=self.viewport_height,
                    timeout=self.timeout,
                )
                client._playwright = self._playwright
                client._browser = self._shared_browser
                client._context = await self._shared_browser.new_context(
                    viewport={"width": self.viewport_width, "height": self.viewport_height},
                )
                client._page = await client._context.new_page()
                client._page.set_default_timeout(self.timeout)
            return client
        
        async def create_and_login(cred: Dict[str, str]) -> BrowserClient:
            """Create context and login."""
            client = await create_context()
            if login:
                await client.login(cred["email"], cred["password"])
            return client
        
        total = len(credentials)
        completed = 0
        effective_batch_size = batch_size
        
        for batch_start in range(0, total, effective_batch_size):
            batch_end = min(batch_start + effective_batch_size, total)
            batch_creds = credentials[batch_start:batch_end]
            
            batch_tasks = [create_and_login(cred) for cred in batch_creds]
            batch_clients = await asyncio.gather(*batch_tasks, return_exceptions=True)
            
            # Retry failures individually
            for i, result in enumerate(batch_clients):
                if isinstance(result, Exception):
                    try:
                        client = await create_context()
                        if login:
                            await asyncio.sleep(1)
                            await client.login(
                                batch_creds[i]["email"],
                                batch_creds[i]["password"],
                                max_retries=5,
                            )
                        self._clients.append(client)
                    except Exception as e:
                        raise RuntimeError(f"Failed to create/login client {batch_start + i}: {e}")
                else:
                    self._clients.append(result)
            
            completed += len(batch_creds)
            if progress_callback:
                progress_callback(completed, total)
            
            if batch_end < total:
                await asyncio.sleep(0.3)
        
        return self._clients
    
    @property
    def clients(self) -> List[BrowserClient]:
        """Get the list of browser clients."""
        return self._clients
    
    async def close_all(self) -> None:
        """Close all clients and clean up."""
        for client in self._clients:
            try:
                if self.use_isolated_browsers:
                    await client.close()
                else:
                    if client._context:
                        await client._context.close()
                        client._context = None
                        client._page = None
            except Exception:
                pass
        
        self._clients = []
        
        if self._shared_browser:
            await self._shared_browser.close()
            self._shared_browser = None
        
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
