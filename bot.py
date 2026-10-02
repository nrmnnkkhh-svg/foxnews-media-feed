import asyncio, json, os, traceback
from difflib import SequenceMatcher
import aiohttp
from twikit import Client

TWITTER_USER   = "FoxNews"
TELEGRAM_CHAT  = "@foxnews_media_feed"
TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
COOKIES_STR    = os.environ["X_COOKIES"]
STATE_FILE     = "state.json"
TEMPLATE_FILE  = "template.txt"

SEPARATOR = "\n\n"

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_tweet_id": None, "thread_messages": {}, "total_sent": 0}
    with open(STATE_FILE) as f:
        state = json.load(f)
    state.setdefault("last_tweet_id", None)
    state.setdefault("thread_messages", {})
    state.setdefault("total_sent", 0)
    return state

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_template():
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()

def get_footer():
    return load_template().replace("{text}", "").strip()

def parse_cookies(cookie_string: str) -> dict:
    cookies = {}
    for part in cookie_string.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies

def unwrap(result):
    return result.get("tweet", result) if isinstance(result, dict) else result

def extract_media(result):
    """Return (clean_text, media_list)."""
    try:
        leg = unwrap(result)["legacy"]
    except Exception:
        return "", []

    # Handle retweets
    rt = leg.get("retweeted_status_result", {}).get("result")
    if rt:
        try:
            leg = unwrap(rt)["legacy"]
        except Exception:
            pass

    # Long tweets store full text here
    text = leg.get("full_text", "")
    note = leg.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    if note and note.get("text"):
        text = note["text"]

    media_out = []
    for m in (leg.get("extended_entities") or {}).get("media", []):
        tco = m.get("url", "")
        if tco:
            text = text.replace(tco, "").strip()

        mtype = m.get("type")
        if mtype == "photo":
            base = m.get("media_url_https", "")
            media_out.append({
                "type": "photo",
                "url_orig": base + "?name=orig",
                "url_large": base + "?name=large",
                "thumb": base,
            })
        elif mtype in ("video", "animated_gif"):
            vi = m.get("video_info", {})
            variants = vi.get("variants", [])
            mp4 = sorted(
                [v for v in variants if v.get("content_type") == "video/mp4"],
                key=lambda v: v.get("bitrate", 0),
                reverse=True,
            )
            media_out.append({
                "type": mtype,
                "variants": mp4,
                "duration_ms": vi.get("duration_millis", 0),
                "thumb": m.get("media_url_https", ""),
            })

    return text.strip(), media_out

async def send_message(text: str, tweet_id: str) -> int | None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    msg = load_template().replace("{text}", safe)
    payload = {"chat_id": TELEGRAM_CHAT, "text": msg, "disable_web_page_preview": True}
    for attempt in range(5):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"✅ Sent text tweet {tweet_id} → msg {data['result']['message_id']}")
                        return data["result"]["message_id"]
                    if data.get("error_code") == 429:
                        wait = data.get("parameters", {}).get("retry_after", 10)
                        print(f"⏳ Rate limited. Waiting {wait}s")
                        await asyncio.sleep(wait + 2)
                        continue
                    print(f"❌ Telegram error: {data}")
                    return None
        except Exception as exc:
            print(f"❌ Telegram error: {exc}")
            await asyncio.sleep(2 ** attempt)
    return None

async def main():
    print("🚀 Run started")

    try:
        cookies = parse_cookies(COOKIES_STR)
        client = Client(language="en-US")
        client.set_cookies(cookies)

        async def noop_transaction_init(http, ct_headers):
            print("Bypassing x-client-transaction-id init")
            return

        def fake_generate_transaction_id(method="GET", path="/"):
            return "00000000000000000000000000000000"

        client.client_transaction.init = noop_transaction_init
        client.client_transaction.generate_transaction_id = fake_generate_transaction_id
        if not hasattr(client.client_transaction, "key"):
            try:
                client.client_transaction.key = ""
            except Exception:
                pass

        print("✅ Cookies set")

        raw_user_response, _ = await client.gql.user_by_screen_name(TWITTER_USER)
        user_data = raw_user_response.get("data", {}).get("user", {}).get("result", {})
        user_id = user_data.get("rest_id") or user_data.get("id_str") or str(user_data.get("id", ""))
        if not user_id:
            raise Exception("Could not find user ID")
        print(f"✅ User ID: {user_id}")

        raw_tweets_response, _ = await client.gql.user_tweets(user_id, cursor=None, count=30)

        raw_items = []
        instructions = (
            raw_tweets_response.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline_v2", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        for instruction in instructions:
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                tweet_result = (
                    entry.get("content", {})
                    .get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                if tweet_result:
                    raw_items.append(tweet_result)

        print(f"📥 Fetched {len(raw_items)} raw items")
    except Exception as e:
        print(f"❌ Fetch failed: {e}")
        traceback.print_exc()
        return

    if not raw_items:
        print("No tweets")
        return

    # Parse into structured items
    parsed = []
    for r in raw_items:
        try:
            tid = unwrap(r).get("rest_id")
            if not tid:
                continue
            leg = unwrap(r).get("legacy", {})
            conv_id = str(leg.get("conversation_id_str") or tid)
            text, media = extract_media(r)
            if not text and not media:
                continue
            parsed.append({
                "id": int(tid),
                "text": text,
                "media": media,
                "conv_id": conv_id,
            })
        except Exception as e:
            print(f"Skipping item: {e}")
            continue

    if not parsed:
        print("No parseable tweets")
        return

    state = load_state()
    last_id = int(state.get("last_tweet_id", 0))
    thread_map = state.get("thread_messages", {})
    footer = get_footer()

    new_tweets = [t for t in parsed if t["id"] > last_id]
    new_tweets.sort(key=lambda x: x["id"])

    if not new_tweets:
        print("No new tweets")
    else:
        print(f"📬 {len(new_tweets)} new tweet(s)")
        for tw in new_tweets:
            media_kinds = [m["type"] for m in tw["media"]]
            print(f"  → {tw['id']} | media: {media_kinds or 'none'} | text: {tw['text'][:60]!r}")

            # For now only send text-only tweets
            if not tw["media"]:
                await send_message(tw["text"], str(tw["id"]))
                await asyncio.sleep(1.5)

            state["last_tweet_id"] = str(tw["id"])
            save_state(state)

    print("✅ Run complete")

if __name__ == "__main__":
    asyncio.run(main())
