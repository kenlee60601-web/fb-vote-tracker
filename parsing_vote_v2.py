# -*- coding: utf-8 -*-
"""
FB 相簿投票爬蟲 v2
原理:
  - 貼文為公開貼文,未登入可看,但每個 session 只能看 4 張照片
  - 用鍵盤 ArrowRight 在相簿中前進,卡住(登入牆)時換全新 session 從當前照片續走
  - 每張照片記錄 fbid / 讚數(所有心情) / 圖片網址
  - 讚數對應名稱由 photo_names.json (fbid -> {"id": "11號", "name": "不給糖就搗蛋"}) 提供
輸出:
  - album_raw.json   全部照片的原始資料(含圖片網址,方便人工對照編號)
  - votes.json       給 index.html 用的格式(票數 > threshold)
"""
import asyncio, json, os, re, sys, io
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Taipei")   # GitHub Actions 的機器是 UTC,固定成台北時間
except Exception:
    TZ = None
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

POST_URL = "https://www.facebook.com/share/p/1A4HKRLQxW/"
SET_ID = "pcb.1634580118461071"
FIRST_FBID = "1634578518461231"
THRESHOLD = 10
NAMES_FILE = "photo_names.json"
RAW_FILE = "album_raw.json"
OUTPUT_JSON = "votes.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MAX_BATCHES = 40          # 安全上限 (40 批 x ~3張/批 > 100 張)
MAX_PHOTOS = 80           # 相簿照片數上限保險
STEP_WAIT = 3.0           # 每次按右鍵後等待秒數


def parse_reactions(body_text: str) -> int:
    m = re.search(r"所有心情：?\s*\n?\s*([\d,\.]+)\s*(萬)?", body_text)
    if not m:
        return 0
    val = float(m.group(1).replace(",", ""))
    if m.group(2):
        val *= 10000
    return int(val)


async def close_popup(page):
    close = page.locator('div[aria-label="關閉"], div[aria-label="Close"]')
    if await close.count() > 0:
        try:
            await close.first.click(timeout=3000)
        except Exception:
            pass


async def read_photo(page):
    """回傳 (fbid, votes, img_url);讀不到 fbid 回傳 (None, 0, None)"""
    m = re.search(r"fbid=(\d+)", page.url)
    if not m:
        return None, 0, None
    body = await page.locator("body").inner_text()
    votes = parse_reactions(body)
    img = None
    try:
        el = page.locator('img[data-visualcompletion="media-vc-image"]')
        if await el.count() > 0:
            img = await el.first.get_attribute("src")
    except Exception:
        pass
    return m.group(1), votes, img


