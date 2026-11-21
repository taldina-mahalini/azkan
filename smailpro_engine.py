import asyncio
import json
import logging
import re
import time
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Playwright, BrowserContext, Page
import config
from database import db

logger = logging.getLogger(__name__)

class SmailProEngine:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.lock = asyncio.Lock()
        self.is_initialized = False
        self.keep_alive_task: Optional[asyncio.Task] = None

    async def initialize(self):
        """Initializes the persistent browser context and opens SmailPro."""
        if self.is_initialized and self.page and not self.page.is_closed():
            return

        try:
            self.playwright = await async_playwright().start()
            
            # Persistent context keeps cookies and login state
            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
            
            logger.info("Starting Playwright persistent context at %s (headless=%s)", 
                        config.BROWSER_PROFILE_DIR, config.SMAILPRO_HEADLESS)
            
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(config.BROWSER_PROFILE_DIR),
                headless=config.SMAILPRO_HEADLESS,
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                args=args
            )

            # Try to load session from environment variables or file
            loaded_session = None
            if config.SMAILPRO_SESSION_JSON:
                try:
                    loaded_session = json.loads(config.SMAILPRO_SESSION_JSON)
                    logger.info("Loaded SmailPro session from SMAILPRO_SESSION_JSON environment variable")
                except Exception as e:
                    logger.warning("Failed parsing SMAILPRO_SESSION_JSON: %s", e)
            elif config.SMAILPRO_SESSION_B64:
                try:
                    import base64
                    raw_decoded = base64.b64decode(config.SMAILPRO_SESSION_B64).decode("utf-8")
                    loaded_session = json.loads(raw_decoded)
                    logger.info("Loaded BotOtpbyGMAIL session from SMAILPRO_SESSION_B64 environment variable")
                except Exception as e:
                    logger.warning("Failed parsing SMAILPRO_SESSION_B64: %s", e)
            elif db.get_setting("smailpro_session"):
                try:
                    import base64
                    db_b64 = db.get_setting("smailpro_session")
                    raw_decoded = base64.b64decode(db_b64).decode("utf-8")
                    loaded_session = json.loads(raw_decoded)
                    logger.info("Loaded BotOtpbyGMAIL session from persistent database settings table")
                except Exception as e:
                    logger.warning("Could not load session from database settings: %s", e)
            elif config.SESSION_STATE_FILE.exists():
                try:
                    with open(config.SESSION_STATE_FILE, "r", encoding="utf-8") as f:
                        loaded_session = json.load(f)
                        logger.info("Loaded BotOtpbyGMAIL session from %s", config.SESSION_STATE_FILE)
                except Exception as e:
                    logger.warning("Could not load session state file: %s", e)

            if loaded_session and "cookies" in loaded_session:
                cleaned_cookies = []
                for c in loaded_session["cookies"]:
                    cd = dict(c)
                    if cd.get("sameSite") not in ["Strict", "Lax", "None"]:
                        cd["sameSite"] = "Lax"
                    cleaned_cookies.append(cd)
                await self.context.add_cookies(cleaned_cookies)
                logger.info("Successfully added %d session cookies to browser context", len(cleaned_cookies))

            # Open SmailPro page
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            
            logger.info("Navigating to https://smailpro.com/temporary-email ...")
            try:
                await self.page.goto("https://smailpro.com/temporary-email", timeout=45000)
                await self.page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning("Initial page navigation notice: %s", e)

            # Check login status
            status = await self.check_session_status()
            logger.info("BotOtpbyGMAIL initial session status: %s", status)

            # If not logged in on startup, attempt automatic recovery via SSO
            if not status.get("isLoggedIn"):
                logger.info("Attempting automatic session recovery via SSO on startup...")
                recovered = await self.attempt_session_recovery()
                if recovered:
                    status = await self.check_session_status()
                    logger.info("Post-recovery session status: %s", status)

            self.is_initialized = True

            # Start aggressive keep-alive loop (pings every 4 minutes & auto-saves cookies)
            if not self.keep_alive_task or self.keep_alive_task.done():
                self.keep_alive_task = asyncio.create_task(self._keep_alive_loop())
        except Exception as e:
            logger.error("Failed to initialize SmailProEngine: %s", e, exc_info=True)
            self.is_initialized = False
            raise

    async def ensure_page_ready(self):
        """Ensures the page is open and alive."""
        if not self.is_initialized or not self.page or self.page.is_closed():
            await self.initialize()
        else:
            try:
                # Ping page
                await self.page.evaluate("() => document.title")
            except Exception:
                logger.warning("Page unresponsive, reinitializing...")
                await self.initialize()

    async def check_session_status(self) -> Dict[str, Any]:
        """Checks if current session on Smailpro is logged in and has premium."""
        if not self.page or self.page.is_closed():
            return {"isLoggedIn": False, "isPremium": False}

        try:
            status = await self.page.evaluate('''() => {
                // Method 1: Check DOM user menu
                const menuUser = document.querySelector('[x-show="menu_user"]');
                let menuText = '';
                if (menuUser) {
                    menuText = (menuUser.innerText || '').toLowerCase();
                }

                const isSignOut = menuText.includes('sign out') || menuText.includes('dashboard');
                const isGuest = menuText.includes('hi! guest') || menuText.includes('sign in');
                const isPremiumByText = menuText.includes('premium') || menuText.includes('days left');

                let userName = '';
                let daysLeft = '';
                if (menuUser) {
                    const lines = (menuUser.innerText || '').split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                    if (lines.length > 0 && !lines[0].toLowerCase().includes('guest')) {
                        userName = lines[0];
                    }
                    for (const line of lines) {
                        if (line.toLowerCase().includes('days left') || line.toLowerCase().includes('day left')) {
                            daysLeft = line;
                            break;
                        }
                    }
                }

                // Method 2: Check Alpine stack if available
                let alpineLoggedIn = false;
                let alpinePremium = false;
                const apps = document.querySelectorAll('[x-data]');
                apps.forEach(app => {
                    if (app._x_dataStack) {
                        app._x_dataStack.forEach(d => {
                            if (typeof d.isLoggedIn !== 'undefined' && d.isLoggedIn) alpineLoggedIn = true;
                            if (typeof d.isPremium !== 'undefined' && d.isPremium) alpinePremium = true;
                        });
                    }
                });

                const finalLoggedIn = (isSignOut && !isGuest) || alpineLoggedIn;
                const finalPremium = isPremiumByText || alpinePremium;

                return {
                    isLoggedIn: !!finalLoggedIn,
                    isPremium: !!finalPremium,
                    userName: userName || (finalLoggedIn ? 'Member' : 'Guest'),
                    daysLeft: daysLeft || (finalPremium ? 'Aktif' : '')
                };
            }''')
            return status
        except Exception as e:
            logger.error("Error checking session status: %s", e)
            return {"isLoggedIn": False, "isPremium": False, "error": str(e)}

    async def save_current_session_to_disk(self, status: Optional[Dict[str, Any]] = None):
        """Persists live refreshed cookies and status to disk (config.SESSION_STATE_FILE)."""
        try:
            if not self.context:
                return
            cookies = await self.context.cookies()
            if not cookies:
                return
            cur_status = status or await self.check_session_status()
            session_data = {
                "cookies": cookies,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": cur_status
            }
            raw_str = json.dumps(session_data, indent=2)
            with open(config.SESSION_STATE_FILE, "w", encoding="utf-8") as f:
                f.write(raw_str)
            logger.debug("Successfully saved %d live session cookies to %s", len(cookies), config.SESSION_STATE_FILE)

            # Also persist to database settings
            try:
                import base64
                b64_str = base64.b64encode(raw_str.encode("utf-8")).decode("utf-8")
                db.set_setting("smailpro_session", b64_str)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Could not persist session cookies to disk: %s", e)

    async def attempt_session_recovery(self) -> bool:
        """
        Attempts to revive session using stored persistent remember_web token via my.sonjj.com.
        When successful, saves refreshed cookies to disk immediately.
        """
        try:
            logger.info("Attempting session recovery: navigating to my.sonjj.com SSO...")
            await self.page.goto("https://my.sonjj.com/login?back=https%3A%2F%2Fsmailpro.com%2Ftemporary-email", timeout=45000)
            await self.page.wait_for_timeout(4000)

            # If remember_web cookie was valid, my.sonjj.com automatically redirects to smailpro.com
            curr_url = self.page.url
            if "temporary-email" not in curr_url:
                await self.page.goto("https://smailpro.com/temporary-email", timeout=30000)
                await self.page.wait_for_timeout(3000)

            status = await self.check_session_status()
            if status.get("isLoggedIn"):
                logger.info("SSO Recovery SUCCESS! Status: %s", status)
                await self.save_current_session_to_disk(status)
                return True
        except Exception as e:
            logger.warning("Error during session recovery attempt: %s", e)
        return False

    async def reload_session(self, session_data: dict) -> Dict[str, Any]:
        """Injects new cookies into browser context, reloads page via SSO, and checks status."""
        async with self.lock:
            await self.ensure_page_ready()
            if "cookies" in session_data:
                cleaned_cookies = []
                for c in session_data["cookies"]:
                    cd = dict(c)
                    if cd.get("sameSite") not in ["Strict", "Lax", "None"]:
                        cd["sameSite"] = "Lax"
                    cleaned_cookies.append(cd)
                await self.context.add_cookies(cleaned_cookies)
                logger.info("Injected %d cleaned cookies into context from reload_session", len(cleaned_cookies))

            # Trigger SSO handshake directly to sync my.sonjj.com session to smailpro.com
            logger.info("Performing SSO handshake to sync cookies...")
            try:
                await self.page.goto("https://my.sonjj.com/login?back=https%3A%2F%2Fsmailpro.com%2Ftemporary-email", timeout=45000)
                await self.page.wait_for_timeout(4000)
            except Exception as e:
                logger.warning("SSO navigation notice: %s", e)

            if "temporary-email" not in self.page.url:
                await self.page.goto("https://smailpro.com/temporary-email", timeout=30000)
                await self.page.wait_for_timeout(3000)

            status = await self.check_session_status()
            if not status.get("isLoggedIn"):
                logger.info("Direct check after SSO showed not logged in, running session recovery...")
                await self.attempt_session_recovery()
                status = await self.check_session_status()

            if status.get("isLoggedIn"):
                await self.save_current_session_to_disk(status)
            return status

    async def _keep_alive_loop(self):
        """
        Pings Smailpro every 4 minutes (240s) to keep session active,
        auto-renews session tokens, and continuously writes live cookies to disk.
        """
        while True:
            try:
                await asyncio.sleep(240)  # Ping every 4 minutes (prevents Laravel 120-min session timeout)
                if self.is_initialized and self.page and not self.page.is_closed():
                    async with self.lock:
                        # Step 1: Lightweight HEAD touch
                        try:
                            await self.page.evaluate('''() => {
                                fetch('/app/create?username=random&type=real&domain=gmail.com&server=2', { method: 'HEAD' }).catch(() => {});
                            }''')
                        except Exception:
                            pass

                        # Step 2: Check status
                        status = await self.check_session_status()
                        if status.get("isLoggedIn"):
                            # Save live renewed cookies to disk so reboots/restarts keep the session!
                            await self.save_current_session_to_disk(status)
                        else:
                            logger.warning("Keep-alive detected session dropped! Attempting auto-recovery via SSO...")
                            recovered = await self.attempt_session_recovery()
                            if recovered:
                                logger.info("Keep-alive SSO recovery succeeded!")
                            else:
                                logger.warning("Keep-alive SSO recovery could not restore session.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("SmailPro keep-alive error: %s", e)

    async def create_email(self, service: str = "gmail", max_retries: int = 10) -> Dict[str, Any]:
        """
        Creates a new temporary email for Gmail (Server 2/3), Outlook (Server 2), or Hotmail (Server 2).
        Uses Real Account (Premium) and guarantees unique email that was never generated before.
        """
        import random
        from database import db

        async with self.lock:
            await self.ensure_page_ready()
            
            service_lower = service.lower().strip()
            if service_lower == "gmail":
                domain = "gmail.com"
                server_choice = random.choice(["2", "3"])  # Server 2 (1000) & Server 3 (756) = ~1700 stok
                email_type = "google"
            elif service_lower == "hotmail":
                domain = "hotmail.com"
                server_choice = "2"  # Server 2 = ~8700 stok
                email_type = "microsoft"
            elif service_lower == "outlook":
                domain = "outlook.com"
                server_choice = "2"  # Server 2 = ~8700 stok
                email_type = "microsoft"
            else:  # outlook/hotmail / microsoft
                domain = random.choice(["outlook.com", "hotmail.com"])
                server_choice = "2"  # Server 2 = ~8700 stok
                email_type = "microsoft"

            account_type = "real"  # Real Account Premium
            last_error = "Tidak ada respon dari server"

            for attempt in range(max_retries):
                if service_lower == "gmail" and attempt > 0:
                    server_choice = random.choice(["2", "3"])
                elif service_lower not in ("gmail", "hotmail", "outlook") and attempt > 0:
                    domain = random.choice(["outlook.com", "hotmail.com"])

                logger.info("Requesting fresh %s (%s, server-%s, %s) email from SmailPro (attempt %d/%d)...",
                            service.upper(), domain, server_choice, account_type, attempt + 1, max_retries)

                try:
                    result = await self.page.evaluate('''async (cfg) => {
                        const query = `?username=random&type=${cfg.type}&domain=${cfg.domain}&server=${cfg.server}`;
                        const url = `/app/create${query}`;
                        
                        const headers = { 'Content-Type': 'application/json' };
                        const response = await fetch(url, {
                            method: 'GET',
                            headers: headers
                        });

                        if (!response.ok) {
                            const err = await response.json().catch(() => ({ msg: 'HTTP ' + response.status }));
                            return { success: false, error: err.msg || err.message || ('HTTP ' + response.status) };
                        }

                        const data = await response.json();
                        if (data && data.address) {
                            return {
                                success: true,
                                email: data.address,
                                key: data.key || '',
                                timestamp: data.timestamp || Math.floor(Date.now() / 1000)
                            };
                        }
                        return { success: false, error: data.msg || 'No email returned' };
                    }''', {"domain": domain, "type": account_type, "server": server_choice, "emailType": email_type})

                    if result.get("success") and result.get("email"):
                        cand = result["email"]
                        if db.is_email_used(cand):
                            logger.warning("Duplicate detected: %s was already used in an earlier order. Regenerating...", cand)
                            last_error = f"Email {cand} duplikat"
                            await asyncio.sleep(0.5)
                            continue

                        logger.info("Successfully generated fresh real email: %s", cand)
                        return result
                    else:
                        last_error = result.get("error", "Unknown error")
                        logger.warning("Attempt %d returned: %s", attempt + 1, result)

                except Exception as e:
                    last_error = str(e)
                    logger.warning("Internal fetch failed on attempt %d: %s", attempt + 1, e)

                await asyncio.sleep(0.5)

            # Check if session is expired or not logged in
            session_info = await self.check_session_status()
            if not session_info.get("isLoggedIn"):
                logger.warning("Session not logged in during create_email. Attempting auto SSO recovery...")
                if await self.attempt_session_recovery():
                    logger.info("Session successfully recovered via SSO! Performing immediate retry...")
                    try:
                        retry_res = await self.page.evaluate('''async (cfg) => {
                            const query = `?username=random&type=${cfg.type}&domain=${cfg.domain}&server=${cfg.server}`;
                            const url = `/app/create${query}`;
                            const headers = { 'Content-Type': 'application/json' };
                            const response = await fetch(url, { method: 'GET', headers: headers });
                            if (!response.ok) return { success: false, error: 'HTTP ' + response.status };
                            const data = await response.json();
                            if (data && data.address) {
                                return {
                                    success: true,
                                    email: data.address,
                                    key: data.key || '',
                                    timestamp: data.timestamp || Math.floor(Date.now() / 1000)
                                };
                            }
                            return { success: false, error: data.msg || 'No email returned' };
                        }''', {"domain": domain, "type": account_type, "server": server_choice, "emailType": email_type})
                        if retry_res.get("success") and retry_res.get("email"):
                            cand = retry_res["email"]
                            if not db.is_email_used(cand):
                                return retry_res
                    except Exception as retry_err:
                        logger.warning("Post-recovery retry failed: %s", retry_err)

                return {
                    "success": False,
                    "error": f"Sesi BotOtpbyGMAIL belum login atau kedaluwarsa ({last_error}). Silakan perbarui sesi login via Panel Admin."
                }

            return {"success": False, "error": f"BotOtpbyGMAIL: {last_error}"}

    async def check_inbox(self, email_address: str, key: str = "", timestamp: int = 0) -> Dict[str, Any]:
        """
        Polls inbox for the given email using SmailPro + Sonjj API pipeline.
        Returns:
        {
            "success": bool,
            "has_messages": bool,
            "messages": [
                {
                    "mid": str,
                    "sender": str,
                    "subject": str,
                    "body": str,
                    "otp_code": str (if extracted)
                }
            ],
            "error": str
        }
        """
        async with self.lock:
            await self.ensure_page_ready()
            
            logger.info("Checking inbox for %s...", email_address)

            # Determine service route
            domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
            if "gmail" in domain:
                service_path = "temp_gmail"
            elif "outlook" in domain or "hotmail" in domain:
                service_path = "temp_outlook"
            else:
                service_path = "temp_email"

            try:
                # Execute full inbox check inside browser context (carries all auth cookies & avoids CORS)
                inbox_result = await self.page.evaluate('''async (param) => {
                    // Step 1: Request payload from /app/inbox
                    const body = [{
                        address: param.address,
                        timestamp: param.timestamp || Math.floor(Date.now() / 1000),
                        key: param.key || ''
                    }];

                    const inboxResp = await fetch('/app/inbox', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body)
                    });

                    if (!inboxResp.ok) {
                        return { success: false, error: 'HTTP ' + inboxResp.status };
                    }

                    const inboxData = await inboxResp.json();
                    if (!Array.isArray(inboxData) || inboxData.length === 0) {
                        return { success: true, messages: [] };
                    }

                    const payload = inboxData[0]?.payload;
                    if (!payload) {
                        return { success: true, messages: [] };
                    }

                    // Step 2: Fetch message list from api.sonjj.com
                    const sonjjInboxUrl = `https://api.sonjj.com/v1/${param.service}/inbox?payload=${encodeURIComponent(payload)}`;
                    const sonjjResp = await fetch(sonjjInboxUrl);
                    if (!sonjjResp.ok) {
                        return { success: false, error: 'Sonjj HTTP ' + sonjjResp.status };
                    }

                    const sonjjData = await sonjjResp.json();
                    const rawMessages = sonjjData?.messages || sonjjData?.data?.messages || [];

                    if (!Array.isArray(rawMessages) || rawMessages.length === 0) {
                        return { success: true, messages: [] };
                    }

                    // Step 3: Fetch body for the messages
                    const messagesWithBody = [];
                    for (const m of rawMessages) {
                        const mid = m.mid || m.id;
                        let bodyText = m.body || '';

                        if (mid && !bodyText) {
                            try {
                                const msgUrl = `https://api.sonjj.com/v1/${param.service}/message?payload=${encodeURIComponent(payload)}&mid=${encodeURIComponent(mid)}`;
                                const msgResp = await fetch(msgUrl);
                                if (msgResp.ok) {
                                    const msgData = await msgResp.json();
                                    bodyText = msgData?.body || msgData?.data?.body || '';
                                }
                            } catch (e) {
                                console.error('Error fetching body for mid:', mid, e);
                            }
                        }

                        messagesWithBody.push({
                            mid: mid,
                            sender: m.textFrom || m.from || m.sender || '',
                            subject: m.textSubject || m.subject || '',
                            date: m.textDate || m.date || '',
                            body: bodyText
                        });
                    }

                    return { success: true, messages: messagesWithBody };
                }''', {
                    "address": email_address,
                    "key": key,
                    "timestamp": timestamp,
                    "service": service_path
                })

                if not inbox_result.get("success"):
                    logger.warning("Inbox request failed: %s", inbox_result.get("error"))
                    return {"success": False, "has_messages": False, "messages": [], "error": inbox_result.get("error")}

                raw_messages = inbox_result.get("messages", [])
                if not raw_messages:
                    logger.info("No messages found for %s", email_address)
                    return {"success": True, "has_messages": False, "messages": []}

                logger.info("Found %d incoming message(s) for %s!", len(raw_messages), email_address)

                parsed_messages = []
                for msg in raw_messages:
                    sender = msg.get("sender") or "Tidak Diketahui"
                    subject = msg.get("subject") or "(Tanpa Subjek)"
                    raw_body = msg.get("body") or ""
                    clean_body = self.clean_html(raw_body) if raw_body else ""

                    # Extract OTP from subject + body (prioritizing subject, supporting alphanumeric & numeric)
                    otp_code = self.extract_otp(subject, clean_body)

                    parsed_messages.append({
                        "mid": msg.get("mid"),
                        "sender": sender.strip(),
                        "subject": subject.strip(),
                        "body": clean_body[:500],
                        "otp_code": otp_code
                    })

                return {
                    "success": True,
                    "has_messages": len(parsed_messages) > 0,
                    "messages": parsed_messages
                }

            except Exception as e:
                logger.error("Error checking inbox: %s", e, exc_info=True)
                return {"success": False, "has_messages": False, "messages": [], "error": str(e)}

    @staticmethod
    def extract_otp(subject: str, body: Optional[str] = None) -> Optional[str]:
        """
        Extracts alphanumeric or numeric OTP verification codes from subject and body.
        Supports digits, alphanumeric uppercase codes (e.g. Dreamina 'XSRKGH'), Google G-codes, etc.
        """
        clean_subj = subject.strip() if subject else ""
        clean_b = body.strip() if body else ""

        # Keyword prefix pattern for OTP / verification code
        kw_prefix = (
            r'(?i)(?:verification\s*code|security\s*code|confirm(?:ation)?\s*code|'
            r'login\s*code|access\s*code|single-use\s*code|one-time\s*(?:passcode|password|code)|'
            r'kode\s*(?:verifikasi|keamanan|otp|akses|masuk)|otp\s*code|otp|code|kode|pin)'
            r'\s*(?:is|adalah|:|;|-|=|\s)\s*'
        )
        # Keyword suffix pattern (e.g. 'XSRKGH is your verification code')
        kw_suffix = (
            r'([A-Za-z0-9]{4,10})\s+(?:is\s+your|adalah\s+kode|is\s+the)\s+'
            r'(?:[a-zA-Z0-9\s]+)?(?:verification\s*code|security\s*code|code|otp|kode)'
        )

        banned_words = {
            "EMAIL", "ACCOUNT", "GOOGLE", "VERIFY", "WELCOME", "PLEASE", "UPDATE",
            "SERVICE", "PASSWORD", "MESSAGE", "SUPPORT", "SECURITY", "000000", "FFFFFF"
        }

        # --- 1. SEARCH SUBJECT FIRST (highest accuracy, zero HTML noise) ---
        if clean_subj:
            # Pattern 1a: Prefix match in subject (e.g. 'code is XSRKGH', 'OTP: 123456')
            m = re.search(kw_prefix + r'([A-Za-z0-9]{4,10})\b', clean_subj)
            if m:
                val = m.group(1).strip()
                if val.upper() not in banned_words:
                    return val

            # Pattern 1b: Suffix match in subject (e.g. 'XSRKGH is your code')
            m = re.search(kw_suffix, clean_subj, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val.upper() not in banned_words:
                    return val

            # Pattern 1c: Standalone uppercase token or numbers in subject (e.g. '[Dreamina] XSRKGH')
            tokens = re.findall(r'\b([A-Za-z0-9]{5,8})\b', clean_subj)
            for t in tokens:
                if t.isupper() and t not in banned_words:
                    # Must have at least 1 digit or be 6 chars like 'XSRKGH'
                    if any(c.isdigit() for c in t) or len(t) == 6:
                        return t

        # --- 2. SEARCH BODY WITH KEYWORDS ---
        if clean_b:
            m = re.search(kw_prefix + r'([A-Za-z0-9]{4,10})\b', clean_b)
            if m:
                val = m.group(1).strip()
                if val.upper() not in banned_words:
                    return val

            m = re.search(kw_suffix, clean_b, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val.upper() not in banned_words:
                    return val

            # Pattern 2a: Google format 'G-123456'
            m = re.search(r'\b[Gg]-([0-9]{6})\b', clean_b)
            if m:
                return m.group(1).strip()

            # Pattern 2b: Dashed numbers '123-456'
            m = re.search(r'\b([0-9]{3}[-\s][0-9]{3})\b', clean_b)
            if m:
                return m.group(1).replace("-", "").replace(" ", "").strip()

            # Pattern 2c: Standalone 6-digit number (excluding colors like 000000 or years)
            six_digits = re.findall(r'(?<!\d)([0-9]{6})(?!\d)', clean_b)
            for d in six_digits:
                if d not in ("000000", "111111", "999999", "123456") and not d.startswith("202"):
                    return d

            # Pattern 2d: Standalone 4-8 digit number
            digits = re.findall(r'(?<!\d)([0-9]{4,8})(?!\d)', clean_b)
            for d in digits:
                if d not in ("2024", "2025", "2026", "2027", "0000", "00000", "000000"):
                    return d

        return None

    @staticmethod
    def clean_html(raw_html: str) -> str:
        """Removes html tags, style/script blocks, CSS hex colors, and decodes simple text."""
        if not raw_html:
            return ""
        # 1. Remove style and script tags with inner content
        text = re.sub(r'(?is)<style.*?>.*?</style>', ' ', raw_html)
        text = re.sub(r'(?is)<script.*?>.*?</script>', ' ', text)
        # 2. Remove all other html tags & entities
        text = re.sub(r'<.*?>|&([a-z0-9]+|#[0-9]{1,6}|#x[0-9a-f]{1,6});', ' ', text)
        # 3. Remove hex color references like #000000 or #ffffff
        text = re.sub(r'#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b', ' ', text)
        return " ".join(text.split())

    async def close(self):
        """Clean shutdown of browser context."""
        try:
            if self.context:
                await self.context.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.error("Error closing browser: %s", e)

# Singleton SmailProEngine Instance
smailpro = SmailProEngine()
