# ESP32 Razorpay Payment Backend

This is a Headless/RESTful backend built with Python (FastAPI) to handle dynamic Razorpay payment links and communicate real-time payment status back to an ESP32 hardware device (or any frontend) via WebSockets.

## Prerequisites
1. Python 3.8+ installed on your system.
2. An active Razorpay account with your API keys. (Already added to `.env`)
3. A publicly accessible URL (e.g., via [ngrok](https://ngrok.com/)) for the Razorpay Webhook to reach your local backend.

## Installation & Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables:**
   - Open the `.env` file and ensure your `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` are correct.
   - Set up a secret phrase for `RAZORPAY_WEBHOOK_SECRET` (e.g., `my_super_secret_string`). You'll need this when configuring the webhook in the Razorpay dashboard.

3. **Start the server:**
   ```bash
   python main.py
   ```
   *The server will start on `http://0.0.0.0:8000`, making it accessible on your local network (so the ESP32 can reach it).*

## How to Configure Razorpay Webhook

Razorpay needs to know where to send payment updates.
1. Go to your Razorpay Dashboard -> Account & Settings -> Webhooks.
2. Click "Add New Webhook".
3. **Webhook URL**: Enter your public server URL followed by `/webhook` (e.g., `https://your-ngrok-url.ngrok.app/webhook`).
4. **Secret**: Enter the exact string you used for `RAZORPAY_WEBHOOK_SECRET` in your `.env` file.
5. **Active Events**: Check `payment_link.paid` and `payment_link.cancelled`.
6. Click Save.

---

## API Documentation for ESP32 Integration

### 1. Generate Payment QR Link
The ESP32 calls this endpoint to initiate a payment.

- **URL:** `http://<YOUR_COMPUTER_IP>:8000/api/payment/create`
- **Method:** `POST`
- **Headers:** `Content-Type: application/json`
- **Body:**
  ```json
  {
    "amount": 500, 
    "description": "Optional payment description"
  }
  ```
  *(Note: amount is passed in INR (e.g., 500 = ₹500). The backend will automatically convert it to paise for Razorpay).*

- **Response:**
  ```json
  {
    "success": true,
    "payment_link_id": "plink_xyz123",
    "payment_url": "https://rzp.io/i/xxxxxx",
    "status": "created"
  }
  ```
**Hardware Action:** The ESP32 should extract the `payment_url` and generate a QR code for it to display on the 7-inch LCD.

### 2. Listen for Real-Time Status via WebSocket
Instead of polling, the ESP32 establishes a continuous WebSocket connection to instantly know when a customer scans and pays.

- **URL:** `ws://<YOUR_COMPUTER_IP>:8000/ws/status/{payment_link_id}`
- **Protocol:** `ws` (WebSocket)

**Hardware Action:**
Immediately after the ESP32 receives the `payment_link_id` from the `POST` request, it should open a WebSocket connection to the above URL.

When the customer pays successfully, Razorpay triggers the webhook on our backend. The backend immediately sends a JSON message down the WebSocket to the ESP32:

- **Received Message:**
  ```json
  {
    "event": "payment_link.paid",
    "payment_link_id": "plink_xyz123",
    "status": "paid"
  }
  ```
**Hardware Action:** Upon receiving this message, the ESP32 should update the LCD UI to show a "Payment Successful" screen.
