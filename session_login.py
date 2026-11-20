import asyncio
import base64
import json
import logging
import sys
import time
from playwright.async_api import async_playwright
import config

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def run_login_helper():
    print("\n" + "=" * 70)
    print("      🔐 BotOtpbyGMAIL PREMIUM LOGIN & AUTO-SYNC HELPER")
    print("=" * 70)
    print("Membuka browser Chromium di layar Anda...")
    print(f"Direktori Profil: {config.BROWSER_PROFILE_DIR}\n")
    
    playwright = await async_playwright().start()
    
    # Launch persistent context headed
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(config.BROWSER_PROFILE_DIR),
        headless=False,
        viewport={"width": 1280, "height": 850},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check"
        ]
    )
    
    page = context.pages[0] if context.pages else await context.new_page()
    
    login_url = "https://my.sonjj.com/login?back=https://smailpro.com/temporary-email"
    print(f"Membuka halaman login: {login_url}")
    await page.goto(login_url)
    
    # Otomatis centang Remember Me agar cookies tahan jangka panjang
    try:
        await page.wait_for_selector("#remember_me", timeout=6000)
        if not await page.is_checked("#remember_me"):
            await page.check("#remember_me")
            print("✅ Opsi 'Remember me' otomatis dicentang agar sesi login awet bertahun-tahun!")
    except Exception:
        pass

    print("\n" + "#" * 70)
    print("👉 PETUNJUK:")
    print("1. Jendela browser Chromium sudah terbuka.")
    print("2. Silakan ketik Email & Password akun Smailpro Premium Anda.")
    print("3. Pastikan kotak 'Remember me' tetap tercentang.")
    print("4. Klik tombol Login dan selesaikan tantangan jika ada.")
    print("5. Tunggu hingga halaman dialihkan ke https://smailpro.com/temporary-email.")
    print("6. Script ini akan OTOMATIS mendeteksi status login aktif dan menyimpan sesi!")
    print("   (Atau tekan ENTER jika ingin menyimpan manual setelah dashboard terbuka).")
    print("#" * 70 + "\n")
    
    # Task 1: Auto-detection in background
    async def auto_detect():
        while True:
            await asyncio.sleep(2)
            try:
                curr = page.url
                if "smailpro.com" in curr and "/login" not in curr:
                    status = await page.evaluate('''() => {
                        const app = document.querySelector('[x-data]');
                        if (app && app._x_dataStack) {
                            for (const d of app._x_dataStack) {
                                if (typeof d.isLoggedIn !== 'undefined') {
                                    return {
                                        isLoggedIn: !!d.isLoggedIn,
                                        isPremium: !!d.isPremium
                                    };
                                }
                            }
                        }
                        return { isLoggedIn: false, isPremium: false };
                    }''')
                    if status.get("isLoggedIn") or status.get("isPremium"):
                        print("\n🎉 Auto-detect: Status login Premium terdeteksi aktif!")
                        # Wait a moment for all cookies to settle
                        await asyncio.sleep(2)
                        return status
            except Exception:
                pass

    # Task 2: Wait for user press ENTER
    async def wait_enter():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, sys.stdin.readline)
        print("\nTombol ENTER ditekan oleh pengguna!")
        return True

    detect_task = asyncio.create_task(auto_detect())
    enter_task = asyncio.create_task(wait_enter())

    # Wait for either auto-detect or user pressing Enter
    done, pending = await asyncio.wait(
        [detect_task, enter_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    for p in pending:
        p.cancel()

    # Re-check status on page
    status = {}
    try:
        if not page.is_closed():
            status = await page.evaluate('''() => {
                const app = document.querySelector('[x-data]');
                if (app && app._x_dataStack) {
                    for (const d of app._x_dataStack) {
                        if (typeof d.isLoggedIn !== 'undefined') {
                            return {
                                isLoggedIn: !!d.isLoggedIn,
                                isPremium: !!d.isPremium,
                                email: d.selectedEmail ? d.selectedEmail.address : null
                            };
                        }
                    }
                }
                return { isLoggedIn: false, isPremium: false };
            }''')
    except Exception:
        pass

    # Save cookies & session
    cookies = await context.cookies()
    session_data = {
        "cookies": cookies,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status
    }
    
    with open(config.SESSION_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2)

    raw_json_str = json.dumps(session_data)
    b64_str = base64.b64encode(raw_json_str.encode("utf-8")).decode("utf-8")
        
    print("\n" + "=" * 70)
    print(f"✅ SESI BOTOTPBYGMAIL BERHASIL DISIMPAN KE: {config.SESSION_STATE_FILE}")
    print(f"📊 Total Cookies Tersimpan: {len(cookies)}")
    print(f"🔑 Status Akun: LoggedIn={status.get('isLoggedIn')}, Premium={status.get('isPremium')}")
    print("=" * 70)
    
    print("\n" + "-" * 70)
    print("📋 KODE BASE64 UNTUK BOT TELEGRAM / RENDER (Bisa langsung dipaste di bot Telegram):")
    print("-" * 70)
    print(b64_str)
    print("-" * 70)
    print("💡 CARA MUDAH UPDATE SESI KE RENDER / VPS:")
    print("1. Buka Bot Telegram Anda sebagai Admin -> Klik [⚙️ Panel Admin]")
    print("2. Klik menu [🔑 Input Sesi (Base64)]")
    print("3. Tempel (Paste) kode base64 di atas -> Selesai! Bot langsung aktif seketika.")
    print("4. Untuk Render, salin juga kode di atas ke Environment Variables: SMAILPRO_SESSION_B64.")
    print("=" * 70)

    print("\nMenutup browser dalam 3 detik...")
    await asyncio.sleep(3)
    try:
        await context.close()
        await playwright.stop()
    except Exception:
        pass
    print("✅ Selesai! Sesi siap digunakan.\n")

if __name__ == "__main__":
    asyncio.run(run_login_helper())