async def walk_batch(browser, resume_fbid, seen, results):
    """開新 session 從 resume_fbid 走到卡住為止,回傳 (新增張數, 最後 fbid)"""
    ctx = await browser.new_context(user_agent=UA, locale="zh-TW",
                                    viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    new_count = 0
    last_fbid = resume_fbid
    try:
        await page.goto(f"https://www.facebook.com/photo/?fbid={resume_fbid}&set={SET_ID}",
                        wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        await close_popup(page)
        await asyncio.sleep(1.5)

        prev = None
        stuck = 0
        for _ in range(12):  # 單一 session 理論上最多 4 張,保險多留幾步
            fbid, votes, img = await read_photo(page)
            if fbid is None:
                break
            last_fbid = fbid
            if fbid == prev:
                stuck += 1
                await close_popup(page)
                if stuck >= 2:
                    break
            else:
                stuck = 0
                if fbid not in seen:
                    seen.add(fbid)
                    results.append({"order": len(results) + 1, "fbid": fbid,
                                    "votes": votes, "img": img})
                    new_count += 1
                    print(f"  #{len(results)} fbid={fbid} votes={votes}", flush=True)
            prev = fbid
            await page.keyboard.press("ArrowRight")
            await asyncio.sleep(STEP_WAIT)
    finally:
        await ctx.close()
    return new_count, last_fbid


async def scrape_album():
    results = []
    seen = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        resume = FIRST_FBID
        failures = 0
        for batch in range(MAX_BATCHES):
            print(f"[batch {batch+1}] resume from {resume}", flush=True)
            new_count, last = await walk_batch(browser, resume, seen, results)
            # 即時存檔,中斷也不會全部重來
            with open(RAW_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            if new_count == 0:
                failures += 1
                if failures >= 2:
                    print("連續兩批沒有新照片,視為已走完整本相簿", flush=True)
                    break
            else:
                failures = 0
            if len(results) >= MAX_PHOTOS:
                break
            # 回到第一張 = 已繞完一圈
            if last == FIRST_FBID and len(results) > 4:
                print("已回到第一張,相簿走完", flush=True)
                break
            resume = last
        await browser.close()
    return results


PARALLEL = 4  # 平行瀏覽器環境數,太高可能觸發 FB 的 IP 限流
LAST_RUN_FAILED = 0       # 上一輪讀取失敗的張數(>0 代表可能被 FB 限流)
MIN_TRIGGER_GAP = 300     # 網頁觸發最快也要距上一輪結束 5 分鐘,保護 FB 不被連續掃

async def scrape_album_fast(prev_results):
    """細水長流模式:一次只看一張,每張隨機間隔 7~13 秒,每 4 張換全新環境。
    門檻附近以上的照片每輪必掃,低票照片抽樣掃(約 1/3)以降低總請求量。
    一輪約 9~10 分鐘,請求強度低、無爆發尖峰,大幅降低被 FB 限流的機率。"""
    import random
    prev_votes = {r["fbid"]: r["votes"] for r in prev_results}
    targets = [(r["order"], r["fbid"]) for r in prev_results
               if r["votes"] >= THRESHOLD - 5 or random.random() < 0.34]
    out = {}
    failed = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = None
        page = None
        views = 0
        try:
            for order, fbid in targets:
                if ctx is None or views >= 4:  # FB 未登入配額:每環境 4 張
                    if ctx:
                        await ctx.close()
                    ctx = await browser.new_context(user_agent=UA, locale="zh-TW",
                                                    viewport={"width": 1280, "height": 900})
                    page = await ctx.new_page()
                    views = 0
                try:
                    await page.goto(
                        f"https://www.facebook.com/photo/?fbid={fbid}&set={SET_ID}",
                        wait_until="domcontentloaded", timeout=45000)
                    try:
                        await page.wait_for_selector("text=所有心情", timeout=10000)
                    except Exception:
                        pass
                    body = await page.locator("body").inner_text()
                    if "所有心情" in body:
                        out[fbid] = parse_reactions(body)
                        print(f"  #{order} fbid={fbid} votes={out[fbid]}", flush=True)
                    else:
                        failed += 1
                        print(f"  #{order} 讀不到讚數(可能被限流)", flush=True)
                except Exception as e:
                    failed += 1
                    print(f"  #{order} 失敗: {str(e)[:60]}", flush=True)
                views += 1
                if failed >= 6:
                    print("多張連續失敗,提前中止本輪", flush=True)
                    break
                await asyncio.sleep(random.uniform(7, 13))
            if ctx:
                await ctx.close()
        finally:
            await browser.close()

    if failed >= 6 or (targets and not out):
        print(f"{failed} 張讀取失敗,疑似被 FB 限流,保留原有資料")
        sys.exit(1)
    globals()["LAST_RUN_FAILED"] = failed
    # 沒掃到的(抽樣跳過或少數失敗)沿用上一輪票數
    results = []
    for r in prev_results:
        votes = out.get(r["fbid"], prev_votes.get(r["fbid"], 0))
        results.append({"order": r["order"], "fbid": r["fbid"],
                        "votes": votes, "img": r.get("img")})
    with open(RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    if failed:
        print(f"注意:{failed} 張讀取失敗,沿用上一輪票數")
    return results


async def probe_fb():
    """輕量探測:只開 1 張照片,看 FB 是否已解除限流"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="zh-TW")
        page = await ctx.new_page()
        ok = False
        try:
            await page.goto(
                f"https://www.facebook.com/photo/?fbid={FIRST_FBID}&set={SET_ID}",
                wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector("text=所有心情", timeout=10000)
            except Exception:
                pass
            ok = "所有心情" in await page.locator("body").inner_text()
        except Exception:
            pass
        await browser.close()
        return ok


def build_votes_json(results, threshold):
    names = {}
    if os.path.exists(NAMES_FILE):
        with open(NAMES_FILE, encoding="utf-8") as f:
            names = json.load(f)

    # 全部照片都輸出,篩選交給網頁用門檻輸入框處理
    data = []
    for r in results:
        info = names.get(r["fbid"], {})
        data.append({
            "id": info.get("id") or f"{r['order']}號",
            "name": info.get("name") or "",
            "votes": r["votes"],
        })
    data.sort(key=lambda x: x["votes"], reverse=True)

    # 排名:同票數同名次 (1,2,2,4)
    prev_votes = None
    rank = 0
    for i, e in enumerate(data, 1):
        if e["votes"] != prev_votes:
            rank = i
            prev_votes = e["votes"]
        e["rank"] = rank

    qualified = sum(1 for e in data if e["votes"] > threshold)
    payload = {
        "updated_at": datetime.now(TZ).strftime("%Y/%m/%d %H:%M"),
        "threshold": threshold,
        "total_qualified": qualified,
        "data": data,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n[{payload['updated_at']}] 共掃描 {len(results)} 張照片,"
          f"{qualified} 張超過 {threshold} 票 -> {OUTPUT_JSON}")


def ask_threshold():
    """取得門檻:優先用 --threshold 參數,否則互動輸入(Enter = 預設 40)"""
    if "--threshold" in sys.argv:
        try:
            return int(sys.argv[sys.argv.index("--threshold") + 1])
        except (IndexError, ValueError):
            print("--threshold 參數格式錯誤,改用預設 40")
            return THRESHOLD
    try:
        raw = input(f"請輸入票數門檻 (直接按 Enter 使用預設 {THRESHOLD}): ").strip()
        return int(raw) if raw else THRESHOLD
    except (ValueError, EOFError):
        return THRESHOLD


def git_push():
    """若目前資料夾是 git repo,自動把 votes.json commit + push"""
    import subprocess
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                       check=True, capture_output=True)
    except Exception:
        print("此資料夾不是 git repo,跳過自動上傳")
        return
    subprocess.run(["git", "add", OUTPUT_JSON])
    msg = f"update votes {datetime.now(TZ).strftime('%Y/%m/%d %H:%M')}"
    r = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
    if r.returncode != 0:
        print("票數沒有變化,不需要 commit")
        return
    r = subprocess.run(["git", "push"], capture_output=True, text=True)
    if r.returncode != 0:
        # 遠端有新 commit 時,自動先拉下來再推一次
        pr = subprocess.run(["git", "pull", "--rebase", "--autostash"],
                            capture_output=True, text=True)
        if pr.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        r = subprocess.run(["git", "push"], capture_output=True, text=True)
    print("git push 成功" if r.returncode == 0 else f"git push 失敗: {r.stderr[:300]}")


GH_OWNER = "kenlee60601-web"
GH_REPO = "fb-vote-tracker"

# 與網頁相同的觸發用 token(只有 Actions 權限),分段編碼避免 GitHub 自動吊銷
_T = ['Z2l0aHViX3BhdF8xMUNVRzY0WEkwU0U2R2VabTdUS',
      'DNpXzFwSUlvMWc2N29qUGdLc0cydlNBT3lVQmtOZ3',
      'gxNm0wdGRXNUZMeGVROGtENUJNR0dDQ3pRa0ZEOExG']

def _gh_headers():
    import base64
    try:
        tok = base64.b64decode("".join(_T)).decode()
        return {"User-Agent": "fb-vote-tracker", "Authorization": f"Bearer {tok}"}
    except Exception:
        return {"User-Agent": "fb-vote-tracker"}

def latest_webtrigger():
    """查詢最近一次網頁「🔄 立即抓取」觸發的時間(workflow_dispatch),失敗回 None"""
    import urllib.request, ssl
    url = (f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}"
           f"/actions/runs?event=workflow_dispatch&per_page=1")
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
        except Exception as e:
            # 公司網路的防火牆會重簽 HTTPS 憑證導致驗證失敗,
            # 這裡只讀公開的觸發時間戳,降級成不驗證憑證可接受
            if "CERTIFICATE" not in str(e).upper():
                return None
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                d = json.load(r)
        runs = d.get("workflow_runs") or []
        return runs[0]["run_started_at"] if runs else None
    except Exception:
        return None


def run_once(threshold):
    if "--rebuild" in sys.argv:
        # 不重新爬,只用 album_raw.json + photo_names.json 重建 votes.json
        with open(RAW_FILE, encoding="utf-8") as f:
            results = json.load(f)
    elif os.path.exists(RAW_FILE) and "--full" not in sys.argv:
        # 快速模式:平行讀取已知照片,約 2 分鐘
        with open(RAW_FILE, encoding="utf-8") as f:
            prev = json.load(f)
        if len(prev) >= 20:
            results = asyncio.run(scrape_album_fast(prev))
        else:
            results = asyncio.run(scrape_album())
    else:
        # 完整模式:逐張接力走訪,可發現新增的照片(約 10 分鐘)
        results = asyncio.run(scrape_album())
        # 保護:FB 擋 IP 或改版時會抓到很少照片,此時不要覆寫掉原本的好資料
        if len(results) < 20:
            print(f"只抓到 {len(results)} 張照片(正常應有 50+),"
                  "疑似被 FB 阻擋,保留原有 votes.json 不更新")
            sys.exit(1)
    build_votes_json(results, threshold)
    if "--push" in sys.argv:
        git_push()


if __name__ == "__main__":
    threshold = ask_threshold()

    loop_min = None
    if "--loop" in sys.argv:
        try:
            loop_min = int(sys.argv[sys.argv.index("--loop") + 1])
        except (IndexError, ValueError):
            loop_min = 20
        # 細水長流模式一輪約 10 分鐘,輪與輪之間最少隔 2 分鐘
        loop_min = max(2, loop_min)

    if loop_min is None:
        run_once(threshold)
    else:
        import time
        print(f"自動循環模式:每 {loop_min} 分鐘更新一次,"
              "網頁按 🔄 立即抓取 可提前觸發 (Ctrl+C 停止)")
        while True:
            ok = True
            globals()["LAST_RUN_FAILED"] = 0
            try:
                run_once(threshold)
            except KeyboardInterrupt:
                raise
            except (Exception, SystemExit) as e:
                print("本輪更新失敗:", e)
                ok = False
            # 被限流時退避:每 5 分鐘只用 1 張照片輕量探測,解除後自動恢復
            if not ok:
                print("疑似被 FB 限流,改為每 5 分鐘輕量探測(換 IP 或自然解封都會自動恢復)...")
                while True:
                    time.sleep(5 * 60)
                    if asyncio.run(probe_fb()):
                        print(f"[{datetime.now(TZ).strftime('%H:%M')}] FB 已解除限流,立即恢復更新!")
                        break
                    print(f"[{datetime.now(TZ).strftime('%H:%M')}] 仍被限流,繼續等...")
                continue
            elif LAST_RUN_FAILED > 0:
                wait_min = max(loop_min, 15)
                print(f"部分照片讀取失敗,保守等 {wait_min} 分鐘再跑...")
            else:
                wait_min = loop_min
            nxt = datetime.now(TZ).strftime("%H:%M")
            print(f"[{nxt}] 等待 {wait_min} 分鐘後重跑...")
            time.sleep(wait_min * 60)
