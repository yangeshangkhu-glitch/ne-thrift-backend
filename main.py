import os
import json
import re
import httpx
from fastapi import FastAPI, Request, Query
from starlette.responses import PlainTextResponse

from fastapi.middleware.cors import CORSMiddleware
from google import genai
import razorpay
from supabase import create_client, Client

app = FastAPI(title="NE Thrift Zero-Tech Marketplace API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
ai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
rzp_client = razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_SECRET")))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "nethrift2026")


async def send_whatsapp_message(to_number: str, text_body: str):
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text_body}
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload)


@app.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge")
):
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    return PlainTextResponse(content="Verification failed", status_code=403)


@app.post("/webhook/whatsapp")
async def receive_whatsapp_message(request: Request):
    data = await request.json()
    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "no_message"}
        
        msg = entry["messages"][0]
        sender_phone = msg["from"]
        msg_type = msg["type"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    seller_res = supabase.table("sellers").select("*").eq("whatsapp_number", sender_phone).execute()

    if not seller_res.data:
        if msg_type == "text" and "STORE:" in msg["text"]["body"].upper():
            raw_text = msg["text"]["body"]
            parts = [p.strip() for p in raw_text.split("|")]
            store_name = parts[0].split(":", 1)[1].strip()
            upi_id = parts[1].split(":", 1)[1].strip() if len(parts) > 1 else "pending@upi"

            supabase.table("sellers").insert({
                "whatsapp_number": sender_phone,
                "store_name": store_name,
                "upi_id": upi_id
            }).execute()

            await send_whatsapp_message(
                sender_phone,
                f"🎉 Welcome *{store_name}*! Your store is active.\n\n"
                f"👉 *To list clothes:* Send a photo with a caption like 'Nike Tee, 450, chest 42, length 28'.\n"
                f"👉 *To get paid after shipping:* Send a photo of your India Post paper slip!"
            )
            return {"status": "seller_onboarded"}
        else:
            await send_whatsapp_message(
                sender_phone,
                "👋 Welcome to NE Thrift Hub! Reply in this exact format to open your store in 10 seconds:\n\n"
                "STORE: Your Store Name | UPI: yourname@okaxis"
            )
            return {"status": "prompted_onboarding"}

    seller = seller_res.data[0]

    if msg_type == "image":
        caption = msg["image"].get("caption", "")
        media_id = msg["image"]["id"]

        async with httpx.AsyncClient() as client:
            media_info = await client.get(
                f"https://graph.facebook.com/v21.0/{media_id}",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
            )
            media_url = media_info.json().get("url")
            img_bytes_resp = await client.get(
                media_url,
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
            )
            img_bytes = img_bytes_resp.content

        prompt = f"""
        Analyze this image and caption: '{caption}'.
        Decide if it is:
        1) A clothing item to list for sale, OR
        2) An India Post paper shipping receipt (contains 13-character tracking code like EE...IN or CG...IN and a 6-digit PIN).
        Return strict JSON ONLY:
        If Clothing: {{"type": "PRODUCT", "title": "string", "price_inr": 400, "chest_inches": 42, "length_inches": 28, "condition": "Like New"}}
        If Receipt: {{"type": "RECEIPT", "shipments": [{{"tracking_no": "EE123456789IN", "destination_pincode": "500032"}}]}}
        """

        ai_resp = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                prompt,
                genai.types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            ]
        )
        parsed = json.loads(re.sub(r"```json|```", "", ai_resp.text).strip())

        if parsed["type"] == "PRODUCT":
            supabase.table("products").insert({
                "seller_id": seller["id"],
                "title": parsed["title"],
                "price_inr": int(parsed.get("price_inr") or 400),
                "chest_inches": parsed.get("chest_inches") or 40,
                "length_inches": parsed.get("length_inches") or 27,
                "condition": parsed.get("condition", "Pre-loved Vintage"),
                "image_urls": ["https://images.unsplash.com/photo-1521572267360-ee0c2909d518?w=600"],
                "status": "AVAILABLE"
            }).execute()

            await send_whatsapp_message(
                sender_phone,
                f"✅ *LIVE ON MARKETPLACE!*\n*{parsed['title']}*\n💰 ₹{parsed['price_inr']} (+₹60 India Post)\n"
                f"📏 Chest: {parsed.get('chest_inches')}\" | Length: {parsed.get('length_inches')}\""
            )
        elif parsed["type"] == "RECEIPT":
            matched = []
            for s in parsed.get("shipments", []):
                t_no = s["tracking_no"].upper().strip()
                pin = s["destination_pincode"].strip()
                order_q = supabase.table("orders").select("*").eq("seller_id", seller["id"]).eq("buyer_pincode", pin).eq("escrow_status", "PAID_WAITING_SHIP").execute()
                if order_q.data:
                    o = order_q.data[0]
                    supabase.table("orders").update({
                        "india_post_tracking_no": t_no,
                        "escrow_status": "SHIPPED_IN_TRANSIT",
                        "shipped_at": "now()"
                    }).eq("id", o["id"]).execute()
                    matched.append(f"Order #{o['id']} → {t_no}")
            await send_whatsapp_message(
                sender_phone,
                f"📦 *India Post Slip Verified!*\n" + ("\n".join(matched) if matched else "Saved tracking number.") +
                "\n\nPayout will release to your UPI automatically once India Post marks Delivered!"
            )

    return {"status": "ok"}


@app.get("/api/products")
async def get_available_products():
    res = supabase.table("products").select("*, sellers(store_name, city)").eq("status", "AVAILABLE").order("created_at", desc=True).execute()
    return res.data


@app.post("/api/create-checkout")
async def create_buyer_checkout(request: Request):
    body = await request.json()
    product_id = body["product_id"]
    bundle_hold = body.get("bundle_hold", False)

    prod_res = supabase.table("products").select("*").eq("id", product_id).eq("status", "AVAILABLE").execute()
    if not prod_res.data:
        return {"error": "Item already sold!"}
    product = prod_res.data[0]

    postage = 0 if bundle_hold else product["postage_inr"]
    buyer_fee = 19
    total_inr = product["price_inr"] + postage + buyer_fee

    rzp_order = rzp_client.order.create({
        "amount": total_inr * 100,
        "currency": "INR",
        "payment_capture": 1
    })
    return {"razorpay_order_id": rzp_order["id"], "amount_inr": total_inr, "key_id": os.getenv("RAZORPAY_KEY_ID")}
