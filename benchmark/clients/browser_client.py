"""
Browser client for UI-based benchmarking using Playwright.
"""

import asyncio
import json
import time
import re
from typing import Optional, List, Dict, Any, Callable, Set
from dataclasses import dataclass

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright


@dataclass
class BrowserChatResult:
    """Result from a browser-based chat interaction."""
    content: str
    first_status_ms: float  # Time to first status emitter appearing in UI (0 if none)
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
        "status_emitter": '.status-description',
        "streaming_indicator": '.typing-indicator, [class*="loading"], [class*="streaming"]',
        "error_toast": '[data-sonner-toast][data-type="error"]',
        "error_toast_title": '[data-sonner-toast][data-type="error"] [data-title]',
    }

    # Common provider/model error text patterns surfaced in the assistant pane.
    # Keep these conservative to avoid false positives on normal content.
    ERROR_TEXT_PATTERNS = [
        r"^error\s*[:\-]",
        r"^something went wrong\b",
        r"^request failed\b",
        r"^failed to (generate|get|fetch|complete)\b",
        r"\brate limit(?:ed| exceeded)?\b",
        r"\btoo many requests\b",
        r"\binsufficient[_ ]quota\b",
        r"\binvalid api key\b",
        r"\bmodel .* not found\b",
        r"\bprovider .* error\b",
        r"\bconnection (?:error|failed|refused|timed out)\b",
        r"\b502 bad gateway\b",
        r"\b503 service unavailable\b",
        r"\b504 gateway timeout\b",
        r"\bcontext length exceeded\b",
    ]
    
    def __init__(
        self,
        base_url: str,
        headless: bool = True,
        slow_mo: int = 0,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        timeout: float = 30000,
        capture_network_trace: bool = False,
        network_trace_max_entries: int = 5000,
    ):
        self.base_url = base_url.rstrip('/')
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout = timeout
        self.capture_network_trace = capture_network_trace
        self.network_trace_max_entries = max(100, network_trace_max_entries)
        
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._is_logged_in: bool = False
        self._network_events: List[Dict[str, Any]] = []
        self._network_trace_attached: bool = False
        self._pending_network_trace_tasks: Set[asyncio.Task] = set()
    
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
        self._initialize_page()
    
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
        self._network_events = []
        self._network_trace_attached = False
        self._pending_network_trace_tasks.clear()
    
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
    
    async def login(
        self,
        email: str,
        password: str,
        max_retries: int = 5,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Log in to Open WebUI with retry and exponential backoff."""
        last_error = None
        
        for attempt in range(max_retries):
            try:
                if status_callback:
                    status_callback(f"login attempt {attempt + 1}/{max_retries}")
                return await self._attempt_login(email, password, status_callback=status_callback)
            except Exception as e:
                last_error = e
                if status_callback:
                    status_callback(f"login attempt {attempt + 1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    backoff = 2 ** (attempt + 1)
                    if status_callback:
                        status_callback(f"backing off {backoff}s before retry")
                    await asyncio.sleep(backoff)
        
        raise RuntimeError(f"Login failed after {max_retries} attempts: {last_error}")
    
    async def _attempt_login(
        self,
        email: str,
        password: str,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Single login attempt."""
        try:
            if status_callback:
                status_callback("navigating to /auth")
            await self.page.goto(f"{self.base_url}/auth", wait_until="domcontentloaded", timeout=60000)
            
            if status_callback:
                status_callback("waiting for auth form")
            await self.page.wait_for_selector(
                self.SELECTORS["email_input"],
                state="visible",
                timeout=60000,
            )
            await self.page.wait_for_timeout(500)
            
            if status_callback:
                status_callback("submitting credentials")
            await self.page.fill(self.SELECTORS["email_input"], email)
            await self.page.fill(self.SELECTORS["password_input"], password)
            await self.page.click(self.SELECTORS["login_button"])
            
            try:
                if status_callback:
                    status_callback("waiting for network idle")
                await self.page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            
            # Wait for redirect away from /auth
            if status_callback:
                status_callback("waiting for redirect from /auth")
            for _ in range(10):
                current_url = self.page.url
                if "/auth" not in current_url:
                    # Look for chat input as indicator of successful login
                    try:
                        if status_callback:
                            status_callback("waiting for chat input")
                        await self.page.wait_for_selector(
                            self.SELECTORS["chat_input"],
                            state="visible",
                            timeout=5000,
                        )
                    except Exception:
                        pass  # Chat input not found but we're past auth
                    if status_callback:
                        status_callback("login complete")
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
        first_status_time: Optional[float] = None
        poll_interval_s = 0.1
        max_stable_checks = 10  # ~1s of stable content before considering response complete
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
                    first_status_ms=0,
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
                    if first_status_time is None:
                        try:
                            status_node = await response_message.query_selector(self.SELECTORS["status_emitter"])
                            if status_node:
                                status_text = (await status_node.inner_text() or "").strip()
                                if status_text:
                                    first_status_time = time.time()
                        except Exception:
                            pass
                    
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
                        
                        # Check if streaming is complete (content stable for ~1s)
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
            first_status_ms = (first_status_time - send_time) * 1000 if first_status_time else 0
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
                        first_status_ms=first_status_ms,
                        ttft_ms=ttft_ms,
                        total_duration_ms=total_duration_ms,
                        tokens_rendered=0,
                        success=False,
                        error=timeout_reason,
                    )
                return BrowserChatResult(
                    content=content,
                    first_status_ms=first_status_ms,
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
                    first_status_ms=first_status_ms,
                    ttft_ms=ttft_ms,
                    total_duration_ms=total_duration_ms,
                    tokens_rendered=tokens_rendered,
                    success=False,
                    error=timeout_reason,
                )

            ui_error = await self._detect_ui_error_state(
                response_message=response_message if 'response_message' in locals() else None,
                assistant_content=content_stripped,
            )
            if ui_error:
                return BrowserChatResult(
                    content=content,
                    first_status_ms=first_status_ms,
                    ttft_ms=ttft_ms,
                    total_duration_ms=total_duration_ms,
                    tokens_rendered=tokens_rendered,
                    success=False,
                    error=ui_error,
                )

            detected_error = self._detect_assistant_error_text(content_stripped)
            if detected_error:
                return BrowserChatResult(
                    content=content,
                    first_status_ms=first_status_ms,
                    ttft_ms=ttft_ms,
                    total_duration_ms=total_duration_ms,
                    tokens_rendered=tokens_rendered,
                    success=False,
                    error=detected_error,
                )
            
            return BrowserChatResult(
                content=content,
                first_status_ms=first_status_ms,
                ttft_ms=ttft_ms,
                total_duration_ms=total_duration_ms,
                tokens_rendered=tokens_rendered,
                success=True,
            )
            
        except Exception as e:
            end_time = time.time()
            return BrowserChatResult(
                content="",
                first_status_ms=0,
                ttft_ms=0,
                total_duration_ms=(end_time - start_time) * 1000,
                tokens_rendered=0,
                success=False,
                error=str(e),
            )

    def _detect_assistant_error_text(self, content: str) -> Optional[str]:
        """Return an error classification if the assistant content looks like a UI-rendered error."""
        text = (content or "").strip()
        if not text:
            return None

        # Normalize whitespace for simpler matching.
        normalized = re.sub(r"\s+", " ", text).strip()
        lowered = normalized.lower()

        # Strong exact/near-exact phrases commonly shown by UIs/providers.
        exactish = {
            "something went wrong",
            "request failed",
            "internal server error",
            "service unavailable",
            "gateway timeout",
            "bad gateway",
        }
        if lowered in exactish:
            return f"Assistant returned error message: {normalized[:200]}"

        # Heuristic scope guard: error messages are usually fairly short.
        # Allow longer matches only for very strong leading patterns.
        for pattern in self.ERROR_TEXT_PATTERNS:
            if re.search(pattern, lowered):
                if len(normalized) <= 600 or re.match(r"^(error|something went wrong|request failed)", lowered):
                    return f"Assistant returned error message: {normalized[:200]}"

        return None

    async def _detect_ui_error_state(self, response_message: Optional[Any], assistant_content: str) -> Optional[str]:
        """Detect UI-rendered errors that may still produce placeholder assistant content like '{}'."""
        # 1) Global error toast (Open WebUI uses sonner toasts)
        try:
            toast_title = await self.page.query_selector(self.SELECTORS["error_toast_title"])
            if toast_title:
                text = (await toast_title.inner_text() or "").strip()
                if text:
                    return f"UI error toast: {text[:200]}"
        except Exception:
            pass

        # 2) Inline assistant error panel (red alert-style block in response container)
        if response_message is not None:
            try:
                inline_error = await response_message.query_selector(
                    '[id="response-content-container"] [class*="border-red"], '
                    '[id="response-content-container"] [class*="bg-red"]'
                )
                if inline_error:
                    panel_text = (await inline_error.inner_text() or "").strip()
                    panel_text = re.sub(r"\s+", " ", panel_text).strip()
                    # Open WebUI sometimes renders "{}" inside a red error panel.
                    if panel_text in {"{}", ""}:
                        return "Inline assistant error panel (empty/JSON placeholder)"
                    return f"Inline assistant error panel: {panel_text[:200]}"
            except Exception:
                pass

        # 3) Fallback: placeholder JSON-ish content is suspicious even without text patterns.
        if assistant_content.strip() in {"{}", "[]", "null"}:
            try:
                toast = await self.page.query_selector(self.SELECTORS["error_toast"])
                if toast and await toast.is_visible():
                    return f"UI error toast with placeholder assistant content: {assistant_content[:50]}"
            except Exception:
                pass

        return None
    
    async def take_screenshot(self, path: str) -> None:
        """Take a screenshot of the current page."""
        await self.page.screenshot(path=path)

    def _initialize_page(self) -> None:
        """Apply page defaults and optional diagnostics hooks."""
        if self._page is None:
            return
        self._page.set_default_timeout(self.timeout)
        self._attach_network_trace_handlers()

    def _attach_network_trace_handlers(self) -> None:
        """Capture request/response/failure events for later debugging artifacts."""
        if not self.capture_network_trace or self._page is None or self._network_trace_attached:
            return

        def append_event(event: Dict[str, Any]) -> None:
            self._network_events.append(event)
            if len(self._network_events) > self.network_trace_max_entries:
                overflow = len(self._network_events) - self.network_trace_max_entries
                del self._network_events[:overflow]

        def on_request(request: Any) -> None:
            event = {
                "ts": time.time(),
                "event": "request",
                "method": getattr(request, "method", None),
                "url": getattr(request, "url", None),
                "resource_type": getattr(request, "resource_type", None),
            }
            try:
                if self._should_capture_network_body(
                    event.get("url"),
                    event.get("resource_type"),
                ):
                    post_data = getattr(request, "post_data", None)
                    if post_data:
                        event["post_data_snippet"] = str(post_data)[:2000]
            except Exception:
                pass
            append_event(event)

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            event = {
                "ts": time.time(),
                "event": "response",
                "status": getattr(response, "status", None),
                "ok": getattr(response, "ok", None),
                "url": getattr(response, "url", None),
                "method": getattr(request, "method", None) if request else None,
                "resource_type": getattr(request, "resource_type", None) if request else None,
            }
            append_event(event)
            try:
                if self._should_capture_network_body(
                    event.get("url"),
                    event.get("resource_type"),
                ):
                    task = asyncio.create_task(self._capture_response_details(response))
                    self._pending_network_trace_tasks.add(task)
                    task.add_done_callback(lambda t: self._pending_network_trace_tasks.discard(t))
            except Exception:
                pass

        def on_request_failed(request: Any) -> None:
            failure_value = None
            try:
                failure_attr = getattr(request, "failure", None)
                failure_value = failure_attr() if callable(failure_attr) else failure_attr
            except Exception:
                failure_value = None
            append_event({
                "ts": time.time(),
                "event": "request_failed",
                "method": getattr(request, "method", None),
                "url": getattr(request, "url", None),
                "resource_type": getattr(request, "resource_type", None),
                "failure": failure_value,
            })

        self._page.on("request", on_request)
        self._page.on("response", on_response)
        self._page.on("requestfailed", on_request_failed)
        self._network_trace_attached = True

    def get_network_trace_cursor(self) -> int:
        """Return the current end index for incremental trace capture."""
        return len(self._network_events)

    def get_network_trace_events(self, start_index: int = 0) -> List[Dict[str, Any]]:
        """Return a copy of recorded network events from a starting cursor."""
        if start_index < 0:
            start_index = 0
        return [dict(evt) for evt in self._network_events[start_index:]]

    def save_network_trace(self, path: str, start_index: int = 0) -> int:
        """Write recorded network events to JSON for debugging. Returns count written."""
        events = self.get_network_trace_events(start_index=start_index)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
        return len(events)

    async def flush_network_trace(self, timeout_ms: int = 2000) -> None:
        """Wait briefly for pending async response detail captures to finish."""
        if not self._pending_network_trace_tasks:
            return
        pending = list(self._pending_network_trace_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout_ms / 1000,
            )
        except Exception:
            # Best-effort diagnostics; don't fail benchmark flow on trace capture timing.
            pass

    def _should_capture_network_body(
        self,
        url: Optional[str],
        resource_type: Optional[str],
    ) -> bool:
        """Limit expensive body/header capture to likely API fetches."""
        if resource_type not in {"fetch", "xhr"}:
            return False
        if not url:
            return False
        return "/api/" in url

    async def _capture_response_details(self, response: Any) -> None:
        """Capture headers and a bounded text snippet for API responses."""
        try:
            headers = {}
            try:
                headers = await response.all_headers()
            except Exception:
                headers = {}

            content_type = (
                headers.get("content-type")
                or headers.get("Content-Type")
                or ""
            )

            body_snippet = None
            body_capture_error = None
            lower_ct = content_type.lower()
            if any(token in lower_ct for token in ("json", "text/", "event-stream")) or not lower_ct:
                try:
                    body_text = await response.text()
                    body_snippet = (body_text or "")[:4000]
                except Exception as e:
                    body_capture_error = str(e)

            self._network_events.append({
                "ts": time.time(),
                "event": "response_details",
                "status": getattr(response, "status", None),
                "url": getattr(response, "url", None),
                "content_type": content_type,
                "headers": headers,
                "body_preview_200": (body_snippet[:200] if body_snippet else None),
                "body_snippet": body_snippet,
                "body_truncated": bool(body_snippet and len(body_snippet) >= 4000),
                "body_capture_error": body_capture_error,
            })
            if len(self._network_events) > self.network_trace_max_entries:
                overflow = len(self._network_events) - self.network_trace_max_entries
                del self._network_events[:overflow]
        except Exception:
            pass


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
        capture_network_trace: bool = False,
        network_trace_max_entries: int = 5000,
    ):
        self.base_url = base_url
        self.headless = headless
        self.slow_mo = slow_mo
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout = timeout
        self.use_isolated_browsers = use_isolated_browsers
        self.capture_network_trace = capture_network_trace
        self.network_trace_max_entries = max(100, network_trace_max_entries)
        
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
        status_callback: Optional[Callable[[str], None]] = None,
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
                    capture_network_trace=self.capture_network_trace,
                    network_trace_max_entries=self.network_trace_max_entries,
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
                    capture_network_trace=self.capture_network_trace,
                    network_trace_max_entries=self.network_trace_max_entries,
                )
                client._playwright = self._playwright
                client._browser = self._shared_browser
                client._context = await self._shared_browser.new_context(
                    viewport={"width": self.viewport_width, "height": self.viewport_height},
                )
                client._page = await client._context.new_page()
                client._initialize_page()
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
        total_batches = (total + effective_batch_size - 1) // effective_batch_size if total else 0
        
        for batch_num, batch_start in enumerate(range(0, total, effective_batch_size), start=1):
            batch_end = min(batch_start + effective_batch_size, total)
            batch_creds = credentials[batch_start:batch_end]

            if status_callback:
                action = "Creating/logining" if login else "Creating"
                status_callback(
                    f"{action} batch {batch_num}/{total_batches} "
                    f"({len(batch_creds)} sessions)"
                )

            async def create_and_login_indexed(i: int, cred: Dict[str, str]):
                try:
                    def per_session_status(msg: str):
                        if status_callback:
                            status_callback(
                                f"Session {batch_start + i + 1}/{total}: {msg}"
                            )

                    per_session_status("creating browser context")
                    client = await create_context()
                    if login:
                        per_session_status("starting login")
                        await client.login(
                            cred["email"],
                            cred["password"],
                            status_callback=per_session_status,
                        )
                    return i, client
                except Exception as e:
                    return i, e

            batch_results: List[Any] = [None] * len(batch_creds)
            batch_tasks = [
                asyncio.create_task(create_and_login_indexed(i, cred))
                for i, cred in enumerate(batch_creds)
            ]

            for finished in asyncio.as_completed(batch_tasks):
                i, result = await finished
                batch_results[i] = result
                if status_callback:
                    if isinstance(result, Exception):
                        status_callback(
                            f"Session {batch_start + i + 1}/{total} failed initial "
                            f"{'login' if login else 'create'}; retrying..."
                        )
                    else:
                        completed += 1
                        if progress_callback:
                            progress_callback(completed, total)
                        verb = "Logged in" if login else "Created"
                        status_callback(
                            f"{verb.lower()} {completed}/{total} sessions "
                            f"(batch {batch_num}/{total_batches})"
                        )
                elif not isinstance(result, Exception):
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total)
            
            # Retry failures individually
            for i, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    try:
                        if status_callback:
                            status_callback(
                                f"Retrying session {batch_start + i + 1}/{total} after login failure"
                            )
                        client = await create_context()
                        if login:
                            await asyncio.sleep(1)
                            if status_callback:
                                status_callback(
                                    f"Session {batch_start + i + 1}/{total}: retry login start"
                                )
                            await client.login(
                                batch_creds[i]["email"],
                                batch_creds[i]["password"],
                                max_retries=5,
                                status_callback=(
                                    (lambda msg, session_idx=batch_start + i + 1:
                                        status_callback(f"Session {session_idx}/{total}: {msg}"))
                                    if status_callback else None
                                ),
                            )
                        self._clients.append(client)
                        completed += 1
                        if progress_callback:
                            progress_callback(completed, total)
                        if status_callback:
                            status_callback(
                                f"Retry succeeded for session {batch_start + i + 1}/{total} "
                                f"({completed}/{total} ready)"
                            )
                    except Exception as e:
                        raise RuntimeError(f"Failed to create/login client {batch_start + i}: {e}")
                else:
                    self._clients.append(result)

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
